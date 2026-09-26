#!/usr/bin/env python3
"""route_cache.py — 路由决策的跨进程缓存（存储层）。

为什么需要（2026-09-17 实测）：route_query 的**首次调用**要付约 103 ms 的
进程级初始化——238 条域正则编译 33 ms（而跑完全部匹配只要 0.2 ms）、惰性导入、
引擎环境/准入与 TF-IDF 装载；同进程内的后续调用只要 3.7 ms。CLI 每次调用都是
新进程，于是这笔启动税每次重付。实测：跳过一次 route_query 后，缓存命中的
一次完整搜索只要 26 ms（对比 route 首调 137 ms + 执行 3 ms）。

判据为什么与 config 磁盘缓存不同：那一层的产物被当作**事实**（db_path 等），
必须逐字节正确，所以用内容摘要；这一层的产物是**优化结果**，偏差的后果只是
一段时间内引擎排序不最优，且有 TTL 与下游失败分型兜底，故用 config_stamp()
这个既有的 mtime 综合戳（registry 热加载同款）——键计算从约 5 ms 降到 0.1 ms。

本模块只管**存储**（键、指纹、读写、清理）；「什么时候用缓存」是编排决策，
留在 route.route_query_cached——那是它唯一的调用方，放这里会绕成
route_cache → route → route_cache 的循环导入。
"""

from __future__ import annotations

import time
from typing import Any

# ── 路由决策缓存（跨进程） ─────────────────────────────────────────────────────
#
# 为什么需要（2026-09-17 实测）：route_query 的**首次调用**要付约 103 ms 的
# 进程级初始化——238 条域正则编译 33 ms（而跑完全部匹配只要 0.2 ms）、惰性导入、
# 引擎环境/准入与 TF-IDF 装载；同进程内的后续调用只要 3.7 ms。CLI 每次调用都是
# 新进程，于是这笔启动税每次重付。实测：跳过一次 route_query 后，缓存命中的
# 一次完整搜索只要 26 ms（对比 route 首调 137 ms + 执行 3 ms）。
#
# 判据为什么与 config 磁盘缓存不同：那一层的产物被当作**事实**（db_path 等），
# 必须逐字节正确，所以用内容摘要；这一层的产物是**优化结果**，偏差的后果只是
# 一段时间内引擎排序不最优，且有 TTL 与下游失败分型兜底，故用 config_stamp()
# 这个既有的 mtime 综合戳（registry 热加载同款）——键计算从约 5 ms 降到 0.1 ms。

_ROUTE_CACHE_SCHEMA = 1
# TTL 只兜自适应学习器（adaptive.db）的渐进漂移——影响路由的持久状态
# （config 改动 / 额度耗尽 / 熔断禁用）都在指纹里，变了键就换。原值 300 s
# 让隔了几分钟的重复查询白付整笔 route_query 启动税（实测 130–300 ms），
# 而指纹盖不住的那点排序漂移在一小时内不构成路由错误，放宽到 1 h。
_ROUTE_CACHE_TTL_S = 3600.0
_ROUTE_CACHE_MAX_ENTRIES = 200

# _route_cache_read 的进程内解析缓存：(mtime_ns, size) -> entries。None = 未缓存。
# 声明在此而非函数内，是为了让「这个模块有一个可变全局」这件事在阅读时可见。
_READ_MEMO: tuple[tuple[int, int], dict[str, Any]] | None = None


def _route_cache_enabled() -> bool:
    """ARGO_ROUTE_CACHE=0/false/no/off 关闭；判定链不可用时按「开」处理。

    缓存是纯性能优化，关掉不影响正确性；判不开时维持既有行为（每次都实算）。
    """
    try:
        from engine_env import env_flag
        return env_flag("ARGO_ROUTE_CACHE", default=True)
    except Exception:
        return True


def _route_cache_file():
    import argo_paths
    return argo_paths.state_path("route-cache.json")


