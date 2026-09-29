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

from typing import Any

_VARIANT_MAX_QUERIES = 2
_VARIANT_MAX_ENGINES = 2
_VARIANT_TIMEOUT_CAP_S = 6.0


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
    return sum(len(c) for c in clean_lists) < max_results


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
    # [1:] 去掉归一化主词本身，只留衍生变体；去空、去与主串相同者
    variants = [v for v in retrieval_variants(base, max_n=_VARIANT_MAX_QUERIES + 1)[1:]
                if v and v != base]
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
    extra: list[list[dict[str, Any]]] = []
    for v in variants[:_VARIANT_MAX_QUERIES]:
        for eng in engines:
            try:
                res = engine_search(
                    v, eng, n=req.max_results, timeout=timeout,
                    depth=req.depth, mode=req.mode,
                    since=req.since_iso, until=req.until_iso, skip_cache=True,
                )
            except Exception:
                continue  # 单条变体/单引擎失败不影响主结果
            goods = [r for r in (res or []) if isinstance(r, dict) and "error" not in r]
            for r in goods:
                r.setdefault("_engine", eng)
            if goods:
                extra.append(goods)
    return extra


def augment_with_variants(req: Any, engine_search: Any, clean_lists: list,
                          max_results: int) -> list:
    """按 gate 决定是否补变体召回，返回（可能扩充的）clean_lists。

    失败安全：任何异常都原样返回入参 clean_lists，绝不影响主 query 融合。
    """
    if not should_variant_recall(req, clean_lists, max_results):
        return clean_lists
    try:
        extra = variant_recall_wave(req, engine_search)
    except Exception:
        return clean_lists
    return clean_lists + extra if extra else clean_lists
