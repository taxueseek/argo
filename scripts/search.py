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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache
try:
    from cache import query_similarity as _query_similarity
except ImportError:
    _query_similarity = None  # type: ignore
from route import route_query
from engines import search as engine_search, available_engines
from config import get_execution_config, get_cost_factor, get_engines
try:
    from telemetry import emit as _emit_telemetry
except ImportError:
    _emit_telemetry = None  # type: ignore


# ── 时间辅助（时间窗归一化 / published_at 解析 / 后过滤 / 排序）──────────────
#
# 时间窗三层语义：
#   1. 下推（since/until → 引擎）：入口统一归一化为绝对 ISO，引擎收到确定值
#   2. 后过滤（结果层兜底）：引擎不带时间窗能力时，按 published_at 剔除超窗
#   3. 排序（--sort）：仅展示顺序，不影响召回
#
# 归一化规则：相对量（Nd/Nh/Nw）→ 绝对日期；绝对时间无时区按本地时区解释
# （与 _published_ts 一致）；非法输入保持原样下推、不参与后过滤，不阻断搜索。

# published_at 常见形态：YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、ISO(YYYY-MM-DDTHH:MM:SS)
_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
# 相对时间：Nd / Nh / Nw（不区分大小写）
_REL_TIME_RE = re.compile(r"^(\d+)\s*([dhw])$", re.IGNORECASE)
# 纯日期 YYYY-MM-DD（until 边界含当天；下推保留日期形态）
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def _published_ts(r: dict[str, Any]) -> float | None:
    """解析结果的 published_at → epoch 秒；无法解析返回 None（恒排最后）。

    ISO 优先（fromisoformat 支持 T/空格分隔、Z、±HH:MM 时区），
    带时区正确换算 epoch，无时区按本地时区解释；回退 YYYY-MM-DD 手工解析。
    """
    raw = r.get("published_at")
    if not raw:
        return None
    text = str(raw).strip()
    if text.isdigit():  # 部分引擎给 epoch 秒时间戳
        try:
            return float(text)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        # aware datetime 正确换算 UTC epoch；naive 按本地时区解释
        return dt.timestamp()
    except ValueError:
        pass
    m = _DATE_RE.match(text)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
        ).timestamp()
    except ValueError:
        return None


def _parse_time_value(value: Any) -> tuple[str | None, float | None]:
    """解析单边时间窗 → (归一化 ISO 字符串, epoch 秒)。

    支持：相对量（Nd/Nh/Nw）、YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、
    ISO 8601（含 Z / ±HH:MM）、纯数字 epoch 秒。
    相对量归一化为绝对日期（YYYY-MM-DD），语义确定、可入缓存键；
    无法解析返回 (None, None)，调用方保持原样下推、不参与后过滤。
    """
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    # epoch 秒
    if text.isdigit():
        try:
            ts = float(text)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat(timespec="seconds"), ts
        except (ValueError, OSError):
            return None, None
    # 相对量：Nd / Nh / Nw → 绝对日期（本地时区零点）
    m = _REL_TIME_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        now = datetime.now()
        if unit == "h":
            dt = now - timedelta(hours=amount)
        elif unit == "w":
            dt = now - timedelta(weeks=amount)
        else:
            dt = now - timedelta(days=amount)
        d = dt.date()
        return d.isoformat(), datetime(d.year, d.month, d.day).timestamp()
    # 绝对时间：fromisoformat 优先（T/空格分隔、Z、±HH:MM）
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is not None:
        return dt.isoformat(timespec="seconds"), dt.timestamp()
    # 无时区：按本地时区（与 _published_ts 无时区行为一致）；
    # 纯日期（时间为零点）保留 YYYY-MM-DD 形态下推，兼容引擎既有解析
    if (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0):
        return dt.date().isoformat(), dt.timestamp()
    return dt.isoformat(timespec="seconds"), dt.timestamp()


def _normalize_time_window(
    since: str | None, until: str | None
) -> tuple[str | None, str | None, float | None, float | None]:
    """归一化时间窗 → (since_iso, until_iso, since_ts, until_ts)。

    下推与缓存键使用归一化 ISO（相对值转绝对日期，消除 7d 与绝对日期的
    缓存碎片）；后过滤使用 epoch 秒。非法输入 iso 保留原始字符串、
    ts 为 None：仍会下推原值、缓存键仍区分，但不参与后过滤，不阻断搜索。
    """
    s_iso, s_ts = _parse_time_value(since)
    u_iso, u_ts = _parse_time_value(until)
    # 纯日期 until 语义为「含当天」：边界取当天最后一刻，
    # 使次日零点及之后的结果被剔除、当天 23:59:59 保留
    if u_iso and _DATE_ONLY_RE.fullmatch(u_iso):
        try:
            d = datetime.fromisoformat(u_iso).date()
            u_ts = datetime(d.year, d.month, d.day, 23, 59, 59, 999999).timestamp()
        except ValueError:
            pass
    s_raw = str(since).strip() if since not in (None, "") else None
    u_raw = str(until).strip() if until not in (None, "") else None
    return (s_iso or s_raw, u_iso or u_raw, s_ts, u_ts)


def _apply_time_window(
    results: list[dict[str, Any]], since_ts: float | None, until_ts: float | None
) -> tuple[list[dict[str, Any]], int]:
    """结果后过滤兜底：剔除「有 published_at 且明确超窗」的条目。

    宽松策略：无时间字段的结果无法判断、予以保留（避免大多数引擎清空）；
    只有时间明确落在窗口外的才剔除。返回 (保留列表, 剔除数) 供 envelope 上报。
    """
    if since_ts is None and until_ts is None:
        return results, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for r in results:
        ts = _published_ts(r)
        if ts is not None:
            if since_ts is not None and ts < since_ts:
                dropped += 1
                continue
            if until_ts is not None and ts > until_ts:
                dropped += 1
                continue
        kept.append(r)
    return kept, dropped


# 带发布时间能力（published_at）的引擎集合。时间窗的缓存键隔离与后过滤
# 只对含这些引擎的组合生效：不带时间字段的引擎会忽略时间窗、结果相同，
# 隔离缓存键只会白白降低命中率（7d/30d 查同一引擎本可共享缓存）。
# local_search 聚合内部可能选中 news 类子引擎（带日期），保守纳入。
_TIME_CAPABLE_ENGINES: frozenset[str] = frozenset({
    "realtime_index", "wayback_cdx", "local_search",
    "local_bing_news", "local_google_news", "local_duckduckgo_news",
    "local_ddgs_news",
    # parallel（after_date 下推）/ you（freshness 动态化 + page_age）
    "parallel", "you",
})


def _is_time_capable(eng: str) -> bool:
    """引擎是否可能返回 published_at（决定时间窗是否参与缓存键/后过滤）。"""
    return eng in _TIME_CAPABLE_ENGINES


def _sort_results(results: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """按时间重排结果集：oldest 升序 / newest 降序 / relevance 原序。

    排序是纯本地展示顺序：不改变结果集、不进入缓存键、不影响缓存内容；
    无日期条目恒排最后；同时间保持原相对顺序（稳定排序，结果可复现）。
    """
    if sort not in ("oldest", "newest"):
        return results
    if len(results) <= 1:
        return results

    def _key(r: dict[str, Any]) -> tuple[int, float]:
        ts = _published_ts(r)
        if ts is None:
            return (1, 0.0)  # 无日期恒排最后
        return (0, ts if sort == "oldest" else -ts)

    return sorted(results, key=_key)


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


def _results_sufficient(
    results: list[dict[str, Any]],
    mode: str = "auto",
    min_results: int | None = None,
) -> bool:
    return _sufficient_internal(results, mode, min_results)


def _cumulative_sufficient(raw_results: dict[str, list[dict[str, Any]]],
                           mode: str = "auto",
                           min_results: int | None = None) -> bool:
    """跨引擎累计结果是否已够（wave-2 提前终止判定）。

    与 _results_sufficient 同阈值，但把 raw_results 里所有已完成的
    引擎结果合并成一条列表再判，避免单一引擎不足时重复等待慢源。
    """
    merged: list[dict[str, Any]] = []
    for res in raw_results.values():
        if not res:
            continue
        merged.extend(
            r for r in res if isinstance(r, dict) and "error" not in r
        )
    return _sufficient_internal(merged, mode, min_results)


def _sufficient_internal(
    results: list[dict[str, Any]],
    mode: str,
    min_results: int | None,
) -> bool:
    """渐进检索 early-stop：结果是否已够用。

    轻量启发式（不依赖网络/LLM）：
      - 默认 auto：至少 3 条非错误 + 2 条有 snippet
      - 默认 fast：至少 2 条 + 1 个 snippet
      - min_results：域配置覆盖（答案型源 1 条快照即够用）
    """
    goods = [r for r in results if isinstance(r, dict) and "error" not in r]
    if not goods:
        return False
    with_snippet = sum(
        1 for r in goods
        if (r.get("snippet") or r.get("title") or "").strip()
    )
    if min_results is not None:
        try:
            need = max(1, int(min_results))
        except (TypeError, ValueError):
            need = 1
        # 答案型 1 条要求有可展示正文；≥3 条时至少 2 条有 snippet
        need_snip = 1 if need <= 2 else max(2, need - 1)
        return len(goods) >= need and with_snippet >= min(need_snip, len(goods))
    if mode == "fast":
        return len(goods) >= 2 and with_snippet >= 1
    return len(goods) >= 3 and with_snippet >= 2


def _record_quota(engine: str, success: bool) -> None:
    """真实打网后写配额；失败静默。"""
    try:
        from quota import get_quota_manager
        get_quota_manager().record(engine, success=success)
    except Exception:
        pass


# ── 进度阶段 ──────────────────────────────────────────────────────────────────

class Stage(str, Enum):
    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"
    ERROR = "error"


# ── RRF 融合 ───────────────────────────────────────────────────────────────────

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "ref", "from", "share_token", "source", "refer", "trace",
    "clicktime", "clickid", "scid", "scene", "sessionid",
})


