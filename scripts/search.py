#!/usr/bin/env python3
"""
search.py — Unified Search v2 CLI 主入口 & 执行编排

职责：
  - 解析命令行参数
  - 通过 route.py 做路由决策（含预算模式）
  - 通过 cache.py 做双层缓存
  - 通过 engines.py 执行引擎搜索
  - RRF 融合 + Bocha Reranker 精排
  - 通过 adaptive.py 记录引擎表现
  - 输出统一 JSON / 文本格式
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Callable, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache
from route import route_query
from engines import search as engine_search, available_engines
from config import get_execution_config, get_cost_factor


# ── 查询改写辅助 ────────────────────────────────────────────────────────────────

def _apply_query_rewrite(query: str) -> tuple[str, dict | None]:
    """统一查询改写逻辑，返回 (改写后的查询, 改写结果字典)。

    改写失败时静默返回原查询，不影响搜索流程。
    """
    try:
        from query_rewriter import rewrite_query as do_rewrite
        result = do_rewrite(query)
        if result["rewritten"] and result["confidence"] >= 0.7:
            return result["rewritten"], result
    except ImportError:
        pass  # query_rewriter 模块不可用，使用原查询
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询改写跳过: {type(e).__name__}")
    return query, None


# ── 进度阶段 ──────────────────────────────────────────────────────────────────

class Stage(str, Enum):
    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"


# ── RRF 融合 ───────────────────────────────────────────────────────────────────

def rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 合并多引擎结果，保留 consensus_engines。"""
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for results in ranked_lists:
        for i, r in enumerate(results):
            url = r.get("url", "") or f"__title__:{r.get('title', '')}"
            scores[url] = scores.get(url, 0.0) + 1.0 / (k + i + 1)
            eng = r.get("_engine") or r.get("source", "") or ""
            if url not in items:
                item = dict(r)
                cons: list[str] = []
                if eng:
                    cons.append(eng)
                item["consensus_engines"] = cons
                items[url] = item
            else:
                if r.get("score", 0) > items[url].get("score", 0):
                    # 保留已累积的 consensus
                    prev_cons = list(items[url].get("consensus_engines") or [])
                    items[url].update(r)
                    items[url]["consensus_engines"] = prev_cons
                sources = {items[url].get("source", ""), r.get("source", "")}
                items[url]["source"] = "/".join(s for s in sources if s)
                cons = list(items[url].get("consensus_engines") or [])
                if eng and eng not in cons:
                    cons.append(eng)
                items[url]["consensus_engines"] = cons

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [items[url] for url, _ in ranked]


