#!/usr/bin/env python3
"""intent_router.py — 高层意图路由（--intent）。

把「调用方记忆 explicit_only 引擎名」转为「声明查询意图」：
  - news          时事/动态：有密钥且未超预算（且非 fast 模式）走 deepseek_search，
                  否则透明降级到自动路由 + domain=news 并附降级说明；
  - academic      学术多源：depth 至少为 deep + domain=academic；
  - social        社交与社区：domain=social；
  - image-reverse 反搜图：自动从 animetrace/tracemoe/saucenao/google_lens 选取可用源；
  - calc          精确计算/换算：wolframalpha 可用时自动启用，否则降级 auto 并提示。

设计约束：
  - 仅在显式传入 --intent 时由 search_cli.py 懒加载，默认路径零启动开销；
  - 所有环境/配额/配置探测器均支持依赖注入，便于离线单测与回退验证。
"""

from __future__ import annotations

from typing import Any, Callable

SUPPORTED_INTENTS: tuple[str, ...] = (
    "news",
    "academic",
    "social",
    "image-reverse",
    "calc",
)

_REVERSE_IMAGE_CANDIDATES: tuple[str, ...] = (
    "animetrace",
    "tracemoe",
    "saucenao",
    "google_lens",
)


def _default_get_engines() -> dict[str, dict[str, Any]]:
    from config import get_engines
    return get_engines()


def _default_missing_env(engine_id: str, spec: dict[str, Any] | None) -> list[str]:
    from engine_env import missing_env_for
    return missing_env_for(engine_id, spec)


def _default_quota_available(engine_id: str, mode: str) -> bool:
    from quota import get_quota_manager
    return get_quota_manager().is_available(engine_id, mode=mode)


def _engine_ready(
    engine_id: str,
    engines_cfg: dict[str, dict[str, Any]],
    mode: str,
    check_env: Callable[[str, dict[str, Any] | None], list[str]],
    check_quota: Callable[[str, str], bool],
) -> tuple[bool, str]:
    """检查单个目标引擎是否可用，返回 (ready, unavailable_reason)。"""
    spec = engines_cfg.get(engine_id)
    if not isinstance(spec, dict) or not spec.get("enabled", True):
        return False, f"{engine_id} 未启用"
    missing = check_env(engine_id, spec)
    if missing:
        return False, f"{engine_id} 缺少环境变量 {', '.join(missing)}"
    if not check_quota(engine_id, mode):
        return False, f"{engine_id} 配额或月度预算已耗尽"
    return True, ""


def resolve_intent(
    intent: str,
    *,
    engine: str = "auto",
    domain: str | None = None,
    depth: str = "fast",
    mode: str = "auto",
    get_engines_fn: Callable[[], dict[str, dict[str, Any]]] | None = None,
    check_env_fn: Callable[[str, dict[str, Any] | None], list[str]] | None = None,
    check_quota_fn: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """将高层意图解析为 (engine, domain, depth) 与可审计的降级说明。"""
    if intent not in SUPPORTED_INTENTS:
        raise ValueError(
            f"不支持的意图 {intent!r}，可选: {', '.join(SUPPORTED_INTENTS)}"
        )

    explicit_engine = bool(engine and engine != "auto")
    resolved_engine = engine or "auto"
    resolved_domain = domain or None
    resolved_depth = depth or "fast"
    degraded = False
    reason = ""
    warnings: list[str] = []

    if intent == "academic":
        if not resolved_domain:
            resolved_domain = "academic"
        if resolved_depth == "fast":
            resolved_depth = "deep"
        reason = "学术多源模式（domain=academic, depth=deep）"
        if explicit_engine:
            warnings.append(f"--engine {engine} 覆盖了 --intent {intent} 的默认多源路由")
        return _pack(
            intent, resolved_engine, resolved_domain, resolved_depth,
            degraded, reason, explicit_engine, warnings,
        )

    if intent == "social":
        if not resolved_domain:
            resolved_domain = "social"
        reason = "社交与社区垂直域（domain=social）"
        if explicit_engine:
            warnings.append(f"--engine {engine} 覆盖了 --intent {intent} 的默认多源路由")
        return _pack(
            intent, resolved_engine, resolved_domain, resolved_depth,
            degraded, reason, explicit_engine, warnings,
        )

    if explicit_engine:
        warnings.append(f"--engine {engine} 覆盖了 --intent {intent} 的默认引擎选择")
        if intent == "news" and not resolved_domain:
            resolved_domain = "news"
        return _pack(
            intent, resolved_engine, resolved_domain, resolved_depth,
            False, f"已由显式 --engine {engine} 覆盖", True, warnings,
        )

    engines_cfg = (get_engines_fn or _default_get_engines)()
    check_env = check_env_fn or _default_missing_env
    check_quota = check_quota_fn or _default_quota_available

    if intent == "news":
        if mode == "fast":
            degraded = True
            resolved_domain = resolved_domain or "news"
            reason = "mode=fast 跳过付费时事源 deepseek_search，已降级为免费新闻自动路由"
            warnings.append(f"[intent:news] {reason}")
        else:
            ok, why = _engine_ready("deepseek_search", engines_cfg, mode, check_env, check_quota)
            if ok:
                resolved_engine = "deepseek_search"
                reason = "时事意图自动命中 deepseek_search（密钥与预算均可用）"
            else:
                degraded = True
                resolved_domain = resolved_domain or "news"
                reason = f"{why}，已降级为自动路由（domain=news）"
                warnings.append(f"[intent:news] {reason}")

    elif intent == "image-reverse":
        usable: list[str] = []
        block_reasons: list[str] = []
        for cand in _REVERSE_IMAGE_CANDIDATES:
            ok, why = _engine_ready(cand, engines_cfg, mode, check_env, check_quota)
            if ok:
                usable.append(cand)
            elif why:
                block_reasons.append(why)
        if usable:
            chosen = usable[:2]
            resolved_engine = ",".join(chosen)
            reason = f"反搜图自动选取可用源: {resolved_engine}"
        else:
            degraded = True
            reason = f"反搜图候选源均不可用（{'; '.join(block_reasons[:3])}），已降级为自动路由"
            warnings.append(f"[intent:image-reverse] {reason}")

    elif intent == "calc":
        ok, why = _engine_ready("wolframalpha", engines_cfg, mode, check_env, check_quota)
        if ok:
            resolved_engine = "wolframalpha"
            reason = "计算意图自动命中 wolframalpha"
        else:
            degraded = True
            reason = f"{why}，已降级为自动路由"
            warnings.append(f"[intent:calc] {reason}")

    return _pack(
        intent, resolved_engine, resolved_domain, resolved_depth,
        degraded, reason, False, warnings,
    )


def _pack(
    intent: str,
    engine: str,
    domain: str | None,
    depth: str,
    degraded: bool,
    reason: str,
    overridden: bool,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "intent": intent,
        "engine": engine,
        "domain": domain,
        "depth": depth,
        "degraded": degraded,
        "reason": reason,
        "overridden_by_engine": overridden,
        "warnings": warnings,
    }