def _canonical_url(url: str) -> str:
    """URL 归一化：统一 http/https、小写、去 www.、去 tracking 参数、去 fragment 与尾斜杠。

    用于融合/去重的键，保证 http/https、www、utm 等变体合并为同一结果。
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        p = urlparse(url.lower())
        netloc = p.netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        scheme = "https" if p.scheme in ("http", "https") else p.scheme
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
        path = p.path.rstrip("/")
        return urlunparse((scheme, netloc, path, p.params, urlencode(q), ""))
    except Exception:
        return url


# 引擎融合权重（WG-RRF：按来源质量加权，权威源提权、社交/低质源降权）
_ENGINE_FUSION_WEIGHTS: dict[str, float] = {
    # 权威百科/学术/官方
    "wikipedia": 1.4, "wikidata": 1.4, "zh_wikipedia": 1.4, "baidu_baike": 1.3,
    "arxiv": 1.3, "openalex": 1.3, "crossref": 1.3, "semantic_scholar": 1.3,
    "dblp": 1.3, "europepmc": 1.3, "pubmed": 1.3, "google_scholar": 1.3,
    "pubchem": 1.3, "uniprot": 1.3, "rcsb_pdb": 1.3,
    "github": 1.2, "pypi": 1.2, "npm": 1.2, "crates": 1.2, "mdn": 1.2,
    "stackoverflow": 1.1, "imdb": 1.2, "thesportsdb": 1.2, "itunes": 1.2,
    "finviz": 1.2, "sina_quote": 1.2, "tencent_quote": 1.2, "eastmoney": 1.2,
    "fred": 1.3, "worldbank": 1.3, "nbs_stats": 1.3, "eurostat": 1.3,
    # 通用引擎（基线）
    "duckduckgo": 1.0, "local_bing": 1.0, "local_duckduckgo": 1.0,
    "local_google": 1.0, "anysearch": 1.05, "byted": 1.1, "bocha": 1.0,
    "bocha_ai": 1.3,  # 垂直结构化模态卡（实时值）
    "brave": 1.0, "uapi": 1.0, "local_search": 1.0, "octen": 1.0,
    "gdelt": 1.0, "opencorporates": 1.2, "google_patents": 1.2,
    # 社交/低质（降权）
    "twitter": 0.7, "reddit": 0.7, "xiaohongshu": 0.7, "bilibili": 0.7,
    "weibo": 0.7, "v2ex": 0.8, "zhihu": 0.8, "hackernews": 0.8, "zhihu_hot": 0.8,
    "baidu_hot": 0.8, "toutiao_hot": 0.8, "bilibili_hot": 0.8,
}


# 动态可靠性因子（weakest-link，论文 arxiv 2508.01405）：熔断/高错误引擎降权，
# 避免「弱检索路径」在融合时拖垮整体精度。带 30s TTL 缓存，避免热路径重复查询。
_rel_factor_cache: dict[str, tuple[float, float]] = {}
_REL_FACTOR_TTL = 30.0


def _single_reliability(engine: str) -> float:
    now = time.time()
    cached = _rel_factor_cache.get(engine)
    if cached and cached[1] > now:
        return cached[0]
    factor = 1.0
    try:
        from circuit_breaker import get_breaker
        st = get_breaker().status(engine)
        state = st.get("state")
        if state == "disabled":
            factor = 0.5
        elif state == "open":
            factor = 0.7
        elif state == "half_open":
            factor = 0.85
        failures = int(st.get("failures") or 0)
        if failures >= 5:
            factor = min(factor, 0.8)
    except Exception:
        factor = 1.0
    _rel_factor_cache[engine] = (factor, now + _REL_FACTOR_TTL)
    return factor


def _engine_weight(source: str) -> float:
    """按引擎来源返回融合权重（source 可能含 'local_bing/sina_quote' 合并形式）。

    静态基础权重（权威/学术提权、社交降权）× 动态可靠性因子（weakest-link）：
    熔断/高错误源降权，健康权威源维持提权。论文 2508.01405 的路径质量评估落地。
    """
    if not source:
        return 1.0
    # 合并来源：静态权重取最高源，可靠性取最低源（weakest-link：任一路径弱即降权）
    parts = [p.strip() for p in str(source).split("/") if p.strip()]
    if not parts:
        return 1.0
    static = max([_ENGINE_FUSION_WEIGHTS.get(p, 1.0) for p in parts])
    rel = min([_single_reliability(p) for p in parts])
    return round(static * rel, 3)


def rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = 60,
              weighted: bool = True) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 合并多引擎结果，保留 consensus_engines。

    键用归一化 URL（http/https、www、utm 变体合并）；RRF 分单独存 _rrf_score，
    首次遇到的结果保留完整字段，后续同 URL 只累加共识、择优补充 snippet，
    避免「score 字段赢家通吃」覆盖共识内容。

    weighted（WG-RRF）：按引擎来源加权（权威源提权、社交源降权），
    默认开启；传 False 回到经典 RRF 行为。
    """
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for results in ranked_lists:
        for i, r in enumerate(results):
            # 无 URL 时用 title 兜底；模态卡再退到 card_type（避免空 title 互撞）
            key = (
                _canonical_url(r.get("url", ""))
                or (f"__title__:{r.get('title', '')}" if r.get("title") else "")
                or (f"__card__:{r.get('card_type', '')}" if r.get("card_type") else "")
                or f"__idx__:{i}"
            )
            w = _engine_weight(r.get("_engine") or r.get("source") or "") if weighted else 1.0
            scores[key] = scores.get(key, 0.0) + w / (k + i + 1)
            eng = r.get("_engine") or r.get("source", "") or ""
            if key not in items:
                item = dict(r)
                item["_rrf_score"] = 0.0  # 排序后统一写回
                cons: list[str] = []
                if eng:
                    cons.append(eng)
                item["consensus_engines"] = cons
                items[key] = item
            else:
                cur = items[key]
                # 择优保留内容更完整的版本（title+snippet 更长者胜），不覆盖其余字段
                new_txt = f"{r.get('title', '')} {r.get('snippet', '')}"
                cur_txt = f"{cur.get('title', '')} {cur.get('snippet', '')}"
                if len(new_txt) > len(cur_txt):
                    cur["title"] = r.get("title", cur.get("title"))
                    cur["snippet"] = r.get("snippet", cur.get("snippet"))
                sources = {cur.get("source", ""), r.get("source", "")}
                cur["source"] = "/".join(s for s in sources if s)
                cons = list(cur.get("consensus_engines") or [])
                if eng and eng not in cons:
                    cons.append(eng)
                cur["consensus_engines"] = cons

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for key, _ in ranked:
        item = items[key]
        item["_rrf_score"] = round(scores[key], 6)
        out.append(item)
    return out