def deduplicate_by_url(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """URL 去重。"""
    seen: set[str] = set()
    out = []
    for r in results:
        key = r.get("url", "") or f"title:{r.get('title', '')}"
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── Bocha Reranker ──────────────────────────────────────────────────────────────

def rerank_results(query: str, results: list[dict[str, Any]],
                   top_n: int = 10, timeout: float = 5
                   ) -> tuple[list[dict[str, Any]], str]:
    """使用博查语义排序模型对搜索结果二次精排。

    返回 (results, status)：status ∈ ok | skipped_no_key | skipped_short |
    skipped_fast | fallback
    """
    if not results or len(results) <= 1:
        return results, "skipped_short"

    api_key = os.environ.get("BOCHA_API_KEY", "")
    if not api_key:
        return results, "skipped_no_key"

    documents = []
    for r in results:
        doc_text = f"{r.get('title', '')} {r.get('snippet', '')}".strip()
        documents.append(doc_text or "empty")

    import urllib.request
    payload = json.dumps({
        "model": "gte-rerank", "query": query,
        "documents": documents[:50],
        "top_n": min(top_n, len(documents)),
        "return_documents": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.bocha.cn/v1/rerank", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rerank_results_list = data.get("data", {}).get("results", [])
            if not rerank_results_list:
                return results, "fallback"
            scored = []
            for rr in rerank_results_list:
                idx = rr.get("index", -1)
                score = rr.get("relevance_score", 0)
                if 0 <= idx < len(results):
                    item = dict(results[idx])
                    orig_score = item.get("score", 0) or 0
                    item["score"] = round(score * 0.7 + orig_score * 0.3, 4)
                    scored.append(item)
            if scored:
                scored.sort(key=lambda x: x.get("score", 0), reverse=True)
                return scored[:top_n], "ok"
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return results, "fallback"
    return results, "fallback"


# ── 执行层 ─────────────────────────────────────────────────────────────────────

def _classify_engine_outcome(eng: str, res: list[dict[str, Any]],
                             latency_ms: int, status_hint: str | None = None
                             ) -> dict[str, Any]:
    """将单引擎结果归类为可观测 outcome。"""
    if status_hint:
        return {
            "engine": eng, "status": status_hint,
            "results_count": 0, "latency_ms": latency_ms,
        }
    if not res:
        return {
            "engine": eng, "status": "no-results",
            "results_count": 0, "latency_ms": latency_ms,
        }
    errors = [r for r in res if isinstance(r, dict) and "error" in r]
    goods = [r for r in res if isinstance(r, dict) and "error" not in r]
    if errors and not goods:
        msg = str(errors[0].get("error", "")).lower()
        if "timeout" in msg:
            st = "timeout"
        elif "rate" in msg or "429" in msg:
            st = "rate-limited"
        elif "auth" in msg or "401" in msg or "403" in msg:
            st = "auth-failed"
        else:
            st = "error"
        return {
            "engine": eng, "status": st,
            "results_count": 0, "latency_ms": latency_ms,
            "detail": str(errors[0].get("error", ""))[:200],
        }
    if goods and errors:
        return {
            "engine": eng, "status": "partial",
            "results_count": len(goods), "latency_ms": latency_ms,
        }
    return {
        "engine": eng, "status": "ok",
        "results_count": len(goods), "latency_ms": latency_ms,
    }


def execute_search(query: str, decision: dict[str, Any], max_results: int,
                   timeout: int, depth: str, cache: SearchCache, skip_cache: bool,
                   mode: str = "auto",
                   on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None) -> dict[str, Any]:
    """执行搜索：缓存 → 熔断/负缓存 → 引擎 → 融合 → 精排 → 写缓存。"""
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    engines = list(engines_combo)
    parallel = decision.get("parallel", False) and len(engines) > 1

    if on_progress:
        on_progress(Stage.START, {"query": query})

    cache_engine_key = "+".join(sorted(engines)) if len(engines) > 1 else engines[0]

    if on_progress:
        on_progress(Stage.ROUTING, {"domain": domain, "engine": engine_label, "engines": engines})

    # combo 缓存命中（含 depth + 柔性命中）
    if not skip_cache:
        t_cache_start = time.time()
        hit = cache.get(query, cache_engine_key, max_results, domain=domain,
                        mode=mode, depth=depth)
        if hit:
            cache_elapsed = int((time.time() - t_cache_start) * 1000)
            if on_progress:
                on_progress(Stage.CACHE_HIT, {"cache_level": hit.get("_cache_level", "L?")})
            tfidf_scores = decision.get("tfidf_scores", [])
            if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
                tfidf_scores = []
            return {
                "query": query, "engine": engine_label, "engines": engines,
                "engines_combo": engines_combo, "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain, "elapsed_ms": cache_elapsed,
                "tfidf_scores": tfidf_scores,
                "results": hit.get("results", []),
                "count": len(hit.get("results", [])),
                "mode": mode, "depth": depth,
                "reranker": "skipped_cache",
                "engine_outcomes": hit.get("engine_outcomes") or [],
            }

    if on_progress:
        on_progress(Stage.SEARCHING, {"engines": engines})

    try:
        from circuit_breaker import get_breaker
        breaker = get_breaker()
    except ImportError:
        breaker = None

    t0 = time.time()
    raw_results: dict[str, list[dict[str, Any]]] = {}
    engine_outcomes: list[dict[str, Any]] = []
    engine_latency: dict[str, int] = {}
    wasted_ms = 0

    exec_cfg = get_execution_config()
    retry_count = exec_cfg.get("retry_count", 0)

    def _exec_engine(eng: str, retries: int = retry_count) -> list[dict[str, Any]]:
        last_result: list[dict[str, Any]] = []
        for _attempt in range(retries + 1):
            last_result = engine_search(
                query, eng, n=max_results, timeout=timeout, depth=depth, mode=mode,
            )
            if last_result and any("error" not in r for r in last_result):
                return last_result
        if depth != "balanced":
            last_result = engine_search(
                query, eng, n=max_results, timeout=timeout, depth="balanced", mode=mode,
            )
        return last_result

    def _run_one(eng: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], int]:
        """单引擎：负缓存 → 熔断 → per-engine 缓存 → 网络。"""
        t_eng = time.time()

        # 熔断
        if breaker is not None:
            allowed, reason = breaker.allow(eng)
            if not allowed:
                lat = int((time.time() - t_eng) * 1000)
                outcome = _classify_engine_outcome(eng, [], lat, status_hint="skipped-circuit-open")
                outcome["detail"] = reason
                return eng, [], outcome, lat
            neg = breaker.get_negative(query, eng)
            if neg:
                lat = int((time.time() - t_eng) * 1000)
                outcome = _classify_engine_outcome(
                    eng, [], lat, status_hint="no-results-cached",
                )
                outcome["detail"] = neg.get("status", "no-results")
                return eng, [], outcome, lat

        # per-engine 缓存
        if not skip_cache:
            eng_hit = cache.get_engine(
                query, eng, max_results, domain=domain, mode=mode, depth=depth,
            )
            if eng_hit is not None:
                lat = int((time.time() - t_eng) * 1000)
                # 标记缓存来源
                for r in eng_hit:
                    if isinstance(r, dict):
                        r.setdefault("_engine", eng)
                outcome = _classify_engine_outcome(eng, eng_hit, lat)
                outcome["status"] = "ok-cached" if eng_hit else "no-results-cached"
                return eng, eng_hit, outcome, lat

        # 网络调用
        try:
            res = _exec_engine(eng)
        except Exception as e:
            res = [{"error": str(e), "source": eng}]
        lat = int((time.time() - t_eng) * 1000)
        for r in res:
            if isinstance(r, dict):
                r.setdefault("_engine", eng)
                r.setdefault("_elapsed", lat / 1000.0)

        outcome = _classify_engine_outcome(eng, res, lat)
        goods = [r for r in res if isinstance(r, dict) and "error" not in r]

        if breaker is not None:
            if outcome["status"] == "ok":
                breaker.record_success(eng)
                breaker.clear_negative(query, eng)
            elif outcome["status"] == "no-results":
                breaker.record_failure(eng, kind="empty")
                breaker.set_negative(query, eng, status="no-results")
            elif outcome["status"] == "timeout":
                breaker.record_failure(eng, kind="timeout")
                breaker.set_negative(query, eng, status="timeout")
            else:
                breaker.record_failure(eng, kind="error")
                breaker.set_negative(query, eng, status=outcome["status"])

        if not skip_cache and goods:
            cache.set_engine(
                query, eng, max_results, goods,
                domain=domain, mode=mode, depth=depth,
            )
        elif not skip_cache and not goods:
            # 空结果短 TTL 写入 per-engine，配合负缓存
            cache.set_engine(
                query, eng, max_results, [],
                domain=domain, mode=mode, depth=depth,
            )

        return eng, (goods if goods else res), outcome, lat

    to_run = list(engines)
    if parallel and to_run:
        with ThreadPoolExecutor(max_workers=min(len(to_run), 3)) as ex:
            futures = {ex.submit(_run_one, eng): eng for eng in to_run}
            try:
                for fut in as_completed(futures, timeout=timeout + 2):
                    eng = futures[fut]
                    try:
                        e, res, outcome, lat = fut.result()
                        raw_results[e] = res
                        engine_outcomes.append(outcome)
                        engine_latency[e] = lat
                        if outcome["status"] not in ("ok", "ok-cached", "partial"):
                            wasted_ms += lat
                    except Exception as e:
                        raw_results[eng] = [{"error": str(e), "source": eng}]
                        engine_outcomes.append(_classify_engine_outcome(
                            eng, raw_results[eng], 0,
                        ))
            except TimeoutError:
                for fut, eng in futures.items():
                    if not fut.done():
                        fut.cancel()
                        raw_results[eng] = [{"error": "timeout", "source": eng}]
                        engine_outcomes.append(_classify_engine_outcome(
                            eng, raw_results[eng], timeout * 1000, "timeout",
                        ))
                        wasted_ms += timeout * 1000
            for fut in futures:
                if not fut.done():
                    fut.cancel()
    else:
        for eng in to_run:
            e, res, outcome, lat = _run_one(eng)
            raw_results[e] = res
            engine_outcomes.append(outcome)
            engine_latency[e] = lat
            if outcome["status"] not in ("ok", "ok-cached", "partial"):
                wasted_ms += lat
            if res and any(isinstance(r, dict) and "error" not in r for r in res):
                break

    elapsed = int((time.time() - t0) * 1000)

    # 融合
    valid_lists = [
        res for res in raw_results.values()
        if res and any(isinstance(r, dict) and "error" not in r for r in res)
    ]
    # 去掉 error-only 列表中的 error 条目
    clean_lists = []
    for res in valid_lists:
        clean = [r for r in res if isinstance(r, dict) and "error" not in r]
        if clean:
            clean_lists.append(clean)

    if len(clean_lists) > 1:
        merged = rrf_merge(clean_lists)[:max_results]
    elif clean_lists:
        merged = deduplicate_by_url(clean_lists[0])[:max_results]
        # 单引擎也补 consensus
        for r in merged:
            eng = r.get("_engine") or r.get("source") or ""
            if eng:
                r.setdefault("consensus_engines", [eng])
    else:
        merged = []

    # Reranker：fast 模式跳过
    reranker_status = "skipped_short"
    if mode == "fast" or depth == "fast":
        reranker_status = "skipped_fast"
    elif merged and len(merged) > 1:
        merged, reranker_status = rerank_results(query, merged, top_n=max_results)

    if merged:
        # 共识加权后再按 score 排
        for r in merged:
            cons = r.get("consensus_engines") or []
            if len(cons) >= 2:
                base = float(r.get("score", 0) or 0)
                r["score"] = round(base * (1.0 + 0.05 * min(len(cons) - 1, 3)), 4)
                r["consensus_boost"] = True
        merged.sort(key=lambda r: abs(r.get("score", 0) or 0), reverse=True)
        merged = merged[:max_results]

    # 内嵌两阶段信号
    if merged:
        try:
            from evidence import score_authority, score_freshness
            from content_signals import score_evidence_density
            for r in merged:
                url = r.get("url", "")
                source = r.get("source", "")
                title = r.get("title", "") or ""
                snippet = r.get("snippet", "") or ""
                auth = score_authority(url, source)
                fresh = score_freshness(r)
                dens = score_evidence_density(snippet, title)
                selection = auth["score"]
                if auth.get("is_serp"):
                    selection = min(selection, 0.15)
                # 多引擎共识抬升 selection
                cons = r.get("consensus_engines") or []
                if len(cons) >= 2 and not auth.get("is_serp"):
                    selection = min(1.0, selection * (1.0 + 0.1 * min(len(cons) - 1, 2)))
                absorption = dens["absorption_score"]
                orig = float(r.get("score", 0.5) or 0.5)
                r["authority"] = auth["score"]
                r["authority_tier"] = auth["tier"]
                r["freshness"] = fresh["score"]
                r["selection"] = round(selection, 3)
                r["absorption"] = round(absorption, 3)
                r["evidence_flags"] = {
                    "has_numbers": dens["has_numbers"],
                    "has_comparison": dens["has_comparison"],
                    "has_definition": dens["has_definition"],
                    "is_serp": bool(auth.get("is_serp")),
                    "consensus": len(cons),
                }
                r["credibility_fast"] = round(
                    selection * 0.40 + absorption * 0.35 + fresh["score"] * 0.15 + orig * 0.10,
                    3,
                )
        except ImportError:
            pass
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(f"可信度评分跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    result_payload = {
        "results": merged,
        "engines_used": list(raw_results.keys()),
        "domain": domain,
        "engine_outcomes": engine_outcomes,
    }

    # 写 combo 缓存：空结果短 TTL（cache.set 内处理）
    if not skip_cache:
        effective_ttl = None
        if merged and elapsed > 2000:
            multiplier = min(2 ** (elapsed // 2000), 8)
            base_ttl = cache.resolve_ttl(domain)
            effective_ttl = base_ttl * multiplier
        cache.set(
            query, cache_engine_key, max_results, result_payload,
            domain=domain, ttl=effective_ttl, mode=mode, depth=depth,
        )

    # 自适应学习
    try:
        from adaptive import get_learner
        learner = get_learner()
        for eng, res in raw_results.items():
            success = bool(res and any(isinstance(r, dict) and "error" not in r for r in res))
            latency = engine_latency.get(eng, elapsed / max(len(raw_results), 1))
            cost = get_cost_factor(eng)
            learner.record(eng, success=success, latency_ms=latency, cost=0.0 if cost >= 0.85 else 0.001)
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"自适应学习记录跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    return {
        "query": query, "engine": engine_label, "engines": engines,
        "engines_combo": engines_combo, "cached": False,
        "domain": domain, "elapsed_ms": elapsed,
        "tfidf_scores": tfidf_scores, "results": merged,
        "count": len(merged), "engines_used": list(raw_results.keys()),
        "errors": _collect_errors(raw_results),
        "engine_outcomes": engine_outcomes,
        "wasted_engine_ms": wasted_ms,
        "reranker": reranker_status,
        "mode": mode, "depth": depth,
    }


def _collect_errors(raw_results: dict[str, list[dict[str, Any]]]) -> list[str]:
    errors = []
    for eng, res in raw_results.items():
        for r in res:
            if isinstance(r, dict) and "error" in r:
                errors.append(f"{eng}: {r['error']}")
    return errors


# ── 统一入口 ──────────────────────────────────────────────────────────────────

def super_search(query: str, engine: str = "auto", n: int = 5, explain: bool = False,
                 skip_cache: bool = False, timeout: int = 10,
                 depth: str = "fast", mode: str = "auto", local_first: bool = False,
                 rewrite: bool = True, cache: Any = None) -> dict[str, Any]:
    """统一搜索便捷入口。

    Args:
        query: 搜索查询词
        engine: 指定引擎（默认 auto）
        n: 最大结果数
        explain: 是否输出路由解释
        skip_cache: 是否跳过缓存
        timeout: 超时
        depth: 搜索深度
        mode: 预算模式
        local_first: 强制本地优先
        rewrite: 是否自动改写查询（默认 True）
    """
    cache = cache if cache is not None else SearchCache()

    # 查询改写：追加领域关键词提升搜索质量
    rewrite_result = None
    if rewrite:
        query, rewrite_result = _apply_query_rewrite(query)

    if local_first:
        decision = route_query(query, engine_override="local_search", mode=mode)
    else:
        decision = route_query(query, engine_override=engine, mode=mode)
    if explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={mode}",
            file=sys.stderr,
        )
    result = execute_search(query=query, decision=decision, max_results=n,
                          timeout=timeout, depth=depth, cache=cache,
                          skip_cache=skip_cache, mode=mode)
    if rewrite_result and rewrite_result["rewritten"]:
        result["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }
    return result


# ── 输出格式化 ─────────────────────────────────────────────────────────────────

def format_text_output(results: dict[str, Any]) -> str:
    lines = []
    count = results.get("count", 0)
    elapsed = results.get("elapsed_ms", 0)
    engine = results.get("engine", "?")
    cached = results.get("cached", False)
    cache_level = results.get("cache_level", "")
    domain = results.get("domain", "")
    mode = results.get("mode", "auto")

    header = f"=== {count} results ({elapsed}ms via {engine})"
    if cached:
        header += f" [CACHE {cache_level}]"
    elif domain:
        header += f" [domain:{domain}]"
    if mode != "auto":
        header += f" [mode:{mode}]"
    lines.append(header)

    for err in results.get("errors", [])[:3]:
        lines.append(f"  [ERROR] {err}")

    for r in results.get("results", []):
        score = r.get("score", 0)
        title = r.get("title", "?")[:80]
        url = r.get("url", "")
        prefix = f"[{score:.2f}]" if score else "[?]"
        lines.append(f"  {prefix} {title}")
        if url:
            lines.append(f"    {url}")
        snippet = r.get("snippet", "")
        if snippet:
            lines.append(f"    {snippet[:120]}")

    return "\n".join(lines)


# ── CLI 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Unified Search v2 — 统一搜索 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 search.py "python async"
  python3 search.py "英伟达财报" --explain --json
  python3 search.py "基金推荐" --mode fast
  python3 search.py "AAPL" --engine anysearch --domain finance --sub_domain finance.us_stock
        """,
    )
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="auto")
    parser.add_argument("--max-results", "-n", type=int, default=5)
    parser.add_argument("--depth", "-d", default="fast",
                        choices=["fast", "balanced", "deep"])
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", "-t", type=int, default=10)
    parser.add_argument("--list-engines", action="store_true")
    parser.add_argument("--mode", default="auto",
                        choices=["fast", "auto", "deep", "budget"],
                        help="预算模式: fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制")
    parser.add_argument("--local-first", action="store_true",
                        help="强制优先使用 local_search 零成本聚合引擎")
    parser.add_argument("--domain", default="", help="AnySearch 垂直域")
    parser.add_argument("--sub_domain", default="", help="AnySearch 子域")
    parser.add_argument("--progress", action="store_true")

    args = parser.parse_args()

    if args.list_engines:
        print(json.dumps(available_engines(), ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("必须提供搜索关键词")

    cache = SearchCache()
    if args.local_first:
        decision = route_query(args.query, engine_override="local_search", mode=args.mode)
    else:
        decision = route_query(args.query, engine_override=args.engine, mode=args.mode)

    # 查询改写
    search_query, rewrite_result = _apply_query_rewrite(args.query)

    if args.explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={args.mode}",
            file=sys.stderr,
        )
        if rewrite_result and rewrite_result["rewritten"]:
            print(
                f"[改写] {rewrite_result['original']} → {rewrite_result['rewritten']} "
                f"({rewrite_result['confidence']:.0%})",
                file=sys.stderr,
            )

    on_progress = None
    if args.progress:
        def on_progress(stage: Stage, data: dict[str, Any]):
            print(f"[progress] {stage.value} {data}", file=sys.stderr)

    results = execute_search(
        query=search_query, decision=decision, max_results=args.max_results,
        timeout=args.timeout, depth=args.depth, cache=cache,
        skip_cache=args.no_cache, mode=args.mode, on_progress=on_progress,
    )

    if rewrite_result and rewrite_result["rewritten"]:
        results["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }

    if args.json_output:
        public = {k: v for k, v in results.items() if not k.startswith("_")}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(format_text_output(results))


if __name__ == "__main__":
    main()
