"""ncpssd_bridge 的离线单测 —— 不联网，只钉住请求形态与字段降级行为。

上游只吃表单 POST，argo 的 CLI 引擎在拿到桥输出后按 title/url/snippet/published_at
取字段；这两处约定一旦漂，引擎会静默变成空结果，所以在这里钉死。
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ncpssd_bridge as bridge  # noqa: E402


class ExpressionTest(unittest.TestCase):
    def test_covers_every_tag(self):
        expr = bridge._expression("唯物史观")
        self.assertTrue(expr.startswith("(") and expr.endswith(")"))
        for tag in bridge.TAGS:
            self.assertIn(f'{tag}="唯物史观"', expr)
        self.assertEqual(expr.count('="唯物史观"'), len(bridge.TAGS))


class CleanTest(unittest.TestCase):
    def test_strips_tags_and_joins_lists(self):
        self.assertEqual(bridge._clean("<font>蒙慧</font>"), "蒙慧")
        self.assertEqual(bridge._clean(["甲", "", "乙"]), "甲；乙")
        self.assertEqual(bridge._clean(None), "")
        self.assertEqual(bridge._clean("  间隔  "), "间隔")


class DateTest(unittest.TestCase):
    def test_takes_calendar_day(self):
        self.assertEqual(bridge._date("2026-09-18T00:00:00.000+0000"), "2026-09-18")

    def test_missing_or_unparsable_is_empty(self):
        self.assertEqual(bridge._date(""), "")
        self.assertEqual(bridge._date("不详"), "")


class FetchTest(unittest.TestCase):
    @staticmethod
    def _response(payload):
        ctx = mock.MagicMock()
        ctx.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        return ctx

    def test_pdf_url_wins_and_metadata_is_labelled(self):
        payload = {"data": {"rows": [{
            "title": "<font>唯物史观</font>与现代化",
            "creator": "蒙慧[1];秦亚萍[1]",
            "cbw_name": "中国社会科学",
            "years": "2026",
            "volumn": "3",
            "num": "5",
            "beginpage": "12",
            "endpage": "20",
            "type": "中文期刊文章",
            "tab_name": "journalArticle",
            "id": "abc",
            "date": "2026-09-18T00:00:00.000+0000",
            "remark": "<font>摘要</font>正文",
            "pdfurl": "https://ftprp.ncpssd.cn/pdf/1.pdf",
        }]}}
        with mock.patch.object(bridge.urllib.request, "urlopen", return_value=self._response(payload)):
            items = bridge.fetch("唯物史观", 5)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "唯物史观与现代化")
        self.assertEqual(item["url"], "https://ftprp.ncpssd.cn/pdf/1.pdf")
        self.assertEqual(item["published_at"], "2026-09-18")
        self.assertIn(
            "https://www.ncpssd.cn/Literature/articleinfo?type=journalArticle&id=abc",
            item["snippet"],
        )
        for fragment in ("蒙慧[1];秦亚萍[1]", "中国社会科学", "第3卷", "第5期", "12-20页", "摘要正文"):
            self.assertIn(fragment, item["snippet"])

    def test_without_pdf_falls_back_to_detail_page(self):
        payload = {"data": {"rows": [
            {"title": "无全文", "id": "xyz", "tab_name": "journalArticle"},
        ]}}
        with mock.patch.object(bridge.urllib.request, "urlopen", return_value=self._response(payload)):
            item = bridge.fetch("q", 3)[0]

        self.assertEqual(
            item["url"],
            "https://www.ncpssd.cn/Literature/articleinfo?type=journalArticle&id=xyz",
        )
        self.assertNotIn("详情", item["snippet"])

    def test_skips_rows_without_title(self):
        rows = [{"title": ""}, {"title": "A", "id": "1"}, {"title": "B", "id": "2"}]
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               return_value=self._response({"data": {"rows": rows}})):
            items = bridge.fetch("q", 10)
        self.assertEqual([item["title"] for item in items], ["A", "B"])

    def test_request_is_form_post_with_clamped_page_size(self):
        captured = {}

        def fake(req, timeout=None):
            captured["method"] = req.get_method()
            captured["ctype"] = req.get_header("Content-type") or ""
            captured["body"] = req.data.decode("utf-8")
            return self._response({"data": {"rows": []}})

        with mock.patch.object(bridge.urllib.request, "urlopen", side_effect=fake):
            bridge.fetch("a b", 99)

        self.assertEqual(captured["method"], "POST")
        self.assertIn("application/x-www-form-urlencoded", captured["ctype"])
        self.assertIn("pageSize=30", captured["body"])
        self.assertIn("search=%28IKTE%3D%22a+b%22", captured["body"])


class MainTest(unittest.TestCase):
    def test_error_path_prints_parseable_empty_result(self):
        with mock.patch.object(bridge, "fetch", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = bridge.main(["x"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["items"], [])
        self.assertIn("RuntimeError: boom", payload["error"])

    def test_success_path_prints_items(self):
        with mock.patch.object(bridge, "fetch", return_value=[{"title": "t", "url": "u"}]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = bridge.main(["x", "-n", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["items"][0]["title"], "t")


if __name__ == "__main__":
    unittest.main()
