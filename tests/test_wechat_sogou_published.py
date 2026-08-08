#!/usr/bin/env python3
"""tests/test_wechat_sogou_published.py — 搜狗微信引擎发布时间提取

P1 吸收 wechat-article-search 技能的核心增量：公众号文章发布时间。
结果条内嵌 script 写入 document.write(timeConvert('10位unix秒'))，
builder 应将其转换为 ISO 时间写入 published_at 字段（仅当存在时，
避免给无时间结果加空字段噪音）。

全程 mock 网络层，离线必过。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from engines_builders_tech import _build_wechat_sogou_engine  # noqa: E402

# ── 模拟搜狗微信结果页 ────────────────────────────────────────────────────────

_HTML_WITH_TIME = """<html><body>
<li id="sogou_vr_11002601_box_0">
    <h3><a href="/weixin?url=abc123"><span>标题一</span><!--red_beg-->高亮<!--red_end--></a></h3>
    <p class="txt-info">这是摘要一<!--red_beg-->中高亮<!--red_end--></p>
    <span class="all-time-y2">公众号A</span>
    <script>document.write(timeConvert('1786194753'))</script>
</li>
<li id="sogou_vr_11002601_box_1">
    <h3><a href="https://weixin.sogou.com/other">标题二</a></h3>
    <p class="txt-info">摘要二</p>
    <span class="all-time-y2">公众号B</span>
</li>
</body></html>"""

_HTML_WITHOUT_TIME = """<html><body>
<li id="sogou_vr_11002601_box_0">
    <h3><a href="/weixin?url=abc">无时间标题</a></h3>
    <p class="txt-info">无时间摘要</p>
    <span class="all-time-y2">公众号C</span>
</li>
</body></html>"""


class _FakeResp:
    def __init__(self, html: str) -> None:
        self._html = html

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._html.encode("utf-8")


def _run(html: str) -> list[dict]:
    engine = _build_wechat_sogou_engine({})
    with patch("urllib.request.urlopen", return_value=_FakeResp(html)):
        return engine("测试", n=5)


class TestWechatSogouPublishedAt(unittest.TestCase):
    """P1：发布时间的提取、格式与回归保护"""

    def test_timeconvert_extracted(self) -> None:
        results = _run(_HTML_WITH_TIME)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertIn("published_at", first, "有 timeConvert 的结果应带 published_at")
        self.assertRegex(
            first["published_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            f"published_at 应为 ISO 时间: {first['published_at']}",
        )

    def test_missing_time_no_field(self) -> None:
        results = _run(_HTML_WITHOUT_TIME)
        self.assertEqual(len(results), 1)
        self.assertNotIn("published_at", results[0], "无时间戳不应加空字段噪音")

    def test_no_time_does_not_break_other_fields(self) -> None:
        results = _run(_HTML_WITH_TIME)
        first, second = results[0], results[1]
        self.assertEqual(first["title"], "标题一高亮")
        self.assertEqual(first["snippet"], "这是摘要一中高亮")
        self.assertEqual(first["account"], "公众号A")
        self.assertTrue(first["url"].startswith("https://weixin.sogou.com"))
        # 第二条无时间戳：其余字段不受影响
        self.assertEqual(second["title"], "标题二")
        self.assertEqual(second["account"], "公众号B")
        self.assertNotIn("published_at", second)

    def test_bad_timestamp_ignored(self) -> None:
        html = _HTML_WITH_TIME.replace("1786194753", "999999999999")
        results = _run(html)
        self.assertNotIn("published_at", results[0], "非法时间戳应忽略")


if __name__ == "__main__":
    unittest.main()
