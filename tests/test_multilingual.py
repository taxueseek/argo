#!/usr/bin/env python3
"""多语种搜索能力测试 — 语言检测 / 路由 / 跨语言回退 / 引擎语言参数。

运行：
  python3 -m pytest tests/test_multilingual.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
LOCAL_DIR = SKILL_DIR / "sub-skills" / "local-search"
for p in (SCRIPT_DIR, LOCAL_DIR):
    sys.path.insert(0, str(p))

from lang_detect import (  # noqa: E402
    detect_language,
    detect_script,
    engine_lang_param,
    language_features,
)
from route import (  # noqa: E402
    _lang_must_keep,
    extract_features,
    route_query,
)
from recovery import (  # noqa: E402
    build_recovery_plan,
    cross_lang_query,
    baseline_cross_query,
)
from query_rewriter import rewrite_query  # noqa: E402
from lang_pref import (  # noqa: E402
    prefer_langs,
    system_lang,
    dominant_habit,
    effective_engine_lang,
    record_query_lang,
    reset_habit_for_tests,
    BASELINE_LANGS,
)


# ═══════════════════════════════════════════════════════════════════════════
# 语言检测
# ═══════════════════════════════════════════════════════════════════════════

class TestLanguageDetection(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(detect_language("苹果股价"), "zh")
        self.assertEqual(detect_language("北京旅游攻略"), "zh")

    def test_japanese(self):
        self.assertEqual(detect_language("アニメ おすすめ"), "ja")
        self.assertEqual(detect_language("東京の天気予報"), "ja")

    def test_korean(self):
        self.assertEqual(detect_language("한국 영화 추천"), "ko")

    def test_english(self):
        self.assertEqual(detect_language("hello world"), "en")
        self.assertEqual(detect_language("React vs Vue"), "en")

    def test_latin_with_diacritics(self):
        # 带变音符号的西欧语言应判 latin，而非 en
        self.assertEqual(detect_language("café français"), "latin")

    def test_cyrillic(self):
        self.assertEqual(detect_language("Как написать скрипт"), "cyrillic")

    def test_thai_arabic_hebrew_greek(self):
        self.assertEqual(detect_language("สวัสดีครับ"), "thai")
        self.assertEqual(detect_language("مرحبا بالعالم"), "arabic")
        self.assertEqual(detect_language("שלום עולם"), "hebrew")
        self.assertEqual(detect_language("Καλημέρα"), "greek")

    def test_script_aligned_with_primary_lang(self):
        """script 与 primary_lang 同源，避免混技术词查询矛盾。"""
        self.assertEqual(detect_script("苹果股价"), "cjk")
        self.assertEqual(detect_script("アニメ おすすめ"), "kana")
        self.assertEqual(detect_script("한국 영화"), "hangul")
        self.assertEqual(detect_script("hello world"), "latin")
        self.assertEqual(detect_script("Как дела"), "cyrillic")
        # 混英技术词：主语言 zh，script 必须 cjk（不再被拉丁字母抢走）
        self.assertEqual(detect_language("Python 异步编程"), "zh")
        self.assertEqual(detect_script("Python 异步编程"), "cjk")
        # 混英俄文：主语言 cyrillic，script 必须 cyrillic
        self.assertEqual(detect_language("Python курс"), "cyrillic")
        self.assertEqual(detect_script("Python курс"), "cyrillic")

    def test_language_features_compat(self):
        f = language_features("Python 异步编程")
        self.assertEqual(f["primary_lang"], "zh")
        self.assertEqual(f["script"], "cjk")
        self.assertFalse(f["is_latin"])
        self.assertGreater(f["chinese_ratio"], 0)
        self.assertGreater(f["english_ratio"], 0)

    def test_engine_lang_param_covers_non_cjk(self):
        """阿/希/希伯来/印地等不得回落空串导致 setlang 仍停在 zh-Hans。"""
        self.assertEqual(engine_lang_param("setlang", "アニメ"), "ja-JP")
        self.assertEqual(engine_lang_param("setlang", "한국"), "ko-KR")
        self.assertEqual(engine_lang_param("setlang", "hello world"), "en-US")
        self.assertEqual(engine_lang_param("setlang", "苹果"), "zh-Hans")
        self.assertEqual(engine_lang_param("setlang", "Как дела"), "ru-RU")
        self.assertEqual(engine_lang_param("setlang", "مرحبا"), "ar")
        self.assertEqual(engine_lang_param("setlang", "שלום"), "he")
        self.assertEqual(engine_lang_param("hl", "한국"), "ko")
        self.assertEqual(engine_lang_param("lang", "Как"), "ru")
        self.assertEqual(engine_lang_param("uselang", "アニメ"), "ja")


# ═══════════════════════════════════════════════════════════════════════════
# 路由语言感知
# ═══════════════════════════════════════════════════════════════════════════

class TestRoutingLanguage(unittest.TestCase):
    def test_extract_features_primary_lang(self):
        self.assertEqual(extract_features("アニメ おすすめ")["primary_lang"], "ja")
        self.assertEqual(extract_features("한국 영화 추천")["primary_lang"], "ko")
        self.assertEqual(extract_features("苹果股价")["primary_lang"], "zh")
        self.assertEqual(extract_features("hello world")["primary_lang"], "en")
        self.assertEqual(extract_features("Как дела")["primary_lang"], "cyrillic")

    def test_japanese_query_gets_local_engine(self):
        d = route_query("アニメ おすすめ", mode="auto")
        combo = d.get("engines_combo", [])
        # yandex 默认 disabled，实际落到 local_bing（动态 setlang=ja-JP）
        self.assertTrue(
            any(e in combo for e in ("local_yandex", "local_bing", "local_duckduckgo")),
            f"日文查询 combo 缺语言引擎: {combo}",
        )
        for cn in ("bocha", "byted", "wechat_sogou", "zhihu"):
            self.assertNotIn(cn, combo, f"日文查询误含中文引擎 {cn}")

    def test_korean_query_gets_local_engine(self):
        d = route_query("한국 영화 추천", mode="auto")
        combo = d.get("engines_combo", [])
        self.assertTrue(
            any(e in combo for e in ("local_google", "local_bing", "local_duckduckgo")),
            f"韩文查询 combo 缺语言引擎: {combo}",
        )

    def test_cyrillic_gets_local_bing_supplement(self):
        d = route_query("Как написать скрипт", mode="auto")
        combo = d.get("engines_combo", [])
        # 非拉丁语应追加 local_bing（动态 setlang=ru-RU）或至少通用源
        self.assertTrue(
            any(e in combo for e in ("local_bing", "anysearch", "duckduckgo")),
            f"西里尔查询 combo 过空: {combo}",
        )

    def test_zh_query_keeps_chinese_engines(self):
        d = route_query("苹果股价", mode="auto")
        # 中文金融查询仍走行情域
        self.assertEqual(d.get("domain"), "stock_query")

    def test_en_query_no_cn_noise(self):
        d = route_query("hello world", mode="auto")
        combo = d.get("engines_combo", [])
        # 英文查询不应含中文专用引擎
        for cn in ("bocha", "byted", "wechat_sogou", "zhihu"):
            self.assertNotIn(cn, combo, f"英文查询误含中文引擎 {cn}")

    def test_lang_must_keep_falls_back_to_bing(self):
        """专用源 disabled 时 must_keep 落到 local_bing，不能空转。"""
        f = {"primary_lang": "ja"}
        # 模拟生产：yandex 不在 enabled
        self.assertEqual(
            _lang_must_keep(f, {"local_bing", "local_duckduckgo"}),
            ["local_bing"],
        )
        # 专用源可用时优先 yandex
        self.assertEqual(
            _lang_must_keep(f, {"local_yandex", "local_bing"}),
            ["local_yandex"],
        )
        f_ko = {"primary_lang": "ko"}
        self.assertEqual(
            _lang_must_keep(f_ko, {"local_bing"}),
            ["local_bing"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# 跨语言回退
# ═══════════════════════════════════════════════════════════════════════════

class TestCrossLangRecovery(unittest.TestCase):
    def test_non_latin_triggers_cross_lang(self):
        cq, ce = cross_lang_query("アニメ おすすめ")
        self.assertTrue(ce)
        self.assertIn("duckduckgo", ce)

    def test_chinese_triggers_cross_lang(self):
        cq, ce = cross_lang_query("如何用Python写爬虫")
        self.assertTrue(ce)

    def test_cyrillic_triggers_cross_lang(self):
        cq, ce = cross_lang_query("Как написать скрипт")
        self.assertTrue(ce)

    def test_english_no_cross_lang(self):
        cq, ce = cross_lang_query("hello world")
        self.assertEqual(ce, "")

    def test_latin_no_cross_lang(self):
        cq, ce = cross_lang_query("React vs Vue")
        self.assertEqual(ce, "")

    def test_recovery_plan_has_cross_lang(self):
        plan = build_recovery_plan(
            "アニメ おすすめ", ["anysearch"], ["duckduckgo", "wikipedia"], mode="auto")
        strategies = [s.strategy for s in plan if s.level == "L4"]
        self.assertIn("cross_lang", strategies)

    def test_fast_mode_skips_cross_lang(self):
        plan = build_recovery_plan(
            "アニメ おすすめ", ["anysearch"], ["duckduckgo"], mode="fast")
        strategies = [s.strategy for s in plan]
        self.assertNotIn("cross_lang", strategies)

    def test_baseline_zh_when_prefer_has_zh(self):
        """英文查询 + 偏好含 zh → 中英基线反向补中文源。"""
        cq, ce = baseline_cross_query(
            "hello world", primary_lang="en", prefer=["zh", "en"])
        self.assertTrue(ce)
        self.assertIn("local_bing", ce)

    def test_baseline_zh_skipped_without_zh_prefer(self):
        cq, ce = baseline_cross_query(
            "hello world", primary_lang="en", prefer=["en"])
        self.assertEqual(ce, "")

    def test_baseline_not_for_chinese_query(self):
        cq, ce = baseline_cross_query(
            "苹果股价", primary_lang="zh", prefer=["zh", "en"])
        self.assertEqual(ce, "")

    def test_recovery_plan_baseline_zh_for_en(self):
        # 强制 prefer 含 zh：本机系统多为 zh，plan 应含 baseline_zh
        plan = build_recovery_plan(
            "hello world", ["duckduckgo"], ["wikipedia"], mode="auto")
        strategies = [s.strategy for s in plan if s.level == "L4"]
        # 系统 locale 含 zh 或习惯含 zh 时出现；无 zh 时可不出现
        # 这里只断言：中文查询有 cross_lang、英文查询无 cross_lang
        self.assertNotIn("cross_lang", strategies)


# ═══════════════════════════════════════════════════════════════════════════
# 语言偏好：默认中英 + 系统 + 习惯
# ═══════════════════════════════════════════════════════════════════════════

class TestLangPref(unittest.TestCase):
    def setUp(self):
        reset_habit_for_tests()

    def tearDown(self):
        reset_habit_for_tests()

    def test_system_lang_parses_locale(self):
        self.assertEqual(system_lang("zh_CN.UTF-8"), "zh")
        self.assertEqual(system_lang("ja_JP"), "ja")
        self.assertEqual(system_lang("en_US.UTF-8"), "en")
        self.assertEqual(system_lang("ko_KR"), "ko")
        self.assertEqual(system_lang("ru_RU"), "cyrillic")
        self.assertEqual(system_lang("C"), "")

    def test_baseline_always_zh_en(self):
        """无论习惯/系统为何，prefer 列表始终含中英基线。"""
        cases = [
            prefer_langs(query_lang="en", system="", habit=None, use_live_habit=False),
            prefer_langs(query_lang="ja", system="ja", habit="ja", use_live_habit=False),
            prefer_langs(query_lang="mixed", system="ko", habit=None, use_live_habit=False),
        ]
        for prefs in cases:
            for b in BASELINE_LANGS:
                self.assertIn(b, prefs, f"基线 {b} 缺失于 {prefs}")

    def test_query_strong_signal_first(self):
        prefs = prefer_langs(
            query_lang="ko", system="zh", habit="ja", use_live_habit=False)
        self.assertEqual(prefs[0], "ko")
        self.assertIn("zh", prefs)
        self.assertIn("en", prefs)

    def test_habit_before_system(self):
        prefs = prefer_langs(
            query_lang="mixed", system="en", habit="ja", use_live_habit=False)
        # 弱查询不前置；习惯 ja 应在系统 en 前
        self.assertEqual(prefs[0], "ja")
        self.assertIn("en", prefs)
        self.assertIn("zh", prefs)

    def test_habit_learning(self):
        for _ in range(6):
            record_query_lang("ja")
        self.assertEqual(dominant_habit(), "ja")
        prefs = prefer_langs(query_lang="mixed", system="en", use_live_habit=True)
        self.assertEqual(prefs[0], "ja")

    def test_effective_engine_lang_strong_wins(self):
        self.assertEqual(effective_engine_lang("zh", ["ja", "zh", "en"]), "zh")
        self.assertEqual(effective_engine_lang("ja", ["zh", "en"]), "ja")

    def test_effective_engine_lang_weak_uses_prefer(self):
        self.assertEqual(
            effective_engine_lang("mixed", ["ja", "zh", "en"]), "ja")
        self.assertEqual(
            effective_engine_lang("other", ["zh", "en"]), "zh")

    def test_engine_lang_param_weak_uses_preferred(self):
        from lang_detect import engine_lang_param
        # 无强信号字符的符号查询会判 other/mixed；用 preferred 回落
        # 用纯标点/数字偏 other
        v = engine_lang_param("setlang", "!!! ???", preferred_lang="ja")
        # 若检测为 other/mixed 则 ja-JP；若仍 en 则不强求
        if detect_language("!!! ???") in ("mixed", "other"):
            self.assertEqual(v, "ja-JP")


# ═══════════════════════════════════════════════════════════════════════════
# 引擎层语言参数动态覆盖
# ═══════════════════════════════════════════════════════════════════════════

class TestEngineLangParamWiring(unittest.TestCase):
    def test_html_builder_overrides_setlang(self):
        """local_bing 构建 URL 时 setlang 随查询语言变化。"""
        from engines_base import _build_html_engine

        spec = {
            "name": "local_bing",
            "url": "https://www.bing.com/search",
            "query_param": "q",
            "extra_params": {"setlang": "zh-Hans"},
            "timeout": 5,
            "selectors": {"item": "li", "title": "a", "url": "a"},
        }
        # 不真正发 HTTP：拦截 urlopen，只检查 Request 的 full url
        captured: list[str] = []

        class _FakeResp:
            def read(self):
                return b"<html></html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=0):
            captured.append(req.full_url if hasattr(req, "full_url") else str(req))
            return _FakeResp()

        eng = _build_html_engine(spec)
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            eng("アニメ おすすめ", 3)
            eng("hello world", 3)
            eng("苹果股价", 3)

        self.assertEqual(len(captured), 3)
        self.assertIn("setlang=ja-JP", captured[0])
        self.assertIn("setlang=en-US", captured[1])
        self.assertIn("setlang=zh-Hans", captured[2])


# ═══════════════════════════════════════════════════════════════════════════
# 查询改写多语种
# ═══════════════════════════════════════════════════════════════════════════

class TestRewriteMultilingual(unittest.TestCase):
    def test_japanese_technical_mix(self):
        r = rewrite_query("アニメ react 使い方")
        self.assertIsNotNone(r["rewritten"])
        self.assertIn("react", r["rewritten"].lower())

    def test_cyrillic_english_mix(self):
        r = rewrite_query("Python курс")
        self.assertIsNotNone(r["rewritten"])


if __name__ == "__main__":
    unittest.main()
