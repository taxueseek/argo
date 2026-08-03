#!/usr/bin/env python3
"""
research.py — 深度研究工具（含社交舆情模式）

核心能力：
  1. 问题分解：将复杂查询拆分为 3-5 个子查询
  2. 多源采集：对每个子查询并行执行搜索
  3. 综合报告：合并去重 + 来源标注 + 知识缺口识别
  4. 引用追踪：每个结论可追溯到具体搜索结果
  5. 社交舆情：跨平台 UGC 情绪倾向 + 高频讨论点

用法：
  python3 research.py "CRISPR-Cas9 脱靶效应的 AI 预测方法综述"
  python3 research.py "CVE-2024-6387 生产环境影响评估" --depth deep
  python3 research.py "台积电财报分歧分析" --sub-queries 5
  python3 research.py "iPhone 16 用户评价" --mode social-sentiment --platforms xiaohongshu,reddit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from search import super_search, rrf_merge, deduplicate_by_url


# ── 交叉引用检测 ──────────────────────────────────────────────────────────────

def detect_cross_references(results: list[dict[str, Any]], min_sources: int = 2,
                            min_ngram_len: int = 3) -> list[dict[str, Any]]:
    """检测多个来源的交叉引用（n-gram 重叠）。

    如果同一 n-gram 出现在 ≥min_sources 个不同域名的结果中，
    标记为「潜在佐证」。
    """
    import re
    from urllib.parse import urlparse

    # 提取所有 snippet 的 n-gram
    ngram_sources: dict[str, set] = {}  # ngram -> set of (url, title)
    for r in results:
        url = r.get("url", "")
        domain = urlparse(url).netloc.lower().strip("www.") if url else "unknown"
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        # 简单分词（中英文混合）
        # 英文按空格分
        en_tokens = re.findall(r"[a-zA-Z]+", text.lower())
        # 中文按字符 bigram/trigram
        cn_chars = re.findall(r"[\u4e00-\u9fff]+", text)
        cn_tokens = []
        for seg in cn_chars:
            for i in range(len(seg) - min_ngram_len + 1):
                cn_tokens.append(seg[i:i + min_ngram_len])

        all_tokens = en_tokens + cn_tokens
        for n in range(min_ngram_len, min(min_ngram_len + 1, len(all_tokens) + 1)):
            for i in range(len(all_tokens) - n + 1):
                ngram = " ".join(all_tokens[i:i + n])
                if len(ngram) >= 4:  # 过滤太短的 ngram
                    ngram_sources.setdefault(ngram, set()).add(domain)

    # 找出被多个来源佐证的 n-gram
    cross_refs = []
    for ngram, domains in ngram_sources.items():
        if len(domains) >= min_sources:
            cross_refs.append({
                "ngram": ngram,
                "source_count": len(domains),
                "domains": sorted(domains),
            })

    # 按来源数排序，取 top 10
    cross_refs.sort(key=lambda x: x["source_count"], reverse=True)
    return cross_refs[:10]


# ── 问题分解 ──────────────────────────────────────────────────────────────────

def decompose_query(query: str, num_sub: int = 4) -> list[dict[str, str]]:
    """将复杂查询分解为子查询。

    策略：基于关键词特征自动分解，不依赖 LLM。
    """
    sub_queries = []

    # 策略 1：中英文混合 → 分语言搜索
    has_chinese = any("\u4e00" <= c <= "\u9fff" for c in query)
    has_english = any(c.isascii() and c.isalpha() for c in query)

    if has_chinese and has_english:
        # 提取英文核心词
        eng_words = " ".join(w for w in query.split() if w.isascii() and len(w) > 2)
        if eng_words:
            sub_queries.append({
                "query": eng_words,
                "intent": "英文核心概念搜索",
                "strategy": "english_focused"
            })

    # 策略 2：包含年份/时间 → 补充时效性搜索
    import re
    year_match = re.search(r"20\d{2}", query)
    if year_match:
        year = year_match.group()
        sub_queries.append({
            "query": f"{query} {year} latest update",
            "intent": f"{year}年最新进展",
            "strategy": "temporal"
        })

    # 策略 3：包含对比词 → 分别搜索各对象
    compare_match = re.search(r"(?:vs| versus |对比|比较|和|与|及)", query, re.I)
    if compare_match:
        parts = re.split(r"(?:vs| versus |对比|比较|和|与|及)", query, flags=re.I)
        for part in parts[:2]:
            part = part.strip()
            if part and len(part) > 2:
                sub_queries.append({
                    "query": part,
                    "intent": f"独立搜索：{part[:20]}",
                    "strategy": "split_compare"
                })

    # 策略 4：包含「如何/怎么/why」→ 补充教程/方案搜索
    how_match = re.search(r"(?:如何|怎么|how|why|为什么|最佳实践|best practice)", query, re.I)
    if how_match:
        sub_queries.append({
            "query": f"{query} tutorial guide best practices",
            "intent": "教程/最佳实践",
            "strategy": "tutorial"
        })

    # 策略 5：包含「问题/bug/错误」→ 补充社区讨论搜索
    bug_match = re.search(r"(?:bug|error|问题|报错|故障|issue|panic|crash|exception)", query, re.I)
    if bug_match:
        sub_queries.append({
            "query": f"{query} solution fix workaround community",
            "intent": "社区解决方案",
            "strategy": "community_fix"
        })

    # 策略 6：包含「论文/学术」→ 补充学术搜索
    academic_match = re.search(r"(?:论文|paper|arxiv|学术|综述|review|survey|研究)", query, re.I)
    if academic_match:
        sub_queries.append({
            "query": f"{query} arxiv semantic scholar 2024 2025",
            "intent": "学术文献补充",
            "strategy": "academic"
        })

    # 策略 7：包含「安全/CVE」→ 补充安全源
    security_match = re.search(r"(?:CVE|漏洞|vulnerability|security|exploit|PoC)", query, re.I)
    if security_match:
        sub_queries.append({
            "query": f"{query} NVD exploit PoC advisory",
            "intent": "安全数据源补充",
            "strategy": "security"
        })

    # 策略 8：包含「金融/股票/财报」→ 补充金融源
    finance_match = re.search(r"(?:股价|财报|基金|股票|行情|金融|financial|earnings|stock)", query, re.I)
    if finance_match:
        sub_queries.append({
            "query": f"{query} 东方财富 雪球 研报",
            "intent": "金融数据补充",
            "strategy": "finance"
        })

    # 确保至少有原始查询
    if not sub_queries:
        sub_queries.append({
            "query": query,
            "intent": "原始查询",
            "strategy": "direct"
        })

    # 补充通用搜索
    if len(sub_queries) < num_sub:
        sub_queries.append({
            "query": query,
            "intent": "综合搜索",
            "strategy": "general"
        })

    return _deduplicate_sub_queries(sub_queries[:num_sub])


def _deduplicate_sub_queries(sub_queries: list[dict[str, str]]) -> list[dict[str, str]]:
    """基于 Jaccard 相似度去重子查询。"""
    import re as _re
    def _tokens(q: str) -> set:
        return set(_re.findall(r'[a-zA-Z]+|[\u4e00-\u9fff]', q.lower()))
    unique = []
    seen_tokens = []
    for sq in sub_queries:
        tokens = _tokens(sq["query"])
        is_dup = False
        for prev in seen_tokens:
            jaccard = len(tokens & prev) / max(len(tokens | prev), 1)
            if jaccard > 0.6:
                is_dup = True
                break
        if not is_dup:
            unique.append(sq)
            seen_tokens.append(tokens)
    return unique


# ── 多源采集 ──────────────────────────────────────────────────────────────────

def collect_sources(sub_queries: list[dict[str, str]], max_results: int = 5,
                    timeout: int = 15, depth: str = "balanced",
                    mode: str = "auto",
                    engines_priority: list[str] | None = None,
                    profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """对每个子查询并行执行搜索，返回聚合结果。

    共享同一 SearchCache 实例，使子查询间 L1/per-engine 可复用，减少重复联网。

    P0 策略（boost 不 lock）：
      - 始终 engine=auto，由路由 + 域规则决定主源
      - engines_boost 前置 vertical/priority（research_engine_hints）
      - preferred_engine 仅作 boost 首位，不再锁死单引擎（避免冷门源失败整条空）
    """
    all_results = []
    engines_used = set()
    sub_results = []
    t0 = time.time()
    try:
        from cache import SearchCache
        shared_cache = SearchCache()
    except Exception:
        shared_cache = None

    try:
        from engine_policy import research_engine_hints
    except ImportError:
        research_engine_hints = None  # type: ignore

    prio = [e for e in (engines_priority or []) if e]

    def _boost_for(idx: int, sq: dict[str, str]) -> list[str]:
        boosts: list[str] = []
        if research_engine_hints and profile:
            boosts = list(research_engine_hints(profile, idx) or [])
        elif prio:
            # 无 profile 时用 priority 轮转 2 个
            if idx <= 0:
                boosts = prio[:3]
            else:
                n = len(prio)
                start = (idx - 1) % n
                boosts = [prio[(start + i) % n] for i in range(min(2, n))]
        pref = sq.get("preferred_engine")
        if pref and pref != "auto":
            boosts = [pref] + [e for e in boosts if e != pref]
        return boosts

    def _search_one(sq: dict[str, str], idx: int = 0) -> dict[str, Any]:
        # context=research：放行 research_only + 全 combo；boost 抬垂直源
        boosts = _boost_for(idx, sq)
        result = super_search(
            sq["query"], n=max_results, timeout=timeout,
            depth=depth, mode=mode, skip_cache=False,
            cache=shared_cache,
            engine="auto",
            context="research",
            engines_boost=boosts or None,
            envelope=False,  # 研究合成自管结构，减子查询噪音
        )
        return {
            "sub_query": sq["query"],
            "intent": sq["intent"],
            "strategy": sq["strategy"],
            "engine_hint": "auto",
            "engines_boost": boosts,
            "results": result.get("results", []),
            "engines_used": result.get("engines_used", []),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "cached": result.get("cached", False),
        }

    with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as ex:
        futures = {
            ex.submit(_search_one, sq, i): sq
            for i, sq in enumerate(sub_queries)
        }
        all_futures = list(futures.keys())
        try:
            for fut in as_completed(futures, timeout=timeout * 2 + 5):
                try:
                    sr = fut.result()
                    sub_results.append(sr)
                    all_results.extend(sr["results"])
                    engines_used.update(sr["engines_used"])
                except Exception as e:
                    sq = futures[fut]
                    sub_results.append({
                        "sub_query": sq["query"],
                        "intent": sq["intent"],
                        "strategy": sq["strategy"],
                        "results": [],
                        "engines_used": [],
                        "error": str(e),
                        "elapsed_ms": 0,
                    })
        except Exception:
            # 超时后收集已完成的 futures
            for fut in all_futures:
                if fut.done() and not fut.cancelled():
                    try:
                        sr = fut.result()
                        if sr not in sub_results:
                            sub_results.append(sr)
                            all_results.extend(sr["results"])
                            engines_used.update(sr["engines_used"])
                    except Exception:
                        pass

    # RRF 融合
    result_lists = [sr["results"] for sr in sub_results if sr["results"]]
    if len(result_lists) > 1:
        merged = rrf_merge(result_lists)
    elif result_lists:
        merged = deduplicate_by_url(result_lists[0])
    else:
        merged = []

    elapsed = int((time.time() - t0) * 1000)

    return {
        "merged_results": merged[:max_results * 3],
        "sub_results": sub_results,
        "engines_used": sorted(engines_used),
        "total_results": len(merged),
        "elapsed_ms": elapsed,
    }


# ── 知识缺口识别 ──────────────────────────────────────────────────────────────

def identify_gaps(sub_results: list[dict[str, Any]], query: str) -> list[str]:
    """识别搜索结果中的知识缺口。"""
    gaps = []

    # 检查是否有子查询完全失败
    for sr in sub_results:
        if not sr["results"]:
            gaps.append(f"子查询「{sr['intent']}」无结果：{sr['sub_query'][:40]}")

    # 检查是否有子查询结果过少
    for sr in sub_results:
        if sr["results"] and len(sr["results"]) < 2:
            gaps.append(f"子查询「{sr['intent']}」结果稀少（仅 {len(sr['results'])} 条）")

    # 检查来源多样性
    all_sources = set()
    for sr in sub_results:
        for r in sr["results"]:
            src = r.get("source", "")
            if src:
                all_sources.add(src)
    if len(all_sources) < 3:
        gaps.append(f"来源多样性不足：仅 {len(all_sources)} 个引擎有结果（{', '.join(all_sources)}）")

    # 检查时间覆盖
    import re
    year_match = re.search(r"20\d{2}", query)
    if year_match:
        target_year = year_match.group()
        has_recent = False
        for sr in sub_results:
            for r in sr["results"]:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if target_year in title or target_year in snippet:
                    has_recent = True
                    break
        if not has_recent:
            gaps.append(f"未找到 {target_year} 年的直接相关内容")

    return gaps


# ── 综合报告 ──────────────────────────────────────────────────────────────────

# 覆盖度阈值（COVERED/PARTIAL/NOT_COVERED 三态）
_COVERAGE_OK_MIN = 3      # 子查询结果数 ≥3 → COVERED
_COVERAGE_PARTIAL_MIN = 1 # 结果数 ≥1 → PARTIAL，否则 NOT_COVERED


def _coverage_status(sr: dict[str, Any]) -> str:
    """子查询覆盖状态：COVERED / PARTIAL / NOT_COVERED。

      - 结果 ≥3 且来源引擎 ≥2 → COVERED
      - 有结果但少 / 单来源 → PARTIAL
      - 无结果 → NOT_COVERED
    """
    results = sr.get("results") or []
    n = len(results)
    if n >= _COVERAGE_OK_MIN:
        return "COVERED"
    if n >= _COVERAGE_PARTIAL_MIN:
        return "PARTIAL"
    return "NOT_COVERED"


def _evidence_tier(source: str, source_grades: dict[str, Any] | None) -> str:
    """按来源归属证据强度分层。

    source_grades 各层的值可能是 list[str]（引擎名）或 list[str]（来源类型名），
    匹配失败时按 source 关键词粗判，最终兜底 unknown。
    """
    if not source:
        return "unknown"
    grades = source_grades or {}
    # 收集各层的关键词（引擎名 + 描述性来源类型）
    for tier, items in (("primary", grades.get("primary") or grades.get("一手") or []),
                        ("secondary", grades.get("secondary") or grades.get("权威") or []),
                        ("tertiary", grades.get("tertiary") or grades.get("参考") or [])):
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, str):
                continue
            if source == it or source.startswith(it) or it in source:
                return tier
    # 关键词粗判（引擎名后缀匹配）
    s = source.lower()
    if any(k in s for k in ("arxiv", "openalex", "crossref", "semantic_scholar",
                            "pubmed", "europepmc", "github", "cninfo", "fred",
                            "worldbank", "nbs_stats", "sina_quote", "tencent_quote")):
        return "primary"
    if any(k in s for k in ("zhihu", "eastmoney", "byted", "bocha", "octen",
                            "cls_telegraph", "em_global_news", "jin10")):
        return "secondary"
    if any(k in s for k in ("twitter", "reddit", "weibo", "bilibili", "xiaohongshu",
                            "douban", "v2ex", "hackernews", "local_sogou", "local_baidu")):
        return "tertiary"
    return "unknown"


def _build_verification_records(sub_results: list[dict[str, Any]],
                                source_grades: dict[str, Any] | None) -> list[dict[str, Any]]:
    """验证记录表（Claim | 来源 | 核验方法 | 结果）。

    对每个子查询 top1 结果生成一条核验记录：
      - claim：子查询意图对应的主张（snippet 前 120 字）
      - source / url：可追溯来源
      - verification_method：证据强度 + 是否多源共识 + 是否含数字
      - result：verifiable（有来源+可吸收内容） / unverifiable（无来源）
    """
    records = []
    for sr in sub_results:
        results = sr.get("results") or []
        if not results:
            records.append({
                "claim": sr.get("intent", ""),
                "evidence_tier": "unknown",
                "verification_method": "no_results",
                "result": "unverifiable",
                "reason": "no_results",
            })
            continue
        best = results[0]
        url = best.get("url") or ""
        source = best.get("source") or ""
        snippet = (best.get("snippet") or best.get("title") or "")[:120]
        tier = _evidence_tier(source, source_grades)
        # 核验方法：证据强度 + 内容特征
        method_parts = [f"evidence_tier={tier}"]
        if len(results) >= 2:
            method_parts.append("multi_source")
        has_num = any(c.isdigit() for c in (snippet or ""))
        if has_num:
            method_parts.append("has_numbers")
        records.append({
            "claim": snippet or best.get("title") or sr.get("intent", ""),
            "source": source,
            "url": url,
            "evidence_tier": tier,
            "verification_method": "+".join(method_parts),
            "result": "verifiable" if url else "unverifiable",
        })
    return records


def synthesize_report(query: str, collection: dict[str, Any],
                      gaps: list[str],
                      source_grades: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成综合研究报告。"""
    merged = collection["merged_results"]
    sub_results = collection["sub_results"]

    # 按子查询分组的关键发现
    key_findings = []
    for sr in sub_results:
        if sr["results"]:
            best = sr["results"][0]
            key_findings.append({
                "aspect": sr["intent"],
                "strategy": sr["strategy"],
                "top_result": {
                    "title": best.get("title", ""),
                    "url": best.get("url", ""),
                    "snippet": (best.get("snippet", "") or "")[:200],
                    "score": best.get("score", 0),
                    "source": best.get("source", ""),
                },
                "result_count": len(sr["results"]),
            })

    # 来源统计
    source_counts = {}
    for r in merged:
        src = r.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    # 引用列表 + 标准化 sources（与日常搜索 sources[] 同构）
    citations = []
    sources = []
    for i, r in enumerate(merged[:15]):
        ref = i + 1
        url = r.get("url") or ""
        title = r.get("title") or ""
        eng = r.get("source") or ""
        citations.append({
            "id": f"[{ref}]",
            "ref": ref,
            "title": title,
            "url": url,
            "source": eng,
            "score": r.get("score", 0),
            "snippet": (r.get("snippet") or "")[:160] or None,
        })
        if url:
            sources.append({
                "ref": ref,
                "title": title[:160],
                "url": url,
                "engine": eng,
                "score": r.get("score"),
                "snippet": (r.get("snippet") or "")[:160] or None,
            })

    # 关键发现挂上 citation ref，便于正文用 [n] 标注
    url_to_ref = {c["url"]: c["ref"] for c in citations if c.get("url")}
    for kf in key_findings:
        top = kf.get("top_result") or {}
        u = top.get("url") or ""
        if u and u in url_to_ref:
            top["ref"] = url_to_ref[u]
            kf["citation_refs"] = [url_to_ref[u]]
        else:
            kf["citation_refs"] = []

    # 交叉引用检测
    all_results = []
    for sr in sub_results:
        all_results.extend(sr["results"])
    cross_refs = detect_cross_references(all_results)

    # 维度覆盖地图（COVERED/PARTIAL/NOT_COVERED）
    coverage_map = []
    for sr in sub_results:
        results = sr.get("results") or []
        engines = {r.get("source") for r in results if r.get("source")}
        coverage_map.append({
            "dimension": sr.get("intent", ""),
            "sub_query": sr.get("sub_query", ""),
            "status": _coverage_status(sr),
            "result_count": len(results),
            "engine_count": len(engines),
        })

    # 盲区（未覆盖维度 + 单源维度显式化）
    blind_spots = [
        {"dimension": cm["dimension"], "reason": "无结果"}
        for cm in coverage_map if cm["status"] == "NOT_COVERED"
    ]
    blind_spots += [
        {"dimension": cm["dimension"], "reason": "单来源覆盖"}
        for cm in coverage_map
        if cm["status"] == "PARTIAL" and cm["engine_count"] <= 1
    ]
    if not blind_spots and coverage_map:
        blind_spots.append({"dimension": "全局", "reason": "无显式未覆盖维度"})

    # 验证记录表（Claim | 来源 | 核验方法 | 结果）
    verification_records = _build_verification_records(sub_results, source_grades)

    return {
        "query": query,
        "key_findings": key_findings,
        "total_sources": collection["total_results"],
        "engines_used": collection["engines_used"],
        "source_distribution": source_counts,
        "citations": citations,
        "sources": sources,  # 与 search sources[] 对齐
        "cross_references": cross_refs,
        "coverage_map": coverage_map,
        "verification_records": verification_records,
        "blind_spots": blind_spots,
        "gaps": gaps,
        "elapsed_ms": collection["elapsed_ms"],
        "sub_query_count": len(sub_results),
    }


