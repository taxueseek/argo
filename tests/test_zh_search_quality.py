#!/usr/bin/env python3
"""中文检索质量黄金回归集（2026-08）

固化 2026-08-29 故障会话的四个根因修复，防止退化：

F1 语言×预算互斥：zh 查询命中 social 域后，缺密钥引擎（zhihu）被踢出可用集，
   剩余 combo 按 config 顺序被 fast 模式 budget=2 截断成 [bilibili, hackernews]
   —— hackernews 对中文零召回、bilibili 关键词噪声早停，通用源全军覆没。
   修复：_lang_aware_combo_order 在截断前把纯英文社区源移尾、social 域通用
   中文 web 源前置。
F2 缺密钥静默失败：显式 engine=zhihu/exa 覆盖绕过路由 env 过滤，执行层把
   「没配置」当「没结果」返回 no-results。修复：_run_one 前置 env 拦截。
F3a 画像模板污染：build_profile_sub_queries 把「…流行玩法全景：…」长查询
   拼成「… technical overview architecture」。修复：>50 字符查询原样直查。
F3b 改写器型号守卫：子查询里的「GPT-4o」被当品牌歧义词展开成
   「大语言模型 OpenAI ChatGPT GPT-4」。修复：长查询跳过改写 + 型号后缀
   （词后跟数字）不展开。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from route import _lang_aware_combo_order, route_query  # noqa: E402
from search import _missing_env_for  # noqa: E402
from query_rewriter import rewrite_query  # noqa: E402
from topic_research_profiles import TOPIC_PROFILES, build_profile_sub_queries  # noqa: E402

# 当时的故障原查询：提到平台词（小红书/微信），主题却是拍立得照片趋势
POLLUTED_ZH_QUERY = "拍立得 AI 照片 趋势 小红书 微信 老照片合照 年轻的自己"

ZH_CAPABLE_HEAD = {"local_bing", "anysearch", "bilibili", "v2ex",
                   "xiaohongshu", "weibo", "zhihu"}


class TestLangAwareComboOrder(unittest.TestCase):
    """F1 单元层：语言感知排序纯函数。"""

    FEATURES_ZH = {"primary_lang": "zh", "chinese_ratio": 0.8}
    FEATURES_EN = {"primary_lang": "en", "chinese_ratio": 0.0}

    def test_zh_moves_en_only_engines_to_tail(self):
        combo = ["bilibili", "hackernews", "v2ex", "twitter",
                 "xiaohongshu", "weibo", "local_bing"]
        out = _lang_aware_combo_order(combo, self.FEATURES_ZH, "social",
                                      {"local_bing", "anysearch"})
        self.assertEqual(out[0], "local_bing", "social+zh：通用中文 web 源应置首")
        # 硬断言：纯英文源全部位于中文可用源之后
        first_en = min(i for i, e in enumerate(out)
                       if e in {"hackernews", "twitter", "reddit"})
        for i, e in enumerate(out):
            if e in ZH_CAPABLE_HEAD:
                self.assertLess(i, first_en, f"{e} 应排在英文源之前")

    def test_en_query_untouched(self):
        combo = ["hackernews", "bilibili"]
        out = _lang_aware_combo_order(combo, self.FEATURES_EN, "social",
                                      {"hackernews", "bilibili"})
        self.assertEqual(out, combo, "非 zh 查询原样返回")

    def test_no_mutation(self):
        combo = ["hackernews", "local_bing"]
        _lang_aware_combo_order(combo, self.FEATURES_ZH, "social",
                                {"local_bing"})
        self.assertEqual(combo, ["hackernews", "local_bing"], "不改传入列表")


class TestZhSocialRouting(unittest.TestCase):
    """F1 集成层：故障原查询的端到端路由决策。"""

    def test_zh_social_query_never_leads_with_hackernews(self):
        d = route_query(POLLUTED_ZH_QUERY)
        engines = d.get("engines") or []
        self.assertTrue(engines, "应有引擎组合")
        self.assertNotEqual(
            engines[0], "hackernews",
            f"zh 查询不得由 hackernews 领衔（实际：{engines}）")
        # fast 截断发生在 policy 层，这里 combo 可能多于 2——只约束头部语言
        self.assertIn(engines[0], ZH_CAPABLE_HEAD,
                      f"首位应是中文可用/通用源（实际：{engines}）")

    def test_social_domain_disables_early_stop(self):
        d = route_query(POLLUTED_ZH_QUERY)
        self.assertEqual(d.get("domain"), "social")
        self.assertTrue(d.get("no_early_stop"),
                        "social 域应禁早停，防平台噪声短路通用源")


class TestMissingEnvOutcome(unittest.TestCase):
    """F2：缺密钥引擎可被识别，不再静默 no-results。"""

    def test_zhihu_env_missing_detected(self):
        import os
        from pathlib import Path
        for var in ("ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"):
            os.environ.pop(var, None)
        # 屏蔽密钥文件兜底（本机 ~/.config/argo/env 真有 zhihu 密钥）
        with patch("engine_env._envfile_path",
                   lambda: Path("/nonexistent/argo/env")):
            missing = _missing_env_for("zhihu")
        self.assertIn("ARGO_ZHIHU_ACCESS_SECRET", missing)

    def test_configured_engine_not_flagged(self):
        # anysearch 无 env 依赖（本机已可用），缺失列表应为空
        self.assertEqual(_missing_env_for("anysearch"), [])


class TestProfileTemplateGuard(unittest.TestCase):
    """F3a：长查询不套画像模板。"""

    LONG_QUERY = ("2025-2026 年 AI 照片转图像流行玩法全景：拍立得合照、AI手办、"
                  "微缩罐等 trends，代表工具与传播机制、GitHub 同类项目")

    def test_long_query_direct(self):
        sqs = build_profile_sub_queries(self.LONG_QUERY,
                                        TOPIC_PROFILES["ai"], 4)
        self.assertEqual(len(sqs), 1)
        self.assertEqual(sqs[0]["strategy"], "direct")
        self.assertNotIn("technical overview", sqs[0]["query"])
        self.assertNotIn("大语言模型", sqs[0]["query"])

    def test_short_topic_still_uses_templates(self):
        sqs = build_profile_sub_queries("Claude Opus 5",
                                        TOPIC_PROFILES["ai"], 4)
        self.assertEqual(len(sqs), 4, "短主题仍应享受模板扩词")


class TestRewriterGuards(unittest.TestCase):
    """F3b：长查询跳过 + 型号后缀守卫，同时保留原有消歧能力。"""

    def test_long_query_skips_rewrite(self):
        long_q = ("2025-2026 年 AI 照片转图像流行玩法全景：拍立得合照、AI手办等 "
                  "viral photo transformation trends，代表工具 GPT-4o 图像、即梦")
        r = rewrite_query(long_q)
        self.assertIsNone(r["rewritten"])
        self.assertEqual(r["type"], "direct")

    def test_model_number_not_expanded(self):
        r = rewrite_query("GPT-4o 图像能力怎么样")
        self.assertIsNone(r["rewritten"],
                          f"具体型号不应被品牌扩词污染：{r['rewritten']}")

    def test_ambiguity_rewrite_still_works(self):
        r = rewrite_query("苹果 股价")
        self.assertIsNotNone(r["rewritten"], "既有消歧改写不得回归")
        self.assertIn("Apple", r["rewritten"])


class TestHttpEnvelopeError(unittest.TestCase):
    """F5：HTTP 200 业务错误封套必须暴露（byted 10406 配额耗尽曾静默空返回）。"""

    BYTED_MAP = {
        "items": "Result.WebResults",
        "item_title": "Title", "item_url": "Url",
        "item_summary": "Summary", "item_source": "SiteName",
    }

    def test_volcengine_style_quota_error_surfaces(self):
        import json
        from engines_base import _parse_http_payload
        raw = json.dumps({
            "ResponseMetadata": {"RequestId": "x", "Error": {
                "CodeN": 10406, "Code": "10406",
                "Message": "Free quota has been exhausted."}},
            "Result": {"WebResults": []},
        }, ensure_ascii=False)
        out = _parse_http_payload(raw, "", "byted", 3, self.BYTED_MAP, {})
        self.assertEqual(len(out), 1, "封套错误必须返回 error item")
        self.assertIn("error", out[0])
        self.assertIn("10406", out[0]["error"])

    def test_healthy_payload_unaffected(self):
        import json
        from engines_base import _parse_http_payload
        raw = json.dumps({"Result": {"WebResults": [
            {"Title": "拍立得趋势", "Url": "https://example.com/a",
             "Summary": "s", "SiteName": "站点"}]}})
        out = _parse_http_payload(raw, "", "byted", 3, self.BYTED_MAP, {})
        self.assertEqual(len(out), 1, "健康响应不得被误判为错误")
        self.assertNotIn("error", out[0])
        self.assertEqual(out[0]["title"], "拍立得趋势")

    def test_error_field_null_or_empty_ignored(self):
        import json
        from engines_base import _parse_http_payload
        raw = json.dumps({"error": None, "Error": "",
                          "Result": {"WebResults": []}})
        out = _parse_http_payload(raw, "", "byted", 3, self.BYTED_MAP, {})
        self.assertEqual(out, [], "空错误字段不触发封套误判")

    def test_quota_exhausted_classification(self):
        from search import _classify_engine_outcome
        oc = _classify_engine_outcome(
            "byted", [{"error": "byted 10406: Free quota has been exhausted",
                       "source": "byted"}], 100)
        self.assertEqual(oc["status"], "quota-exhausted")


class TestResearchContextRouting(unittest.TestCase):
    """F6：research 语境不得被垂直目录独占（models_dev 曾以 10 条规格页
    早停整条研究子查询，dossier 全噪声）。"""

    SUB_QUERY = ("2025-2026 年 AI 照片转图像流行玩法全景：拍立得合照、AI手办、"
                 "微缩罐等 viral photo transformation trends，代表工具 GPT-4o 图像、即梦")
    AI_BOOSTS = ["arxiv", "semantic_scholar", "openalex"]

    def test_boost_not_overridden_by_primary(self):
        from route import route_query
        d = route_query(self.SUB_QUERY, mode="fast", depth="balanced",
                        context="research", engines_boost=self.AI_BOOSTS)
        engines = d.get("engines") or []
        self.assertTrue(engines)
        self.assertEqual(
            engines[0], self.AI_BOOSTS[0],
            f"research+boosts 时画像垂直源应置首，实际：{engines}")
        self.assertNotEqual(engines[0], "models_dev",
                            "域 primary 不得顶掉研究 boosts")

    def test_research_forces_parallel(self):
        from route import route_query
        d = route_query(self.SUB_QUERY, mode="fast", depth="balanced",
                        context="research", engines_boost=self.AI_BOOSTS)
        self.assertTrue(d.get("parallel"),
                        "research 子查询需并行跑满 combo（no_early_stop）")


if __name__ == "__main__":
    unittest.main()


class TestSymmetricLangOrder(unittest.TestCase):
    """对称语言排序（2026-08-31）：非中文查询的中文专用源移尾。

    实测根因：social 域 patterns 认平台关键词不认查询语言，英文查询
    「reddit recommendations」命中 social 域后 zhihu 排头 + 早停，
    fast/auto 均 5/5 中文结果、hackernews 排第二永远轮不到。
    """

    def test_en_social_moves_zh_only_to_tail(self):
        from route import _lang_aware_combo_order
        combo = ["zhihu", "hackernews", "bilibili", "twitter", "reddit"]
        features = {"primary_lang": "en", "chinese_ratio": 0.0}
        out = _lang_aware_combo_order(combo, features, "social",
                                      enabled=set(combo))
        self.assertEqual(out[:2], ["hackernews", "twitter"])
        self.assertIn("zhihu", out[-2:])

    def test_ja_query_moves_zh_only_to_tail(self):
        from route import _lang_aware_combo_order
        combo = ["zhihu", "local_bing", "bilibili"]
        features = {"primary_lang": "ja", "chinese_ratio": 0.0}
        out = _lang_aware_combo_order(combo, features, "general_search",
                                      enabled=set(combo))
        self.assertEqual(out[0], "local_bing")
        self.assertNotIn("zhihu", out[:1])

    def test_zh_query_protection_unchanged(self):
        from route import _lang_aware_combo_order
        combo = ["bilibili", "hackernews", "local_bing"]
        features = {"primary_lang": "zh", "chinese_ratio": 0.8}
        out = _lang_aware_combo_order(combo, features, "tech",
                                      enabled=set(combo))
        # zh 保护保持：英文源移尾
        self.assertEqual(out, ["bilibili", "local_bing", "hackernews"])

    def test_mixed_zh_ratio_stays_zh_branch(self):
        """zh_ratio > 0.15 的混合查询仍走 zh 保护分支，不误伤。"""
        from route import _lang_aware_combo_order
        combo = ["zhihu", "hackernews"]
        features = {"primary_lang": "mixed", "chinese_ratio": 0.4}
        out = _lang_aware_combo_order(combo, features, "social",
                                      enabled=set(combo))
        self.assertEqual(out[0], "zhihu")

    def test_no_mutation_symmetric(self):
        from route import _lang_aware_combo_order
        combo = ["zhihu", "hackernews"]
        features = {"primary_lang": "en", "chinese_ratio": 0.0}
        out = _lang_aware_combo_order(combo, features, "social",
                                      enabled=set(combo))
        self.assertIsNot(out, combo)
        self.assertEqual(combo, ["zhihu", "hackernews"])
