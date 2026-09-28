#!/usr/bin/env python3
"""search_rank.py — 排序、去重与融合后处理（纯函数层）。

承载「拿到各引擎结果之后、交付之前」的全部加工：URL 规范化与去重、RRF 加权
融合、minhash 近重复合并、语言偏好软排序、Bocha 精排与本地五维保底精排、
共识/事实对齐信号、域过滤。

为什么单独成模块：这一层是本仓**唯一**能靠离线金标验证的部分
（tests/golden/pipeline_golden.json + replay_eval.py），把它与网络调度、
CLI、输出格式分开后，「排序改动」可以只跑秒级的排序金标，不必拖上整条链路。
函数一律吃列表、吐列表/标量，不碰全局状态（缓存除外，见下）。

2026-09-27 拆分：单条结果的**打分算子**（相关性/完整性/各项惩罚）搬去
rank_signals.py，本文件保留融合与调度。

缓存说明：`_rel_factor_cache` / `_weight_cache` 是进程内 TTL 记忆化，
`invalidate_engine_weight_cache()` 是它的显式失效口（配置变更后调用）。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from engine_env import env_flag, get_env
from url_canon import canonical_url as _canonical_url_impl

# cache 延迟到首次真正需要相似度时再导入（2026-09-27，方案 B3）：顶层 import
# 会把 cache.py 连带 sqlite3/shutil/tempfile 整条拉进每次 import search 的
# 路径（实测 16-25ms，而 import 本身只要 30ms）。调用点 _content_similarity
# 自带 Jaccard 兜底，故延迟导入不改变行为。
# 名字**不能**叫 `_query_similarity`：那会把包装对象遮蔽成自身，递归到
# RecursionError 后被 except 吞掉、静默退化成 Jaccard——去重阈值 0.85 之下
# Jaccard 0.88 会把 20 条不同结果并成 1 条（minhash 只有 0.375）。
_QUERY_SIMILARITY = None


def _resolve_query_similarity():
    """取 cache.query_similarity（首次调用导入，之后走模块级记忆）。"""
    global _QUERY_SIMILARITY
    if _QUERY_SIMILARITY is None:
        try:
            from cache import query_similarity as _fn
        except ImportError:
            return None
        _QUERY_SIMILARITY = _fn
    return _QUERY_SIMILARITY

# 打分算子已拆分到 rank_signals（2026-09-27，本文件触及 1000 行硬上限时拆出）。
# 分工：rank_signals 只回答「这条结果多匹配、这份内容多可信」，本文件负责
# 融合/去重/语言偏好/截断等调度。开关与常量仍走 ARGO_*，行为不变。
from rank_signals import (  # noqa: E402,F401
    CJK_STOPCHARS as _CJK_STOPCHARS,
    combined_source_penalties as _combined_source_penalties,
    completeness_v2_enabled as _completeness_v2_enabled,
    # 兼容转出：搜索路径走 combined_source_penalties，不再直接调这三个，
    # 但 tests 直接 from search_rank import 它们，删掉会 ImportError。
    cross_domain_homogeneity_enabled as _cross_domain_homogeneity_enabled,
    cross_domain_homogeneity_penalty as _cross_domain_homogeneity_penalty,
    domain_concentration_penalty as _domain_concentration_penalty,
    domain_penalty_enabled as _domain_penalty_enabled,
    domain_score_floors as _domain_score_floors,
    host_of as _host_of,
    relevance_units as _relevance_units,
    relevance_v2_enabled as _relevance_v2_enabled,
    score_completeness as _score_completeness,
    score_relevance as _score_relevance,
    title_stuffing_penalty as _title_stuffing_penalty,
)


# 中文/英文混合分词用的正则，延迟编译（首次 _tokens 时建）
_CJK_OR_WORD = None

# 域相关性地板表（按域/源），延迟加载一次


def _canonical_url(url: str) -> str:
    """URL 归一化（薄转发到 url_canon 唯一来源）。"""
    return _canonical_url_impl(url)


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


_rel_factor_cache: dict[str, tuple[float, float]] = {}


_REL_FACTOR_TTL = 30.0
_MAX_CACHE_SIZE = 256


def _evict_cache(cache: dict, now: float) -> None:
    """缓存淘汰：先清除过期条目，如果仍超上限则清除最老的条目。"""
    if len(cache) < _MAX_CACHE_SIZE:
        return
    # 第一轮：清除过期条目
    expired = [k for k, v in cache.items() if v[1] <= now]
    for k in expired:
        del cache[k]
    # 第二轮：如果仍超上限，清除最老的条目（按过期时间排序）
    if len(cache) >= _MAX_CACHE_SIZE:
        sorted_keys = sorted(cache.keys(), key=lambda k: cache[k][1])
        for k in sorted_keys[:len(cache) - _MAX_CACHE_SIZE + 1]:
            del cache[k]


_weight_cache: dict[tuple[str, str], tuple[float, float]] = {}


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
    _evict_cache(_rel_factor_cache, now)
    _rel_factor_cache[engine] = (factor, now + _REL_FACTOR_TTL)
    return factor


def _engine_weight(source: str, lang: str | None = None) -> float:
    """按引擎来源返回融合权重（source 可能含 'local_bing/sina_quote' 合并形式）。

    静态基础权重（权威/学术提权、社交降权）× 动态可靠性因子（weakest-link）：
    熔断/高错误源降权，健康权威源维持提权。论文 2508.01405 的路径质量评估落地。

    lang（可选）启用**语言能力加权**：由 18语言×29引擎 矩阵实测得到的
    能力画像（data/lang_matrix/lang_capability.json）决定——该语言下实测
    良好的引擎提权、实测噪声的降权、无数据的保持中性。画像缺失/过期时
    完全退化为原行为（见 lang_capability 的安全降级契约）。

    结果缓存（_weight_cache，TTL = _REL_FACTOR_TTL）：
    `rrf_merge` 对**每条结果**调用一次本函数，而同一次融合里 (source, lang)
    的取值空间只有「参与引擎数 × 1」，300 条结果实测 0.54ms 全花在重复的
    `split`/`max`/`min` 上。TTL 对齐底层可靠性因子的 30s 窗口，因此本缓存
    **不会把熔断状态变化多冻结哪怕一秒**（旧实现靠 _rel_factor_cache 记忆，
    同一个 TTL）。语言画像更新走 invalidate_engine_weight_cache()。
    """
    if not source:
        return 1.0
    ck = (str(source), lang or "")
    now = time.time()
    ent = _weight_cache.get(ck)
    if ent is not None and ent[1] > now:
        return ent[0]
    # 合并来源：静态权重取最高源，可靠性取最低源（weakest-link：任一路径弱即降权）
    parts = [p.strip() for p in str(source).split("/") if p.strip()]
    if not parts:
        return 1.0
    static = max([_ENGINE_FUSION_WEIGHTS.get(p, 1.0) for p in parts])
    rel = min([_single_reliability(p) for p in parts])
    out = static * rel
    adjusted = True
    if lang:
        try:
            from lang_capability import score_adjust
            # 多来源取最高：某个来源在该语言下有能力即可（不因合并源里
            # 混入一个未知引擎而失去提权）
            adj = max([score_adjust(p, lang) for p in parts] or [1.0])
            out *= adj
        except Exception:
            # 调整失败：本次按未调整值使用，但**不写缓存**——缓存键含 lang，
            # 固化 30s 会让该语言持续拿到降级权重（与「退化结果不写缓存」同源）
            adjusted = False
    out = round(out, 3)
    if adjusted:
        _evict_cache(_weight_cache, now)
        _weight_cache[ck] = (out, now + _REL_FACTOR_TTL)
    return out


def invalidate_engine_weight_cache() -> None:
    """清空 _engine_weight 结果缓存。

    与 lang_capability.reload() 配对使用：语言画像更新后必须调用本函数，
    否则本缓存会在 TTL 窗口内继续返回旧画像算出的权重。熔断状态无需调用
    ——两者 TTL 相同（_REL_FACTOR_TTL），不会互相冻结。
    """
    _weight_cache.clear()


def _rrf_weighted_default() -> bool:
    """RRF 是否默认按引擎加权（WG-RRF）。

    逃生开关 `ARGO_RRF_WEIGHTED=0`（或 off/no/false）退回**经典 RRF**：
    Claude Shannon 原文那版，各引擎同位次等权。

    为什么需要它：加权版把「权威源提权、社交源降权」的领域先验编进了融合层，
    这在多数查询上是净收益，但它**改变了跨引擎的相对次序**——实测同一组
    三引擎结果，加权版把「权威源第 1 条」排在首位，经典版则把「被两引擎
    共同命中的共识条目」提到第 2。两者是**可辩驳的排序哲学差异**，不是
    对错之分。留一个开关的意义在于：出现「本次结果不对劲」时能把融合层
    单独摘出去定位（是融合的锅还是引擎的锅），以及为回归对比提供基线。

    读环境变量而非写死常量：与仓库既有 ARGO_* 开关同一约定（如
    ARGO_MINHASH_DEDUPE / ARGO_FETCH_JINA），且 CLI 与 MCP 两种宿主都能
    在不改代码的前提下切换。
    """
    try:
        from engine_env import get_env
        v = get_env("ARGO_RRF_WEIGHTED")
    except Exception:
        v = os.environ.get("ARGO_RRF_WEIGHTED")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() not in ("0", "off", "no", "false", "disable", "disabled")


def rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = 60,
              weighted: bool | None = None,
              lang: str | None = None) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 合并多引擎结果，保留 consensus_engines。

    键用归一化 URL（http/https、www、utm 变体合并）；RRF 分单独存 _rrf_score，
    首次遇到的结果保留完整字段，后续同 URL 只累加共识、择优补充 snippet，
    避免「score 字段赢家通吃」覆盖共识内容。

    weighted（WG-RRF）：按引擎来源加权（权威源提权、社交源降权）。
    **默认值改为 None 表示「按 _rrf_weighted_default() 决定」**（即默认仍为
    加权，与旧行为逐位一致），传 True/False 可显式覆盖——此前签名写死
    `weighted: bool = True`，调用方想走经典 RRF 只能显式传 False，而
    ARGO_RRF_WEIGHTED 这类环境开关无处生效。测试与消融脚本传显式值时
    行为完全不变。
    """
    if weighted is None:
        weighted = _rrf_weighted_default()
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for _li, results in enumerate(ranked_lists):
        for i, r in enumerate(results):
            # 无 URL 时用 title 保底；模态卡再退到 card_type（避免空 title 互撞）。
            # 最后保底必须带**列表身份**：此前用裸 `i`（单列表内的局部索引），
            # 跨引擎必然同值 —— 两条都没有 url/title/card_type 的不同结果会在
            # `__idx__:0` 处相撞，表现为 ①丢结果 ②伪造 consensus_engines
            # （两个引擎"都投了"同一条，其实各是各的）③字段错配（_engine 留 A、
            # snippet 被 B 覆盖）。同文件 deduplicate_by_url 用全局递增计数器
            # `anon:{len(out)}` 就没有这个问题，此处保持一致该写法。
            key = (
                _canonical_url(r.get("url", ""))
                or (f"__title__:{r.get('title', '')}" if r.get("title") else "")
                or (f"__card__:{r.get('card_type', '')}" if r.get("card_type") else "")
                or f"__idx__:{_li}:{i}"
            )
            w = _engine_weight(r.get("_engine") or r.get("source") or "",
                               lang=lang) if weighted else 1.0
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
    _sim_fn = _resolve_query_similarity()   # cache 不可用时返回 None
    if _sim_fn is not None:
        try:
            return float(_sim_fn(a, b))
        except Exception:
            pass  # 单条相似度计算失败按「无相似度」处理，回退 Jaccard
    import re as _re
    sa, sb = set(_re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", a.lower())), set(_re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def _content_sig(r: dict[str, Any]) -> str:
    return f"{r.get('title', '') or ''} {r.get('snippet', '') or ''}".strip()


def _distinct_data_rows(ka: str, kb: str) -> bool:
    """两条结果的规范 URL 是否「同文档、不同查询」——即同一资源的不同数据行。

    时序/截面类数据引擎（fred/worldbank/eurostat/comtrade…）按观测期逐行发
    条目，URL 以查询参数承载内容身份（?obs=…&PartnerAreas=…），文本彼此仅
    差日期与数值，minhash 相似度恒过阈值。查询参数不同即内容不同，不做
    近重复折叠；跨站同质网页（不同 host/path）不受影响，仍按原文折叠。
    """
    from urllib.parse import urlparse
    pa, pb = urlparse(ka), urlparse(kb)
    if (pa.netloc, pa.path) != (pb.netloc, pb.path):
        return False
    return pa.query != pb.query


_RERANK_POOL_FACTOR = 3


_RERANK_POOL_MIN = 15


def _rerank_pool_limit(max_results: int) -> int:
    """精排池容量（去重提前停与放宽截断的唯一来源）。"""
    return max(max_results * _RERANK_POOL_FACTOR, _RERANK_POOL_MIN)


def minhash_dedupe(
    results: list[dict[str, Any]], threshold: float = 0.85,
    enabled: bool | None = None, max_keep: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """结果级近重复去重（MMR 前置）：同一事件多引擎同质网页堆叠时去重。

    流程：URL 归一键已去重 → 剩余按(score, selection)降序贪心，content_similarity ≥ threshold 视为近重复，仅留首条。
    开关：ARGO_MINHASH_DEDUPE=0 时关闭；默认开启（阈值可由 ARGO_MINHASH_THRESHOLD 覆盖，默认 0.85）。
    返回 (deduped, removed_count)，每条被移除的结果记 `_near_dup=True`。

    max_keep：只保证「前 max_keep 条非重复结果」与不设上限时逐位一致，到达
    上限即停。调用侧拿到结果后紧接着就截断到同一个上限，所以这不改变任何
    输出，却把 O(n²) 的两两比较降成 O(n · max_keep)——实测 800 条结果时快
    677 倍（输出前 max_keep 条完全相同）。`removed` 相应变为下界：只统计到
    提前停为止，被截掉的尾部本来也不参与输出。
    """
    if enabled is None:
        enabled = env_flag("ARGO_MINHASH_DEDUPE")
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
    # 已保留项的 (内容签名, 归一 URL)。此前是两条并行数组再 zip——两者必须
    # 同步推进才有意义，拆散了就是一个静默的错位陷阱。
    kept_keys: list[tuple[str, str]] = []
    removed = 0
    for r in pool:
        if max_keep is not None and len(kept) >= max_keep:
            break
        sig = _content_sig(r)
        rkey = _canonical_url(r.get("url", ""))
        is_dup = any(
            _content_similarity(sig, ks) >= threshold
            and not _distinct_data_rows(rkey, ku)
            for ks, ku in kept_keys
        )
        if is_dup:
            removed += 1
            r["_near_dup"] = True
        else:
            kept.append(r)
            kept_keys.append((sig, rkey))
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


# 「结果是否以目标语言书写」判定表：lang -> (命中, 排除)。
# 命中 = 目标语言的特征码位；排除 = 与之共享码位、必须让位的语言特征。
#
# 这张表取代了原先三处并存的硬编码闸门（rerank 入口 `not in ("ja","ko")`、
# search_pipeline 调用点 `in ("ja","ko")`、噪声门 `not in ("zh","en",...)`）。
# 原 bug：`argo search "记忆宫殿"` 的 5 条结果里有 3 条是日文/捷克文/英文，
# 而 zh 被三道闸门同时排除在语言处理之外——中文路径零语言感知。
#
# 为什么是数据驱动而不是再加一个 if：任何新增的受追踪语言只需要往这张表
# 加一行，而不是去三处各改一次字面量；漏改的后果是「静默失效」，肉眼只能
# 从结果语种不对上看出来。tests/test_lang_prefer_scope.py 的
# TestNoHardcodedLangGateRemains 把这条纪律钉成断言。
#
# 为什么需要「排除」侧：中日共享汉字表。日文标题「記憶の宮殿」里的「記憶」
# 落在 zh 的 CJK 区间内，只判命中就会把日文当成中文顶到第一位——比原 bug
# 更糟（用户看到的第一条是日文，而语种过滤「本来是管这个的」）。故 zh 的
# 判定额外要求「不含假名」：现代日文文本几乎总带假名，而纯中文文本不会。
_LANG_SCRIPT: dict[str, tuple[str, str | None]] = {
    "ja": (r"[\u3040-\u30ff]", None),                      # 平假名 / 片假名
    "ko": (r"[\uac00-\ud7af]", None),                      # 谚文音节
    "zh": (r"[\u4e00-\u9fff]", r"[\u3040-\u30ff\uac00-\ud7af]"),  # CJK，且非日/韩
    "ru": (r"[\u0400-\u04ff]", None),                      # 西里尔
    "el": (r"[\u0370-\u03ff]", None),                      # 希腊
    "ar": (r"[\u0600-\u06ff]", None),                      # 阿拉伯
    "he": (r"[\u0590-\u05ff]", None),                      # 希伯来
    "th": (r"[\u0e00-\u0e7f]", None),                      # 泰
    "hi": (r"[\u0900-\u097f]", None),                      # 天城文
}


def _lang_prefer_rerank(results: list[dict[str, Any]],
                        primary_lang: str | None) -> list[dict[str, Any]]:
    """按目标语言的书写系统把属于该语言的结果前置（软排序，不删除）。

    `en` 刻意不在表里：拉丁字母是 web 的通用语种，「含拉丁字母」对几乎
    所有结果都命中，该信号无区分度。对 en 而言 RRF 融合的相关度排序已经
    够用，故保持恒等（返回原对象，不引入无谓重排）。
    """
    if not results:
        return results
    spec = _LANG_SCRIPT.get(primary_lang or "")
    if not spec:
        return results
    hit_src, excl_src = spec
    pat = re.compile(hit_src)
    excl = re.compile(excl_src) if excl_src else None

    def _key(r: dict[str, Any]) -> int:
        hay = f"{r.get('title', '')} {r.get('snippet', '')}"
        if not pat.search(hay):
            return 1
        if excl is not None and excl.search(hay):
            return 1
        return 0

    keys = [_key(r) for r in results]
    # 没有任何结果属于目标语言时原样返回：既省掉一次无意义的 list 拷贝，
    # 也让「软排序不改变输入」这条性质对调用方恒真（否则 identity 断言会
    # 在「无命中」这个最常见的场景下假性失败）。
    if not any(k == 0 for k in keys):
        return results
    # stable sort：属于目标语言的结果在前，其余保持原 RRF 顺序
    return sorted(results, key=_key)


_RERANK_BREAKER_KEY = "rerank:bocha"


def _rerank_breaker():
    """精排端点的熔断器；不可用时返回 None（精排降级，不阻断搜索）。"""
    try:
        from circuit_breaker import get_breaker
        return get_breaker()
    except Exception:
        return None


def _note_rerank_failure(breaker, category: str, detail: str) -> None:
    """把精排端点的失败写进熔断器（含归因）。"""
    if breaker is None:
        return
    try:
        breaker.record_failure(
            _RERANK_BREAKER_KEY, kind="error",
            attribution={"category": category,
                         "reason": "语义精排端点不可用", "detail": detail},
        )
    except Exception:
        pass


def rerank_results(query: str, results: list[dict[str, Any]],
                   top_n: int = 10, timeout: float = 5
                   ) -> tuple[list[dict[str, Any]], str]:
    """使用博查语义排序模型对搜索结果二次精排。

    返回 (results, status)：status ∈ ok | skipped_no_key | skipped_short |
    skipped_fast | skipped_circuit_open | fallback
    """
    if not results or len(results) <= 1:
        return results, "skipped_short"

    api_key = get_env(["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"])
    if not api_key:
        return results, "skipped_no_key"

    # 端点熔断：先问「还该不该打这一枪」。
    #
    # 为什么必须记住失败：这是一次**同步阻塞**的网络调用，压在 CPU 后处理链上。
    # 实测账户余额不足时（403 `{"code":"403","message":"You do not have enough
    # money"}`）每次搜索都真发一次请求、真等一次 RTT——223 ms，占 balanced
    # 档墙钟的 63%、占全部后处理耗时的 89%——而旧实现把 HTTPError 一律吞成
    # "fallback"，既不计数也不冷却，于是每次搜索都重犯同一笔开销。
    # 参数类失败（凭证/额度）不会因为再试一次自愈，只有周期性探测才有意义，
    # 这正是熔断器「冷却 + 半开探测」的语义；状态写入文件，所以后续 CLI 单发进程
    # 也直接跳过（进程内记忆对一次性 CLI 没有意义）。
    breaker = _rerank_breaker()
    if breaker is not None:
        try:
            allowed, _reason = breaker.allow(_RERANK_BREAKER_KEY)
        except Exception:
            allowed = True
        if not allowed:
            return results, "skipped_circuit_open"

    documents = []
    for r in results:
        doc_text = f"{r.get('title', '')} {r.get('snippet', '')}".strip()
        documents.append(doc_text or "empty")

    import urllib.request
    from net_proxy import open_url  # 出口调度唯一入口（issue #13 同类修复）
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
        with open_url(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # 拿到可解析响应即视为端点健康（含「有响应但无排序结果」），
            # 闭合熔断状态——否则一次历史失败会让人工恢复后仍被拦。
            if breaker is not None:
                try:
                    breaker.record_success(_RERANK_BREAKER_KEY)
                except Exception:
                    pass
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
    except urllib.error.HTTPError as e:
        # 401/403（凭证失效 / 额度耗尽）与 429（限流）都不是瞬时抖动：
        # 立刻重试不会自愈，只会把同一笔 RTT 再付一次。分类只影响归因展示，
        # 熔断策略一视同仁（都是 kind="error"）。
        cat = "rate_limited" if e.code == 429 else "auth"
        _note_rerank_failure(breaker, cat, f"HTTP {e.code}")
        return results, "fallback"
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _note_rerank_failure(breaker, "network", f"{type(e).__name__}: {e}")
        return results, "fallback"
    return results, "fallback"


def _tokens(text: str) -> list[str]:
    """轻量分词：中文单字 + 英文单词，统一小写（复用 tfidf 风格）。

    保留单字切分供 `_bigrams`（新颖性去冗余）使用——那里比的是「两段文本
    的用字是否雷同」，单字粒度足够且更宽容。**相关性**不能用它，见
    `_relevance_units` 的模块注释。
    """
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




def _consensus_prior(results: list[dict[str, Any]]) -> list[float]:
    """融合先验：把 RRF 分与跨引擎共识数归一化成 [0,1]（逐条保持一致 results）。

    为什么需要它（这是本函数存在的唯一理由）：
    `local_five_dim_rerank` 用 relevance(token 覆盖率)/completeness(文本长度)
    等**文本自身**的维度重新打分，这在结构上偏爱「啰嗦的长网页」而压制
    「多个引擎都认同但摘要简短」的结果。实测：一条 3 引擎共识条目
    (RRF 0.0418) 会被单源长文本条目 (RRF 0.0164) 反超——融合层的核心产出
    在最终排序中丢失。

    这里**不改五维公式**，只把融合信号作为一个独立先验维度加进来，避免与
    `score` 字段竞争（`score` 已被本函数覆写，拿它当输入是循环依赖）。

    归一化计算方式：RRF 分取最大值归一到 1（保序，不放大）；共识数按
    `min(n-1, 3)/3` 计（封顶 3，避免 5 源共识把量纲压过其他维度）。
    共识在排序路径**只有这一个入口**：旧版此处之外还有一次乘法共识
    boost（×(1+0.05·min(n-1,3))，2026-09-13 移除）——同一信号被重复
    计分，3 引擎共识合计被放大约 19%。evidence selection 阶段的
    `selection` 乘法是「先核验哪条」的独立信号，不影响本排序。
    """
    priors: list[float] = []
    raw = [float(r.get("_rrf_score", 0.0) or 0.0) for r in results]
    peak = max(raw) if raw else 0.0
    for r, rrf in zip(results, raw):
        rrf_norm = (rrf / peak) if peak > 0 else 0.0
        cons = len(r.get("consensus_engines") or [])
        cons_norm = min(max(cons - 1, 0), 3) / 3.0
        # 两个子信号取均值：单纯多引擎重复 != 更可信，RRF 分还含引擎权重
        priors.append(0.5 * rrf_norm + 0.5 * cons_norm)
    return priors





def local_five_dim_rerank(query: str, results: list[dict[str, Any]],
                          domain: str = "general", top_n: int = 10
                          ) -> list[dict[str, Any]]:
    """本地五维精排（无 Bocha Key / fallback 时保底）。

    维度权重（通用，前五维和为 0.88，余下 0.12 给融合先验）：
      相关性 0.26 + 权威性 0.26 + 时效性 0.18 + 完整性 0.13 + 新颖性 0.05
      + **融合先验 0.12**（RRF 分 + 跨引擎共识，见 `_consensus_prior`）
    tech/code 域：权威 0.18、相关 0.35（技术查询更看内容匹配）。

    无融合信息时（单引擎路径）先验恒为 0，与旧行为等价：此时五维按 0.88
    整体折算，是原权重比例的等比缩放，排序结果不变。

    新颖性：标题 bigram 与「已排更高结果」的 Jaccard 互补（1 − overlap），
    奖励信息增量，抑制近重复堆叠。

    每个结果写入 rerank_dims 明细（含 prior），供可观测。
    """
    if not results:
        return results

    def _src_has(source: str, name: str) -> bool:
        # rrf_merge 会把同 URL 结果的 source 合并成 "local_bing/sina_quote"，
        # 精确匹配会漏掉合并后的结果，这里按「/」切分做成员判断。
        return name in str(source).split("/")

    # 权重表（MECE）。前五维整体缩放 (1 - W_PRIOR)，把余量留给融合先验。
    # 关键性质：prior 缺席时（单引擎路径）五维被同一常数缩放，是原权重比例的
    # 等比变换 —— 排序结果与改造前逐位一致。该等价性由测试锁定。
    W_PRIOR = 0.12
    _BASE = 1.0 - W_PRIOR
    is_tech = domain in ("tech_deep", "code_search", "local_code", "academic")
    if is_tech:
        # 原 tech 权重：rel .40 / auth .20 / fresh .20 / comp .15 / nov .05
        w = {"relevance": 0.40 * _BASE, "authority": 0.20 * _BASE,
             "freshness": 0.20 * _BASE, "completeness": 0.15 * _BASE,
             "novelty": 0.05 * _BASE}
    else:
        # 原通用权重：rel .30 / auth .30 / fresh .20 / comp .15 / nov .05
        w = {"relevance": 0.30 * _BASE, "authority": 0.30 * _BASE,
             "freshness": 0.20 * _BASE, "completeness": 0.15 * _BASE,
             "novelty": 0.05 * _BASE}
    _priors = _consensus_prior(results)

    # 复用 evidence 的权威/时效评分（若可用）
    try:
        from evidence import score_authority, score_freshness
        _has_evidence = True
    except ImportError:
        _has_evidence = False

    # 相关性比对单元（2026-09-27 换算子）：CJK 二元组 + 拉丁词，不再是中文
    # 单字。ARGO_RELEVANCE_V2=0 时 _score_relevance 退回旧口径，而那时它需要
    # 单字集合——故按开关形态分别构造，保证逃生门「逐位回到旧行为」成立。
    query_tokens = (set(_tokens(query)) if not _relevance_v2_enabled()
                    else set(_relevance_units(query)))
    floors = _domain_score_floors().get(domain, {})

    # 来源侧惩罚（域级集中 + 跨域同质），均为 _static4 的乘子（≤1），在
    # 贪心之前应用——故 K 剪枝上界 U_i = 静态分 + w_novelty + W_PRIOR·prior
    # 仍保守成立（系数 ≤1 只会让上界更小，不会失效）。
    domain_penalty, _xh = _combined_source_penalties(results)

    # 先计算前四维静态分
    enriched = []
    for _i, r in enumerate(results):
        title = r.get("title", "") or ""
        snippet = r.get("snippet", "") or ""
        url = r.get("url", "") or ""
        source = r.get("source", "") or ""
        relevance = _score_relevance(query_tokens, title, snippet)
        # 域级源保底分（答案型源：书目/行情/汇率/官方公告），声明在
        # config.yaml 各域 score_floors——排序代码对源类型无知
        for _src, fl in floors.items():
            if "relevance" in fl and _src_has(source, _src):
                relevance = max(relevance, fl["relevance"])
        if _has_evidence:
            try:
                authority = float(score_authority(url, source).get("score", 0.5))
            except Exception:
                authority = 0.5
            try:
                freshness = float(score_freshness(r).get("score", 0.5))
            except Exception:
                freshness = 0.5
            # 权威/时效保底（行情快照/官方公告源在 evidence 域名表里
            # 偏低，实为高可信答案源），声明同上
            for _src, fl in floors.items():
                if not _src_has(source, _src):
                    continue
                if "authority" in fl:
                    authority = max(authority, fl["authority"])
                if "freshness" in fl:
                    freshness = max(freshness, fl["freshness"])
        else:
            authority, freshness = 0.5, 0.5
        completeness = _score_completeness(title, snippet)
        enriched.append({
            "r": r, "title": title,
            "relevance": relevance, "authority": authority,
            "freshness": freshness, "completeness": completeness,
            # bigrams 一次性预算：贪心选序会反复查阅同一标题，把分词+哈希
            # 摊到外层避免 O(n²) 重复计算（n 为待排结果数）。
            "bg": _bigrams(_tokens(title)),
            "prior": _priors[_i],
        })

    # K 相关剪枝（可证明无损，2026-09-19）：
    # 边际分 = 静态四维 + w_novelty·novelty + W_PRIOR·prior。novelty∈[0,1] 且
    # prior 已知，所以任一条目在任一轮的边际分都 ≤ U_i = 静态分 + w_novelty
    # + W_PRIOR·prior_i。记 L 为静态分的第 K 大值（K = top_n）：前 K 轮里最多
    # 选走 K−1 条，池中必然还剩至少一条静态分 ≥ L 的条目，它的边际分 ≥ L。
    # 因此 U_i < L 的条目在前 K 轮里不可能被选中——剪掉它，前 K 个选序与逐条
    # 真算逐位一致（平局规则「首个索引胜出」也不受影响，被剪条目本来就赢不了）。
    #
    # 为什么不用「上一轮分数作上界」的增量剪枝：selected_bigrams 是并集，加入
    # bigram 不重叠的条目会让 jaccard 下降、novelty 回升，边际分不是单调不增的，
    # 那条界不成立（2026-09-19 对拍出 8 处差异后废弃，勿再尝试）。
    #
    # 静态分按原加法顺序（左结合到 completeness）预算，再逐项加 novelty 与
    # prior——浮点加法顺序与改造前一致，金标对拍才可能零差异。
    pool = enriched[:]
    for e in pool:
        e["_static4"] = (w["relevance"] * e["relevance"]
                         + w["authority"] * e["authority"]
                         + w["freshness"] * e["freshness"]
                         + w["completeness"] * e["completeness"])
        # 来源侧惩罚（域级集中 / 跨域同质）作为静态分乘子（≤1）在**贪心之前**
        # 应用：① 无需事后重排，且惩罚表为空时本行是 ×1.0、浮点上逐位无操作，
        # 「无惩罚时与旧实现逐位一致」的不变式不被破坏；② K 剪枝上界
        # penalty×_static4 + w_novelty + W_PRIOR×prior ≤ 旧上界，剪枝仍安全。
        _pf = domain_penalty.get(_host_of(e["r"].get("url") or ""))
        if _pf is not None and _pf < 1.0:
            e["_static4"] *= _pf
            e["_domain_factor"] = _pf
        _xf = _xh.get(e["r"].get("url") or "")
        if _xf is not None and _xf < 1.0:
            e["_static4"] *= _xf
            e["_cross_domain_factor"] = _xf
        e["_ub"] = e["_static4"] + w["novelty"] + W_PRIOR * e["prior"]
    if 0 < top_n < len(pool):
        l_k = sorted((e["_static4"] for e in pool), reverse=True)[top_n - 1]
        kept = [e for e in pool if e["_ub"] >= l_k]
        if len(kept) < len(pool):
            pool = kept

    # 贪心排序：每步选边际得分最高者，novelty 相对已选集合动态计算。
    # 域级惩罚在选序后按最终分乘算（见下），保证 K 剪枝上界不受影响。
    ranked: list[dict[str, Any]] = []
    selected_bigrams: set[str] = set()
    while pool:
        best_idx, best_score, best_novelty = 0, -1.0, 1.0
        for i, e in enumerate(pool):
            novelty = 1.0 - _jaccard(e["bg"], selected_bigrams)
            score = e["_static4"] + w["novelty"] * novelty + W_PRIOR * e["prior"]
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
            "prior": round(chosen["prior"], 4),
        }
        # 降权项留可观测项：线上出现「为什么这条掉了」时才查得到原因
        _df = chosen.get("_domain_factor")
        if _df is not None:
            r["rerank_dims"]["domain_concentration"] = _df
        _xdf = chosen.get("_cross_domain_factor")
        if _xdf is not None:
            r["rerank_dims"]["cross_domain_homogeneity"] = _xdf
        selected_bigrams |= chosen["bg"]
        ranked.append(r)

    return ranked[:top_n]


def _apply_consensus_and_sort(merged: list[dict[str, Any]],
                              max_results: int) -> list[dict[str, Any]]:
    """融合层最终排序：按五维 rerank 的 score 降序并截断。

    排序只认 rerank 写入的 score（含 ① 融合先验维度）。此处曾有第二道乘法
    共识 boost（×(1+0.05·min(n-1,3))），与 ① 对同一信号重复计分：3 引擎
    共识合计被放大 ~19%（1.08×1.10），2026-09-13 移除（金标 18 条对拍
    无序位回归）。共识的排序影响由 ① 表达，可观测面由 `consensus_engines`
    与 evidence selection 的 `selection` 字段表达。
    """
    merged.sort(key=lambda r: abs(r.get("score", 0) or 0), reverse=True)
    return merged[:max_results]


def _attach_selection_signals(merged: list[dict[str, Any]], mode: str,
                              depth: str) -> None:
    """两阶段 selection 信号（authority/freshness/selection/absorption/…）。

    这是 evidence「先核验哪条」的依据，独立于排序 score——共识在此阶段
    合法地参与（提高待核验优先级），不属于排序重复计分。
    fast 模式跳过（MCP 默认紧凑也不返回这些字段）。失败静默：观测层
    不得拖累搜索主路径。
    """
    if not merged or mode == "fast" or depth == "fast":
        return
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


def _align_facts_safe(merged: list[dict[str, Any]], mode: str,
                      depth: str) -> dict[str, Any] | None:
    """关键事实交叉标记（P0-004）。仅 deep/auto 且结果 ≥3；fast 跳过。

    输出体积限制：corroborated/conflicts 各最多保留 10 条，避免大结果集
    下 fact_alignment 膨胀（实测极端案例单条冲突含 50+ domains，输出 >5KB）。
    """
    if not merged:
        return None
    try:
        from fact_align import align_facts
        raw = align_facts(merged, min_results=3, mode=mode, depth=depth)
        if raw is None:
            return None
        # 体积截断：保留 stats 完整性，截断明细数组
        corroborated = raw.get("fact_corroborated", [])[:10]
        conflicts = raw.get("fact_conflicts", [])[:10]
        # 单条冲突的 domains 也限制（保留前 5 个域名）
        for c in conflicts:
            for v in c.get("values", []):
                if len(v.get("domains", [])) > 5:
                    v["domains"] = v["domains"][:5]
        return {
            "enabled": raw.get("enabled", True),
            "fact_conflicts": conflicts,
            "fact_corroborated": corroborated,
            "stats": raw.get("stats", {}),
            "truncated": bool(
                len(raw.get("fact_corroborated", [])) > 10
                or len(raw.get("fact_conflicts", [])) > 10
            ),
        }
    except ImportError:
        return None  # fact_align 模块不可用
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"事实交叉标记跳过: {type(e).__name__}")
        return None


def _domain_matches(host: str, domain: str) -> bool:
    """host 等于域或是其子域（github.com 命中 api.github.com）。"""
    return host == domain or host.endswith("." + domain)


def filter_results_by_domains(
    results: list[Any] | None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> tuple[list[Any], str | None]:
    """域名后置过滤（引擎无关，融合排序之后执行）。

    include：仅保留命中域名（含子域）的结果；exclude：剔除命中域名的结果。
    返回 (保留列表, 说明文本)；两组过滤都为空时原样返回。
    """
    inc = [str(d).strip().lower() for d in (include_domains or []) if str(d).strip()]
    exc = [str(d).strip().lower() for d in (exclude_domains or []) if str(d).strip()]
    if not inc and not exc:
        return results or [], None
    kept: list[Any] = []
    dropped = 0
    for r in results or []:
        host = ""
        if isinstance(r, dict):
            try:
                from urllib.parse import urlparse as _up
                host = (_up(r.get("url", "") or "").hostname or "").lower()
            except Exception:
                host = ""
        if inc and not any(_domain_matches(host, d) for d in inc):
            dropped += 1
            continue
        if any(_domain_matches(host, d) for d in exc):
            dropped += 1
            continue
        kept.append(r)
    note = f"domain filter: kept {len(kept)}, dropped {dropped}"
    return kept, note

# 「bocha 没有产出排序」的唯一来源：落到本地五维保底的状态全集。
#
# 此前是内联在调用点的四元素元组，新增状态极易漏改，而漏改的后果是静默的——
# 既不精排也不保底，最终顺序退化成 RRF 原始序，没有任何信号。它属于精排层，
# 因此住在精排函数旁边（search.py 只是同名转出）。
_RERANK_DEGRADED_STATUSES = frozenset({
    "skipped_no_key",        # 未配置密钥
    "skipped_short",         # 结果太少，不值得精排
    "skipped_fast",          # fast 档不付远程精排
    "skipped_circuit_open",  # 端点熔断中（见 _RERANK_BREAKER_KEY）
    "fallback",              # 端点报错或返回不可用数据
})
