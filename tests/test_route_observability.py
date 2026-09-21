#!/usr/bin/env python3
"""路由可观测性回归钉（2026-09-14）。

钉死两个当日修复，防止再回归：

1. --engine 逗号多引擎拆分（route_query engine_override 分支）：
   曾整串直通——「local_bing,local_baidu」被当成一个引擎名进 combo，
   registry 查无 → 「未知引擎」空跑，用户显式指定的引擎全部失效
   （--list-engines 路径一直是拆的，两条路径计算方式分裂）。

2. tfidf_scores 只在 TF-IDF 真正参与决策时输出：
   域命中路径曾照搬原始 TF-IDF 前三，与实际执行的域 combo 无关
   （实测「python asyncio tutorial」报 qiita 前三、实际执行 octen/exa），
   误导读 JSON 的 agent。落选的近失信号由 reason 的 [TF-IDF→x] 承载，
   最终输出新增 route_reason 字段承载真实路由依据。

全部离线（不触网）。

运行：
  python3 -m pytest tests/test_route_observability.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from route import _feature_labels, route_query  # noqa: E402


class TestEngineOverrideCommaSplit(unittest.TestCase):
    """--engine 逗号串必须拆分（与 --list-engines 路径同计算方式）。"""

    def test_comma_separated_engines_are_split(self):
        d = route_query("test", engine_override="local_bing,local_baidu")
        self.assertEqual(d["engines_combo"], ["local_bing", "local_baidu"])
        self.assertEqual(d["engines"], ["local_bing", "local_baidu"])
        self.assertEqual(d["engine"], "local_bing")

    def test_comma_with_spaces_and_empty_parts(self):
        d = route_query("test", engine_override="local_bing, , local_baidu")
        self.assertEqual(d["engines_combo"], ["local_bing", "local_baidu"])

    def test_single_engine_unchanged(self):
        d = route_query("test", engine_override="anysearch")
        self.assertEqual(d["engines_combo"], ["anysearch"])

    def test_reason_lists_all_engines(self):
        d = route_query("test", engine_override="local_bing,local_baidu")
        self.assertIn("local_baidu", d["reason"])
        self.assertIn("local_bing", d["reason"])


class TestTfidfScoresEmission(unittest.TestCase):
    """tfidf_scores 只在 TF-IDF 参与决策时输出；route_reason 恒在。"""

    def test_domain_win_without_tfidf_hides_scores(self):
        # 英文技术查询命中 english_tech 域（combo 来自域配置），TF-IDF
        # 举荐的 qiita 落选——分数不得输出，避免与执行引擎脱钩的假信号。
        d = route_query("python asyncio tutorial", mode="auto", depth="fast")
        self.assertEqual(d.get("tfidf_scores"), [])
        self.assertTrue(d.get("reason"))

    def test_tfidf_driven_path_keeps_scores(self):
        # 天气查询由 TF-IDF 语义路由主导（open_meteo 进 combo 且居首），
        # 分数是真实决策依据，必须保留。
        d = route_query("北京天气", mode="auto", depth="fast")
        scores = d.get("tfidf_scores") or []
        self.assertTrue(scores, "TF-IDF 主导路径不应清空 tfidf_scores")
        self.assertEqual(scores[0]["engine"], d["engines_combo"][0])

    def test_fallback_path_hides_below_threshold_scores(self):
        # 保底路径 tfidf_best 必为空：低于阈值的候选分不是路由依据。
        d = route_query("zzz qqx unrelated tokens 2026", mode="auto",
                        depth="fast")
        if d.get("reason", "").startswith("TF-IDF 语义路由"):
            self.skipTest("查询意外命中 TF-IDF 主导路径，跳过兜底检查")
        self.assertEqual(d.get("tfidf_scores"), [])
        self.assertTrue(d.get("reason"))


class TestReasonLanguageLabel(unittest.TestCase):
    """route_reason 的语言标签必须按真实语种写。

    2026-09-21 实测：标签由 `chinese_ratio` 二分（>0.6 中文 / <0.1 英文），
    谚文、假名、西里尔、阿拉伯查询的 chinese_ratio 都是 0 → 一律被写成
    「英文 → 命中域 [x]」。reason 是排障时第一眼看归因的地方，标签错了会把
    人引向「为什么命中了英文源」这个不存在的问题。

    判据直接打在 `_feature_labels` 上（纯函数），避免依赖某条查询恰好走到
    哪条路由分支；另留一条端到端断言防止标签在拼装 reason 时被丢掉。
    """

    def test_non_latin_scripts_are_labelled_honestly(self):
        cases = [
            ("ko", "韩文"),
            ("ja", "日文"),
            ("cyrillic", "西里尔语系"),
            ("arabic", "阿拉伯语"),
            ("thai", "泰语"),
        ]
        for lang, want in cases:
            with self.subTest(lang=lang):
                got = _feature_labels({"primary_lang": lang, "chinese_ratio": 0.0})
                self.assertEqual(got, want)

    def test_latin_and_mixed_keep_previous_labels(self):
        # 纯英文仍是「英文」；中英混合（cr 在 0.1~0.6）维持不写语言标签的旧形态；
        # 中文（cr>0.6）仍是「中文」；语言判不出来时不编造语种。
        self.assertEqual(_feature_labels({"primary_lang": "en", "chinese_ratio": 0.0}), "英文")
        self.assertEqual(_feature_labels({"primary_lang": "zh", "chinese_ratio": 0.9}), "中文")
        self.assertEqual(_feature_labels({"primary_lang": "en", "chinese_ratio": 0.3}), "通用查询")
        self.assertEqual(_feature_labels({"primary_lang": "mixed", "chinese_ratio": 0.0}), "英文")

    def test_reason_carries_the_label_end_to_end(self):
        # 域命中路径的 reason 前缀就是语言标签（不依赖 TF-IDF 分支）。
        reason = route_query("에펠탑 어디").get("reason", "")
        self.assertTrue(reason.startswith("韩文"), f"reason 未带语言标签：{reason}")


if __name__ == "__main__":
    unittest.main()
