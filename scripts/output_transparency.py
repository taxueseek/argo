#!/usr/bin/env python3
"""output_transparency.py — 搜索输出的可信度评估、结构化失败清单与精简档透明修饰。

从 search_output.py 拆出（2026-10-21）：
  1. 单一职责：search_output 管「结果成形与人读格式」，本模块管「可信度/失败分类/
     不可信边界」三件透明化信号；拆开后 search_output 保持在 ~650 行，远离千行硬限。
  2. 口径合一：`_FAILURE_NON_ERROR` 与 `_NON_ERROR_OUTCOME` 同源派生，不再维护
     两份独立 frozenset。
"""

from __future__ import annotations

import sys
from typing import Any

import engine_states  # 状态词表唯一真源（2026-11-09 收口）

# 本次调用不算失败的基础状态（引擎正常交付、无结果、或被早停主动取消）
_FAILURE_NON_ERROR: frozenset[str] = frozenset({
    engine_states.OUTCOME_OK, engine_states.OUTCOME_OK_CACHED,
    engine_states.OUTCOME_PARTIAL, engine_states.OUTCOME_NO_RESULTS,
    engine_states.OUTCOME_NO_RESULTS_CACHED, engine_states.OUTCOME_CANCELLED,
})

# errors[] 与 uncertainty 排除的 outcome 状态：在基础非错误集之上再排除
# skipped-quota-exhausted（持续一天的配额跳过状态，不作为单次检索错误刷屏）
_NON_ERROR_OUTCOME: frozenset[str] = _FAILURE_NON_ERROR | {
    engine_states.OUTCOME_SKIPPED_QUOTA,
}

# 二手/聚合平台（转载与洗稿高发地）。**刻意不复用 evidence 的词表**：
# evidence 属热路径禁入名单（tests/test_hotpath_import_lint.py 的 HEAVY_TOP，
# 它会拖进 content_signals/stylometry/burstiness 约 9ms），而本函数在每次
# 搜索的收口处无条件执行。两处词表需同步——改这里时看 evidence.
# _SECONDARY_PLATFORM_PATTERNS。
_AGGREGATOR_HOSTS: frozenset[str] = frozenset({
    "baijiahao.baidu.com",   # 百家号（自媒体洗稿重灾区）
    "toutiao.com",           # 头条号（聚合+洗稿）
    "k.sina.com.cn",         # 新浪博客/聚合
    "guba.eastmoney.com",    # 股吧（UGC 讨论，非事实来源）
})

# 可信度等级（与 dsh-free-search 的 meta.uncertainty 同形）
_UNCERTAINTY_HIGH = "high"
_UNCERTAINTY_MEDIUM = "medium"
_UNCERTAINTY_LOW = "low"
# 严重度序：用于「多信号取最差」——等级只能降不升。
_SEVERITY = {_UNCERTAINTY_HIGH: 0, _UNCERTAINTY_MEDIUM: 1, _UNCERTAINTY_LOW: 2}


def _hostname(url: Any) -> str:
    """URL → 归一化主机名（小写、去 www.）；解析失败返回空串。"""
    try:
        from urllib.parse import urlparse
        host = urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""
    return host.lower().removeprefix("www.")


# 失败类别 → (归一类别, 可执行建议, 是否可重试)。类别名与 dsh-free-search
# 的 failureClass 对齐（quota/auth/bot-wall/transient/rate-limit/unknown），
# 但归类依据是 argo 自己的 outcome.status（retry_policy.classify_error_status
# 的唯一真源），不另写一套错误词表。
_FAILURE_META: dict[str, tuple[str, str, bool]] = {
    engine_states.OUTCOME_TIMEOUT: ("transient", "超时；可重试或换源", True),
    engine_states.OUTCOME_QUOTA_EXHAUSTED: ("quota", "配额耗尽；换源或等窗口重置", False),
    engine_states.OUTCOME_RATE_LIMITED: ("rate-limit", "被限流；稍后重试或换源", True),
    engine_states.OUTCOME_BLOCKED: ("bot-wall", "被反爬拦截；稍后重试或换源", True),
    engine_states.OUTCOME_AUTH_FAILED: ("auth", "检查 API Key 是否有效、是否有该接口权限", False),
    engine_states.OUTCOME_ERROR: ("unknown", "源端错误；可重试或换源", True),
    engine_states.OUTCOME_SKIPPED_MISSING_ENV: (
        "missing-key", "未配置密钥；配置后即可参与", False),
    engine_states.OUTCOME_SKIPPED_QUOTA: ("quota", "配额耗尽，本轮跳过", False),
    engine_states.OUTCOME_SKIPPED_CIRCUIT_OPEN: (
        "cooldown", "熔断冷却中，稍后自动恢复", True),
    engine_states.OUTCOME_SKIPPED_BUDGET: ("budget", "本轮预算不足，未执行", True),
}


