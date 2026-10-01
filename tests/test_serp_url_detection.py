"""搜索引擎结果页识别：一份数据源 + 后缀匹配，不能有第二份字面量。

原状（2026-09-26 实测）：

    https://www.google.com/search?q=x     -> DROP
    https://www.google.co.jp/search?q=x   -> keep   ← 漏
    https://duckduckgo.com/?q=x           -> keep   ← 漏
    https://search.yahoo.co.jp/search?p=x -> keep   ← 漏

同一个概念（搜索引擎结果页，不可当信源正文）得到两种判定。后果不是「多留了
一条噪声」而是**这类 URL 会带着正常的 authority 分进入最终输出**——被当成本
站正文引用，而它其实是聚合页。

两个成因，都已修：

1. `serp_host_markers`（source_types_cn.json）与 `evidence.py` 里并列的字面量
   集合**各写一份**。后者少列 google.co.jp / duckduckgo.com 等 international
   入口，漏网就是这么来的。现在代码只读数据源。
2. 判定用**等值**比较，而 `_normalize_domain` 只去 `www.`、子域原样保留。
   同一搜索引擎常有多个入口域（yahoo.co.jp 与 search.yahoo.co.jp、
   duckduckgo.com 与 lite.duckduckgo.com、brave.com 与 search.brave.com），
   等值下每上一个入口就要补一条表项——那正是「加一个漏一个」的机制本身。
   现在用后缀匹配，配一张短表就覆盖全部入口。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from evidence import is_serp_or_jump_url  # noqa: E402


class TestSerpDetectionCoversAllCcTldsAndEntrypoints(unittest.TestCase):
    MUST_DROP = [
        "https://www.bing.com/search?q=test",
        "https://www.google.com/search?q=x",
        "https://www.google.co.jp/search?q=x",
        "https://www.google.com.hk/search?q=x",
        "https://search.yahoo.co.jp/search?p=x",
        "https://duckduckgo.com/?q=x",
        "https://lite.duckduckgo.com/lite/?q=y",
        "https://search.brave.com/search?q=z",
        "https://weixin.sogou.com/weixin?type=2",
        "https://www.baidu.com/s?wd=x",
        "https://baidu.com/link?url=abc",
        "https://www.sogou.com/web?query=z",
    ]

    MUST_KEEP = [
        "https://example.com/page",
        "https://ja.wikipedia.org/wiki/記憶の宮殿",
        "https://qiita.com/npaka/items/abc123",
        "https://news.yahoo.co.jp/articles/2026/abc",   # 真文章，不是结果页
        "https://blog.google.co.jp/products/nexus",     # 真文章，不是 /search
        "https://gigazine.net/news/20260924-google-heir/",
        # 网盘分享正文链：host 后缀命中 baidu.com，但 path 是 /s/<id>——
        # 曾被 path.startswith("/s") 连坐误杀（中文资源类查询的常见结果）；
        # 百度 SERP 的 path 恰为 /s，精确元组已覆盖
        "https://pan.baidu.com/s/1abcDEF-xyz?pwd=x",
    ]

    def test_result_pages_are_dropped(self):
        for url in self.MUST_DROP:
            with self.subTest(url=url):
                self.assertTrue(
                    is_serp_or_jump_url(url),
                    f"搜索结果页未被识别，会被当正文信源：{url}",
                )

    def test_real_articles_are_kept(self):
        for url in self.MUST_KEEP:
            with self.subTest(url=url):
                self.assertFalse(
                    is_serp_or_jump_url(url),
                    f"正文页被误判为结果页，会损失正常信源：{url}",
                )

    def test_no_second_literal_set_in_code(self):
        """数据源是唯一真源：代码里不得再并列写一份域名集合。

        两处各写一份时，漏网是必然的——加搜索引擎的人只会想到其中一处。
        """
        import inspect
        import evidence
        src = inspect.getsource(evidence.is_serp_or_jump_url)
        self.assertNotIn(
            '"bing.com"', src,
            "evidence.py 里又出现了 serp 域名字面量，与 source_types_cn.json "
            "构成第二处真源",
        )
        self.assertIn(
            "serp_host_markers", src,
            "应从 source_types_cn.json 的 serp_host_markers 读",
        )

    def test_uses_suffix_matching_not_equality(self):
        """入口子域必须自动覆盖，而不是靠往表里逐条补。"""
        import inspect
        import evidence
        src = inspect.getsource(evidence.is_serp_or_jump_url)
        self.assertIn(
            'endswith("." + m)', src,
            "仍用等值比较：search.yahoo.co.jp 这类入口域需要逐条补表项，"
            "正是「加一个漏一个」的成因",
        )


if __name__ == "__main__":
    unittest.main()
