#!/usr/bin/env python3
"""engine_health.py — 引擎运行时健康画像（失效证据的一等公民化）。

第一性原理：Argo 的每次真实搜索都是一次对引擎的免费真实调用，失败证据
（熔断归因、空结果、开闸）每天都在熔断器里落盘。本模块**不发起任何网络
请求**，只把已发生的失败变成可积累、可见、会分诊的知识——发现死源靠
「每次被踩到都记账且可见」，不靠事前探测（探测有 58/80 伪报问题，实测
2026-11-08；画像只记真实发生的失败，无狼来了）。

数据源（全部零请求）：circuit_breaker 的持久化状态
（failures/opens/last_attribution/suspected_stale/auth_disabled）。

分诊口径（复用 engine_failure 的 category 语义，不另造分类）：
  auth_down      auth/quota 归因直达禁用——修凭证或改代码，`argo engine revive` 复活
  suspect        空结果基线破位——历史常非空、近期连续空且跨查询，疑似失效
                 （如上游退役被吞成空结果：met_museum /search 410 死 5 周实例）
  auto_disabled  连续 error/timeout 爬满开闸被自动禁用——1h 后自动探测回归
  flapping       当前处于 open 冷却——瞬时类，60s 后自动探测
  watch          近期有失败但未熔断
  unknown        熔断器无该引擎记录（低流量源从未被踩到：如实标「无数据」，
                 不假装健康）

纪律：只读不写（不推进 half-open 探测、不改状态）；本地状态不出本机——
画像只进 `argo stats` 与本 CLI，不进公开仓文档（catalog 保持环境中立）。

用法：
  python3 scripts/engine_health.py            # 文本摘要
  python3 scripts/engine_health.py --json     # 供脚本消费
  python3 scripts/engine_health.py --engine met_museum
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_io import dumps  # noqa: E402
from circuit_breaker import AUTH_DISABLE_AFTER  # noqa: E402
# 状态词表唯一真源（2026-11-09 收口）：verdict / breaker state
import engine_states


def _verdict(st: dict[str, Any]) -> tuple[str, str]:
    """单引擎分诊（纯函数）：输入熔断器状态，输出 (verdict, 一句人话)。

    判定顺序即严重度：auth_down > suspect > auto_disabled > flapping > watch。
    """
    state = st.get("state", engine_states.BREAKER_CLOSED)
    # 阈值引用熔断器常量（不手抄）：legacy 态（disabled 无 auth_disabled 标志）
    # 的判线必须与真实禁用线永远一致，否则恢复指引给错。
    if st.get("auth_disabled") or (state == engine_states.BREAKER_DISABLED
                                   and st.get("auth_streak", 0) >= AUTH_DISABLE_AFTER):
        cat = (st.get("last_attribution") or {}).get("category") or engine_states.FAILURE_AUTH
        return (engine_states.HEALTH_AUTH_DOWN,
                f"结构性失败（{cat}）已停用：修凭证/改代码后 "
                f"`argo engine revive <name>` 或换 key 自动恢复")
    if st.get("suspected_stale"):
        return (engine_states.HEALTH_SUSPECT,
                "疑似失效：历史常非空，近期连续空结果且跨查询（上游退役/改版"
                "被吞成空结果的典型形态），已降权；一次真实成功即解除")
    if state == engine_states.BREAKER_DISABLED:
        return (engine_states.HEALTH_AUTO_DISABLED,
                "连续失败达阈值已自动禁用；1h 后自动探测回归，"
                "`argo engine revive <name>` 可提前复活")
    if state == engine_states.BREAKER_OPEN:
        return (engine_states.HEALTH_FLAPPING,
                "处于熔断冷却（瞬时类，60s 后自动探测）")
    failures = int(st.get("failures") or 0)
    if failures > 0:
        return (engine_states.HEALTH_WATCH, f"近期有 {failures} 次失败（未熔断），观察")
    return (engine_states.HEALTH_UNKNOWN, "无失败记录（低流量或一直健康；证据不足不下结论）")


def picture() -> dict[str, dict[str, Any]]:
    """全引擎运行时画像：只含熔断器里有证据的引擎（无证据不进画像）。

    逐引擎走 status() 而不是 snapshot()：suspected_stale / auth_streak 是
    status() 里的**派生字段**（从 empty_recent 现算），snapshot 的原始状态
    里没有——画像若读原始态会把破位源显示成正常（2026-11-08 实测抓到）。
    """
    from circuit_breaker import get_breaker
    br = get_breaker()
    out: dict[str, dict[str, Any]] = {}
    for eng in br.snapshot().keys():
        st = br.status(eng)
        verdict, reason = _verdict(st)
        if verdict == engine_states.HEALTH_UNKNOWN and not st.get("failures"):
            # 无失败记录的 closed 条目（record_success 留下的干净态）不进画像：
            # 画像的价值密度来自「有证据的引擎」，全量罗列 290 个 healthy 是噪声
            continue
        out[eng] = {
            "verdict": verdict,
            "reason": reason,
            "state": st.get("state", engine_states.BREAKER_CLOSED),
            "failures": int(st.get("failures") or 0),
            "opens": int(st.get("opens") or 0),
            "auth_streak": int(st.get("auth_streak") or 0),
            "last_kind": st.get("last_kind"),
            "last_attribution": st.get("last_attribution"),
            "last_fail": st.get("last_fail"),
            "suspected_stale": bool(st.get("suspected_stale")),
            "auth_disabled": bool(st.get("auth_disabled")),
        }
    return out


def summary() -> dict[str, Any]:
    """分诊汇总：按严重度排序的问题源 + 各类计数。"""
    pic = picture()
    counts: dict[str, int] = {}
    for v in pic.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    offenders = sorted(
        pic.items(),
        key=lambda kv: (engine_states.health_severity_index(kv[1]["verdict"]),
                        -(kv[1].get("failures") or 0)))
    return {
        "generated_at": time.time(),
        "engines_with_evidence": len(pic),
        "counts": counts,
        # 只列非 watch 的（watch 是噪声底），watch 总数给个数字就够
        "offenders": [
            {"engine": eng, **info} for eng, info in offenders
            if info["verdict"] != engine_states.HEALTH_WATCH
        ],
        "watch_count": counts.get(engine_states.HEALTH_WATCH, 0),
    }


def _render_text(rep: dict[str, Any]) -> str:
    lines: list[str] = []
    counts = rep["counts"]
    if not counts:
        lines.append("熔断器无任何失效证据（全部引擎健康或从未被踩到）。")
        return "\n".join(lines)
    head = "、".join(f"{k} {v}" for k, v in counts.items())
    lines.append(f"引擎运行时画像（零请求，来自熔断器落盘）：{head}")
    for o in rep["offenders"]:
        attr = o.get("last_attribution") or {}
        detail = str(attr.get("detail") or attr.get("reason") or "")[:60]
        last = time.strftime("%m-%d %H:%M", time.localtime(o["last_fail"])) \
            if o.get("last_fail") else "?"
        lines.append(f"  [{o['verdict']}] {o['engine']}  "
                     f"fail={o['failures']} opens={o['opens']} last={last}"
                     + (f"  {detail}" if detail else ""))
        lines.append(f"      {o['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="argo engine-health",
        description="引擎运行时健康画像（零请求，来自熔断器落盘证据）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--engine", default=None,
                    help="只看指定引擎（诊断单源；默认全量分诊）")
    args = ap.parse_args(argv)
    rep = summary()
    if args.engine:
        rep["offenders"] = [o for o in rep["offenders"]
                            if o.get("engine") == args.engine]
        single = picture().get(args.engine)
        rep["single"] = single or {
            "verdict": engine_states.HEALTH_UNKNOWN,
            "reason": "熔断器无该引擎的失效证据（低流量或一直健康）",
        }
        if args.json:
            print(dumps(rep))
        else:
            s = rep["single"]
            print(f"[{s['verdict']}] {args.engine}\n  {s['reason']}")
        return 0
    print(dumps(rep) if args.json else _render_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
