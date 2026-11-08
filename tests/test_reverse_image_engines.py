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
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
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

    def test_missing_local_file_reports_error(self):
        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        out = eng("C:/no/such/dir/pic.png", n=3)
        self.assertIn("读不到本地图片", out[0]["error"])


class TestLocalImageInput(unittest.TestCase):
    """本地图片入口：SauceNAO 走 multipart、AnimeTrace 走 base64、Lens 走 SerpAPI 上传。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jpg = Path(self._tmp.name) / "pic.jpg"
        self.jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
        self.txt = Path(self._tmp.name) / "note.txt"
        self.txt.write_text("not an image", encoding="utf-8")

    def test_saucenao_uploads_file_instead_of_url(self):
        captured = {}

        def fake_post(params, data, filename, mime, timeout, engine):
            captured.update(params=params, data=data, filename=filename, mime=mime)
            return json.dumps({"header": {"status": 0}, "results": [
                {"header": {"similarity": "88.0"},
                 "data": {"pixiv_id": "123456", "ext_urls": [_PIXIV]}}]}), ""

        eng = engines._BUILDERS["saucenao"](_spec("saucenao"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image._saucenao_post_file", side_effect=fake_post):
            out = eng(self.jpg.as_uri(), n=3)
        self.assertEqual(out[0]["url"], _PIXIV)
        keys = [k for k, _ in captured["params"]]
        self.assertNotIn("url", keys)
        self.assertIn("api_key", keys)
        self.assertEqual(captured["filename"], "pic.jpg")
        self.assertEqual(captured["mime"], "image/jpeg")

    def test_animetrace_sends_base64_for_local_file(self):
        captured = {}

        class _Ctx:
            def __enter__(self):
                return io.BytesIO(json.dumps({"code": 0, "data": []}).encode())

            def __exit__(self, *a):
                return False

        def fake_open(req, **kw):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Ctx()

        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        with patch("engines_builders_reverse_image.http_open", side_effect=fake_open):
            eng(self.jpg.as_uri(), n=3)
        self.assertIn("base64", captured["body"])
        self.assertNotIn("url", captured["body"])

    def test_google_lens_uploads_local_file_and_searches_by_image_id(self):
        captured = {}

        class _Ctx:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return io.BytesIO(self._body)

            def __exit__(self, *a):
                return False

        def fake_open(req, **kw):
            if "serpapi.com/image" in req.full_url:
                captured["upload"] = req.data
                captured["upload_type"] = req.get_header("Content-type")
                return _Ctx(json.dumps({"message": "ok", "image_id": "img-1"}).encode())
            captured["search_url"] = req.full_url
            return _Ctx(json.dumps({"visual_matches": [
                {"title": "相似图", "link": "https://e.example/p"}]}).encode())

        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image.http_open", side_effect=fake_open):
            out = eng(self.jpg.as_uri(), n=3)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(captured["search_url"]).query)
        self.assertEqual(query["image_id"], ["img-1"])
        self.assertNotIn("url", query)
        self.assertIn("multipart/form-data", captured["upload_type"])
        self.assertIn(b'name="image"', captured["upload"])
        self.assertIn(b"\xff\xd8\xff", captured["upload"])
        self.assertEqual(out[0]["url"], "https://e.example/p")

    def test_google_lens_upload_error_surfaces(self):
        class _Ctx:
            def __enter__(self):
                return io.BytesIO(json.dumps(
                    {"error": "Invalid image format. Supported format: jpg, jpeg, png, webp"}
                ).encode())

            def __exit__(self, *a):
                return False

        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image.http_open", return_value=_Ctx()):
            out = eng(self.jpg.as_uri(), n=3)
        self.assertEqual(len(out), 1)
        self.assertIn("Invalid image format", out[0]["error"])

    def test_google_lens_refuses_image_over_500kb(self):
        big = Path(self._tmp.name) / "big.jpg"
        big.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * (600 * 1024))
        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"):
            out = eng(big.as_uri(), n=3)
        self.assertIn("500KB", out[0]["error"])

    def test_google_lens_refuses_mime_serpapi_cannot_take(self):
        gif = Path(self._tmp.name) / "pic.gif"
        gif.write_bytes(b"GIF89a" + b"0" * 32)
        eng = engines._BUILDERS["google_lens"](_spec("google_lens"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"):
            out = eng(gif.as_uri(), n=3)
        self.assertIn("JPEG/PNG/WebP", out[0]["error"])

    def test_saucenao_upload_shrinks_big_local_image(self):
        try:
            from PIL import Image as PILImage
        except ImportError:
            self.skipTest("Pillow 未安装，缩放分支不生效")
        big = Path(self._tmp.name) / "big.png"
        PILImage.new("RGB", (2400, 1600), (200, 120, 60)).save(big)
        captured = {}

        def fake_post(params, data, filename, mime, timeout, engine):
            captured.update(data=data, filename=filename, mime=mime)
            return json.dumps({"header": {"status": 0}, "results": []}), ""

        eng = engines._BUILDERS["saucenao"](_spec("saucenao"))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image._saucenao_post_file", side_effect=fake_post):
            eng(big.as_uri(), n=3)
        self.assertEqual(captured["mime"], "image/jpeg")
        self.assertEqual(captured["filename"], "big.jpg")
        self.assertTrue(captured["data"].startswith(b"\xff\xd8\xff"))
        self.assertLess(len(captured["data"]), big.stat().st_size)

    def test_non_image_file_is_refused(self):
        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        out = eng(self.txt.as_uri(), n=3)
        self.assertIn("不是可识别的图片", out[0]["error"])

    def test_local_roots_restrict_readable_dirs(self):
        other = Path(self._tmp.name) / "allowed"
        other.mkdir()
        eng = engines._BUILDERS["animetrace"](_spec("animetrace"))
        with patch.dict(os.environ, {"ARGO_IMAGE_LOCAL_ROOTS": str(other)}):
            out = eng(self.jpg.as_uri(), n=3)
        self.assertIn("ARGO_IMAGE_LOCAL_ROOTS", out[0]["error"])
        with patch.dict(os.environ, {"ARGO_IMAGE_LOCAL_ROOTS": str(self.jpg.parent)}):
            out = eng(self.txt.as_uri(), n=3)  # 目录放行，内容仍要过图片校验
        self.assertIn("不是可识别的图片", out[0]["error"])


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
        self.assertIn("作品 123456", out[0]["snippet"])
        self.assertIn("artist", out[0]["snippet"])
        self.assertEqual(out[0]["author"], "artist")
        self.assertEqual(out[0]["similarity"], 92.5)
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

    def test_request_carries_filter_and_dedupe_params(self):
        captured = {}

        def fake_get(url, *a, **kw):
            captured["url"] = url
            return json.dumps({"header": {"status": 0}, "results": []})

        eng = engines._BUILDERS["saucenao"](_spec("saucenao", hide=1, dedupe=2, db=999))
        with patch("engines_builders_reverse_image.get_env", return_value="k"), \
             patch("engines_builders_reverse_image._http_get_raw", side_effect=fake_get):
            eng(_IMG, n=3)
        url = captured["url"]
        self.assertIn("hide=1", url)
        self.assertIn("dedupe=2", url)
        self.assertIn("db=999", url)
        self.assertIn("output_type=2", url)
        self.assertIn("minsim=30", url)


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
        # URL 输入引擎含单条推文抽取；反搜图集合只收「图 → 出处页」的三条。
        self.assertIn("twitter_syndication", plan.URL_INPUT_ENGINES)
        self.assertNotIn("twitter_syndication", plan.REVERSE_IMAGE_ENGINES)
        for name in ("saucenao", "animetrace", "google_lens"):
            self.assertIn(name, plan.URL_INPUT_ENGINES)
            self.assertIn(name, plan.REVERSE_IMAGE_ENGINES)

    def test_registered_in_builders_and_families(self):
        from engine_families import family_of

        for name in ("saucenao", "animetrace", "google_lens"):
            self.assertIn(name, engines._BUILDERS)
            self.assertEqual(family_of(name), "misc_vertical")


if __name__ == "__main__":
    unittest.main()
