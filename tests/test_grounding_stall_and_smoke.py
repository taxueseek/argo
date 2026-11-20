#!/usr/bin/env python3
"""test_grounding_stall_and_smoke.py — 原文数字锚定/单位归一化、研究停滞传感器与隔离烟测单测。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import evidence_grounding  # noqa: E402
import numeric_verify  # noqa: E402
import research_gates  # noqa: E402
import source_discovery  # noqa: E402


def test_ungrounded_number_blocks_fabricated_template_price():
    page = "景点名录：故宫博物院、颐和园、天坛公园。开放时间 8:30-17:00。"
    assert evidence_grounding.check_ungrounded_numbers(page, "全价票 100 元/人") == ["100"]
    assert evidence_grounding.check_ungrounded_numbers(page, "17:00") == []
    # 千分位与全角数字折叠命中
    finance_page = "2025 财年总营收达到 673,622,371 美元。"
    assert evidence_grounding.check_ungrounded_numbers(finance_page, "６７３６２２３７１") == []


def test_anchored_context_picks_nearest_entity_window():
    page = (
        "Markdown Content:\n"
        "A 公司 2024 年定价为 199 元，面向基础版用户。"
        + "无关说明段落。" * 30
        + "B 公司旗舰版定价为 199 元，含企业支持。"
    )
    ctx = evidence_grounding.extract_anchored_context(page, "199", entity="B 公司", window=30)
    assert "B 公司" in ctx["excerpt"]
    assert ctx["span"] is not None
    assert ctx["excerpt_sha1"].startswith("sha1:")


def test_normalize_magnitude_unit_conservative_scaling():
    # 未换算（原样抄录 131 到 亿美元 列）→ 自动换算为 1310
    res = evidence_grounding.normalize_magnitude_unit(
        "131", "Total revenue reached $131 billion in FY2025.", "亿美元"
    )
    assert res["changed"] is True
    assert res["normalized"] == "1310"

    # 已经换算过（1310 ≠ 131）→ 保守保持不变，杜绝二次放大
    already = evidence_grounding.normalize_magnitude_unit(
        "1310", "Total revenue reached $131 billion in FY2025.", "亿美元"
    )
    assert already["changed"] is False
    assert already["normalized"] == "1310"


def test_numeric_verify_attaches_anchored_excerpts():
    results = [
        {
            "title": "苹果 iPhone 17 Pro Max 售价 11,999 元",
            "url": "https://apple.example.com/iphone",
            "snippet": "官网公布 iPhone 17 Pro Max 起售价 11,999 元",
        }
    ]

    def fake_fetch(url: str, max_chars: int = 8000, timeout: float = 4.0):
        return {
            "ok": True,
            "content": "标准版起售价 5999 元。" + "参数对比。" * 20 + "iPhone 17 Pro Max 售价 11,999 元起。",
            "fetch_method": "http",
        }

    rep = numeric_verify.numeric_verify_results(
        results, query="iPhone 17 Pro Max", top_k=1, fetch_fn=fake_fetch
    )
    nv = results[0]["numeric_verification"]
    assert rep["summary"]["confirmed"] == 1
    assert nv["anchored_excerpts"]
    assert nv["anchored_excerpts"][0]["excerpt_sha1"].startswith("sha1:")
    assert "11,999" in nv["anchored_excerpts"][0]["excerpt"]


def test_coverage_stall_tracker_and_gates_integration():
    tracker = research_gates.CoverageStallTracker(max_stalled_rounds=2)
    tracker.bind_evidence("wp1", "ev1", "supports")
    tracker.bind_evidence("wp2", "ev1", "context")
    assert len(tracker.task_bindings) == 2

    # 第 1 轮有新 opened → 未停滞
    r1 = tracker.observe_round(opened_urls=["https://a.com"], covered_slots=["wp1:price"])
    assert r1["stalled_rounds"] == 0
    assert r1["stop_recommended"] is False

    # 第 2 轮重复 URL 无新进展 → stalled=1
    r2 = tracker.observe_round(opened_urls=["https://a.com"], covered_slots=["wp1:price"])
    assert r2["stalled_rounds"] == 1
    assert r2["stop_recommended"] is False

    # 第 3 轮仍无新进展 → 达阈值，触发 stop_recommended
    r3 = tracker.observe_round(opened_urls=["https://a.com"])
    assert r3["stalled_rounds"] == 2
    assert r3["stop_recommended"] is True
    assert r3["stop_reason"] == "coverage_stalled_x2"

    gate_res = research_gates.evaluate_dossier_gates({"sources": [], "stall_state": r3})
    assert gate_res["stop_recommended"] is True
    assert any(w["id"] == "coverage_stalled" for w in gate_res["warnings"])


def test_isolated_smoke_pass_reject_and_lookup(tmp_path):
    good_spec = {
        "id": "demo_good",
        "type": "http",
        "url": "https://example.com/api/search",
        "query_param": "q",
        "headers": {"Accept": "application/json"},
    }
    ok_res = source_discovery.verify_spec_draft_isolated(
        good_spec, rejected_dir=tmp_path, runner=lambda p, t: (True, '{"status":200}')
    )
    assert ok_res["passed"] is True
    assert source_discovery.lookup_rejected("demo_good", rejected_dir=tmp_path) is None

    # 失败时自动归档至拒绝档案，并可被 lookup_rejected 检索
    bad_spec = dict(good_spec, id="demo_bad")
    bad_res = source_discovery.verify_spec_draft_isolated(
        bad_spec, rejected_dir=tmp_path, runner=lambda p, t: (False, "HTTP 403")
    )
    assert bad_res["passed"] is False
    assert bad_res["reason"] == "smoke_test_failed"
    archived = source_discovery.lookup_rejected("demo_bad", rejected_dir=tmp_path)
    assert archived is not None
    assert archived["reason"] == "smoke_test_failed"

    # SSRF 私有地址与泄露 Cookie 头一律在烟测前直接拒绝
    ssrf_res = source_discovery.verify_spec_draft_isolated(
        {"id": "local_bad", "url": "http://127.0.0.1:8000/search"},
        rejected_dir=tmp_path,
    )
    assert ssrf_res["passed"] is False
    assert ssrf_res["reason"] == "ssrf_blocked"

    leak_res = source_discovery.verify_spec_draft_isolated(
        {"id": "leak_bad", "url": "https://example.com/s", "headers": {"Cookie": "a=1"}},
        rejected_dir=tmp_path,
    )
    assert leak_res["passed"] is False
    assert leak_res["reason"] == "sensitive_header_leaked"
