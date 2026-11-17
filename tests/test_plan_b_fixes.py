#!/usr/bin/env python3
"""test_plan_b_fixes — Plan B 优化与缺陷修复的回归门禁。"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import badcase_ledger as bl  # noqa: E402
import engine_states  # noqa: E402
import engines_schema_drift as esd  # noqa: E402
import fetch_v3 as fv3  # noqa: E402
import rank_signals as rs  # noqa: E402
import semantic_evidence as se  # noqa: E402
import time_utils as tu  # noqa: E402
from social_engines import zhihu_engine as ze  # noqa: E402


class TestPlanBFixes(unittest.TestCase):
    def test_gemini_classify_respects_deadline(self):
        def slow_choice(text, labels, instructions, timeout, criteria=None):
            if "slow" in text:
                time.sleep(0.6)
                return {"label": "a", "confidence": 0.9, "scores": {}}
            return {"label": "a", "confidence": 0.9, "scores": {"a": 0.9}}

        t0 = time.monotonic()
        with patch.object(se, "gemini_api_key", return_value="k"), \
                patch.object(se, "DEFAULT_GEMINI_DEADLINE_S", 0.15), \
                patch.object(se, "_gemini_choice", side_effect=slow_choice):
            out = se._classify_gemini(
                ["fast item", "slow item"], ["a", "b"],
                keep=[(0, "fast item"), (1, "slow item")], timeout=5.0,
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.45)
        self.assertIsNotNone(out)
        self.assertIsNotNone(out[0])

    def test_jsrun_success_skips_browser_fetch(self):
        fake_http = {"url": "https://example.com", "html": "<html>challenge</html>",
                     "content": "", "success": False, "status_code": 403}
        fake_jsrun = {"url": "https://example.com", "content": "x" * 200,
                      "success": True, "fetch_method": engine_states.FETCH_JSRUN_CHALLENGE}
        with patch.object(fv3, "_http_fetch", return_value=fake_http), \
                patch.object(fv3._fetch_jina, "enabled", return_value=False), \
                patch.object(fv3, "_needs_browser", return_value=True), \
                patch.object(fv3, "_jsrun_enabled", return_value=True), \
                patch.object(fv3, "_jsrun_challenge_fetch", return_value=fake_jsrun) as mock_jsrun, \
                patch.object(fv3, "_tinyfish_enabled", return_value=False), \
                patch.object(fv3, "_browser_fetch") as mock_browser:
            res = fv3.fetch_v3("https://example.com", use_browser_fallback=True, deadline_s=10.0)
        mock_jsrun.assert_called_once()
        mock_browser.assert_not_called()
        self.assertEqual(res.get("fetch_method"), engine_states.FETCH_JSRUN_CHALLENGE)

    def test_zhihu_failed_expansion_counts_toward_max_expand(self):
        items = [
            {"Title": f"Q{i}", "Url": f"https://www.zhihu.com/question/{i}",
             "ContentType": "question", "ContentText": "body"}
            for i in range(6)
        ]
        fake_body = json.dumps({"Code": 0, "Data": {"Items": items}}).encode("utf-8")
        with patch.object(ze, "_secret", return_value="sec"), \
                patch.object(ze, "http_get", return_value=(fake_body, {})), \
                patch.object(ze, "fetch_question_answers", return_value=[]) as mock_expand:
            ze.search("test", n=6, expand_questions=True)
        self.assertEqual(mock_expand.call_count, 2)

    def test_badcase_overview_unattributed_count_and_ingest_cleanup(self):
        fake_stats = {
            "total": 10,
            "by_root_cause": [{"root_cause": "unattributed", "count": 7}],
        }
        with patch.object(bl, "stats", return_value=fake_stats), \
                patch("dormant_ledger.dormant_engines", return_value=set()), \
                patch("dormant_ledger.check_dormant_ledger", return_value=[]):
            ov = bl.overview()
        self.assertEqual(ov["badcases"]["unattributed"], 7)

    def test_time_capable_covers_specs_and_keeps_non_members_false(self):
        self.assertTrue(tu.is_time_capable("gdelt"))
        self.assertTrue(tu.is_time_capable("cisa_alerts"))
        self.assertFalse(tu.is_time_capable("octen"))
        self.assertFalse(tu.is_time_capable("anysearch"))

    def test_schema_drift_does_not_misclassify_ok_envelope_with_drifted_container(self):
        raw = {"message": "ok", "hits": [{"title": "A", "link": "https://a.test"}]}
        diag = esd.diagnose_schema_drift(
            raw, {"response": {"items_path": "results"}, "output_map": {"item_title": "title", "item_url": "url"}}
        )
        self.assertNotEqual(diag["stage"], "envelope")
        self.assertTrue(diag["drift_detected"])

    def test_upstream_score_boost_bounded(self):
        self.assertEqual(rs.upstream_score_boost({}), 0.0)
        self.assertEqual(rs.upstream_score_boost({"upstream_score": 0}), 0.0)
        b = rs.upstream_score_boost({"upstream_score": 500})
        self.assertGreater(b, 0.0)
        self.assertLessEqual(b, 0.05)

    def test_heuristic_multilingual_cjk_and_polarity_window(self):
        self.assertEqual(
            se.heuristic_support(
                "央行降准释放流动性",
                "中国人民银行宣布下调金融机构存款准备金率0.5个百分点，向市场提供长期流动性约1万亿元。",
            )["label"],
            "supports the query",
        )
        self.assertEqual(
            se.heuristic_support(
                "美联储本周降息",
                "美联储议息会议决定将联邦基金利率目标区间维持在当前水平不变，并未如部分机构预期那样在本周降息。",
            )["label"],
            "contradicts the query",
        )
        self.assertEqual(
            se.heuristic_support(
                "日本の最低賃金引き上げ",
                "厚生労働省の審議会は今年度の全国平均の最低賃金を過去最大幅となる時給50円引き上げる方針を決定した。",
            )["label"],
            "supports the query",
        )
        self.assertEqual(
            se.heuristic_substantive_discussion(
                "삼성전자 파운드리",
                "오늘 점심 메뉴는 김치찌개였고, 친구가 잠깐 삼성전자 파운드리 이야기를 꺼냈지만 바로 다른 화제로 넘어갔다.",
            )["label"],
            "passing mention or brief reference",
        )


if __name__ == "__main__":
    unittest.main()