def _content_similarity(a: str, b: str) -> float:
    """标题+片段的 minhash 相似度（复用 cache.query_similarity，失败回退 Jaccard）。"""
    if not a or not b:
        return 0.0
    if _query_similarity is not None:
        try:
            return float(_query_similarity(a, b))
        except Exception:
            pass
    import re as _re
    sa, sb = set(_re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", a.lower())), set(_re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def _content_sig(r: dict[str, Any]) -> str:
    return f"{r.get('title', '') or ''} {r.get('snippet', '') or ''}".strip()


def minhash_dedupe(
    results: list[dict[str, Any]], threshold: float = 0.85, enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """结果级近重复去重（MMR 前置）：同一事件多引擎同质网页堆叠时去重。

    流程：URL 归一键已去重 → 剩余按(score, selection)降序贪心，content_similarity ≥ threshold 视为近重复，仅留首条。
    开关：ARGO_MINHASH_DEDUPE=0 时关闭；默认开启（阈值可由 ARGO_MINHASH_THRESHOLD 覆盖，默认 0.85）。
    返回 (deduped, removed_count)，每条被移除的结果记 _near_dup_of 指向保留条 ref。
    """
    if enabled is None:
        enabled = os.environ.get("ARGO_MINHASH_DEDUPE", "1").strip() not in ("0", "false", "False", "no")
    if not enabled or not results or len(results) <= 1:
        return results, 0
    try:
        thr = float(os.environ.get("ARGO_MINHASH_THRESHOLD", str(threshold)))
        threshold = max(0.5, min(0.98, thr))
    except Exception:
        pass
    pool = sorted(
        results,
        key=lambda r: (float(r.get("score", 0) or 0), float(r.get("selection", 0) or 0)),
        reverse=True,
    )
    kept: list[dict[str, Any]] = []
    kept_sigs: list[str] = []
    removed = 0
    for r in pool:
        sig = _content_sig(r)
        is_dup = False
        for ks in kept_sigs:
            if _content_similarity(sig, ks) >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(r)
            kept_sigs.append(sig)
        else:
            removed += 1
            r["_near_dup"] = True
    return kept, removed


def deduplicate_by_url(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """URL 去重（归一化键）。"""
    seen: set[str] = set()
    out = []
    for r in results:
        key = (
            _canonical_url(r.get("url", ""))
            or (f"title:{r.get('title', '')}" if r.get("title") else "")
            or (f"card:{r.get('card_type', '')}" if r.get("card_type") else "")
            or f"anon:{len(out)}"
        )
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── 多语言结果语言偏好软排序（P2-覆盖，2026-08 新增）────────────────────────
# ja/ko 明确主语言查询：把含目标语言字符（假名/谚文）的结果前移，纯相反语言
# 结果后移。**软排序不删除**（避免误删混合/技术结果），其余语言零开销返回。
def _lang_prefer_rerank(results: list[dict[str, Any]],
                        primary_lang: str | None) -> list[dict[str, Any]]:
    if not results or primary_lang not in ("ja", "ko"):
        return results
    if primary_lang == "ja":
        _pat = re.compile(r"[\u3040-\u30ff]")
    else:
        _pat = re.compile(r"[\uac00-\ud7af]")

    def _key(r: dict[str, Any]) -> int:
        hay = f"{r.get('title', '')} {r.get('snippet', '')}"
        return 0 if _pat.search(hay) else 1

    # stable sort：含目标语言字符在前，其余保持原 RRF 顺序
    return sorted(results, key=_key)


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


# ── P0-003：本地五维 Rerank 兜底 ──────────────────────────────────────────────

_CJK_OR_WORD = None  # 延迟编译


def _tokens(text: str) -> list[str]:
    """轻量分词：中文单字 + 英文单词，统一小写（复用 tfidf 风格）。"""
    global _CJK_OR_WORD
    if _CJK_OR_WORD is None:
        import re as _re
        _CJK_OR_WORD = _re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")
    return [t for t in _CJK_OR_WORD.findall((text or "").lower())]


def _bigrams(tokens: list[str]) -> set[str]:
    return {f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _score_relevance(query_tokens: set[str], title: str, snippet: str) -> float:
    """相关性：查询 token 在 title+snippet 的覆盖率（title 权重更高）。"""
    if not query_tokens:
        return 0.5
    t_tokens = set(_tokens(title))
    s_tokens = set(_tokens(snippet))
    title_cov = len(query_tokens & t_tokens) / len(query_tokens)
    snip_cov = len(query_tokens & s_tokens) / len(query_tokens)
    return round(min(1.0, 0.65 * title_cov + 0.35 * snip_cov), 4)


def _score_completeness(title: str, snippet: str) -> float:
    """完整性：snippet 长度 + 是否含数字/结构信号，归一到 0-1。"""
    length = len(snippet or "")
    length_score = min(length / 200.0, 1.0)
    has_digit = 1.0 if any(c.isdigit() for c in (snippet or "")) else 0.0
    has_title = 1.0 if (title or "").strip() else 0.0
    return round(min(1.0, 0.6 * length_score + 0.2 * has_digit + 0.2 * has_title), 4)


def local_five_dim_rerank(query: str, results: list[dict[str, Any]],
                          domain: str = "general", top_n: int = 10
                          ) -> list[dict[str, Any]]:
    """本地五维精排（无 Bocha Key / fallback 时兜底）。

    维度权重（通用）：
      相关性 0.30 + 权威性 0.30 + 时效性 0.20 + 完整性 0.15 + 新颖性 0.05
    tech/code 域：权威 0.20、相关 0.40（技术查询更看内容匹配）。

    新颖性：标题 bigram 与「已排更高结果」的 Jaccard 互补（1 − overlap），
    奖励信息增量，抑制近重复堆叠。

    每个结果写入 rerank_dims 明细，供可观测。
    """
    if not results:
        return results

    def _src_has(source: str, name: str) -> bool:
        # rrf_merge 会把同 URL 结果的 source 合并成 "local_bing/sina_quote"，
        # 精确匹配会漏掉合并后的结果，这里按「/」切分做成员判断。
        return name in str(source).split("/")

    # 权重表（MECE，和为 1）
    is_tech = domain in ("tech_deep", "code_search", "local_code", "academic")
    if is_tech:
        w = {"relevance": 0.40, "authority": 0.20, "freshness": 0.20,
             "completeness": 0.15, "novelty": 0.05}
    else:
        w = {"relevance": 0.30, "authority": 0.30, "freshness": 0.20,
             "completeness": 0.15, "novelty": 0.05}

    # 复用 evidence 的权威/时效评分（若可用）
    try:
        from evidence import score_authority, score_freshness
        _has_evidence = True
    except ImportError:
        _has_evidence = False

    query_tokens = set(_tokens(query))

    # 先计算前四维静态分
    enriched = []
    for r in results:
        title = r.get("title", "") or ""
        snippet = r.get("snippet", "") or ""
        url = r.get("url", "") or ""
        source = r.get("source", "") or ""
        relevance = _score_relevance(query_tokens, title, snippet)
        # book_search 域：微信读书/豆瓣/Open Library 的书目结果是「答案型」，
        # 书名对查询词的 token 覆盖率天然低（「三体全集」vs「科幻小说推荐」），
        # 直接按网页标准评 relevance 会被列表页碾压。给书目源保底相关分。
        if domain == "book_search" and source in ("weread", "douban_book", "open_library"):
            relevance = max(relevance, 0.6)
        # 金融答案型源保底：行情快照/汇率/官方公告的标题是「答案」而非「网页」，
        # token 覆盖率天然低（「茅台股价」vs「贵州茅台 1350.600 ↓-0.82%」）。
        # 这些源命中即答案，且 cninfo 是官方公告源，权威分也保底。
        if domain == "stock_query" and _src_has(source, "sina_quote"):
            relevance = max(relevance, 1.0)
        if domain == "stock_query" and _src_has(source, "tencent_quote"):
            relevance = max(relevance, 1.0)
        if domain == "stock_query" and _src_has(source, "em_flow"):
            relevance = max(relevance, 0.9)
        if domain == "macro_data" and _src_has(source, "fx_rate"):
            relevance = max(relevance, 0.9)
        if domain == "macro_data" and _src_has(source, "worldbank"):
            relevance = max(relevance, 0.85)
        if domain == "financial_news" and _src_has(source, "cninfo"):
            relevance = max(relevance, 0.85)
        if _has_evidence:
            try:
                authority = float(score_authority(url, source).get("score", 0.5))
            except Exception:
                authority = 0.5
            try:
                freshness = float(score_freshness(r).get("score", 0.5))
            except Exception:
                freshness = 0.5
            # cninfo/sina/fx 域名被 evidence 判为低权威（0.5-0.55），
            # 实为官方公告/行情源，权威分保底避免被商业站碾压。
            if (_src_has(source, "cninfo") or _src_has(source, "sina_quote")
                    or _src_has(source, "fx_rate") or _src_has(source, "tencent_quote")
                    or _src_has(source, "em_flow") or _src_has(source, "worldbank")):
                authority = max(authority, 0.85)
            # 行情/汇率/资金流快照是「实时答案」，时效分保底，避免被陈旧新闻页压制。
            if (_src_has(source, "sina_quote") or _src_has(source, "fx_rate")
                    or _src_has(source, "tencent_quote") or _src_has(source, "em_flow")):
                freshness = max(freshness, 0.85)
        else:
            authority, freshness = 0.5, 0.5
        completeness = _score_completeness(title, snippet)
        enriched.append({
            "r": r, "title": title,
            "relevance": relevance, "authority": authority,
            "freshness": freshness, "completeness": completeness,
        })

    # 贪心排序：每步选边际得分最高者，novelty 相对已选集合动态计算
    ranked: list[dict[str, Any]] = []
    selected_bigrams: set[str] = set()
    pool = enriched[:]
    while pool:
        best_idx, best_score, best_novelty = 0, -1.0, 1.0
        for i, e in enumerate(pool):
            bg = _bigrams(_tokens(e["title"]))
            novelty = 1.0 - _jaccard(bg, selected_bigrams)
            score = (w["relevance"] * e["relevance"]
                     + w["authority"] * e["authority"]
                     + w["freshness"] * e["freshness"]
                     + w["completeness"] * e["completeness"]
                     + w["novelty"] * novelty)
            if score > best_score:
                best_idx, best_score, best_novelty = i, score, novelty
        chosen = pool.pop(best_idx)
        r = chosen["r"]
        r["score"] = round(best_score, 4)
        r["rerank_dims"] = {
            "relevance": chosen["relevance"],
            "authority": round(chosen["authority"], 4),
            "freshness": round(chosen["freshness"], 4),
            "completeness": chosen["completeness"],
            "novelty": round(best_novelty, 4),
        }
        selected_bigrams |= _bigrams(_tokens(chosen["title"]))
        ranked.append(r)

    return ranked[:top_n]


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
                   since: str | None = None, until: str | None = None,
                   sort: str = "relevance",
                   on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None) -> dict[str, Any]:
    """执行搜索：缓存 → 熔断/负缓存 → 引擎 → 融合 → 精排 → 过滤 → 写缓存。"""
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    # 防御：过滤空引擎名（空串会在 registry 查无 → 空结果 → 熔断空键 ''）。
    # 全空时兜底 anysearch，防止下方 engines[0] IndexError（route 层已保证
    # combo 非空，此处仅防畸形 decision 直接调用 execute_search）。
    engines = [e for e in engines_combo if e] or ["anysearch"]
    parallel = decision.get("parallel", False) and len(engines) > 1

    # P0-001：查询理解 — clean_query 用于检索，exclude_terms 用于融合后过滤
    exclude_terms: list[str] = []
    retrieval_query = query
    qu = None
    try:
        from query_understanding import _understand_cached as understand
        qu = understand(query)
        exclude_terms = qu.exclude_terms
        # 仅当去否定片段后仍有实义内容时才替换检索词，避免空检索
        if qu.clean_query and qu.clean_query.strip():
            retrieval_query = qu.clean_query
    except ImportError:
        pass  # query_understanding 不可用
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询理解跳过: {type(e).__name__}")

    # 词形规范化：全角→半角、拆斜杠、压多余空格（提升精确源命中，治型号/日期分隔符）
    try:
        from query_enhance import normalize_query
        retrieval_query = normalize_query(retrieval_query)
    except ImportError:
        pass

    if on_progress:
        on_progress(Stage.START, {"query": query})

    # 网络环境感知：慢网放大超时预算（避免误杀），快网收紧（更快响应）
    _eff_timeout = timeout
    try:
        from network_aware import adjusted_timeout, network_profile
        _eff_timeout = adjusted_timeout(timeout, engines)
        if _eff_timeout != timeout:
            import logging
            logging.getLogger("unified_search").debug(
                f"网络感知超时: {timeout}s → {_eff_timeout}s "
                f"({network_profile(engines).get('network')})",
            )
    except ImportError:
        pass

    # 时间窗归一化：下推/缓存键用归一化 ISO（相对值转绝对日期），
    # 后过滤用 epoch 秒；非法输入保持原样下推、不参与后过滤。
    since_iso, until_iso, since_ts, until_ts = _normalize_time_window(since, until)

    cache_engine_key = "+".join(sorted(engines)) if len(engines) > 1 else engines[0]
    # 时间窗并入缓存键：同一 query 不同 since/until 不串缓存；
    # 用归一化 ISO（7d 与等价绝对日期共享缓存；相对窗跨天自然过期不串旧数据）。
    # 仅当组合内含带时间能力引擎时隔离：无时间字段引擎忽略时间窗、结果相同，
    # 隔离只会降低命中率（7d/30d 查 octen/anysearch 命中同一缓存）。
    time_aware = any(_is_time_capable(e) for e in engines)
    if since_iso and time_aware:
        cache_engine_key += f"|since={since_iso}"
    if until_iso and time_aware:
        cache_engine_key += f"|until={until_iso}"

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
            # 排序在缓存读出后、返回前：缓存内容保持 score 序，sort 只改展示顺序
            hit_results = _sort_results(hit.get("results", []), sort)
            return {
                "query": query, "engine": engine_label, "engines": engines,
                "engines_combo": engines_combo, "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain, "elapsed_ms": cache_elapsed,
                "tfidf_scores": tfidf_scores,
                "login_hint": decision.get("login_hint"),
                "results": hit_results,
                "count": len(hit_results),
                "engines_used": hit.get("engines_used") or engines,
                "mode": mode, "depth": depth,
                "reranker": "skipped_cache",
                "engine_outcomes": hit.get("engine_outcomes") or [],
                "time_filtered": 0,
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

    # 慢源禁重试：timeout ≥ 8s 的引擎超时即放弃，避免「10s×3 次=30s」线性放大。
    # 超时本质上是源端慢/网络抖，重试不改变结果，只放大尾延迟；快速失败
    # （连接错/4xx）保留重试，重试成本低。
    try:
        _engine_specs = get_engines()
    except Exception:
        _engine_specs = {}

    def _engine_retries(eng: str) -> int:
        spec = (_engine_specs or {}).get(eng) or {}
        eng_timeout = None
        if isinstance(spec, dict):
            t = spec.get("timeout")
            if isinstance(t, (int, float)) and t > 0:
                eng_timeout = float(t)
        if eng_timeout is not None and eng_timeout >= 8.0:
            return 0
        return retry_count

    def _exec_engine(eng: str, retries: int | None = None,
                     eff_timeout: float | None = None) -> list[dict[str, Any]]:
        # P0-001：用 retrieval_query（clean_query）检索
        if retries is None:
            retries = _engine_retries(eng)
        to = eff_timeout if eff_timeout is not None else timeout
        last_result: list[dict[str, Any]] = []
        for _attempt in range(retries + 1):
            last_result = engine_search(
                retrieval_query, eng, n=max_results, timeout=to, depth=depth, mode=mode,
                since=since_iso, until=until_iso, skip_cache=skip_cache,
            )
            if last_result and any("error" not in r for r in last_result):
                return last_result
        # 慢源（retries=0，超时即弃）不再用 balanced 补跑，避免超时场景双倍耗时
        if retries > 0 and depth != "balanced":
            last_result = engine_search(
                retrieval_query, eng, n=max_results, timeout=to, depth="balanced", mode=mode,
                since=since_iso, until=until_iso, skip_cache=skip_cache,
            )
        return last_result

    def _run_one(eng: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], int]:
        """单引擎：负缓存 → 熔断 → per-engine 缓存 → 网络。"""
        t_eng = time.time()

        # 时间窗只隔离带时间能力引擎的 per-engine 缓存（与 combo 键同语义）
        eng_since = since_iso if _is_time_capable(eng) else None
        eng_until = until_iso if _is_time_capable(eng) else None

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
                since=eng_since, until=eng_until,
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
        # 答案型域（early_min 存在，1 条快照即可交付）的慢源收紧超时：
        # FRED/Eurostat 这类 timeout=10s 的源一旦挂掉就阻塞整条串行路径，
        # 而快源（worldbank 等 ~150ms）已能交付答案。慢源 5s 内没回就让位。
        # 非答案域（fast/auto/budget 且非 deep）：timeout≥10s 的引擎同样收紧
        # 到 6s——多数正常引擎 <2s，10-15s 的超时只为极端慢源兜底，
        # 串行/并行组合里一个慢源就会拖垮整个响应尾部。
        eff_to: float | None = None
        _tighten = (early_min is not None) or (
            mode in ("fast", "auto", "budget") and depth != "deep"
        )
        if _tighten:
            spec = (_engine_specs or {}).get(eng) or {}
            eng_to = None
            if isinstance(spec, dict):
                t = spec.get("timeout")
                if isinstance(t, (int, float)) and t > 0:
                    eng_to = float(t)
            cap = 5.0 if early_min is not None else 6.0
            # half_open 半开探测收紧到 2s：熔断器允许半开探测恢复，但探测应短促，
            # 避免 6s 探测阻塞串行/并行主路径（慢源拖尾主因）。2026-08 修复。
            if breaker is not None:
                try:
                    if breaker.status(eng).get("state") == "half_open":
                        cap = min(cap, 2.0)
                except Exception:
                    pass
            if eng_to is not None and eng_to >= 8.0:
                eff_to = min(float(timeout), cap)
        try:
            res = _exec_engine(eng, eff_timeout=eff_to)
        except Exception as e:
            res = [{"error": str(e), "source": eng}]
        lat = int((time.time() - t_eng) * 1000)
        for r in res:
            if isinstance(r, dict):
                r.setdefault("_engine", eng)
                r.setdefault("_elapsed", lat / 1000.0)

        outcome = _classify_engine_outcome(eng, res, lat)
        goods = [r for r in res if isinstance(r, dict) and "error" not in r]
        _record_quota(eng, success=bool(goods))

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
                since=eng_since, until=eng_until,
            )
        elif not skip_cache and not goods:
            # 空结果短 TTL 写入 per-engine，配合负缓存
            cache.set_engine(
                query, eng, max_results, [],
                domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )

        return eng, (goods if goods else res), outcome, lat

    def _ingest(eng: str, res: list, outcome: dict, lat: int) -> None:
        raw_results[eng] = res
        engine_outcomes.append(outcome)
        engine_latency[eng] = lat
        if outcome["status"] not in ("ok", "ok-cached", "partial"):
            nonlocal_wasted[0] += lat

    nonlocal_wasted = [0]
    early_stopped = False
    to_run = list(engines)
    # deep 模式全量并行；fast/auto/budget 可渐进 early-stop
    allow_early = mode in ("fast", "auto", "budget") and depth != "deep"

    early_min = decision.get("early_stop_min_results")
    if parallel and to_run and allow_early and len(to_run) > 1:
        # Wave-1：主引擎；足够则停（no_early_stop 域除外），否则 wave-2 并行补全
        no_early = bool(decision.get("no_early_stop", False))
        primary, rest = to_run[0], to_run[1:]
        e, res, outcome, lat = _run_one(primary)
        _ingest(e, res, outcome, lat)
        goods_primary = [r for r in res if isinstance(r, dict) and "error" not in r]
        if not no_early and _results_sufficient(
            goods_primary, mode=mode, min_results=early_min,
        ):
            early_stopped = True
        else:
            with ThreadPoolExecutor(max_workers=min(len(rest), 3)) as ex:
                futures = {ex.submit(_run_one, eng): eng for eng in rest}
                try:
                    for fut in as_completed(futures, timeout=_eff_timeout + 2):
                        eng = futures[fut]
                        try:
                            e2, res2, outcome2, lat2 = fut.result()
                            _ingest(e2, res2, outcome2, lat2)
                        except Exception as exc:
                            raw_results[eng] = [{"error": str(exc), "source": eng}]
                            engine_outcomes.append(_classify_engine_outcome(
                                eng, raw_results[eng], 0,
                            ))
                        # 累计结果已足够 → 提前终止剩余 wave-2（不白等慢源拖尾）
                        if not no_early and _cumulative_sufficient(
                                raw_results, mode=mode, min_results=early_min):
                            for fut2 in futures:
                                if not fut2.done():
                                    fut2.cancel()
                            early_stopped = True
                            break
                except TimeoutError:
                    for fut, eng in futures.items():
                        if not fut.done():
                            fut.cancel()
                            raw_results[eng] = [{"error": "timeout", "source": eng}]
                            engine_outcomes.append(_classify_engine_outcome(
                                eng, raw_results[eng], timeout * 1000, "timeout",
                            ))
                            nonlocal_wasted[0] += timeout * 1000
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
    elif parallel and to_run:
        with ThreadPoolExecutor(max_workers=min(len(to_run), 3)) as ex:
            futures = {ex.submit(_run_one, eng): eng for eng in to_run}
            try:
                for fut in as_completed(futures, timeout=_eff_timeout + 2):
                    eng = futures[fut]
                    try:
                        e, res, outcome, lat = fut.result()
                        _ingest(e, res, outcome, lat)
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
                        nonlocal_wasted[0] += timeout * 1000
            for fut in futures:
                if not fut.done():
                    fut.cancel()
    else:
        for eng in to_run:
            e, res, outcome, lat = _run_one(eng)
            _ingest(e, res, outcome, lat)
            goods = [r for r in res if isinstance(r, dict) and "error" not in r]
            if not goods:
                continue  # 无结果：串行试下一引擎
            # 答案型域 min_results=1：1 条快照即 early-stop
            if allow_early and _results_sufficient(
                goods, mode=mode, min_results=early_min,
            ):
                early_stopped = True
                break
            # 默认串行：任一引擎有结果即停（历史行为）；答案型不够用则继续补源
            if early_min is None:
                break

    wasted_ms = nonlocal_wasted[0]
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
        merged = rrf_merge(clean_lists)
    elif clean_lists:
        merged = deduplicate_by_url(clean_lists[0])
        # 单引擎也补 consensus
        for r in merged:
            eng = r.get("_engine") or r.get("source") or ""
            if eng:
                r.setdefault("consensus_engines", [eng])
    else:
        merged = []

    # ── D6：macro_data 域证据下限（事实核查防单源）─────────────────────
    # deep 研究场景下结果 <2 条说明结构化源未覆盖该查询：追加通用兜底引擎
    # 补证据，避免「单引擎单结果」被事实核查 / 融合阶段当作答案；补搜结果
    # 一并进 RRF，consensus 维度天然加权。
    if (domain == "macro_data" and merged and len(merged) < 2
            and depth in ("deep", "research")):
        _done = set(raw_results.keys())
        _cands = [
            e for e in ("anysearch", "duckduckgo", "local_bing")
            if e not in _done
            and e in set(available_engines())
            and (breaker is None or breaker.allow(e)[0])
        ]
        _extra_lists = []
        for _eng in _cands[:2]:
            _e, _res, _out, _lat = _run_one(_eng)
            _ingest(_e, _res, _out, _lat)
            _goods = [r for r in _res if isinstance(r, dict) and "error" not in r]
            if _goods:
                _extra_lists.append(_goods)
        if _extra_lists:
            merged = rrf_merge([merged] + _extra_lists)

    # ── P0：过滤 SERP/跳转 URL（搜索结果页、baidu.com/link 等不可当信源正文）──
    if merged:
        try:
            from evidence import is_serp_or_jump_url as _is_serp
            merged = [r for r in merged if not _is_serp(r.get("url", ""))]
        except ImportError:
            pass  # evidence 不可用时跳过（本地五维 rerank 已对 SERP 降权）

    # ── minhash 近重复去重（结果级，RRF 后 / SERP 后）─────────────────────
    minhash_removed = 0
    if merged and len(merged) > 1:
        try:
            deduped, minhash_removed = minhash_dedupe(merged)
            merged = deduped
        except Exception as _e:
            import logging
            logging.getLogger("unified_search").debug(f"minhash 去重跳过: {type(_e).__name__}")

    # ── P2：多语言语言偏好软排序（ja/ko 前置含目标语言字符结果，软排不删除）──
    try:
        _p_lang = (decision or {}).get("features", {}).get("primary_lang")
        if _p_lang in ("ja", "ko"):
            merged = _lang_prefer_rerank(merged, _p_lang)
    except Exception:
        pass

    # 放宽截断：rerank 阶段看到 max_results*3 条，最终输出再截断
    merged = merged[:max(max_results * 3, 15)]

    # ── P0-001：按 exclude_terms 过滤（否定约束）──
    excluded_count = 0
    if merged and exclude_terms:
        kept = []
        low_terms = [t.lower() for t in exclude_terms if t]
        for r in merged:
            hay = f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}".lower()
            if any(t in hay for t in low_terms):
                excluded_count += 1
                continue
            kept.append(r)
        merged = kept

    # ── 时间窗结果后过滤兜底 ──
    # 仅当组合内含带时间能力引擎时执行：无时间字段引擎的结果没有可滤对象，
    # 跳过遍历省开销；语义上与缓存键隔离保持一致（7d/30d 共享同一缓存内容）。
    time_filtered = 0
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        merged, time_filtered = _apply_time_window(merged, since_ts, until_ts)
        if time_filtered:
            import logging
            logging.getLogger("unified_search").debug(
                f"时间窗后过滤剔除 {time_filtered} 条（since={since_iso}, until={until_iso}）")

    # D5：时间窗空操作告警——用户指定了时间窗，组合内含时间能力引擎，
    # 但结果没有任何 published_at（下推缺失/源端未返回）：
    # `--since/--until` 实际未生效（宽松策略保留无时间字段条目，
    # time_filtered 恒 0）。透传 warning 而非静默降级。
    # 组合内不含时间能力引擎时是已知常态，不重复告警。
    time_filter_warning: str | None = None
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        if not any(r.get("published_at") for r in merged):
            time_filter_warning = (
                f"引擎未返回 published_at，时间窗 {since_iso or '任意'} ~ "
                f"{until_iso or '任意'} 未实际过滤"
            )

    # ── P0-002：空结果错误恢复决策树 ──
    recovery_info: dict[str, Any] | None = None
    # 复杂度门控：低复杂度查询只允许低成本放宽（L1/L2），
    # 禁用高价多源/跨语言（L3/L4）——简单问题不搞多轮，省 token。
    _max_rec_level: str | None = None
    try:
        from query_enhance import complexity_gate
        if qu is not None and complexity_gate(query, qu) == "low":
            _max_rec_level = "L2"
    except Exception:
        pass
    if not merged:
        try:
            from recovery import run_recovery
            tried = list(raw_results.keys()) or list(engines)
            fallback_engines = decision.get("engines_fallback") or []
            try:
                enabled_set = set(available_engines())
            except Exception:
                enabled_set = None

            def _recovery_executor(rq: str, rengines: list[str]) -> list[dict[str, Any]]:
                """恢复执行器：串行跑候选引擎，取首个非空。跳过缓存避免污染。"""
                out: list[dict[str, Any]] = []
                for eng in rengines:
                    try:
                        # 恢复路径同样携带时间窗，避免恢复时丢弃用户约束
                        res = engine_search(rq, eng, n=max_results,
                                            timeout=timeout, depth=depth, mode=mode,
                                            since=since_iso, until=until_iso)
                    except Exception:
                        res = []
                    goods = [r for r in (res or [])
                             if isinstance(r, dict) and "error" not in r]
                    if goods:
                        for r in goods:
                            r.setdefault("_engine", eng)
                            r.setdefault("_recovered", True)
                        out.extend(goods)
                        break
                return out

            rec_results, rec_result = run_recovery(
                query, tried, _recovery_executor,
                engines_fallback=fallback_engines, enabled=enabled_set, mode=mode,
                max_level=_max_rec_level)
            recovery_info = rec_result.to_dict()
            # P2-6：恢复遥测——query 截断脱敏，只记概览不记明细
            if _emit_telemetry is not None:
                try:
                    _emit_telemetry("recovery", {
                        "query": (query[:60] if query else query),
                        "triggered": recovery_info.get("triggered"),
                        "recovered": recovery_info.get("recovered"),
                        "level_used": recovery_info.get("level_used"),
                        "strategy_used": recovery_info.get("strategy_used"),
                        "steps_tried": len(recovery_info.get("steps_tried") or []),
                        "final_query": (recovery_info.get("final_query") or "")[:60],
                        "note": recovery_info.get("note", ""),
                    })
                except Exception:
                    pass
            if rec_results:
                merged = deduplicate_by_url(rec_results)[:max_results]
                for r in merged:
                    eng = r.get("_engine") or r.get("source") or ""
                    if eng:
                        r.setdefault("consensus_engines", [eng])
        except ImportError:
            pass  # recovery 模块不可用
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"错误恢复跳过: {type(e).__name__}")

    # Reranker：ARGO_LOCAL_RERANK 开关（0 关闭本地五维兜底；默认 1 开启）
    local_rerank_on = os.environ.get("ARGO_LOCAL_RERANK", "1").strip() not in ("0", "false", "False", "no")
    reranker_status = "skipped_short"
    rank_method = "none"
    if mode == "fast" or depth == "fast":
        reranker_status = "skipped_fast"
    elif merged and len(merged) > 1:
        # 全量重排（top_n=len），由最终输出统一截断 max_results
        merged, reranker_status = rerank_results(query, merged, top_n=len(merged))
        if reranker_status == "ok":
            rank_method = "bocha"

    # 本地五维 rerank 兜底：受 ARGO_LOCAL_RERANK 开关控制（可观测 rank_method）
    if local_rerank_on and merged and len(merged) > 1 and reranker_status in (
            "skipped_no_key", "fallback", "skipped_fast", "skipped_short"):
        try:
            merged = local_five_dim_rerank(query, merged, domain=domain,
                                           top_n=len(merged))
            rank_method = "local_five_dim"
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"本地五维 rerank 跳过: {type(e).__name__}")
    elif not local_rerank_on and reranker_status in ("skipped_no_key", "fallback", "skipped_fast", "skipped_short"):
        rank_method = "none"

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

    # 内嵌两阶段信号：fast 模式跳过（MCP 默认紧凑也不返回这些字段）
    if merged and mode != "fast" and depth != "fast":
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

    # ── P0-004：关键事实交叉标记（仅 deep/auto 且结果 ≥3；fast 跳过）──
    fact_alignment: dict[str, Any] | None = None
    if merged:
        try:
            from fact_align import align_facts
            fact_alignment = align_facts(merged, min_results=3, mode=mode, depth=depth)
        except ImportError:
            pass  # fact_align 模块不可用
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"事实交叉标记跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    result_payload = {
        "results": merged,
        "engines_used": list(raw_results.keys()),
        "domain": domain,
        "engine_outcomes": engine_outcomes,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
    }

    # 写 combo 缓存：空结果短 TTL / 时效 cap 由 cache.set 处理
    if not skip_cache:
        effective_ttl = None
        if merged and elapsed > 2000:
            # 慢查询略延长；时效域最多 2×，且仍受 resolve_ttl cap
            base_ttl = cache.resolve_ttl(domain, query=query)
            multiplier = min(2 ** (elapsed // 2000), 8)
            if base_ttl <= 900:
                effective_ttl = min(base_ttl * min(multiplier, 2), base_ttl * 2)
            else:
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

    # 语言偏好：记录本轮查询语 + 输出观测快照（默认中英 + 系统 + 习惯）
    lang_pref_info: dict[str, Any] | None = None
    try:
        from lang_pref import record_query_lang, lang_pref_snapshot
        feats = decision.get("features") or {}
        q_lang = feats.get("primary_lang") or ""
        if not q_lang:
            try:
                from lang_detect import detect_language
                q_lang = detect_language(query)
            except ImportError:
                q_lang = ""
        if q_lang:
            record_query_lang(q_lang)
        lang_pref_info = lang_pref_snapshot(query_lang=q_lang)
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"语言偏好记录跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    # 排序在返回前、写缓存后：缓存内容保持 score 序（缓存键/内容不受 sort 影响），
    # sort 只改变本次展示顺序；缓存命中路径在 return 前同样处理，两路径行为一致。
    out_results = _sort_results(merged, sort)

    out: dict[str, Any] = {
        "query": query, "engine": engine_label, "engines": engines,
        "engines_combo": engines_combo, "cached": False,
        "domain": domain, "elapsed_ms": elapsed,
        "tfidf_scores": tfidf_scores, "results": out_results,
        "count": len(out_results), "engines_used": list(raw_results.keys()),
        "errors": _collect_errors(raw_results),
        "engine_outcomes": engine_outcomes,
        "wasted_engine_ms": wasted_ms,
        "early_stopped": early_stopped,
        "reranker": reranker_status,
        "rank_method": rank_method,
        "minhash_removed": minhash_removed,
        "local_rerank_on": local_rerank_on,
        "recovery": recovery_info,
        "fact_alignment": fact_alignment,
        "exclude_terms": exclude_terms,
        "excluded_count": excluded_count,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "mode": mode, "depth": depth,
        "login_hint": decision.get("login_hint"),
    }
    if lang_pref_info is not None:
        out["lang_pref"] = lang_pref_info
    return out


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
                 rewrite: bool = True, cache: Any = None,
                 on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
                 input_kind: str = "auto",
                 plan_only: bool = False,
                 force_search: bool = False,
                 envelope: bool = True,
                 context: str = "search",
                 engines_boost: list[str] | None = None,
                 since: str | None = None,
                 until: str | None = None,
                 sort: str = "relevance",
                 include_local: bool = False) -> dict[str, Any]:
    """统一搜索便捷入口。

    执行分层（不阻塞日常）：
      - daily（默认 auto/fast）：直搜，不挂 plan，不要求用户确认
      - professional（mode=deep 或 depth=deep）：直搜 + 附加 plan 元数据
      - plan_only：仅离线计划（显式开关，不进热路径默认）
      - known-url：工具分流 handoff（不是「请确认后再搜」）

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
        on_progress: 可选进度回调 (stage, data)
        input_kind: auto|keyword|url-seed|known-url
        plan_only: 仅离线计划，不联网
        force_search: 即使判定 known-url 也强制多引擎搜索
        envelope: 附加 candidates/coverage/limitations
        context: search | research
        engines_boost: 垂直引擎前置列表（研究路径 boost，不锁死单引擎）
        since/until: 发布时间时间窗（如 7d / 2026-08-01），下推到支持时间窗的引擎

    注意：路由永远基于原始 query。改写词只用于引擎检索，避免
    「Python → 追加 pip/库」之类改写污染 package_search 等域规则。
    """
    cache = cache if cache is not None else SearchCache()
    original_query = query

    # 查询改写：仅影响检索串，不影响路由（在执行引擎前应用）
    rewrite_result = None
    search_query = original_query

    # ── 离线计划 / URL 分流（离线计划 / 输入分流）──
    # 纪律：build_plan 无网络、不回调本函数 → 无 plan↔search 死循环
    kind = "keyword"
    tier = "daily"
    plan_info: dict[str, Any] | None = None
    try:
        from plan import (
            build_plan, classify_input_kind, execution_tier, should_attach_plan,
        )
        kind = classify_input_kind(query, input_kind)
        tier = execution_tier(mode, depth, context)
        if plan_only:
            return build_plan(
                query, mode=mode, depth=depth, max_results=n,
                engine=engine if not local_first else "local_search",
                input_kind=input_kind,
                context=context,
            )
        if kind == "known-url" and not force_search:
            plan_info = build_plan(
                query, mode=mode, depth=depth, max_results=n,
                engine=engine, input_kind="known-url", context=context,
            )
            # 不发起多引擎搜索；返回 handoff 形态，避免把读链接当热搜
            out = {
                "query": query,
                "engine": None,
                "engines": [],
                "engines_combo": [],
                "cached": False,
                "domain": None,
                "elapsed_ms": 0,
                "results": [],
                "count": 0,
                "errors": [],
                "engine_outcomes": [],
                "wasted_engine_ms": 0,
                "early_stopped": False,
                "mode": mode,
                "depth": depth,
                "status": "handoff_required",
                "input_kind": "known-url",
                "execution_tier": tier,
                "requires_confirmation": False,
                "plan": plan_info,
                "handoff": plan_info.get("handoff"),
                "limitations": plan_info.get("limitations") or [],
                "schema_version": "1.0",
                "candidates": [],
                "coverage": [],
            }
            return out
    except ImportError:
        kind = "keyword"
        tier = "daily"
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"plan 分流跳过: {type(e).__name__}")

    # 查询改写：追加领域关键词提升搜索质量
    # local_first 路径跳过改写：改写词面向 web 引擎召回设计，套到本地
    # 聚合（search_v3 智能路由）上会稀释查询、收窄引擎选择、扩大失败面
    #（实测改写词把「Python 异步编程」扩为 5 词长句后本地聚合返回空）。
    rewrite_result = None
    original_query = query
    if rewrite and not local_first:
        rewritten, rewrite_result = _apply_query_rewrite(original_query)
        if rewrite_result and rewrite_result.get("rewritten"):
            search_query = rewritten

    if local_first:
        decision = route_query(
            original_query, engine_override="local_search", mode=mode,
            depth=depth, context=context, engines_boost=engines_boost,
        )
    else:
        decision = route_query(
            original_query, engine_override=engine, mode=mode,
            depth=depth, context=context, engines_boost=engines_boost,
        )
    if explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={mode} kind={kind} tier={tier}",
            file=sys.stderr,
        )
        if search_query != original_query:
            print(f"[改写] {original_query} → {search_query}", file=sys.stderr)
    result = execute_search(
        query=search_query, decision=decision, max_results=n,
        timeout=timeout, depth=depth, cache=cache,
        skip_cache=skip_cache, mode=mode, on_progress=on_progress,
        since=since, until=until, sort=sort,
    )
    # 对外仍报告用户原始 query
    result["query"] = original_query
    if since:
        result["since"] = since
    if until:
        result["until"] = until
    if sort and sort != "relevance":
        result["sort"] = sort
    if rewrite_result and rewrite_result.get("rewritten"):
        result["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }
    result["input_kind"] = kind
    result["status"] = "completed"
    result["execution_tier"] = tier
    result["requires_confirmation"] = False  # 日常/专业热路径永不阻塞等确认
    if original_query != query:
        result["query_original"] = original_query

    # professional：附加离线 plan 元数据（不阻断、不二次搜索）
    try:
        from plan import build_plan, should_attach_plan
        if should_attach_plan(mode, depth, context, plan_only=False):
            result["plan"] = build_plan(
                original_query, mode=mode, depth=depth, max_results=n,
                engine=engine if not local_first else "local_search",
                input_kind=kind if kind != "auto" else "auto",
                context=context,
            )
    except Exception:
        pass

    # 候选交接包（附加字段，不改 results 排序）
    if envelope:
        try:
            from candidate_envelope import attach_envelope
            extra_lim = []
            if kind == "url-seed":
                extra_lim.append(
                    "url-seed: seed URL was not fetched; results are related discovery only"
                )
            if result.get("recovery"):
                extra_lim.append("recovery used; engine fallback may differ from primary route")
            if tier == "daily":
                extra_lim.append(
                    "daily tier: direct search; no pre-confirm gate"
                )
            elif tier == "professional":
                extra_lim.append(
                    "professional tier: plan metadata attached; verify top-k before hard claims"
                )
            attach_envelope(
                result,
                query=query,
                input_kind=kind,
                route_reason=decision.get("reason"),
                extra_limitations=extra_lim,
            )
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"envelope 跳过: {type(e).__name__}")
            result.setdefault("schema_version", "1.0")
            result.setdefault("limitations", [])

    # 证据闭环 P0：回填已核验证据分 + 高后果门控（finance/health/legal）
    # 输出 fetch_required / evidence_loop 汇总，每条结果带 fetch_suggested
    # 与 has_fetched_evidence / post_fetch_absorption（若此前 fetch 过）。
    try:
        from evidence_loop import gate_results
        gate = gate_results(result.get("results") or [], result.get("domain"))
        result["fetch_required"] = gate["fetch_required"]
        result["evidence_loop"] = {
            "high_consequence_domain": gate["high_consequence_domain"],
            "suggested": gate["suggested"],
            "verified_count": gate["verified_count"],
            "pending_count": gate["pending_count"],
        }
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"证据门控跳过: {type(e).__name__}")

    # 相关信源标准化（日常搜索底部引用列表；与 results 顺序一致）
    result["sources"] = build_sources(result.get("results") or [])

    # 本地命中并入（默认关）：seek 结果尾部拼入，来源 local_files，
    # 不参与融合评分。仅显式开启（--include-local / MCP include_local）才触发。
    if include_local:
        try:
            local_hits = _run_local_seek(query, n)
        except Exception as e:
            local_hits = []
            logging.getLogger("unified_search").debug(
                f"[include-local] {type(e).__name__}: {e}")
        if local_hits:
            result.setdefault("results", []).extend(local_hits)
            result["local_results"] = local_hits
        result["include_local"] = True

    return result


