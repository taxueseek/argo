#!/usr/bin/env python3
"""variant_recall.py — multi-query 变体召回波（Step 1，argo 普适性改进）。

问题：argo 主搜索路径只发**单个** retrieval_query，模糊/新颖 query 无法通过多路
召回补漏——召回天花板 = 单串 × 引擎自身改写能力。query_enhance.retrieval_variants
早已写好（归一化 + 拆型号 + 同义变体），却从没接到主路径（此前只在 research /
个别付费引擎内部用）。本模块把它接进主路径。

接法（最小且隔离）：主 query 召回不足时，用变体对 combo 内主引擎补充召回，结果
作为**独立 ranked list** 并入 clean_lists，让 rrf_merge 按「多 query 共识」天然
加权（同一 URL 被多变体命中 → RRF 分累加 → 排更前）。

隔离设计（为何绕开 _run_one 直接用 engine_search）：
  - 不走熔断/负缓存/per-engine 记账——变体 query 的失败不该污染主引擎健康，
    也不该把「这个变体没结果」固化成主 query 的负缓存；
  - skip_cache=True：变体结果不读不写缓存，避免缓存键维度膨胀；
  - 失败安全：单条变体/单引擎异常整体吞掉，绝不影响主 query 结果。

成本受控（多 query 时收敛引擎数是通行做法）：最轻量预算档 mode=fast|budget
不触发；仅主 query 召回不足才补；最多 2 变体 × combo 前 2 引擎，超时收紧到 ≤6s。

为何独立成模块：search_pipeline.py 受 1000 行门禁约束（见 test_module_size_gate），
新增召回机制应拆出而非塞进融合管线。本模块**不** import search_pipeline（避免循环），
以 duck typing 消费 req.retrieval_query / req.engines / req.max_results / req.eff_timeout
/ req.since_iso / req.until_iso / req.mode / req.depth 等字段。
"""
from __future__ import annotations

import re
from typing import Any

_VARIANT_MAX_QUERIES = 2
_VARIANT_MAX_ENGINES = 2
_VARIANT_TIMEOUT_CAP_S = 6.0
# 波级总预算（秒）：per-call 超时盖不住 2×2 串行累计（最坏 4×6s=24s），而主调度
# 的 auto 预算不含这段追加——必须自设总闸。超线即停，已拿到的部分照常并入。
_WAVE_BUDGET_S = 8.0


def should_variant_recall(req: Any, clean_lists: list, max_results: int) -> bool:
    """是否触发 multi-query 变体召回波。

    两条硬边界（抽成纯函数供回归门直接锁）：
      - 最轻量预算档 mode=fast|budget 不触发——时延可预期优先；
      - 仅主 query 召回不足（clean_lists 结果总数 < max_results）才补——结果
        够就零成本不动，不够才用变体补召回。

    刻意**不**按 depth=fast 拦截：默认调用是 mode=auto + depth=fast，而 auto 有
    10s 总预算（远宽于 mode=fast 的 6s），足以容纳「召回不足才补」的少量变体
    调用。若连默认路径都拦掉，multi-query 对普通用户永不生效，普适性提升落空。

    运维 kill-switch：ARGO_MULTI_QUERY=0 一键回滚，也便于无侵入 A/B 对照。
    """
    try:
        from engine_env import env_flag
        if not env_flag("ARGO_MULTI_QUERY", default=True):
            return False
    except Exception:
        pass  # env_flag 不可用时按默认开启，不阻断搜索
    if req.mode in ("fast", "budget"):
        return False
    # 答案型域 route 会下发 early_stop_min_results（1-2 行快照即完整答案）：
    # 完整性判据按它算，否则这类域每次搜索都白付变体调用。
    need = max_results
    dec = getattr(req, "decision", None)
    if isinstance(dec, dict):
        m = dec.get("early_stop_min_results")
        if isinstance(m, int) and 0 < m <= max_results:
            need = m
    return sum(len(c) for c in clean_lists) < need