# ── 主入口 ────────────────────────────────────────────────────────────────────

def deep_research(query: str, num_sub_queries: int = 4, max_results: int = 5,
                  timeout: int = 15, depth: str = "balanced",
                  mode: str = "auto",
                  profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行深度研究。

    流程（单次，无 plan↔search 死循环）：
      1. 离线 build_plan 一次（元数据 / limitations）
      2. 查询改写 + 子查询分解（有 profile 时优先模板）
      3. 多源采集（super_search 直搜，context=research 仅作分层标记）
      4. 综合报告；挂上质量门禁/报告结构；requires_confirmation 恒 False
    """
    original_query = query
    engines_priority = list((profile or {}).get("engines_priority") or [])
    vertical_engines = list((profile or {}).get("vertical_engines") or [])

    # 0. 离线 plan 一次（不联网、不回调 research）
    plan_info: dict[str, Any] | None = None
    try:
        from plan import build_plan
        plan_info = build_plan(
            query, mode=mode, depth=depth, max_results=max_results,
            context="research",
        )
    except Exception:
        plan_info = None

    # 1. 查询改写
    rewrite_result = None
    try:
        from query_rewriter import rewrite_query as do_rewrite
        rewrite_result = do_rewrite(query)
        if rewrite_result["rewritten"] and rewrite_result["confidence"] >= 0.7:
            query = rewrite_result["rewritten"]
    except Exception:
        pass

    # 2. 问题分解：profile 模板优先，再用启发式补足
    sub_queries: list[dict[str, str]] = []
    if profile:
        try:
            from topic_research_profiles import build_profile_sub_queries
            sub_queries = build_profile_sub_queries(
                query, profile, num_sub_queries
            )
        except Exception:
            sub_queries = []
    heuristic = decompose_query(query, num_sub_queries)
    # 合并去重
    seen_q = {sq["query"] for sq in sub_queries}
    for sq in heuristic:
        if len(sub_queries) >= num_sub_queries:
            break
        if sq["query"] not in seen_q:
            sub_queries.append(sq)
            seen_q.add(sq["query"])
    if not sub_queries:
        sub_queries = heuristic
    sub_queries = _deduplicate_sub_queries(sub_queries[:num_sub_queries])

    # 3. 多源采集：auto + engines_boost（vertical/priority），不锁死单引擎
    collection = collect_sources(
        sub_queries, max_results, timeout, depth, mode,
        engines_priority=engines_priority or None,
        profile=profile,
    )

    # 4. 知识缺口
    gaps = identify_gaps(collection["sub_results"], query)

    # 5. 综合报告（source_grades 供证据强度分层 / 验证记录表）
    source_grades = (profile or {}).get("source_grades") if profile else None
    report = synthesize_report(query, collection, gaps, source_grades=source_grades)

    report["execution_tier"] = "deep_research"
    report["requires_confirmation"] = False
    report["query_original"] = original_query
    report["sub_queries"] = [
        {"query": sq["query"], "intent": sq["intent"], "strategy": sq["strategy"]}
        for sq in sub_queries
    ]
    if profile:
        try:
            from topic_research_profiles import profile_meta
            meta = profile_meta(profile)
        except Exception:
            meta = {
                "name": profile.get("name"),
                "discipline": profile.get("discipline"),
                "quality_gates": profile.get("quality_gates") or [],
                "report_sections": profile.get("report_sections") or [],
                "source_grades": profile.get("source_grades") or {},
            }
        report["topic_profile"] = meta.get("name")
        report["topic_profile_key"] = None  # 由 main 回填
        report["discipline"] = meta.get("discipline")
        report["quality_gates"] = meta.get("quality_gates") or []
        report["report_sections"] = meta.get("report_sections") or []
        report["source_grades"] = meta.get("source_grades") or {}
        report["engines_priority"] = meta.get("engines_priority") or engines_priority
        report["vertical_engines"] = meta.get("vertical_engines") or vertical_engines
        # 金融域固定免责（研究包）
        if meta.get("discipline") == "finance":
            report["disclaimer"] = (
                "本输出基于公开检索结果整理，供研究线索与信息核验，"
                "不构成投资建议；关键数据请回源核对。"
            )
        if meta.get("discipline") == "academic":
            report["academic_discipline"] = {
                "no_fabricated_doi": True,
                "separate_evidence_layers": True,
                "note": "公开摘要≠已审稿结论；DOI/期刊要求须回官方核验",
            }
    if plan_info is not None:
        report["plan"] = plan_info
        # 计划中的 limitations 并入 gaps 旁白，不阻断
        plan_lims = plan_info.get("limitations") or []
        if plan_lims:
            report.setdefault("plan_limitations", plan_lims)

    if rewrite_result and rewrite_result["rewritten"]:
        report["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }

    return report


# ── 社交舆情研究 ─────────────────────────────────────────────────────────────

def social_sentiment_research(query: str, platforms: list[str] | None = None,
                              max_results: int = 5) -> dict[str, Any]:
    """社交舆情研究：跨平台 UGC 情绪与讨论分析"""
    if platforms is None:
        platforms = ["twitter", "reddit", "xiaohongshu"]

    from search import super_search

    platform_results: dict[str, list] = {}
    all_results: list[dict] = []
    engines_used: set[str] = set()
    t0 = time.time()

    def _one(platform: str) -> tuple[str, list[dict], set[str]]:
        try:
            result = super_search(query, engine=platform, n=max_results, mode="fast")
            return platform, result.get("results", []), set(result.get("engines_used", []))
        except Exception:
            return platform, [], set()

    # 并行抓取各平台（串行时 3 平台 × 秒级延迟累积；并行取最慢平台耗时）
    with ThreadPoolExecutor(max_workers=min(len(platforms), 4)) as ex:
        futures = {ex.submit(_one, p): p for p in platforms}
        for fut in as_completed(futures, timeout=90):
            platform, results, used = fut.result()
            platform_results[platform] = results
            all_results.extend(results)
            engines_used.update(used)

    # 互动数据聚合
    engagement_totals = {"likes": 0, "comments": 0, "shares": 0, "views": 0}
    titles: list[str] = []

    for r in all_results:
        meta = r.get("social_meta", {})
        if isinstance(meta, dict):
            engagement_totals["likes"] += meta.get("likes", meta.get("upvotes", meta.get("attitudes_count", 0)))
            engagement_totals["comments"] += meta.get("comments", meta.get("num_comments", 0))
            engagement_totals["shares"] += meta.get("shares", meta.get("retweets", meta.get("reposts_count", 0)))
            engagement_totals["views"] += meta.get("views", meta.get("play_count", 0))
        # 收集标题用于话题提取
        title = r.get("title", "")
        if title:
            titles.append(title)

    # 中英文兼容的话题提取
    top_topics_list = _extract_topics(titles, top_k=10)
    elapsed = int((time.time() - t0) * 1000)

    return {
        "query": query,
        "mode": "social-sentiment",
        "platforms": platforms,
        "total_posts": len(all_results),
        "platform_breakdown": {p: len(r) for p, r in platform_results.items()},
        "engagement_totals": engagement_totals,
        "top_topics": top_topics_list,
        "cross_platform_posts": [
            {
                "platform": r.get("source", ""),
                "title": r.get("title", "")[:100],
                "url": r.get("url", ""),
                "snippet": r.get("snippet", "")[:200],
                "social_meta": r.get("social_meta", {}),
            }
            for r in all_results[:15]
        ],
        "engines_used": sorted(engines_used),
        "elapsed_ms": elapsed,
    }


def aggregate_social_sentiment(query: str, platforms: list[str],
                               platform_results: dict[str, list]) -> dict[str, Any]:
    """社交舆情聚合（MCP argo_social_search mode=sentiment 用）。

    输入为按平台分组的已抓取帖子（social_engines 输出，含 social_meta），
    聚合互动数据汇总与平台分布，限流返回代表性帖子。原为 MCP 分发层的
    内联逻辑，下沉到本模块避免逻辑放错层。
    """
    all_posts: list = []
    for p in platforms:
        all_posts.extend(platform_results.get(p) or [])

    engagement_totals = {"likes": 0, "comments": 0, "reposts": 0, "shares": 0}
    for post in all_posts:
        meta = post.get("social_meta", {}) if isinstance(post, dict) else {}
        engagement_totals["likes"] += meta.get("likes", 0) or meta.get("like_count", 0) or 0
        engagement_totals["comments"] += meta.get("comments", 0) or 0
        engagement_totals["reposts"] += meta.get("reposts", 0) or 0
        engagement_totals["shares"] += meta.get("shares", 0) or 0

    return {
        "query": query,
        "platforms": platforms,
        "platform_breakdown": {p: len(platform_results.get(p) or []) for p in platforms},
        "total_posts": len(all_posts),
        "engagement_totals": engagement_totals,
        "posts": all_posts[:30],  # 限流
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="深度研究工具")
    parser.add_argument("query", nargs="?", default=None, help="研究查询（--topic help 时可省略）")
    parser.add_argument("--sub-queries", type=int, default=None, help="子查询数量（默认 4，或由 --topic 覆盖）")
    parser.add_argument("-n", "--max-results", type=int, default=None, help="每个子查询最大结果数")
    parser.add_argument("--timeout", type=int, default=15, help="超时秒数")
    parser.add_argument("--depth", choices=["fast", "balanced", "deep"], default=None)
    parser.add_argument("--mode", choices=["fast", "auto", "deep", "budget", "social-sentiment"], default="auto",
                        help="研究模式：fast/auto/deep/budget/social-sentiment")
    parser.add_argument("--platforms", type=str, default=None,
                        help="社交平台列表（仅 social-sentiment 模式），逗号分隔")
    parser.add_argument("--topic", type=str, default=None,
                        help="选题类型：ai/investment/finance/academic/tech/tool/internet/social；"
                             "省略时按查询启发式推断。--topic help 列出全部")
    parser.add_argument("--no-auto-topic", action="store_true",
                        help="禁用根据查询自动推断 --topic")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="跳过工作区归档（研究默认归档；日常搜索不归档）",
    )
    parser.add_argument("--archive-dir", type=str, default=None, help="归档根目录")
    parser.add_argument("--archive-tag", default=None, help="归档标签")
    parser.add_argument("--archive-note", default=None, help="归档备注")
    args = parser.parse_args()
    # 研究层默认归档；--no-archive 退出
    do_archive = not args.no_archive

    # 选题类型 profile 应用（参照 zhihu-creator ENTITY_DOMAIN_MAP 模式）
    profile_obj: dict[str, Any] | None = None
    profile_applied = None
    profile_key = None
    try:
        from topic_research_profiles import (
            get_profile, list_profiles, detect_topic_from_query, list_triggers,
        )
    except ImportError:
        get_profile = list_profiles = detect_topic_from_query = list_triggers = None  # type: ignore

    # --topic help: 列出所有可用选题类型 + 触发词（可无 query）
    if args.topic and str(args.topic).lower() in ("help", "list", "--help"):
        if list_profiles:
            print("可用选题类型（--topic）：")
            print(f"  {'键名':15s} {'名称':14s} {'深度':8s} {'领域':10s} {'引擎':36s}")
            print(f"  {'-'*15} {'-'*14} {'-'*8} {'-'*10} {'-'*36}")
            for p in list_profiles():
                engines = ", ".join(p["engines"][:3])
                print(
                    f"  {p['key']:15s} {p['name']:14s} {p['depth']:8s} "
                    f"{p.get('discipline', ''):10s} {engines}"
                )
            print()
            if list_triggers:
                trig = list_triggers()
                print("深度研究触发词（示例）：", "、".join(trig["deep_research_triggers"][:8]), "…")
                print("主斜杠：", trig.get("main_slash") or "/argo")
                print("子技能斜杠：", " ".join(trig.get("slash_commands") or []))
                print("深度研究斜杠：", " ".join(trig.get("research_slash_commands") or [])[:120])
            print()
            print("示例：")
            print("  python3 research.py \"Claude Opus 5\" --topic ai")
            print("  python3 research.py \"CRISPR 脱靶综述\" --topic academic")
            print("  python3 research.py \"台积电估值分歧\" --topic finance")
            print("  python3 research.py \"离岸信托\" --topic investment")
        sys.exit(0)

    if not args.query:
        parser.error("需要研究查询；或使用 --topic help 查看选题与触发词")

    if args.topic and get_profile and list_profiles:
        profile_obj = get_profile(args.topic)
        if not profile_obj:
            available = ", ".join(p["key"] for p in list_profiles())
            print(f"⚠️  未知选题类型 '{args.topic}'。可用: {available}", file=sys.stderr)
            sys.exit(1)
        profile_key = args.topic.strip().lower()
        from topic_research_profiles import ALIASES
        profile_key = ALIASES.get(args.topic.strip()) or ALIASES.get(profile_key) or profile_key
    elif not args.no_auto_topic and detect_topic_from_query and get_profile:
        auto_key = detect_topic_from_query(args.query)
        if auto_key:
            profile_obj = get_profile(auto_key)
            profile_key = auto_key
            if profile_obj and not args.json:
                print(f"  [topic auto] → {auto_key}（{profile_obj['name']}）", file=sys.stderr)

    if profile_obj:
        if args.sub_queries is None:
            args.sub_queries = int(profile_obj.get("sub_queries") or 4)
        if args.max_results is None:
            args.max_results = int(profile_obj.get("max_results") or 5)
        if args.depth is None:
            args.depth = str(profile_obj.get("depth") or "balanced")
        profile_applied = profile_obj["name"]

    # 无 profile 时的默认
    if args.sub_queries is None:
        args.sub_queries = 4
    if args.max_results is None:
        args.max_results = 5
    if args.depth is None:
        args.depth = "balanced"

    # 社交舆情模式
    if args.mode == "social-sentiment":
        platforms = [p.strip() for p in args.platforms.split(",")] if args.platforms else None
        report = social_sentiment_research(args.query, platforms, args.max_results)
    else:
        report = deep_research(
            args.query, args.sub_queries, args.max_results,
            args.timeout, args.depth, args.mode,
            profile=profile_obj,
        )

    if profile_applied:
        report["topic_profile"] = profile_applied
        if profile_key:
            report["topic_profile_key"] = profile_key

    if do_archive:
        try:
            from archive_run import write_search_archive, resolve_archive_root
            # 研究包投影为可归档 envelope（优先 citations/sources）
            flat_results = []
            for c in report.get("citations") or report.get("sources") or []:
                if isinstance(c, dict) and c.get("url"):
                    flat_results.append({
                        "title": c.get("title") or c.get("url"),
                        "url": c.get("url"),
                        "snippet": c.get("snippet") or "",
                        "source": c.get("source") or c.get("engine") or "research",
                        "score": c.get("score"),
                    })
            if not flat_results:
                for block in report.get("key_findings") or []:
                    if not isinstance(block, dict):
                        continue
                    top = block.get("top_result") or {}
                    if top.get("url"):
                        flat_results.append({
                            "title": top.get("title") or top.get("url"),
                            "url": top.get("url"),
                            "snippet": top.get("snippet") or "",
                            "source": top.get("source") or "research",
                        })
            envelope = {
                "query": report.get("query") or args.query,
                "status": "completed",
                "mode": report.get("mode") or args.mode,
                "depth": args.depth,
                "engine": "research",
                "engines": report.get("engines_used") or ["research"],
                "results": flat_results,
                "count": len(flat_results),
                "sources": report.get("sources") or [],
                "citations": report.get("citations") or [],
                "input_kind": "keyword",
                "limitations": [
                    "research archive: multi-hop discovery package; snippets not verified body",
                ],
                "research_meta": {
                    "sub_queries": report.get("sub_queries"),
                    "gaps": report.get("gaps"),
                    "source_distribution": report.get("source_distribution"),
                    "topic_profile": report.get("topic_profile"),
                    "key_findings": report.get("key_findings"),
                },
                "errors": report.get("errors") or [],
            }
            root = resolve_archive_root(args.archive_dir) if args.archive_dir else None
            meta = write_search_archive(
                envelope,
                root=root,
                tag=args.archive_tag or "research",
                note=args.archive_note,
                source="argo_research",
            )
            report["archive"] = meta
            if not args.json:
                print(f"  [archive] {meta.get('run_id')} → {meta.get('run_dir')}", file=sys.stderr)
        except Exception as e:
            print(f"  [archive error] {type(e).__name__}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if profile_applied:
            print(f"📋 选题类型：{profile_applied}", file=sys.stderr)
        if report.get("mode") == "social-sentiment":
            _print_social_report(report)
        else:
            _print_deep_report(report)


def _print_social_report(report: dict):
    """打印社交舆情报告"""
    print(f"\n{'='*60}")
    print(f"社交舆情分析：{report['query']}")
    print(f"{'='*60}")
    print(f"平台：{', '.join(report['platforms'])} | 引擎：{', '.join(report['engines_used'])}")
    print(f"抓取帖子：{report['total_posts']} | 耗时：{report['elapsed_ms']}ms")
    print()
    print("── 平台分布 ──")
    for platform, count in report["platform_breakdown"].items():
        print(f"  {platform}: {count} 条")
    print()
    print("── 互动数据汇总 ──")
    eng = report["engagement_totals"]
    print(f"  点赞/投票：{eng['likes']:,} | 评论：{eng['comments']:,} | 转发：{eng['shares']:,} | 观看：{eng['views']:,}")
    print()
    print("── 高频讨论话题 ──")
    for topic in report["top_topics"]:
        print(f"  「{topic['topic']}」 ({topic['mentions']} 次)")
    print()
    print("── 代表性内容 ──")
    for post in report["cross_platform_posts"][:5]:
        meta = post.get("social_meta", {})
        engagement = ""
        if isinstance(meta, dict):
            likes = meta.get("likes", meta.get("upvotes", meta.get("attitudes_count", 0)))
            comments = meta.get("comments", meta.get("num_comments", 0))
            engagement = f" | 👍{likes} 💬{comments}"
        print(f"  [{post['platform']}] {post['title'][:60]}{engagement}")
        print(f"    {post['url']}")
        print()


def _print_deep_report(report: dict):
    """打印深度研究报告：正文用 [n] 标注，链接统一沉底「相关信源」。"""
    elapsed_s = report.get("elapsed_ms", 0) / 1000
    sub_count = report.get("sub_query_count", len(report.get("sub_findings", [])))
    total_sources = report.get("total_sources", 0)
    citations = report.get("citations") or []
    sources = report.get("sources") or []
    if not sources and citations:
        sources = [
            {
                "ref": c.get("ref") or i + 1,
                "title": c.get("title"),
                "url": c.get("url"),
                "engine": c.get("source"),
            }
            for i, c in enumerate(citations)
            if isinstance(c, dict) and c.get("url")
        ]
    citations_count = len(citations) or len(sources)

    print(f"\n{'='*60}")
    print("=== 深度研究报告 ===")
    print(f"{'='*60}")
    print(f"查询：{report.get('query', '')}")
    if report.get("rewritten_query"):
        rq = report["rewritten_query"]
        print(f"改写：{rq.get('original', '')} → {rq.get('rewritten', '')}（置信度 {rq.get('confidence', 0):.2f}）")
    print(f"子查询：{sub_count} 个 | 来源：{total_sources} 个 | 引用：{citations_count} 个 | 耗时：{elapsed_s:.1f}s")
    if report.get("archive"):
        print(f"归档：{report['archive'].get('run_dir', '')}")
    print()

    # 关键发现（链接用 [n]，不在正文刷 URL）
    print("## 关键发现")
    print()
    for i, kf in enumerate(report.get("key_findings", []), 1):
        aspect = kf.get("aspect", kf.get("sub_query", ""))
        refs = kf.get("citation_refs") or []
        ref_s = "".join(f"[{r}]" for r in refs) if refs else ""
        print(f"### {i}. {aspect} {ref_s}".rstrip())
        top = kf.get("top_result", {}) or {}
        if top:
            tref = top.get("ref")
            tmark = f"[{tref}] " if tref else ""
            print(f"  {tmark}{top.get('title', '')}")
            snippet = top.get("snippet", "")
            if snippet:
                print(f"  {snippet[:200]}")
        findings = kf.get("findings", [])
        if findings:
            for f in findings:
                title = f.get("title", "")
                print(f"  - {title}")
        print(f"  （本方面 {kf.get('result_count', 0)} 条结果）")
        print()

    # 知识缺口
    gaps = report.get("gaps", [])
    if gaps:
        print("## 知识缺口")
        for g in gaps:
            print(f"  - {g}")
        print()

    # 交叉引用
    cross_refs = report.get("cross_references", [])
    if cross_refs:
        print("## 交叉验证")
        for cr in cross_refs[:5]:
            print(f"  - 「{cr.get('ngram', '')}」被 {cr.get('source_count', 0)} 个来源佐证：{', '.join(cr.get('domains', [])[:5])}")
        print()

    # 知识缺口
    gaps = report.get("gaps") or []
    if gaps:
        print("## 知识缺口 / 盲区")
        for g in gaps[:8]:
            print(f"  - {g}")
        print()

    # 专业质量门禁（选题 profile）
    gates = report.get("quality_gates") or []
    if gates:
        print("## 质量门禁（交付前自检）")
        for g in gates:
            print(f"  - [ ] {g}")
        print()

    sections = report.get("report_sections") or []
    if sections:
        print("## 建议报告结构")
        print("  → " + " / ".join(sections))
        print()

    grades = report.get("source_grades") or {}
    if grades:
        print("## 信源级别参考")
        for level, items in grades.items():
            if isinstance(items, list):
                print(f"  - {level}：{', '.join(str(x) for x in items[:6])}")
            else:
                print(f"  - {level}：{items}")
        print()

    if report.get("disclaimer"):
        print(f"⚠ {report['disclaimer']}")
        print()

    # 来源分布
    source_dist = report.get("source_distribution", {})
    if source_dist:
        print("## 引擎分布")
        for src, cnt in sorted(source_dist.items(), key=lambda x: x[1], reverse=True):
            print(f"  - {src}: {cnt}")
        print()

    engines = report.get("engines_used", [])
    if engines:
        print(f"使用引擎：{', '.join(engines)}")
        print()

    # 底部相关信源（传统搜索引擎形态；引用与 sources 统一）
    if sources or citations:
        print(f"── 相关信源（{len(sources) or len(citations)}）──")
        rows = sources if sources else citations
        for c in rows:
            if not isinstance(c, dict):
                continue
            ref = c.get("ref") or (c.get("id") or "").strip("[]") or "?"
            title = (c.get("title") or "")[:70]
            eng = c.get("engine") or c.get("source") or ""
            url = c.get("url") or ""
            eng_s = f" · {eng}" if eng else ""
            if title:
                print(f"  [{ref}] {title}{eng_s}")
            if url:
                print(f"      {url}")
        print()


def _extract_topics(titles: list[str], top_k: int = 10) -> list[dict]:
    """从标题列表提取话题（中英文兼容）。

    英文用 split() 分词，中文用字符级 bigram。
    过滤单字停用词，返回 top_k 个话题。
    """
    import re
    from collections import Counter

    chinese_stopwords = set(
        '的了是在和我你她他它这就也不很但而或如果因为所以而且或者虽然但是可以应该需要已经正在'
        '之前之后时候地方问题工作生活东西事情时间今天昨天明天今年去年明年个些吗呢啊吧哦嗯哈'
        '呀嘛哪谁什么怎么多少几样种类下上里中后前时来回过开给让把被对从向比跟和与及当比'
        '如例包括相关关于根据通过进行使用作为成为具有属于位于来自获得达到实现完成产生形成'
        '存在发生发展提供包含涉及适用于'
    )

    topic_counter = Counter()

    for title in titles:
        # 英文分词（仅保留长度 >= 3 的词）
        en_words = re.findall(r'[a-zA-Z]{2,}', title.lower())
        for w in en_words:
            if len(w) >= 3:
                topic_counter[w] += 1

        # 中文 bigram（连续 2 个中文字符）
        chinese_chars = re.findall(r'[一-鿿]', title)
        for i in range(len(chinese_chars) - 1):
            bigram = chinese_chars[i] + chinese_chars[i + 1]
            if bigram[0] not in chinese_stopwords and bigram[1] not in chinese_stopwords:
                topic_counter[bigram] += 1

    return [{"topic": t, "mentions": c} for t, c in topic_counter.most_common(top_k)]


if __name__ == "__main__":
    main()