def _route_state_fingerprint() -> str:
    """影响路由决策的可变状态摘要；取不到就返回空串（等价于不用缓存）。

    - `config_stamp()`：config.yaml 与外置声明的 mtime，覆盖 enabled / domains /
      engines_combo 的改动。
    - 配额与熔断取**派生集合**（已耗尽额度 / 已自动禁用），不取状态文件的字节或
      mtime。为什么：`quota.json` 每次运行都会被状态机重写（mtime 必变），文件里的
      用量计数也随每次搜索变动——**实测拿 mtime 做摘要会让缓存 100% 失效**
      （第二次调用就换了键）；而真正改变路由结果的只是「哪些源现在不可用」这个
      集合，它只在源真的挂掉或恢复时变化。

    刻意**不含** adaptive.db：自适应学习器每次搜索都写它，同理会让缓存立即失效；
    它只影响引擎排序的软信号、变化渐进，由 TTL 兜住。

    这是**粗粒度**信号：覆盖「源挂了 / 被禁」这类持久状态，瞬时节流
    （rpm 抖动）不在其中，由 TTL 兜住。空串判据与 config 磁盘缓存 digest 取不到
    时的保守选择一致：空摘要永不等于任何已存条目的键，因此不会读到旧结论。

    刻意**不含** quota marks：额度耗尽是 per-engine 信号，已在 _get_engines_combo
    和 _run_one 层面检查；放进指纹会让单个引擎额度变化使所有查询的路由缓存
    失效——实测这是路由缓存命中率最大的敌人。
    """
    try:
        from config import config_stamp
        parts = [f"cfg={config_stamp():.0f}"]
    except Exception:
        return ""
    try:
        from circuit_breaker import get_breaker
        parts.append("cb=" + ",".join(sorted(get_breaker().auto_disabled())))
    except Exception:
        return ""
    return "|".join(parts)


def _route_cache_key(query: str, engine_override: str, mode: str, depth: str,
                     context: str, engines_boost: list[str] | None,
                     fingerprint: str) -> str:
    import hashlib
    import json
    raw = json.dumps({
        "v": _ROUTE_CACHE_SCHEMA,
        # 归一化空白：同一问题多打几个空格不该是两次路由
        "q": " ".join(str(query or "").split()),
        "eo": engine_override or "auto",
        "mode": mode, "depth": depth, "context": context,
        "boost": [str(b) for b in (engines_boost or [])],
        "fp": fingerprint,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _extract_entries(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != _ROUTE_CACHE_SCHEMA:
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def _route_cache_read() -> dict[str, Any]:
    import json
    # 进程内 memo：一次 argo 调用里 _route_cache_read 可能被调多次，而每次都
    # 全量重解析整份 JSON（实测 24 条目 ≈ 0.4 ms，条目满 200 条时线性放大）。
    # 以 mtime_ns + size 为失效键——写路径走 os.replace 原子替换（换 inode、
    # 换 mtime），所以不会读到半写状态；跨进程则由 mtime 变化自然穿透。
    global _READ_MEMO
    f = _route_cache_file()
    try:
        st = f.stat()
    except OSError:
        _READ_MEMO = None
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    if _READ_MEMO is not None and _READ_MEMO[0] == stamp:
        return _READ_MEMO[1]
    try:
        entries = _extract_entries(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    _READ_MEMO = (stamp, entries)
    return entries


def _route_cache_prune(entries: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    fresh = {k: v for k, v in entries.items()
             if isinstance(v, dict)
             and now - float(v.get("ts") or 0) <= _ROUTE_CACHE_TTL_S}
    if len(fresh) > _ROUTE_CACHE_MAX_ENTRIES:
        newest = sorted(fresh.items(),
                        key=lambda kv: float(kv[1].get("ts") or 0), reverse=True)
        fresh = dict(newest[:_ROUTE_CACHE_MAX_ENTRIES])
    return fresh


def _route_cache_write(entries: dict[str, Any]) -> None:
    import argo_paths
    global _READ_MEMO
    try:
        argo_paths.atomic_write_json(
            _route_cache_file(),
            {"schema": _ROUTE_CACHE_SCHEMA, "entries": entries},
            indent=None,
        )
        # mtime_ns 理论上会穿透 memo，但同秒内两次写入的 mtime 粒度差异
        # 在部分文件系统上不足 1ms。显式作废是零成本的那道保险。
        _READ_MEMO = None
    except Exception:
        return


def invalidate_route_cache() -> bool:
    """删除磁盘上的路由决策缓存（测试隔离与显式失效用）。"""
    global _READ_MEMO
    _READ_MEMO = None
    try:
        _route_cache_file().unlink()
        return True
    except OSError:
        return False
