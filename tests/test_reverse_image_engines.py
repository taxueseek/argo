#!/usr/bin/env python3
"""反搜图引擎单元测试：SauceNAO（缺 key/状态码/出处还原）与 AnimeTrace（角色标签/限频）。

两条引擎的输入契约与关键词引擎不同——query 必须是公网图片 URL——这里锁死三件事：

1. 非图片查询在入口就被挡下并给出可读错误，不能静默返回空（空 = 「查了没找到」，
   与「参数喂错了」同形，调用方无从区分）；
2. 上游的业务失败码（SauceNAO header.status、AnimeTrace code 17728/17737）
   映射成 error 条目而不是空结果；
3. 结果必须带 url（SERP 守卫会丢弃空 url 条目）：SauceNAO 用 pixiv/ext_urls 还原
   出处页，AnimeTrace 没有出处页、给 Bangumi 检索入口，注释里已说明那是查证入口。

全 mock，无网络。
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import engines  # noqa: E402
import plan  # noqa: E402

_IMG = "https://files.yande.re/image/abc/yande.re%20447155%20sample.jpg"
_PIXIV = "https://www.pixiv.net/artworks/123456"


def _spec(name: str, **extra) -> dict:
    base = {"type": name, "_name": name, "timeout": 5}
    base.update(extra)
    return base


class TestImageUrlGate(unittest.TestCase):
    def test_keyword_query_is_rejected_with_reason(self):
        for name in ("saucenao", "animetrace"):
            eng = engines._BUILDERS[name](_spec(name))
            out = eng("哲学是什么", n=3)
            self.assertEqual(len(out), 1)
            self.assertIn("公网图片 URL", out[0]["error"])
            self.assertEqual(out[0]["source"], name)

    def test_local_path_is_rejected(self):
        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        out = eng("C:/Users/me/pic.png", n=3)
        self.assertIn("error", out[0])


class TestSaucenao(unittest.TestCase):
    def test_missing_key_explains_anonymous_ban(self):
        eng = engines._BUILDERS["saucenao"](_spec("saucenao"))
        with patch("engines_builders_reverse_image.get_env", return_value=""):
            out = eng(_IMG, n=3)
        self.assertIn("ARGO_SAUCENAO_API_KEY", out[0]["error"])
        self.assertIn("匿名档", out[0]["error"])

    def test_results_carry_pixiv_source_and_similarity(self):
        payload = {
            "header": {"status": 0, "long_remaining": 150},
            "results": [{
                "header": {"similarity": "92.5", "index_name": "Index #5",
                           "thumbnail": "https://img.example/thumb.jpg"},
                "data": {"title": "sample artwork", "member_name": "artist",
                         "pixiv_id": "123456", "ext_urls": [_PIXIV]},
            }],
        }
        eng = engines._BUILDERS["saucenao"](_spec("saucenao", min_similarity=30))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image._http_get_raw",
                   return_value=json.dumps(payload)):
            out = eng(_IMG, n=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], _PIXIV)
        self.assertIn("相似度 92.5%", out[0]["snippet"])
        self.assertIn("artist", out[0]["snippet"])
        self.assertEqual(out[0]["metadata"]["similarity"], 92.5)

    def test_negative_status_is_error_not_empty(self):
        payload = {"header": {"status": -1, "message": "anonymous not permitted"},
                   "results": []}
        eng = engines._BUILDERS["saucenao"](_spec("saucenao"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image._http_get_raw",
                   return_value=json.dumps(payload)):
            out = eng(_IMG, n=3)
        self.assertIn("status -1", out[0]["error"])
        self.assertIn("匿名档", out[0]["error"])


class TestAnimetrace(unittest.TestCase):
    def test_characters_mapped_to_bangumi_lookup(self):
        payload = {
            "code": 0,
            "trace_id": "t-1",
            "data": [{
                "box": [0.1, 0.2, 0.3, 0.4],
                "not_confident": False,
                "character": [{"work": "スイセイギンカ", "character": "星井青香"}],
            }],
        }
        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        resp = io.BytesIO(json.dumps(payload).encode())

        class _Ctx:
            def __enter__(self):
                return resp

            def __exit__(self, *a):
                return False

        with patch("engines_builders_reverse_image.http_open", return_value=_Ctx()):
            out = eng(_IMG, n=3)
        self.assertEqual(len(out), 1)
        self.assertIn("星井青香", out[0]["title"])
        self.assertIn("スイセイギンカ", out[0]["snippet"])
        # 出处页不存在：给 Bangumi 检索入口，且 url 非空（SERP 守卫要 url）
        self.assertTrue(out[0]["url"].startswith("https://bgm.tv/subject_search/"))
        self.assertEqual(out[0]["metadata"]["trace_id"], "t-1")

    def test_rate_limit_code_surfaces_hint(self):
        body = json.dumps({"code": 17737, "zh_message": "请求过于频繁，请稍后再试"})
        err = urllib.error.HTTPError(
            "https://api.animetrace.com/v1/search", 429, "Too Many Requests",
            None, io.BytesIO(body.encode()))
        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        with patch("engines_builders_reverse_image.http_open", side_effect=err):
            out = eng(_IMG, n=3)
        self.assertIn("HTTP 429", out[0]["error"])
        self.assertIn("17737", out[0]["error"])
        self.assertIn("频率限制", out[0]["error"])


class TestGoogleLens(unittest.TestCase):
    def test_missing_key_says_where_to_register(self):
        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        with patch("engines_builders_reverse_image.get_env", return_value=""):
            out = eng(_IMG, n=3)
        self.assertIn("ARGO_SERPAPI_API_KEY", out[0]["error"])

    def test_three_buckets_are_labelled_and_ordered(self):
        payload = {
            "exact_matches": [{"position": 1, "title": "原图页", "link": "https://a.example/p1",
                               "source": "Blog", "thumbnail": "https://a.example/t1.jpg"}],
            "visual_matches": [{"position": 1, "title": "相似图", "link": "https://b.example/p2",
                                "source": "Reddit",
                                "price": {"value": "¥99"}}],
            "organic_results": [{"position": 1, "title": "讲到这张图的网页",
                                 "link": "https://c.example/p3", "snippet": "正文摘要"}],
        }
        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        resp = io.BytesIO(json.dumps(payload).encode())

        class _Ctx:
            def __enter__(self):
                return resp

            def __exit__(self, *a):
                return False

        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image.http_open", return_value=_Ctx()):
            out = eng(_IMG, n=5)
        self.assertEqual([r["url"] for r in out],
                         ["https://a.example/p1", "https://b.example/p2", "https://c.example/p3"])
        self.assertEqual(out[0]["metadata"]["match_type"], "精确匹配")
        self.assertEqual(out[1]["metadata"]["match_type"], "视觉相似")
        self.assertIn("¥99", out[1]["snippet"])
        self.assertEqual(out[2]["metadata"]["match_type"], "相关网页")
        # 精确匹配排在视觉相似之前：分数单调递减
        self.assertGreater(out[0]["score"], out[1]["score"])
        self.assertEqual(out[0]["image_url"], "https://a.example/t1.jpg")

    def test_duplicate_links_collapsed_and_cap_applied(self):
        payload = {
            "visual_matches": [
                {"title": "A", "link": "https://d.example/x"},
                {"title": "A again", "link": "https://d.example/x"},
                {"title": "B", "link": "https://d.example/y"},
            ],
        }
        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        resp = io.BytesIO(json.dumps(payload).encode())

        class _Ctx:
            def __enter__(self):
                return resp

            def __exit__(self, *a):
                return False

        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image.http_open", return_value=_Ctx()):
            out = eng(_IMG, n=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://d.example/x")

    def test_serpapi_error_field_surfaces_as_error_entry(self):
        payload = {"error": "Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key"}
        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        resp = io.BytesIO(json.dumps(payload).encode())

        class _Ctx:
            def __enter__(self):
                return resp

            def __exit__(self, *a):
                return False

        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image.http_open", return_value=_Ctx()):
            out = eng(_IMG, n=3)
        self.assertEqual(len(out), 1)
        self.assertIn("Invalid API key", out[0]["error"])


class TestUrlInputEngines(unittest.TestCase):
    def test_engine_set_covers_all_url_extract_engines(self):
        # URL 输入引擎含单条推文抽取；反搜图集合只收「图 → 出处页」的四条。
        self.assertIn("twitter_syndication", plan.URL_INPUT_ENGINES)
        self.assertNotIn("twitter_syndication", plan.REVERSE_IMAGE_ENGINES)
        for name in ("saucenao", "animetrace", "google_lens", "tineye"):
            self.assertIn(name, plan.URL_INPUT_ENGINES)
            self.assertIn(name, plan.REVERSE_IMAGE_ENGINES)

    def test_registered_in_builders_and_families(self):
        from engine_families import family_of

        for name in ("saucenao", "animetrace", "google_lens"):
            self.assertIn(name, engines._BUILDERS)
            self.assertEqual(family_of(name), "misc_vertical")


if __name__ == "__main__":
    unittest.main()