def variant_recall_wave(req: Any, engine_search: Any) -> list[list[dict[str, Any]]]:
    """主 query 召回不足时的 multi-query 补充波；返回要追加的 ranked lists。

    返回空列表表示「无需补或无处补」，调用方直接沿用主 query 的 clean_lists。
    单条变体/单引擎异常整体吞掉（失败安全），不向上抛。
    """
    try:
        from query_enhance import retrieval_variants
    except ImportError:
        return []
    base = req.retrieval_query
    if not base:
        return []
    # 变体池 = retrieval_variants（归一化/拆型号/同义）+ structural_variants（错误裸
    # token/复合词短语）；合并去重、去主串，取前 _VARIANT_MAX_QUERIES 个（成本受控）。
    pool = (retrieval_variants(base, max_n=_VARIANT_MAX_QUERIES + 1)[1:]
            + structural_variants(base))
    seen: set[str] = set()
    variants: list[str] = []
    for v in pool:
        k = v.strip().lower()
        if v.strip() and v != base and k not in seen:
            seen.add(k)
            variants.append(v)
    if not variants:
        return []
    engines = (req.engines[:_VARIANT_MAX_ENGINES] if len(req.engines) > _VARIANT_MAX_ENGINES
               else list(req.engines))
    if not engines:
        return []
    try:
        timeout = min(float(req.eff_timeout or _VARIANT_TIMEOUT_CAP_S), _VARIANT_TIMEOUT_CAP_S)
    except (TypeError, ValueError):
        timeout = _VARIANT_TIMEOUT_CAP_S
    import time as _time
    deadline = _time.monotonic() + _WAVE_BUDGET_S
    # CLI --domain / --sub_domain 的引擎入参照主路径（engine_dispatch 同口径）透传，
    # 否则用户的域约束被变体旁路。
    dom_kwargs: dict[str, str] = {}
    _d = getattr(req, "engine_domain", None)
    _sd = getattr(req, "engine_sub_domain", None)
    if _d:
        dom_kwargs["domain"] = _d
    if _sd:
        dom_kwargs["sub_domain"] = _sd
    extra: list[list[dict[str, Any]]] = []
    for v in variants[:_VARIANT_MAX_QUERIES]:
        if _time.monotonic() > deadline:
            break
        for eng in engines:
            if _time.monotonic() > deadline:
                break
            try:
                res = engine_search(
                    v, eng, n=req.max_results, timeout=timeout,
                    depth=req.depth, mode=req.mode,
                    since=req.since_iso, until=req.until_iso, skip_cache=True,
                    **dom_kwargs,
                )
            except Exception:
                continue  # 单条变体/单引擎失败不影响主结果
            goods = [r for r in (res or []) if isinstance(r, dict) and "error" not in r]
            for r in goods:
                r.setdefault("_engine", eng)
            if goods:
                extra.append(goods)
    return extra


# 结构信号变体（无词典、纯形状识别）——补 retrieval_variants 覆盖不到的两类：
#   1. 错误/异常裸 token：从 "Rust error[E0499]" 提取 "E0499"（解错页在 issue tracker）；
#   2. 复合词短语：给连字符/snake_case 词加引号（"sqlite-vec"），迫使精确匹配。
# 刻意窄匹配：错误码只认 E\d{3,5}/ERR_*/XxxError 形状，不碰 HTTPS/NGINX 等全大写普通词。
_ERR_SHAPE = re.compile(r"\b(E\d{3,5}|ERR_[A-Z0-9_]+|[A-Z][A-Za-z]*Error)\b")
_COMPOUND_SHAPE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9][A-Za-z0-9_-]*\b")


def structural_variants(query: str, max_n: int = _VARIANT_MAX_QUERIES) -> list[str]:
    """结构信号变体：错误裸 token + 复合词引号短语。无词典、零依赖、确定性。"""
    if not query or not isinstance(query, str):
        return []
    out: list[str] = []
    for m in _ERR_SHAPE.finditer(query):
        tok = m.group(1)
        if tok and tok.lower() != query.lower():
            out.append(tok)
    for m in _COMPOUND_SHAPE.finditer(query):
        tok = m.group(0)
        if ("-" in tok or "_" in tok) and f'"{tok}"' not in out:
            out.append(f'"{tok}"')
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        k = v.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq[:max_n]


def augment_with_variants(req: Any, engine_search: Any, clean_lists: list,
                          max_results: int) -> tuple[list, int]:
    """按 gate 决定是否补变体召回；返回 (clean_lists, 变体补回条数)。

    失败安全：任何异常都原样返回入参 clean_lists，绝不影响主 query 融合。
    补回条数单列：变体条目不进漏斗 returned 口径，调用方拿它做观测补账。
    """
    if not should_variant_recall(req, clean_lists, max_results):
        return clean_lists, 0
    try:
        extra = variant_recall_wave(req, engine_search)
    except Exception:
        return clean_lists, 0
    if not extra:
        return clean_lists, 0
    return clean_lists + extra, sum(len(c) for c in extra)
