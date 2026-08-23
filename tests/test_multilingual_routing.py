#!/usr/bin/env python3
"""多语言 × 多领域黄金回归集（2026-08）

固化 P0（语言门控 + config 词界）与 P2（语言偏好软排序）的关键修复，
防止韩/日查询被中文泛内容域误捕获、或结果语言错配再退化。

触发依据（实测）：韩语「파이썬 웹프레임워크 성능 비교」曾因 weather 韩文
裸音节「비」误入天气域；日语「トヨタ 株価」曾因 chinese_general 的 `[一-\u9fff]`
误入中文通用域。主语言检测（primary_lang）本身正确，错在域判定无语言门控。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


class TestMultilingualLangGate(unittest.TestCase):
    """语言门控：明确的非中文查询不得被中文泛内容域误捕获。"""

    def test_korean_tech_not_weather(self):
        # 韩语「비교」（对比）不应再因「비」（雨）误入天气域
        from route import extract_features, match_domains, get_domains
        q = "파이썬 웹프레임워크 성능 비교"
        f = extract_features(q)
        self.assertEqual(f["primary_lang"], "ko")
        hits = match_domains(q, get_domains(), primary_lang=f["primary_lang"])
        self.assertTrue(all(h["name"] != "weather_query" for h in hits))

    def test_japanese_finance_not_chinese_general(self):
        from route import extract_features, match_domains, get_domains
        q = "トヨタ 株価 チャート"
        f = extract_features(q)
        self.assertEqual(f["primary_lang"], "ja")
        hits = match_domains(q, get_domains(), primary_lang=f["primary_lang"])
        self.assertTrue(all(h["name"] != "chinese_general" for h in hits))

    def test_korean_japanese_weather_still_weather(self):
        # 语言门控不得误伤真实的多语言天气查询
        from route import extract_features, match_domains, get_domains
        for q in ("서울 날씨", "東京の天気"):
            f = extract_features(q)
            hits = match_domains(q, get_domains(), primary_lang=f["primary_lang"])
            self.assertEqual(hits[0]["name"], "weather_query")

    def test_chinese_regression_unchanged(self):
        # 中文查询回归：weather / stock 域判定不受语言门控影响
        from route import match_domains, get_domains
        hits_w = match_domains("北京 今天 天气", get_domains(), primary_lang="zh")
        self.assertEqual(hits_w[0]["name"], "weather_query")
        hits_s = match_domains("贵州茅台 最新股价", get_domains(), primary_lang="zh")
        self.assertEqual(hits_s[0]["name"], "stock_query")


class TestAnimeVsFilmRoute(unittest.TestCase):
    """动漫推荐不误捕 film_search（live L_ja_gen），动画电影仍走 film_search。"""

    def test_anime_recommendation_goes_general(self):
        from route import route_query
        for q in ("アニメ おすすめ", "애니메이션 추천"):
            d = route_query(q)
            self.assertNotEqual(d["domain"], "film_search")
            self.assertEqual(d["domain"], "general_search")

    def test_animated_movie_still_film_search(self):
        from route import route_query
        d = route_query("アニメ映画 おすすめ")
        self.assertEqual(d["domain"], "film_search")
        self.assertIn("imdb", d["engines_combo"])


class TestLanguagePreferRerank(unittest.TestCase):
    """语言偏好软排序：ja/ko 前置含目标语言字符结果，软排不删除。"""

    def test_korean_prefers_hangul(self):
        from search import _lang_prefer_rerank
        results = [
            {"title": "abc xyz", "snippet": ""},
            {"title": "한국 뉴스", "snippet": ""},
            {"title": "python web", "snippet": ""},
            {"title": "파이썬 강좌", "snippet": ""},
        ]
        out = _lang_prefer_rerank(results, "ko")
        # 含谚文的结果应前移，纯拉丁结果后移；稳定保序
        self.assertTrue(out[0]["title"].startswith("한국"))
        self.assertTrue(out[1]["title"].startswith("파이썬"))
        self.assertTrue(any(r["title"] == "abc xyz" for r in out[-2:]))

    def test_non_ja_ko_noop(self):
        from search import _lang_prefer_rerank
        results = [{"title": "abc", "snippet": ""}]
        self.assertIs(_lang_prefer_rerank(results, "zh"), results)
        self.assertIs(_lang_prefer_rerank(results, None), results)


if __name__ == "__main__":
    unittest.main()
