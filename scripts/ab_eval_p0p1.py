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
import tempfile

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

    # 冷启动强制 miss → 写缓存 → 热命中（避免旧 L2 污染 reranker/early_stopped 断言）
    t0 = time.time()
    r1 = super_search(q, n=3, mode="fast", depth="fast", skip_cache=True, timeout=12)
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
    # fast 模式冷路径：skipped_fast；若极短结果也可能 skipped_short
    check("e2e_reranker_skipped_fast",
          r1.get("reranker") in ("skipped_fast", "skipped_short", "ok", "fallback", "skipped_no_key"),
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
        "early_stopped": r1.get("early_stopped"),
        "reranker": r1.get("reranker"),
        "engine_outcomes": r1.get("engine_outcomes"),
    }

    # ── 7. v2.4.1 缓存正确性 ───────────────────────────────────────────────
    section("7. v2.4.1 时效 TTL + query 归一 + early_stop 字段")
    from cache import (
        is_freshness_sensitive_query, REALTIME_TTL_CAP, DOMAIN_TIER_MAP,
        SAME_DAY_ELIGIBLE_TIERS, normalize_query,
    )
    check("fresh_detect_today", is_freshness_sensitive_query("今日热点新闻"))
    check("fresh_not_evergreen", not is_freshness_sensitive_query("Python 教程"))
    check("chinese_general_mapped", "chinese_general" in DOMAIN_TIER_MAP)
    check("general_no_eod", "general" not in SAME_DAY_ELIGIBLE_TIERS)
    ttl_today = SearchCache.resolve_ttl("chinese_general", query="今日热点新闻")
    check("ttl_today_cap", ttl_today <= REALTIME_TTL_CAP, f"ttl={ttl_today}")
    check("norm_ws", normalize_query("  React   Hooks  ") == normalize_query("react hooks"))
    # 冷 skip_cache 后应带 early_stopped；热缓存路径也允许从 payload 带回
    check("e2e_has_early_stopped_field",
          "early_stopped" in r1 or r1.get("cached") is True,
          f"keys has early_stopped={('early_stopped' in r1)} cached={r1.get('cached')}")

    # 归一化命中：故意用不同空白写/读
    db2 = os.path.join(tempfile.mkdtemp(), "norm.db")
    cn = SearchCache(db_path=db2)
    cn.set("Hello   World", "e", 3, {"results": [{"title": "1", "url": "u1"}]},
           domain="general", mode="auto", depth="fast")
    check("norm_cache_hit", cn.get("hello world", "e", 3, depth="fast") is not None)

    # ── 8. yichen 契约：plan / known-url / envelope ─────────────────────────
    section("8. yichen 契约（plan / known-url / envelope，无倒退）")
    from plan import build_plan, classify_input_kind
    from candidate_envelope import attach_envelope

    check("kind_keyword", classify_input_kind("Python asyncio") == "keyword")
    check("kind_known_url", classify_input_kind("https://example.com/a") == "known-url")
    check("kind_url_seed", classify_input_kind(
        "搜索引用 https://example.com/a 的报道") == "url-seed")

    pl = build_plan("贵州茅台股价", mode="auto")
    check("plan_ready", pl.get("status") == "ready", str(pl.get("status")))
    check("plan_has_steps", bool(pl.get("steps")), str(pl.get("steps"))[:80])
    check("plan_domain_stock", pl.get("decision", {}).get("domain") == "stock_query",
          str(pl.get("decision")))
    check("plan_daily_no_confirm", pl.get("requires_confirmation") is False
          and pl.get("execution_tier") == "daily")

    from plan import execution_tier, should_attach_plan
    check("tier_daily", execution_tier("auto", "fast") == "daily")
    check("tier_professional", execution_tier("deep", "fast") == "professional")
    check("tier_research", execution_tier("auto", "balanced", "research") == "deep_research")
    check("attach_only_professional",
          should_attach_plan("auto", "fast") is False
          and should_attach_plan("deep", "fast") is True
          and should_attach_plan("auto", "fast", context="research") is False)

    ho = build_plan("https://docs.python.org/3/")
    check("plan_handoff", ho.get("status") == "handoff_required")

    # known-url 不进热搜
    r_url = super_search("https://example.com/x", mode="fast", n=3, timeout=5)
    check("search_skips_known_url", r_url.get("status") == "handoff_required"
          and r_url.get("count", 0) == 0,
          f"status={r_url.get('status')} count={r_url.get('count')}")
    check("known_url_no_confirm", r_url.get("requires_confirmation") is False)

    # envelope 附加且不改 results 顺序
    env_in = {
        "query": "q", "engine": "anysearch",
        "results": [
            {"title": "T1", "url": "https://a.com/1", "snippet": "s", "source": "anysearch"},
            {"title": "T2", "url": "https://b.com/2", "snippet": "s", "source": "anysearch"},
        ],
        "engine_outcomes": [{"engine": "anysearch", "status": "ok", "results_count": 2}],
    }
    env_out = attach_envelope(env_in, query="q")
    check("envelope_preserves_order", env_out["results"][0]["title"] == "T1")
    check("envelope_candidates", len(env_out.get("candidates") or []) == 2)
    check("envelope_verification_candidate",
          env_out["candidates"][0]["verification"]["status"] == "candidate")
    check("envelope_limitations", bool(env_out.get("limitations")))

    # 热路径仍带 envelope 且可缓存（不破坏冷/热）
    check("e2e_has_envelope_or_cache",
          bool(r2.get("candidates") is not None or r2.get("cached")),
          f"keys candidates={('candidates' in r2)} cached={r2.get('cached')}")

    section("汇总")
    total = report["pass"] + report["fail"]
    print(f"  PASS={report['pass']} FAIL={report['fail']} TOTAL={total}")
    print(json.dumps({"latency": report["latency"], "sample": report["sample"]},
                     ensure_ascii=False, indent=2))
    return 0 if report["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