# ── 信源标准化 ─────────────────────────────────────────────────────────────────

def build_sources(results: list[Any] | None) -> list[dict[str, Any]]:
    """将 results 投影为编号信源列表（传统搜索引擎底部「相关链接」形态）。

    规则：
      - ref 与列表序号一致，从 1 起
      - 无 URL 的条目跳过（不占号？——保留占位会错位；跳过并重编号）
      - 字段齐全便于 Agent/归档复用，不伪造 metrics
    """
    sources: list[dict[str, Any]] = []
    ref = 0
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url:
            continue
        ref += 1
        sources.append({
            "ref": ref,
            "title": (r.get("title") or "")[:160],
            "url": url,
            "engine": r.get("source") or r.get("_engine") or r.get("engine"),
            "score": r.get("score"),
            "snippet": ((r.get("snippet") or "")[:160] or None),
        })
    return sources


# ── 输出格式化 ─────────────────────────────────────────────────────────────────

def format_text_output(results: dict[str, Any]) -> str:
    """日常搜索人读格式：条目正文 + 底部「相关信源」链接（类传统 SERP）。"""
    lines = []
    if results.get("status") == "handoff_required":
        ho = results.get("handoff") or {}
        lines.append("=== HANDOFF (known-url, search skipped) ===")
        lines.append(f"  url: {ho.get('url')}")
        lines.append(f"  suggest: {', '.join(ho.get('suggested_tools') or [])}")
        for lim in (results.get("limitations") or [])[:4]:
            lines.append(f"  ! {lim}")
        return "\n".join(lines)
    if results.get("status") in ("ready",) and results.get("steps") and not results.get("results"):
        # plan-only
        lines.append(f"=== PLAN {results.get('status')} kind={results.get('input_kind')} ===")
        route = results.get("route") or {}
        lines.append(f"  engine={route.get('backend')} domain={route.get('domain')} combo={route.get('engines_combo')}")
        for lim in (results.get("limitations") or [])[:5]:
            lines.append(f"  ! {lim}")
        return "\n".join(lines)

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
    if results.get("input_kind"):
        header += f" [kind:{results.get('input_kind')}]"
    lines.append(header)

    for err in results.get("errors", [])[:3]:
        lines.append(f"  [ERROR] {err}")

    # 正文区：编号 + 标题 + 摘要（链接沉底，避免噪声）
    sources = results.get("sources")
    if not isinstance(sources, list) or not sources:
        sources = build_sources(results.get("results") or [])

    # 用 URL 对齐 ref
    url_to_ref = {s.get("url"): s.get("ref") for s in sources if isinstance(s, dict)}
    body_items = [r for r in (results.get("results") or []) if isinstance(r, dict)]
    for r in body_items:
        url = (r.get("url") or "").strip()
        ref = url_to_ref.get(url)
        if ref is None and url:
            # 未进 sources 时临时编号
            ref = "?"
        score = r.get("score", 0)
        title = (r.get("title") or "?")[:80]
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) and score else "—"
        lines.append(f"  [{ref}] {title}")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            lines.append(f"      {snippet[:140]}")
        elif score:
            lines.append(f"      (score={score_s})")

    # 底部相关信源（传统搜索引擎形态）
    if sources:
        lines.append("")
        lines.append("── 相关信源 ──")
        for s in sources:
            if not isinstance(s, dict):
                continue
            ref = s.get("ref", "?")
            eng = s.get("engine") or ""
            title = (s.get("title") or "")[:60]
            url = s.get("url") or ""
            eng_s = f" · {eng}" if eng else ""
            if title:
                lines.append(f"  [{ref}] {title}{eng_s}")
                if url:
                    lines.append(f"      {url}")
            elif url:
                lines.append(f"  [{ref}] {url}{eng_s}")

    if results.get("limitations"):
        lines.append("")
        lines.append("── limitations ──")
        for lim in results["limitations"][:4]:
            lines.append(f"  ! {lim}")

    return "\n".join(lines)


