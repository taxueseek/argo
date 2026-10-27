#!/usr/bin/env python3
"""视频能力回归：元数据透传、时长筛选、视频路由、视频抓取。

这组测试全部离线：B 站/YouTube 的 HTTP 响应用 fixture 替身，fetch_v3 的
视频分支用 monkeypatch 顶掉，避免 CI 网络抖动把契约测试变成 flaky。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from candidate_envelope import result_to_candidate  # noqa: E402
from image_ops import _cli_result_row  # noqa: E402
from video_meta import (  # noqa: E402
    filter_duration, filter_video_results, is_video_result, is_video_url,
    parse_duration_seconds, result_duration_seconds, video_platform,
)

# 引擎脚本不在 scripts 顶层，按文件路径加载，避免为一个测试造包。
_spec = importlib.util.spec_from_file_location(
    "bilibili_engine_test", SCRIPT_DIR / "social_engines" / "bilibili_engine.py")
bilibili = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bilibili)


class TestVideoMeta(unittest.TestCase):
    def test_duration_formats(self):
        self.assertEqual(parse_duration_seconds(507), 507)
        self.assertEqual(parse_duration_seconds("507"), 507)
        self.assertEqual(parse_duration_seconds("8:27"), 507)
        self.assertEqual(parse_duration_seconds("1:0"), 60)
        self.assertEqual(parse_duration_seconds("1:02:03"), 3723)
        self.assertEqual(parse_duration_seconds("5分钟"), 300)
        self.assertEqual(parse_duration_seconds("90秒"), 90)
        self.assertEqual(parse_duration_seconds("1小时2分"), 3720)
        self.assertIsNone(parse_duration_seconds(None))
        self.assertIsNone(parse_duration_seconds(""))
        self.assertIsNone(parse_duration_seconds("不是时长"))

    def test_video_url_distinguishes_article(self):
        self.assertEqual(video_platform("https://www.bilibili.com/video/BV1x"),
                         "bilibili")
        self.assertTrue(is_video_url("https://youtu.be/abc123"))
        self.assertTrue(is_video_url("https://www.youtube.com/shorts/abc123"))
        self.assertTrue(is_video_url("https://www.douyin.com/video/123"))
        # B 站专栏/动态不是视频
        self.assertFalse(is_video_url("https://www.bilibili.com/read/cv123"))
        self.assertFalse(is_video_url("https://www.bilibili.com/opus/123"))
        # 抖音用户主页不是单条视频
        self.assertFalse(is_video_url("https://www.douyin.com/user/abc"))
        self.assertFalse(is_video_url("https://example.com/post/1"))

    def test_result_duration_prefers_social_meta(self):
        item = {"duration": "1:00",
                "social_meta": {"duration": "8:27", "content_type": "video"}}
        self.assertEqual(result_duration_seconds(item), 507)

    def test_video_filter_keeps_only_video(self):
        items = [
            {"url": "https://www.bilibili.com/video/BV1x"},
            {"url": "https://example.com/article"},
            {"url": "https://www.youtube.com/watch?v=abc"},
        ]
        kept, dropped = filter_video_results(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 1)
        self.assertTrue(is_video_result(kept[0]))

    def test_duration_filter_reports_unknown(self):
        results = [
            {"url": "https://www.bilibili.com/video/a",
             "social_meta": {"duration": "4:00"}},
            {"url": "https://www.bilibili.com/video/b",
             "social_meta": {"duration": "12:00"}},
            {"url": "https://www.youtube.com/watch?v=c"},
        ]
        kept, filtered, unknown = filter_duration(results, 300, 900)
        self.assertEqual([x["url"][-1] for x in kept], ["b", "c"])
        self.assertEqual(filtered, 1)
        self.assertEqual(unknown, 1)


class TestCliPassthrough(unittest.TestCase):
    def test_social_meta_survives_cli_parse(self):
        row = _cli_result_row({
            "title": "t", "url": "https://www.bilibili.com/video/BV1x",
            "snippet": "s",
            "social_meta": {"content_type": "video", "duration": "8:27",
                            "author": "up"},
        }, "bilibili")
        self.assertEqual(row["social_meta"]["duration"], "8:27")
        self.assertEqual(row["social_meta"]["author"], "up")


class TestEnvelopeVideo(unittest.TestCase):
    def test_bilibili_candidate_has_video_fields(self):
        candidate = result_to_candidate({
            "title": "红烧肉",
            "url": "https://www.bilibili.com/video/BV1x",
            "snippet": "UP主 某人 · 时长 8:27",
            "social_meta": {
                "content_type": "video",
                "duration": "8:27",
                "author": "某人",
                "play_count": 12345,
                "like_count": 678,
                "published_at": "2023-04-12T17:01:35+08:00",
            },
        }, "红烧肉", 1)
        self.assertEqual(candidate["content_type"], "video")
        self.assertEqual(candidate["duration_seconds"], 507)
        self.assertEqual(candidate["author"], "某人")
        self.assertEqual(candidate["metrics"]["views"], 12345)
        self.assertEqual(candidate["metrics"]["likes"], 678)
        self.assertEqual(candidate["published_at"],
                         "2023-04-12T17:01:35+08:00")

    def test_youtube_without_social_meta_is_video(self):
        candidate = result_to_candidate({
            "title": "t", "url": "https://www.youtube.com/watch?v=abc",
            "snippet": "s",
        }, "q", 1)
        self.assertEqual(candidate["content_type"], "video")
        self.assertIsNone(candidate["duration_seconds"])

    def test_non_video_social_stays_social_post(self):
        candidate = result_to_candidate({
            "title": "t", "url": "https://zhuanlan.zhihu.com/p/1",
            "snippet": "s", "social_meta": {"author": "u"},
        }, "q", 1)
        self.assertEqual(candidate["content_type"], "social_post")


class TestBilibiliEngine(unittest.TestCase):
    @staticmethod
    def _payload():
        return {"data": {"result": [{"result_type": "video", "data": [
            {"title": "红烧肉 <em class=\"keyword\">做法</em>",
             "duration": "8:27", "author": "楠楠", "play": 1672659,
             "video_review": 1869, "like": 40619, "favorites": 39340,
             "coin": 8926, "share": 10284, "pubdate": 1681290095,
             "bvid": "BV1MM4y127Z3", "description": "家常做法"},
            {"title": "长视频", "duration": "12:00", "author": "up",
             "play": 10, "bvid": "BV2", "description": ""},
        ]}]}}

    def test_parse_metadata_and_snippet(self):
        results = bilibili._parse_bilibili_response(self._payload(), 5)
        first = results[0]
        self.assertEqual(first["social_meta"]["duration_seconds"], 507)
        self.assertEqual(first["social_meta"]["author"], "楠楠")
        self.assertEqual(first["social_meta"]["play_count"], 1672659)
        self.assertIn("UP主 楠楠", first["snippet"])
        self.assertIn("时长 8:27", first["snippet"])
        self.assertIn("播放 167.3万", first["snippet"])
        self.assertNotIn("<em", first["title"])
        self.assertTrue(first["social_meta"]["published_at"].startswith("2023-"))

    def test_post_filter_by_duration(self):
        results = bilibili._parse_bilibili_response(
            self._payload(), 5, min_duration=600)
        self.assertEqual([x["social_meta"]["bvid"] for x in results], ["BV2"])

    def test_duration_bucket_mapping(self):
        # 只有下限：仅 ≥60min 能用档位 4；10/30min 下限不能用档位 2/3，
        # 否则会丢掉更长视频。
        self.assertIsNone(bilibili._duration_bucket(600, None))
        self.assertEqual(bilibili._duration_bucket(3600, None), 4)
        # 只有上限：仅 ≤10min 能用档位 1；30/60min 上限不能用档位 2/3，
        # 否则会丢掉更短视频。
        self.assertEqual(bilibili._duration_bucket(None, 600), 1)
        self.assertIsNone(bilibili._duration_bucket(None, 1800))
        # 双端落在同一档位内才下推，否则回退到后过滤。
        self.assertEqual(bilibili._duration_bucket(600, 1800), 2)
        self.assertIsNone(bilibili._duration_bucket(300, 900))


class TestVideoRoute(unittest.TestCase):
    def test_explicit_video_intent_routes_to_video(self):
        from route_domains import match_domains
        hits = match_domains("红烧肉 做法 视频")
        self.assertEqual(hits[0]["name"], "video")

    def test_platform_video_intent_beats_social(self):
        from route_domains import match_domains
        hits = match_domains("抖音视频 红烧肉 做法")
        self.assertEqual(hits[0]["name"], "video")

    def test_generic_tech_query_not_video(self):
        from route_domains import match_domains
        names = [h["name"] for h in match_domains("视频生成模型 原理")]
        self.assertNotIn("video", names)

    def test_short_video_marketing_not_video(self):
        from route_domains import match_domains
        names = [h["name"] for h in match_domains("短视频 运营 变现")]
        self.assertNotIn("video", names)

    def test_video_decision_shape(self):
        from video_route import video_decision
        d = video_decision(300, 900)
        self.assertEqual(d["domain"], "video")
        self.assertIn("bilibili", d["engines_combo"])
        self.assertTrue(d["video_only"])
        self.assertEqual(d["min_duration"], 300)
        self.assertEqual(d["max_duration"], 900)
        self.assertEqual(d["engine_request"], "--video")


class TestVideoCli(unittest.TestCase):
    def test_parser_accepts_video_flags(self):
        from search_cli import build_parser
        args = build_parser().parse_args(
            ["红烧肉", "--video", "--min-duration", "300",
             "--max-duration", "900"])
        self.assertTrue(args.video)
        self.assertEqual((args.min_duration, args.max_duration), (300, 900))

    def test_parser_rejects_negative_duration(self):
        from search_cli import build_parser
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["红烧肉", "--min-duration", "-1"])


class TestVideoFilterOrdering(unittest.TestCase):
    def test_filter_runs_before_pool_truncation(self):
        """视频过滤必须在 _pool_limit 截断之前。

        造 10 条网页 + 10 条视频、网页排在前面、max_results=3：若先截 top-3
        再过滤，视频会被整批截掉（2026-10-27 实测 --video --min-duration 时
        B 站结果就是这样消失的）。排序修复后应返回 3 条视频。
        """
        import search
        from video_meta import is_video_result
        fakes = [
            {"title": f"网页 {i}", "url": f"https://example.com/{i}",
             "snippet": "网页正文", "source": "bilibili"}
            for i in range(10)
        ] + [
            {"title": f"视频 {i}",
             "url": f"https://www.bilibili.com/video/BV{i}",
             "snippet": "视频简介", "source": "bilibili",
             "social_meta": {"content_type": "video", "duration": "8:27"}}
            for i in range(10)
        ]
        with mock.patch.object(search, "engine_search", return_value=fakes):
            out = search.super_search(
                "红烧肉 做法", engine="bilibili", n=3, video_only=True,
                skip_cache=True, envelope=False)
        self.assertEqual(out["count"], 3)
        self.assertTrue(all(is_video_result(r) for r in out["results"]))


class TestVideoFetch(unittest.TestCase):
    def test_id_helpers(self):
        from video_fetch import _bvid_of, _youtube_id
        self.assertEqual(_bvid_of("https://www.bilibili.com/video/BV1MM4y127Z3"),
                         "BV1MM4y127Z3")
        self.assertEqual(_youtube_id("https://youtu.be/abc123"), "abc123")
        self.assertEqual(_youtube_id("https://www.youtube.com/watch?v=xyz789"),
                         "xyz789")

    def test_bilibili_fetch_uses_api_metadata(self):
        from video_fetch import _fetch_bilibili
        view = {"code": 0, "data": {
            "title": "红烧肉", "desc": "家常做法", "duration": 507,
            "owner": {"name": "楠楠"}, "pubdate": 1681290095,
            "stat": {"view": 100, "like": 10, "favorite": 5, "danmaku": 3},
            "pages": [{"cid": 123}],
        }}
        player = {"code": 0, "data": {"subtitle": {"subtitles": []}}}

        def fake_json(url, timeout):
            return view if "web-interface/view" in url else player

        with mock.patch("video_fetch._get_json", side_effect=fake_json):
            result = _fetch_bilibili(
                "https://www.bilibili.com/video/BV1MM4y127Z3", 8000, 5)
        self.assertTrue(result["success"])
        self.assertTrue(result["content_ok"])
        self.assertIn("家常做法", result["content"])
        self.assertEqual(result["video_meta"]["duration_seconds"], 507)
        self.assertEqual(result["video_meta"]["author"], "楠楠")
        self.assertFalse(result["video_meta"]["transcript_available"])

    def test_douyin_falls_through_to_generic_chain(self):
        from video_fetch import fetch_video
        self.assertIsNone(
            fetch_video("https://www.douyin.com/video/123", 8000, 5))

    def test_fetch_v3_video_branch_sets_page_type(self):
        import fetch_v3
        fixture = {
            "url": "https://www.bilibili.com/video/BV1x",
            "title": "t", "content": "标题：t\n\n简介：d", "html": "",
            "length": 12, "success": True, "error": None,
            "fetch_method": "bilibili_api", "content_ok": True,
            "page_type": "video", "source_type": "video",
            "video_meta": {"duration_seconds": 507},
        }
        with mock.patch("video_fetch.fetch_video", return_value=fixture):
            result = fetch_v3.fetch_v3(
                "https://www.bilibili.com/video/BV1x", max_chars=8000,
                timeout=3, skip_cache=True)
        self.assertEqual(result["fetch_method"], "bilibili_api")
        self.assertEqual(result["page_type"], "video")
        self.assertTrue(result["content_ok"])


if __name__ == "__main__":
    unittest.main()
