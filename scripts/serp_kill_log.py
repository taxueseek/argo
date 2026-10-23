#!/usr/bin/env python3
"""serp_kill_log — serp_guard 整页误杀的运行内账本（2026-10-10 方案甲）。

为什么单开一个模块：engines_base 是祖父文件、行数顶在登记上限，尺寸门的
哲学是「要加东西先拆模块」。_serp_guard_apply 的判定逻辑随本账本一起搬来，
engines_base 留薄委托保持原导入面不变。

背景：_serp_guard_apply 对整页垃圾返回 [] 是「诚实空」（结果无关不进熔断
归因、不伪装成成功），但对调用方等价于「引擎没搜到」——误杀类缺陷（搜狗
跳转壳 10-06、百家号 10-08）都在这里静默发生过。击杀先入账本，search 层
执行完 drain 进 errors：可见、可归因、可用 ARGO_SERP_GUARD=0 复核。
"""
from __future__ import annotations

import threading
from typing import Any

# 判定规则与逃生门（ARGO_SERP_GUARD=0）在 serp_guard 单点；这里只登记
# 哪些引擎接了守卫（解析上游 HTML 页 / 模糊搜索接口最易被缓存 SERP 或
# 默认 feed 静默命中）。people_daily 的 isFuzzy=True 在匹配不到时返回整页
# 最新 feed（issue #18 复现 A），与 local_* 同属「答非所问但形态正常」。
SERP_GUARD_ENGINES = frozenset({
    "local_bing", "local_baidu", "local_sogou", "people_daily",
})

# 进程级 + 锁。并发搜索下 drain 可能拿到彼此的条目——条目只含引擎名与
# 条数、无查询内容，跨运行误挂是无害的信息性噪音。
_LOCK = threading.Lock()
_KILLS: list[dict[str, Any]] = []


def serp_guard_apply(engine_name: str, query: str,
                     results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """守卫接线：命中引擎且整页判垃圾 → 记账后返回 []（诚实空语义不变）。"""
    if not results or engine_name not in SERP_GUARD_ENGINES:
        return results
    try:
        from serp_guard import is_junk_serp
    except ImportError:
        return results  # fail-open：守卫自身故障不放大成搜索故障
    if is_junk_serp(query, results):
        with _LOCK:
            _KILLS.append({"engine": engine_name, "results": len(results)})
        return []
    return results


def drain_serp_guard_kills() -> list[dict[str, Any]]:
    """取走并清空本进程累计的整页击杀记录（search 层专用）。"""
    with _LOCK:
        kills = list(_KILLS)
        _KILLS.clear()
        return kills


def clean_engine_lists(raw_results: dict[str, list[Any]],
                       query: str) -> list[list[dict[str, Any]]]:
    """raw_results{engine → results} → 去 error + SERP 守卫后的非空列表。

    守卫必须同时覆盖缓存命中：per-engine 缓存走 engine_dispatch，不经过
    engines.search，旧条目里的默认 feed/垃圾 SERP 会以 `ok-cached` 直接
    返回（issue #18 的 14ms 复现）。融合前按引擎整页判定一次，缓存与网络
    两条路径就收敛到同一口径；engines.search 里的守卫保留，因为它还要
    阻止垃圾结果影响早停判定。
    """
    out: list[list[dict[str, Any]]] = []
    for eng, res in (raw_results or {}).items():
        goods = [r for r in (res or []) if isinstance(r, dict) and "error" not in r]
        if not goods:
            continue
        goods = serp_guard_apply(eng, query, goods)
        if goods:
            out.append(goods)
    return out