# ── CLI 主入口 ─────────────────────────────────────────────────────────────────

def _run_local_seek(query: str, max_n: int = 5) -> list[dict[str, Any]]:
    """本机文件命中（--include-local 用）：调 local-seek 子技能，JSON 并入。

    仅在显式开启时调用（默认零开销）；结果不参与融合评分，
    仅作尾部来源（source=local_files）。
    """
    import subprocess as _sp

    seek_py = ""
    for cand in (
        os.path.join(SCRIPT_DIR, "..", "sub-skills", "local-seek", "scripts", "seek.py"),
        os.path.expanduser("~/.agents/skills/local-seek/scripts/seek.py"),
        os.path.expanduser("~/.claude/skills/local-seek/scripts/seek.py"),
    ):
        p = os.path.realpath(cand)
        if os.path.exists(p):
            seek_py = p
            break
    if not seek_py:
        return []
    r = _sp.run(
        [sys.executable, seek_py, query, "--json", "--max", str(max(max_n, 1))],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        return []
    hits = payload.get("results") or payload.get("files") or []
    out = []
    for h in hits[:max_n]:
        if not isinstance(h, dict):
            continue
        path = h.get("path") or h.get("file") or ""
        line = h.get("line") or h.get("lineno") or 1
        url = f"file://{path}" + (f"#{line}" if str(line).isdigit() else "")
        out.append({
            "title": path,
            "url": url,
            "snippet": (h.get("snippet") or h.get("text") or h.get("line_text") or "")[:160],
            "source": "local_files",
            "score": 0.0,
            "kind": "local",
        })
    return out


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
    parser.add_argument("--list-engines", action="store_true",
                        help="列出引擎；加 --detail 看 env/准入/routable 状态")
    parser.add_argument("--detail", action="store_true",
                        help="与 --list-engines 联用：输出详细状态")
    parser.add_argument("--routable-only", action="store_true",
                        help="与 --list-engines 联用：仅可自动路由的引擎")
    parser.add_argument("--mode", default="auto",
                        choices=["fast", "auto", "deep", "budget"],
                        help="预算模式: fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制")
    parser.add_argument("--since", default=None,
                        help="发布时间下限（7d / 2026-08-01），下推到支持时间窗的引擎")
    parser.add_argument("--until", default=None,
                        help="发布时间上限（7d / 2026-08-01），下推到支持时间窗的引擎")
    parser.add_argument("--sort", default="relevance",
                        choices=["relevance", "oldest", "newest"],
                        help="时间排序：relevance=相关度（默认）, oldest=最早在前（溯源）, newest=最新在前")
    parser.add_argument("--local-first", action="store_true",
                        help="强制优先使用 local_search 零成本聚合引擎")
    parser.add_argument(
        "--include-local", action="store_true",
        help="并入本机文件命中（seek 结果尾部，source=local_files；默认关）",
    )
    parser.add_argument("--domain", default="", help="AnySearch 垂直域")
    parser.add_argument("--sub_domain", default="", help="AnySearch 子域")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--input-kind", default="auto",
        choices=["auto", "keyword", "url-seed", "known-url"],
        help="输入类型：known-url 默认不热搜；url-seed 只作发现线索",
    )
    parser.add_argument("--plan-only", action="store_true",
                        help="仅输出离线计划（不联网）")
    parser.add_argument("--force-search", action="store_true",
                        help="known-url 也强制多引擎搜索（不推荐）")
    parser.add_argument("--no-envelope", action="store_true",
                        help="不附加 candidates/coverage/limitations")
    parser.add_argument(
        "--archive",
        action="store_true",
        help="将本次搜索 envelope 落盘到工作区归档（不抓正文/不下载）",
    )
    parser.add_argument(
        "--archive-dir",
        type=str,
        default=None,
        help="归档根目录（默认 ARGO_ARCHIVE_ROOT 或 工作区/数据/argo-search-archive）",
    )
    parser.add_argument("--archive-tag", default=None, help="归档标签，便于 list 过滤")
    parser.add_argument("--archive-note", default=None, help="归档备注")
    parser.add_argument(
        "--verify",
        nargs="?",
        const=3,
        type=int,
        default=None,
        metavar="TOP_K",
        help="证据核验：对 top-k 未核验结果 fetch 正文、回填证据分、输出 evidence_revision 分布（默认 3）",
    )

    args = parser.parse_args()

    if args.list_engines:
        if args.detail:
            try:
                from engine_status import list_engines_detail, format_engines_table
                rows = list_engines_detail(routable_only=args.routable_only)
                if args.json_output:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    print(format_engines_table(rows))
            except Exception as e:
                print(json.dumps({"error": str(e), "engines": available_engines()}, ensure_ascii=False, indent=2))
        else:
            try:
                names = available_engines(routable_only=args.routable_only)
            except TypeError:
                names = available_engines()
            print(json.dumps(names, ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("必须提供搜索关键词")

    # 归档需要 envelope；--archive 时强制保留
    use_envelope = (not args.no_envelope) or args.archive
    results = super_search(
        query=args.query,
        engine=args.engine,
        n=args.max_results,
        explain=args.explain,
        skip_cache=args.no_cache,
        timeout=args.timeout,
        depth=args.depth,
        mode=args.mode,
        local_first=args.local_first,
        input_kind=args.input_kind,
        plan_only=args.plan_only,
        force_search=args.force_search,
        envelope=use_envelope,
        since=args.since,
        until=args.until,
        sort=args.sort,
    )
    results["query"] = args.query

    # 本地命中并入（默认关）：seek 结果尾部拼入，来源 local_files，不参与融合评分
    if args.include_local:
        try:
            local_hits = _run_local_seek(args.query, args.max_results)
        except Exception as e:
            local_hits = []
            sys.stderr.write(f"  [include-local] {type(e).__name__}: {e}\n")
        if local_hits:
            results.setdefault("results", []).extend(local_hits)
            results["local_results"] = local_hits
        results["include_local"] = True

    if args.archive and results.get("status") != "handoff_required":
        try:
            from archive_run import write_search_archive, resolve_archive_root
            root = resolve_archive_root(args.archive_dir) if args.archive_dir else None
            if args.archive_dir:
                root = resolve_archive_root(args.archive_dir)
            meta = write_search_archive(
                results,
                root=root,
                tag=args.archive_tag,
                note=args.archive_note,
                source="argo_search",
            )
            if not args.json_output:
                print(
                    f"  [archive] {meta.get('run_id')} → {meta.get('run_dir')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"  [archive error] {type(e).__name__}: {e}", file=sys.stderr)

    # 证据闭环 P0：--verify 显式核验 top-k 未核验结果（fetch + 回填 + revision 分布）
    if args.verify:
        try:
            from evidence_loop import verify_results
            v = verify_results(results.get("results") or [], args.query, top_k=args.verify)
            results["verify"] = v
            results["fetch_required"] = bool(results.get("fetch_required"))
            # verify 已回填/核验结果 → 刷新门控汇总，避免 suggested 含已核验 URL
            try:
                from evidence_loop import gate_results
                gate = gate_results(results.get("results") or [], results.get("domain"))
                results["evidence_loop"] = {
                    "high_consequence_domain": gate["high_consequence_domain"],
                    "suggested": gate["suggested"],
                    "verified_count": gate["verified_count"],
                    "pending_count": gate["pending_count"],
                }
            except Exception:
                pass
            if not args.json_output:
                rs = v.get("revision_summary") or {}
                print(
                    f"  [verify] 核验 {rs.get('n', 0)} 条，"
                    f"improved={rs.get('improved', 0)} unchanged={rs.get('unchanged', 0)} "
                    f"degraded={rs.get('degraded', 0)} mean_delta={rs.get('mean_delta', 0)}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"  [verify error] {type(e).__name__}: {e}", file=sys.stderr)

    if args.json_output:
        public = {k: v for k, v in results.items() if not k.startswith("_")}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(format_text_output(results))
        if results.get("archive"):
            ar = results["archive"]
            print(f"  archived → {ar.get('run_dir')}")


if __name__ == "__main__":
    main()
