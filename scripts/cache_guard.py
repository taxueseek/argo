#!/usr/bin/env python3
"""cache_guard.py — 公共缓存的写入守卫（登录态 + 上游退化）。

从 cache.py 拆出（2026-09-27，方案 C1）。拆的理由是纪律性的：cache.py 已在
tests/test_module_size_gate.py 的 GRANDFATHERED 里登记（1362 行上限），
任何净增长都会被门禁拦下——而「写缓存前该不该写」这条判断不属于缓存的存储
实现，属于「缓存的准入策略」，两者混在一起只会让 cache.py 继续变大。

## 两条守卫

**登录态守卫**（本体与异常类都在 cache.py）：登录态检索的结果不得进入
公共缓存，避免一个账号的私有结果被所有人共享。

**退化守卫**：上游抖动返回的残次品不得写入。
实锤事故见 is_degraded_results 的 docstring——一次上游把整句当单词查、
返回 8 条词典释义，相关性全为 0.1429，却照常缓存并从此固化。

两个守卫的异常类**不在同一个模块**：cache.py 模块级 `from cache_guard
import ...`，所以这里只能用函数级 import 去取 LoginCacheRejected，模块级
会成环。统一入口是 attempt_cache_write，调用方不必自己认这两个类。

## 为什么退化守卫在「写入」这一层而不是「返回」那一层

退化结果**当场返回**是有价值的：它至少让调用方看到「这次没搜到」，
且恢复路径是重打一次（无缓存命中，自然重新请求上游）。
但它**不该被缓存**：一旦缓存，退化就从「一次抖动」变成「永久结果」——
后续每次命中缓存都复现，且没有任何信号提示它曾经退化过。
写入层拦截是唯一能切断这个固化的位置。

## 调用方必须走 attempt_cache_write，不要裸调 cache.set

「不写缓存」和「检索失败」是两件事。守卫抛出的异常如果穿透到调用主路径，
代价是一次检索白跑——而且比白跑更糟，见 attempt_cache_write 的 docstring。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from except_sets import OPT_IMPORT


class DegradedCacheRejected(ValueError):
    """退化结果（低相关/查询词回声）禁止写入公共 SearchCache。"""


class FailedStateCacheRejected(ValueError):
    """配置/状态类失败（缺密钥/熔断/鉴权/配额）且无有效结果：不写负缓存。

    与退化守卫的分工：退化拦「搜到了但内容是垃圾」；本守卫拦「根本没搜成、
    且换个人/等一会/改个配置就能好」。把后者缓存下来，等于把「argo 此刻
    没配对」固化成「这个查询没有答案」——实测形态（2026-09-28）：
    `--engine parallel` 缺 key → L2 落 {"results": [], "ttl": 45}，用户按
    文档配好 PARALLEL_API_KEY 后同一查询仍 cached=true 并回放
    「PARALLEL_API_KEY 未设置」，与 issue #12 报告人的体验逐字同构。
    """


# 配置/状态类失败的状态名（engine_dispatch.classify_engine_outcome 词汇表）。
# 网络类失败（timeout/blocked/rate-limited/no-results）刻意不收——它们中
# 确实混有「真的没有」，负缓存是既有设计（EMPTY_RESULT_TTL），等一会重试
# 由 TTL 自然兜底。本类的共同点：**换环境就能好**，缓存它没有信息量。
_STATE_FAIL_STATUSES = frozenset({
    "skipped-missing-env", "skipped-circuit-open", "auth-failed",
    "quota-exhausted", "skipped-quota-exhausted",
})

# builder 路径漏到 status="error" 的缺密钥形态：路由层 env 拦截覆盖不到
# 自定义 required_env 的引擎（search.py:192-209 docstring 自认的缺口），
# issue #12 的 parallel/seltz/you 就读裸名——状态层 env_ready=True、执行层
# 取不到值。按 detail 文本兜底识别。
_MISSING_ENV_DETAIL_KEYWORDS = (
    "未设置", "api_key", "api key", "apikey", "环境变量",
)


def is_state_failure_outcome(outcome: Any) -> bool:
    """单引擎结局是否「配置/状态类失败」（可修复，不该负缓存）。"""
    if not isinstance(outcome, dict):
        return False
    status = str(outcome.get("status") or "")
    if status in _STATE_FAIL_STATUSES:
        return True
    if status == "error":
        detail = str(outcome.get("detail") or "").lower()
        return any(k in detail for k in _MISSING_ENV_DETAIL_KEYWORDS)
    return False


def failed_state_reason(payload: object) -> str | None:
    """载荷是否「整体没搜成且原因可修复」——是则返回理由（否则 None）。

    判据：**没有任何有效结果**、且**至少一个**引擎结局是配置/状态类失败。
    两条边界的理由：

      - 有有效结果时不拦：个别引擎的配置失败不毒化整批（照常缓存）；
      - 「至少一个」而不是「全部」：缓存命中会**原样回放 engine_outcomes**
        并由 _collect_errors 重建 errors——混合场景里那条「未设置」会在
        用户配好 key 后继续重放 45s，与单引擎形态同构。连带的代价只是
        别的引擎少一份 45s 负缓存（重试一次超时引擎本就无妨）。

    网络类失败单独出现时不拦（见 _STATE_FAIL_STATUSES 注释）。
    """
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if isinstance(results, list) and any(
            isinstance(r, dict) and "error" not in r for r in results):
        return None
    outcomes = payload.get("engine_outcomes")
    if isinstance(outcomes, dict):
        outcomes = [outcomes]  # 单引擎直写形态（CLI --engine 路径）
    if not isinstance(outcomes, list) or not outcomes:
        return None
    failed = [o for o in outcomes if is_state_failure_outcome(o)]
    if failed:
        kinds = sorted({str(o.get("status") or "?") for o in failed
                        if isinstance(o, dict)})
        return (f"{len(failed)}/{len(outcomes)} 个引擎为配置/状态类失败"
                f"（{'/'.join(kinds)}）且无有效结果，不写负缓存")
    return None


def assert_not_failed_state(payload: object, *, context: str) -> None:
    """配置/状态类失败硬拒绝：宁可下次重打，也不把「没配对」固化成「没答案」。"""
    reason = failed_state_reason(payload)
    if reason:
        raise FailedStateCacheRejected(f"{context}: {reason}")


def cache_write_rejections() -> tuple[type[Exception], ...]:
    """写入守卫会抛的全部异常类：退化 + 失败态（本模块）+ 登录态（cache.py）。

    登录态那一个只能函数级取：cache.py 模块级 import 本模块，反向 import
    会成环。取不到（cache 被换掉/裁掉）时只留本模块守卫——少认一种异常最多
    让那一路退回旧行为，不该反过来让调用方炸掉。
    """
    try:
        from cache import LoginCacheRejected
    except OPT_IMPORT:
        return (DegradedCacheRejected, FailedStateCacheRejected)
    return (DegradedCacheRejected, FailedStateCacheRejected, LoginCacheRejected)


def attempt_cache_write(write: Callable[[], Any], *,
                        context: str = "cache") -> str | None:
    """执行一次缓存写入；被写入守卫拒绝时返回异常类别名，而不是抛出。

    守卫的职责是「不固化」，不是「不返回」——两者在调用主路径上必须分开：

      - **combo 写入点**（search_pipeline.finalize）：异常穿透会取消整个
        查询。2026-09-27 实锤——`Apple Inc 10-K annual report 2025` 的 5 条
        结果里 4 条 relevance=0 触发退化守卫，CLI 直接 traceback 退出，
        **已检索到的结果全部丢弃**（这批结果本身是有用的：第 1 条正是
        Apple 的 CIK 归档页）。
      - **per-engine 写入点**（engine_dispatch）：异常被 daemon 线程的兜底
        except 接住，后果更隐蔽——那次**成功的**引擎调用被改写成
        `status=error` 且结果清空，还按 kind=error 记账进熔断器，把一个
        健康引擎推向 auto-disable。

    返回值为 None 表示写入成功（或本就没有可写内容）；返回字符串表示被
    拒绝，值即异常类别名（如 `"DegradedCacheRejected"`），供调用方写进
    响应字段或日志。*context* 只用于日志措辞。
    """
    try:
        write()
    except cache_write_rejections() as exc:
        import logging
        logging.getLogger("unified_search").debug(
            f"{context}: 缓存写入跳过（{type(exc).__name__}）: {exc}")
        return type(exc).__name__
    return None


# 退化判据的阈值。与 rank_signals 的相关性算子配套：正常结果的 rel 普遍
# ≥0.35，查询词回声（上游把整句当单词查、返回词典释义）稳定在 0.05-0.20。
# 0.25 落在两者之间，且远高于噪声、又低于任何真实命中。
DEGRADED_RELEVANCE_FLOOR = 0.25

# 「多数条目低于地板」的比例门槛。不用 1.0：上游降级时通常仍混有 1-2 条
# 正常结果（降级不是全有全无）。0.7 是「压倒性低分」与「正常长尾」的分界。
DEGRADED_BATCH_RATIO = 0.7

# 可判定条目数下限。低于此不判：样本太少时比例不可信，且宁可漏拦不误伤。
DEGRADED_MIN_SCORED = 3


def _entry_relevance(item: Any) -> float | None:
    """取单条结果的相关性；取不到返回 None。

    相关性通常不在顶层，而在 `rerank_dims` 里（本地五维 rerank 写入）。
    顶层也看一眼是为了兼容未跑 rerank 的引擎直写路径。
    踩过的坑：只读顶层会得到「全部取不到 → 一律不判」，守卫静默失效。
    """
    if not isinstance(item, dict):
        return None
    rel = item.get("relevance")
    if not isinstance(rel, (int, float)) or isinstance(rel, bool):
        dims = item.get("rerank_dims")
        rel = dims.get("relevance") if isinstance(dims, dict) else None
    if isinstance(rel, (int, float)) and not isinstance(rel, bool):
        return float(rel)
    return None


def is_degraded_results(results: object) -> bool:
    """这批结果是否是「上游退化」——不完整命中查询词的回声。

    2026-09-27 实锤（`detecting LLM generated content farm SEO spam`）：
    路由正确（anysearch + local_bing），但上游某一刻把整句当成单词去查，
    返回 8 条「detecting 这个词的词典释义」。它们的 rerank_dims.relevance
    全部是 **0.1429**（只命中查询里的 detecting 一词），却照常返回、照常
    进入排序、照常写入缓存——于是这次上游抖动被**固化**下来，之后每次命中
    缓存都复现同样的垃圾结果。funnel 看着完全正常（returned 10 → kept 8），
    所以极难察觉。

    这与「结果少」是两件事：`funnel` 只数数量，不看每条像不像答案。

    判据刻意保守——**只拦整批退化**，不拦单条低分：
      - 单条低相关完全正常（长尾查询本就有弱命中），拦它会误伤真结果；
      - 整批都低于地板才是信号：要么上游确实降级了，要么这个查询真的没有
        答案（此时缓存一份垃圾不如重打一次）。
    """
    if not isinstance(results, list) or not results:
        return False
    scored = [v for v in (_entry_relevance(it) for it in results) if v is not None]
    if len(scored) < DEGRADED_MIN_SCORED:
        # 拿不到足够的相关性数据就不判——不猜，也不用缺字段去拒绝正常写入。
        return False
    low = sum(1 for v in scored if v < DEGRADED_RELEVANCE_FLOOR)
    return (low / len(scored)) >= DEGRADED_BATCH_RATIO


def assert_not_degraded(results: object, *, context: str) -> None:
    """退化结果硬拒绝：宁可下次重打，也不要把上游抖动固化进缓存。"""
    if is_degraded_results(results):
        raise DegradedCacheRejected(
            f"{context}: 多数条目 relevance < {DEGRADED_RELEVANCE_FLOOR}，"
            f"判定为上游退化，不写入公共 SearchCache"
        )
