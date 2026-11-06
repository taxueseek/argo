#!/usr/bin/env python3
"""straggler_policy.py — 收尾尾宽的取值策略（2026-10-16 从 search.py 拆出）。

拆出原因：取值函数是**纯策略**（读开关 + 读自适应学习器 + 分档表），不
依赖 search.py 的任何东西；放在 search.py 里只为「和其他旋钮常量做邻居」。
search.py 是被 module size 门禁盯着的文件（1000 行硬限），本次拆出顺带
把尾宽策略的判据（为什么是 0.7/0.4、fail-open 契约、默认关的理由）集中到
一处，调用侧只留一行接线。

默认关（ARGO_ADAPTIVE_TAIL=1 开启）：理由是**本开关自己的**交错 A/B 未证实
收益——2026-10-16 三轮对拍见
docs-local/调度对拍_尾宽分层与并发_2026-10-16.md：墙钟有改善但统计不显著，
按「调度旋钮的收益必须由 A/B 证明后才转默认开」的规矩保持关闭。
别拿 ARGO_ADAPTIVE_GRACE 的现状作类比：那个 2026-10-19（c52b7d4）已转默认
开，两者的 A/B 结论不同，各自独立判断。
"""

from __future__ import annotations

__all__ = ["straggler_tail"]


def straggler_tail(engine: str, base: float) -> float:
    """尾宽按 backup 引擎历史 P75 分层（ARGO_ADAPTIVE_TAIL 覆盖，默认关）。

    固定 1.5s 尾宽对一切 backup 一视同仁，而 backup 的 P75 差异极大：
    redskill 236ms、eastmoney 258ms 这类等 1.5s 几乎必到（耐心有回报），
    anysearch P75 2.6s 等 1.5s 只有约五成概率——等不到就是纯墙钟。消融实测
    （2026-10-15，ARGO_STRAGGLER_GRACE_S 1.5 → 0.5 → 0）：样本墙钟
    2581 → 2260 → 1873ms（-12% / -27%），证明尾宽是有效杠杆且当前值是
    拍的常数。分层让「值得等的等满、不值得等的早收工」：
      P75 ≤ ½·base（快源）  → base       尾窗内几乎必到，等满不损质量
      ½·base < P75 ≤ 1.5·base → 0.7·base 中速略收
      P75 > 1.5·base（慢源） → 0.4·base（下限 0.3s）大概率白等，早收工

    P75 取 adaptive.latency_percentile（7 天窗、仅 success 且非空交付，
    与 engine_dispatch._adaptive_grace 同源同口径）；样本不足/过时/任何
    异常 → 原样返回 base（fail-open，观测层不阻断调度主路径）。
    """
    if base <= 0 or not engine:
        return base
    try:
        from engine_env import env_flag
        if not env_flag("ARGO_ADAPTIVE_TAIL", False):
            return base
    except Exception:
        return base
    try:
        from adaptive import get_learner
        learner = get_learner()
        p75_ms = learner.latency_percentile(engine, 0.75)
        score = getattr(learner, "get_score", lambda _e: None)(engine)
    except Exception:
        return base
    if not p75_ms or p75_ms <= 0:
        return base
    p75 = float(p75_ms) / 1000.0
    if p75 <= base * 0.5:
        return base
    tail = base * 0.7 if p75 <= base * 1.5 else max(0.3, base * 0.4)
    # PB-HOS 期望价值调制：低健康分（<0.35）慢源进一步收紧尾窗（保底 0.3s）
    if isinstance(score, (int, float)) and not isinstance(score, bool) and 0.0 < score < 0.35:
        tail = max(0.3, round(tail * 0.75, 4))
    return tail
