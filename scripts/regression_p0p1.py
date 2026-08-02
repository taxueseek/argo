#!/usr/bin/env python3
"""regression_p0p1.py — P0/P1 策略与搜索回测 harness

用法：
  # 仅离线（路由 + engine_policy，无网络）
  python scripts/regression_p0p1.py --offline

  # 含联网回测（答案域延迟 + 主源准确率）
  python scripts/regression_p0p1.py --live

  # 全部
  python scripts/regression_p0p1.py --all

退出码：有 FAIL 则为 1。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class Checker:
    def __init__(self) -> None:
        self.ok = 0
        self.fail = 0
        self.rows: list[dict[str, Any]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok += 1
            status = "PASS"
        else:
            self.fail += 1
            status = "FAIL"
        self.rows.append({"name": name, "status": status, "detail": detail})
        mark = "✓" if cond else "✗"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


def run_offline(c: Checker) -> None:
    print("\n== offline: engine_policy ==")
    from engine_policy import (
        boost_into_combo,
        combo_budget,
        filter_combo_by_policy,
        is_research_context,
        research_engine_hints,
    )

    c.check("budget_fast_mode", combo_budget(mode="fast", depth="balanced") == 2)
    c.check("budget_depth_fast", combo_budget(mode="auto", depth="fast") == 2)
    c.check("budget_auto_balanced", combo_budget(mode="auto", depth="balanced") == 3)
    c.check(
        "budget_research_none",
        combo_budget(mode="auto", depth="balanced", context="research") is None,
    )
    c.check("budget_deep_none", combo_budget(mode="auto", depth="deep") is None)
    c.check("is_research_ctx", is_research_context(context="research") is True)
    c.check("is_not_daily", is_research_context(mode="auto", depth="fast") is False)

    combo = ["finviz", "seeking_alpha", "exa", "anysearch"]
    daily = filter_combo_by_policy(
        combo, mode="auto", depth="fast", context="search",
        tier_of=lambda e: "research_only" if e == "seeking_alpha" else "daily_core",
    )
    c.check(
        "filter_drop_research_only",
        "seeking_alpha" not in daily and daily[0] == "finviz",
        detail=str(daily),
    )
    c.check("filter_budget_2", len(daily) <= 2, detail=str(daily))

    research = filter_combo_by_policy(
        combo, mode="auto", depth="balanced", context="research",
        tier_of=lambda e: "research_only" if e == "seeking_alpha" else "daily_core",
    )
    c.check(
        "research_keeps_ro",
        "seeking_alpha" in research and len(research) == 4,
        detail=str(research),
    )

    boosted = boost_into_combo(["anysearch", "zhihu"], ["pubchem", "anysearch"])
    c.check("boost_head", boosted[:2] == ["pubchem", "anysearch"], detail=str(boosted))

    hints0 = research_engine_hints(
        {"vertical_engines": ["a", "b"], "engines_priority": ["c", "d"]}, 0,
    )
    c.check("hints_sub0_len", len(hints0) == 3 and hints0[0] == "a", detail=str(hints0))

    print("\n== offline: route accuracy ==")
    from route import route_query

    cases = [
        ("贵州茅台股价", "stock_query", "sina_quote"),
        ("AAPL 美股 盘前", "us_stock", "finviz"),
        ("阿司匹林 分子式", "chem_search", "pubchem"),
        ("中国 GDP", "macro_data", None),  # worldbank 可能前置
        ("US CPI", "macro_data", "fred"),
        ("RFC 9110", "rfc_search", "rfc_editor"),
    ]
    for q, domain, primary in cases:
        d = route_query(q, mode="auto", depth="fast", context="search")
        got_d = d.get("domain")
        got_e = d.get("engine")
        combo = d.get("engines_combo") or []
        ok_d = got_d == domain
        ok_e = True if primary is None else (got_e == primary or primary in combo[:2])
        # 中国 GDP：macro + worldbank 优先
        if q == "中国 GDP":
            ok_e = "worldbank" in combo[:2] or got_e == "worldbank"
        c.check(
            f"route:{q}",
            ok_d and ok_e,
            detail=f"domain={got_d} engine={got_e} combo={combo}",
        )
        # daily 预算：fast depth 最多 2（答案域意图裁剪可能为 1）
        c.check(
            f"budget:{q}",
            len(combo) <= 2,
            detail=f"len={len(combo)} combo={combo}",
        )
        # research_only 不进日常
        c.check(
            f"no_ro:{q}",
            "seeking_alpha" not in combo and "wayback_cdx" not in combo,
            detail=str(combo),
        )

    # research context：不截断 budget，可含 research_only
    d_res = route_query(
        "AAPL 美股", mode="auto", depth="balanced", context="research",
    )
    c_combo = d_res.get("engines_combo") or []
    c.check(
        "research_combo_longer",
        len(c_combo) >= 2,
        detail=str(c_combo),
    )

    # boost 前置
    d_b = route_query(
        "transformer paper",
        mode="auto",
        depth="deep",
        context="research",
        engines_boost=["arxiv", "semantic_scholar"],
    )
    bc = d_b.get("engines_combo") or []
    c.check(
        "boost_arxiv_first",
        bc and bc[0] == "arxiv",
        detail=str(bc),
    )

    # 用户指定引擎不受 budget 影响
    d_fix = route_query("x", engine_override="duckduckgo", mode="fast", depth="fast")
    c.check("override_intact", d_fix.get("engines_combo") == ["duckduckgo"])


def run_live(c: Checker) -> None:
    print("\n== live: answer-domain latency + accuracy ==")
    from search import super_search

    # (query, expected_domain_or_None, expected_source_substrs, max_ms_cold)
    cases: list[tuple[str, str | None, list[str], int]] = [
        ("贵州茅台股价", "stock_query", ["sina", "tencent", "eastmoney", "em_flow"], 3500),
        ("AAPL stock", "us_stock", ["finviz", "seeking", "exa", "anysearch"], 5000),
        ("阿司匹林 分子式", "chem_search", ["pubchem", "chem"], 8000),
        ("US CPI", "macro_data", ["fred", "worldbank", "nbs", "eurostat"], 5000),
    ]

    for q, domain, sources_ok, max_ms in cases:
        # 冷启动：写入缓存（skip_cache=False 才会 set）
        t0 = time.perf_counter()
        r = super_search(
            q, n=3, mode="auto", depth="fast",
            skip_cache=False, timeout=15, envelope=False,
        )
        ms = int((time.perf_counter() - t0) * 1000)
        results = r.get("results") or []
        used = r.get("engines_used") or []
        eng = (r.get("engine") or "").lower()
        domain_got = r.get("domain")
        src_blob = " ".join(
            str(x.get("source") or x.get("engine") or "") for x in results
        ).lower()
        src_blob += " " + " ".join(str(u).lower() for u in used) + " " + eng

        has_hit = any(s.lower() in src_blob for s in sources_ok)
        ok_domain = domain is None or domain_got == domain
        ok_n = len(results) >= 1
        # 首轮可能已命中历史缓存，用 max(实测, budget) 判断冷路径时放宽化学等慢 API
        ok_lat = ms <= max_ms or bool(r.get("cached"))

        c.check(
            f"live_domain:{q}",
            ok_domain,
            detail=f"got={domain_got}",
        )
        c.check(
            f"live_hit:{q}",
            ok_n and has_hit,
            detail=f"n={len(results)} used={used} ms={ms}",
        )
        c.check(
            f"live_lat:{q}",
            ok_lat,
            detail=f"{ms}ms (budget {max_ms}ms) early={r.get('early_stopped')} cached={r.get('cached')}",
        )

        # 暖缓存：同参数第二次应 L1/L2 命中
        t1 = time.perf_counter()
        r2 = super_search(
            q, n=3, mode="auto", depth="fast",
            skip_cache=False, timeout=15, envelope=False,
        )
        ms2 = int((time.perf_counter() - t1) * 1000)
        c.check(
            f"live_cache:{q}",
            bool(r2.get("cached")) or ms2 < 500,
            detail=f"cached={r2.get('cached')} ms={ms2}",
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    args = ap.parse_args()
    if not (args.offline or args.live or args.all):
        args.offline = True  # 默认离线

    c = Checker()
    if args.offline or args.all:
        run_offline(c)
    if args.live or args.all:
        run_live(c)

    print(f"\n== summary: {c.ok} PASS / {c.fail} FAIL ==")
    if args.json:
        print(json.dumps({"ok": c.ok, "fail": c.fail, "rows": c.rows}, ensure_ascii=False))
    return 1 if c.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
