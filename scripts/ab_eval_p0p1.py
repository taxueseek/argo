#!/usr/bin/env python3
"""P0/P1 实测脚本：路由精度、冷/热延迟、缓存柔性、熔断负缓存、SERP。"""

from __future__ import annotations

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from route import route_query
from search import super_search, rrf_merge
from cache import SearchCache
from evidence import is_serp_or_jump_url, score_authority
from circuit_breaker import CircuitBreaker


def section(title: str):
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def main():
    report: dict = {"cases": [], "pass": 0, "fail": 0}

    def check(name: str, cond: bool, detail: str = ""):
        status = "PASS" if cond else "FAIL"
        report["pass" if cond else "fail"] += 1
        report["cases"].append({"name": name, "ok": cond, "detail": detail})
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    # ── 1. 路由 ────────────────────────────────────────────────────────────
    section("1. 路由精度（P0 零分回退）")
    cases = [
        ("pytest fixtures", None, {"eastmoney", "bilibili", "twitter", "weibo"}),
        ("React hooks tutorial", None, {"eastmoney", "bilibili", "twitter", "weibo"}),
        ("贵州茅台股价", "eastmoney", set()),
        ("基金净值", "eastmoney", set()),
        ("transformer attention paper", None, {"eastmoney", "bilibili"}),
    ]
    for q, expect_eng, forbid in cases:
        d = route_query(q)
        eng = d["engine"]
        detail = f"engine={eng} domain={d.get('domain')} reason={str(d.get('reason'))[:60]}"
        if expect_eng:
            check(f"route:{q[:20]}", eng == expect_eng, detail)
        else:
            check(f"route_not_vertical:{q[:20]}", eng not in forbid, detail)

    # ── 2. 缓存 depth / soft n ───────────────────────────────────────────
    section("2. 缓存契约（depth 隔离 + 柔性命中）")
    import tempfile
    db = os.path.join(tempfile.mkdtemp(), "ab.db")
    c = SearchCache(db_path=db)
    c.set("abq", "e1", 5, {"results": [{"title": str(i), "url": f"u{i}"} for i in range(5)]},
          domain="general", mode="auto", depth="fast")
    check("depth_fast_hit", c.get("abq", "e1", 5, depth="fast") is not None)
    check("depth_deep_miss", c.get("abq", "e1", 5, depth="deep") is None)
    check("soft_n_3", c.get("abq", "e1", 3, depth="fast") is not None
          and len(c.get("abq", "e1", 3, depth="fast")["results"]) == 3)
    check("soft_n_10_miss", c.get("abq", "e1", 10, depth="fast") is None)

    # ── 3. SERP ────────────────────────────────────────────────────────────
    section("3. SERP 覆盖")
    check("serp_bing", is_serp_or_jump_url("https://www.bing.com/search?q=a"))
    check("serp_google", is_serp_or_jump_url("https://www.google.com/search?q=a"))
    check("serp_sogou_wx", is_serp_or_jump_url("https://weixin.sogou.com/weixin?type=2&query=a"))
    check("not_serp_em", not is_serp_or_jump_url(
        "https://finance.eastmoney.com/a/202607213815797914.html"))
    check("source_floor", score_authority("https://rand.test/x", "eastmoney")["score"] >= 0.85)

    # ── 4. RRF consensus ───────────────────────────────────────────────────
    section("4. RRF consensus_engines")
    m = rrf_merge([
        [{"url": "http://same", "title": "t", "_engine": "a", "source": "a"}],
        [{"url": "http://same", "title": "t", "_engine": "b", "source": "b"}],
    ])
    cons = m[0].get("consensus_engines") or []
    check("consensus", set(cons) >= {"a", "b"}, str(cons))

    # ── 5. 熔断 ────────────────────────────────────────────────────────────
    section("5. 熔断器")
    cb_path = os.path.join(tempfile.mkdtemp(), "cb.json")
    cb = CircuitBreaker(state_path=cb_path)
    e = "ab_eval_dead"
    cb.record_failure(e, "timeout")
    cb.record_failure(e, "timeout")
    check("circuit_opens", not cb.allow(e)[0], cb.allow(e)[1])

    # ── 6. 端到端冷/热 ─────────────────────────────────────────────────────
    section("6. 端到端搜索（冷/热 + outcomes）")
    q = "Python dataclasses tutorial"
    # 确保路由不进东财
    d = route_query(q, mode="fast")
    check("e2e_route_not_em", d["engine"] != "eastmoney", d["engine"])

    # 冷启动（写缓存）→ 热命中 L1 → 再命中
    t0 = time.time()
    r1 = super_search(q, n=3, mode="fast", depth="fast", skip_cache=False, timeout=12)
    cold = int((time.time() - t0) * 1000)
    t1 = time.time()
    r2 = super_search(q, n=3, mode="fast", depth="fast", skip_cache=False, timeout=12)
    warm1 = int((time.time() - t1) * 1000)
    t2 = time.time()
    r3 = super_search(q, n=3, mode="fast", depth="fast", skip_cache=False, timeout=12)
    warm2 = int((time.time() - t2) * 1000)

    print(f"  cold_ms={cold} warm1_ms={warm1} warm2_ms={warm2}")
    print(f"  cold_engine={r1.get('engine')} count={r1.get('count')} "
          f"reranker={r1.get('reranker')} wasted={r1.get('wasted_engine_ms')}")
    print(f"  outcomes={json.dumps(r1.get('engine_outcomes'), ensure_ascii=False)[:300]}")
    print(f"  warm_cached={r2.get('cached')}/{r3.get('cached')} "
          f"level={r2.get('cache_level')}/{r3.get('cache_level')}")

    check("e2e_has_outcomes", isinstance(r1.get("engine_outcomes"), list))
    check("e2e_reranker_skipped_fast", r1.get("reranker") == "skipped_fast",
          str(r1.get("reranker")))
    check("e2e_warm_cached", bool(r2.get("cached") and r3.get("cached")),
          f"r2={r2.get('cached')} r3={r3.get('cached')}")
    check("e2e_warm_faster", warm1 < max(cold * 0.2, 50) or warm1 < 50,
          f"warm1={warm1} cold={cold}")
    # n 柔性：请求更少应命中
    r4 = super_search(q, n=2, mode="fast", depth="fast", skip_cache=False, timeout=12)
    check("e2e_soft_n", bool(r4.get("cached")) and r4.get("count", 0) <= 2,
          f"cached={r4.get('cached')} count={r4.get('count')}")

    # 负缓存：对故意空引擎二次调用
    # 用一个几乎必空的垂直引擎强制路径验证 circuit 模块可用
    from circuit_breaker import get_breaker
    br = get_breaker()
    br.set_negative(q, "eastmoney", status="no-results", ttl=60)
    neg = br.get_negative(q, "eastmoney")
    check("e2e_neg_cache_set", neg is not None, str(neg))

    report["latency"] = {"cold_ms": cold, "warm1_ms": warm1, "warm2_ms": warm2}
    report["sample"] = {
        "query": q,
        "engine": r1.get("engine"),
        "count": r1.get("count"),
        "wasted_engine_ms": r1.get("wasted_engine_ms"),
        "reranker": r1.get("reranker"),
        "engine_outcomes": r1.get("engine_outcomes"),
    }

    section("汇总")
    total = report["pass"] + report["fail"]
    print(f"  PASS={report['pass']} FAIL={report['fail']} TOTAL={total}")
    print(json.dumps({"latency": report["latency"], "sample": report["sample"]},
                     ensure_ascii=False, indent=2))
    return 0 if report["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
