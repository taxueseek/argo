#!/usr/bin/env python3
"""retry_policy.py — 失败类别与重试判据（唯一来源）。

拆出 engine_dispatch 的理由有两条：

1. **单一真源**：重试判据（该不该再发一次）与归因判据（这次算什么失败）
   必须用同一套词表与同一个优先级。两处各写一套的下场是重试按一套词表
   跳过、熔断按另一套记账，对同一次失败给出两个不同判断。
2. **行数门禁**：engine_dispatch 已接近祖父上限（tests/test_module_size_gate.
   py），而本模块是纯策略、无 IO、无闭包状态——正好是「拆出去两边都轻」
   的那一类。

本模块不做决定、不碰状态：只把「错误文本 → 类别 → 是否值得重试」算出来。
"""

from __future__ import annotations

from typing import Any

from cache_guard import _QUOTA_ERROR_KEYWORDS
from engine_failure import NOTE_STATUS as _NOTE_STATUS
import engine_states  # 状态词表唯一真源（2026-11-09 收口）

# 拦截页特征词（唯一来源）：error 文本里出现即判 blocked。HTML 引擎的反爬
# 命中没有 error 文本（静默空结果），走 engines_base 的归因寄存器；
# 这张表兜住「error 结果里带拦截页字样」的可见路径。
_BLOCKED_ERROR_KEYWORDS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "challenge", "ddos-guard", "perimeterx", "access denied",
    "handshake failure", "unable to handshake", "安全验证", "滑动验证",
)

# 确定性失败：源端状态，不是网络抖动。重试只是多烧一次调用并推迟切备选
#——配额耗尽重试仍是耗尽，鉴权重试仍是 401，反爬重试仍撞同一堵墙。
# 与 dsh-free-search 的 failure-aware fallback 同构（quota/auth 冷却到会话
# 结束、bot-wall 短退避，只有 transient 才重试一次）。
NON_RETRYABLE_STATUS = engine_states.NON_RETRYABLE_OUTCOMES

# 归因寄存器（engines_base.note_failure）的类别 → 是否确定性失败。
# 与上面的状态表同义，只是入口不同：HTML 抓取类引擎失败时往往返回空列表，
# 错误文本只写进归因寄存器，`classify_error_status` 看不到。2026-10-26 实测：
# octen 余额不足返回 []，调度层把它当「空结果可重试」，跑满 3 次（1934ms）；
# local_bing 被反爬时同样重试。归因类别是同一事实的另一个出口，判据必须一致。
# 从 NOTE_STATUS（唯一映射表）派生，不另抄一份类别名单：rate_limited 映射到
# 非 NON_RETRYABLE_STATUS 的 "rate-limited"，自动保持「限流可重试」。
_DETERMINISTIC_NOTE_CATEGORIES = frozenset(
    cat for cat, status in _NOTE_STATUS.items()
    if status in NON_RETRYABLE_STATUS)


def classify_error_status(message: Any) -> str:
    """错误文本 → 失败类别。**重试判据与归因判据的共同定义点**。

    优先级顺序即语义强弱：超时最具体（描述本次调用的形态），配额/限流/
    拦截是源端状态，鉴权最后（401/403 也可能来自拦截页）。
    """
    m = str(message or "").lower()
    if "timeout" in m or "timed out" in m:
        return engine_states.OUTCOME_TIMEOUT
    if any(k in m for k in _QUOTA_ERROR_KEYWORDS):
        return engine_states.OUTCOME_QUOTA_EXHAUSTED
    if "rate" in m or "429" in m:
        return engine_states.OUTCOME_RATE_LIMITED
    if any(k in m for k in _BLOCKED_ERROR_KEYWORDS):
        return engine_states.OUTCOME_BLOCKED
    if "auth" in m or "401" in m or "403" in m:
        return engine_states.OUTCOME_AUTH_FAILED
    return engine_states.OUTCOME_ERROR


def is_retryable_failure(res: list[Any] | None,
                         failure_note: dict[str, Any] | None = None) -> bool:
    """这组结果值不值得再试一次（纯函数，门禁可直接打到）。

    判据取自 `classify_error_status`，与归因同源。空结果与无错误条目返回
    True——沿用既有「重试一次」语义，本次只收紧确定性失败这一类。

    `failure_note` 是归因寄存器的只读快照（engines_base.peek_failure_note）：
    抓取类引擎失败时返回空列表，错误只落在寄存器里，只查 `res` 会把配额耗尽
    当空结果重试。两个入口共用同一张确定性类别表，避免「重试按一套判据、
    熔断按另一套判据」。
    """
    note_category = str((failure_note or {}).get("category") or "")
    if note_category in _DETERMINISTIC_NOTE_CATEGORIES:
        return False
    for r in res or []:
        if isinstance(r, dict) and "error" in r:
            return classify_error_status(r.get("error")) not in NON_RETRYABLE_STATUS
    return True


def record_attempts(outcome: dict[str, Any], attempts: dict[str, int]) -> None:
    """把「实际发了几次网络请求」如实写进 outcome。

    单独成函数是为了让 engine_dispatch 不必为 3 行记账逻辑长到行数门禁
    外面去；记账口径（至少 1 次）也只在这里定义一次。
    """
    outcome["attempts"] = max(1, int((attempts or {}).get("n", 1)))