def build_failures(result: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    """把引擎 outcome 压成 Agent 可行动的结构化失败清单。

    与 errors 的分工：errors 是给人看的整行文本（兼容旧契约），本字段是
    给模型/宿主判断「要不要重试、换哪个源、是不是缺 key」的分类结果。
    只收录没交出结果的 outcome；ok/no-results 不算失败（后者是「查了没有」，
    不是「没查成」），与 uncertainty 的降级判据同源。
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for o in (result.get("engine_outcomes") or []):
        if not isinstance(o, dict):
            continue
        engine = str(o.get("engine") or "")
        status = str(o.get("status") or "")
        if not engine or status in _FAILURE_NON_ERROR:
            continue
        key = (engine, status)
        if key in seen:
            continue
        seen.add(key)
        category, action, retryable = _FAILURE_META.get(
            status, ("unknown", "源端异常；可重试或换源", True))
        item: dict[str, Any] = {
            "engine": engine,
            "status": status,
            "category": category,
            "retryable": retryable,
            "action": action,
        }
        # skipped-* 是「持续状态」而不是本次尝试失败：配额窗口/熔断冷却在
        # 周期边界前不会因重试改变。标出来，避免 Agent 立刻重试同一个源。
        if status.startswith(engine_states.OUTCOME_SKIPPED_PREFIX):
            item["persistent"] = True
        detail = str(o.get("detail") or "").strip()
        if detail:
            item["detail"] = detail[:160]
        out.append(item)
        if len(out) >= max(1, limit):
            break
    return out


def decorate_compact_payload(out: dict[str, Any],
                             result: dict[str, Any]) -> None:
    """精简响应（MCP/Agent 档）的收尾修饰：边界清洗 + 三件透明信息 + sources 投影。"""
    try:
        from content_boundary import (CONTENT_TRUST_UNTRUSTED,
                                      sanitize_results_boundary)
        sanitize_results_boundary(out.get("results"))
        out["content_trust"] = CONTENT_TRUST_UNTRUSTED
    except Exception:
        pass
    failures = result.get("failures")
    if isinstance(failures, list) and failures:
        out["failures"] = failures[:6]
    meta = result.get("meta")
    if isinstance(meta, dict) and meta.get("uncertainty"):
        out["meta"] = {"uncertainty": meta["uncertainty"]}
    sources = result.get("sources")
    if sources:
        out["sources"] = [
            {
                "ref": s.get("ref"),
                "title": s.get("title"),
                "url": s.get("url"),
                "engine": s.get("engine") or s.get("source"),
            }
            for s in sources
            if isinstance(s, dict) and s.get("url")
        ][:20]


def build_uncertainty(result: dict[str, Any]) -> dict[str, Any]:
    """一次调用的综合可信度：`{"level": ..., "notes": [...]}`。

    与 `limitations` 的分工：limitations 是**逐条**的局限声明（早停、降级、
    缓存复用、站点过滤），本字段是**整体**等级——消费者拿到一个 level 就能
    决定「直接引用」还是「先核验」，不必自己交叉比对五六个字段。
    """
    notes: list[str] = []
    results = [r for r in (result.get("results") or []) if isinstance(r, dict)]

    if not results:
        return {"level": _UNCERTAINTY_LOW, "notes": ["没有返回结果"]}

    level = _UNCERTAINTY_HIGH

    def _worse(candidate: str) -> None:
        nonlocal level
        if _SEVERITY[candidate] > _SEVERITY[level]:
            level = candidate

    if len(results) == 1:
        _worse(_UNCERTAINTY_LOW)
        notes.append("只有 1 个来源，无法交叉验证")
    elif len(results) == 2:
        _worse(_UNCERTAINTY_MEDIUM)
        notes.append("只有 2 个来源")

    hosts = {_hostname(r.get("url")) for r in results}
    hosts.discard("")
    if 0 < len(hosts) == 1:
        _worse(_UNCERTAINTY_MEDIUM)
        notes.append("所有结果来自同一站点")

    # 尊重 search_output._AGGREGATOR_HOSTS 的猴子补丁（test_output_uncertainty 门禁锁）
    so_mod = sys.modules.get("search_output")
    agg_hosts = getattr(so_mod, "_AGGREGATOR_HOSTS", _AGGREGATOR_HOSTS)
    aggregators = sorted(h for h in hosts if h in agg_hosts
                         or any(h.endswith("." + a) for a in agg_hosts))
    if aggregators:
        _worse(_UNCERTAINTY_MEDIUM)
        notes.append(f"含聚合站内容（{'/'.join(aggregators)}），建议二次核对")

    if result.get("time_filter_warning"):
        _worse(_UNCERTAINTY_MEDIUM)
        notes.append("时间窗未实际过滤，结果可能不在请求的时间范围内")

    outcomes = [o for o in (result.get("engine_outcomes") or [])
                if isinstance(o, dict)]
    failed = [o for o in outcomes
              if str(o.get("status") or "") not in _NON_ERROR_OUTCOME]
    if failed:
        _worse(_UNCERTAINTY_LOW)
        notes.append(f"{len(failed)} 个引擎未交出结果，覆盖可能比计划的窄")

    return {"level": level, "notes": notes}
