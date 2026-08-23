#!/usr/bin/env python3
"""readability_extract.py 的单元测试：正文提取、噪音排除、顺序保持。

P0 验收：
  1. 链接密集的导航/侧栏/页脚不进入正文
  2. 正文段落保持文档顺序（旧实现 sort-by-density 会把页脚插进正文）
  3. 标题提取正确
  4. 无正文页面返回空
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from readability_extract import extract_readability, score_blocks  # noqa: E402


def _article_html() -> str:
    """标准文章页：导航 + 侧栏 + 正文 + 页脚。"""
    nav_links = "".join(
        f'<a href="/{i}">导航项目{i} 查看更多内容点击这里</a>' for i in range(8)
    )
    side_links = "".join(
        f'<a href="/tag/{i}">标签{i} 相关推荐更多内容</a>' for i in range(6)
    )
    return f"""<html><head><title>测试文章标题</title></head><body>
<nav>{nav_links}</nav>
<div class="sidebar">{side_links}</div>
<article>
  <h1>第一段正文标题内容</h1>
  <p>这是正文的第一段。讲述了人工智能与机器学习在现代社会中的应用场景，
  以及它们如何改变我们的日常工作和生活方式。内容充实且连贯，适合作为测试用例。</p>
  <p>这是正文的第二段。继续阐述深度学习模型在自然语言处理领域取得的重要进展，
  包括注意力机制与大规模预训练模型的技术细节与工程实践。</p>
  <p>这是正文的第三段。讨论模型部署与推理优化的挑战，包括量化、剪枝与蒸馏等
  技术路径，以及它们在真实业务系统中的落地效果。</p>
</article>
<div class="related">相关阅读 <a href="/r1">推荐文章一</a> <a href="/r2">推荐文章二</a></div>
<footer>© 2026 测试站 | <a href="/about">关于我们</a> <a href="/privacy">隐私政策</a></footer>
</body></html>"""


class TestReadabilityExtract(unittest.TestCase):
    def test_extracts_title(self):
        content, title = extract_readability(_article_html())
        self.assertEqual(title, "测试文章标题")

    def test_keeps_document_order(self):
        """正文三段的顺序必须保持（不按密度重排）。"""
        content, _ = extract_readability(_article_html())
        self.assertIn("第一段正文标题内容", content)
        self.assertIn("这是正文的第一段", content)
        self.assertIn("这是正文的第二段", content)
        self.assertIn("这是正文的第三段", content)
        first = content.index("这是正文的第一段")
        second = content.index("这是正文的第二段")
        third = content.index("这是正文的第三段")
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_excludes_nav_and_footer(self):
        """导航/侧栏/页脚不进入正文。"""
        content, _ = extract_readability(_article_html())
        self.assertNotIn("导航项目", content)
        self.assertNotIn("标签0", content)
        self.assertNotIn("关于我们", content)
        self.assertNotIn("隐私政策", content)
        self.assertNotIn("© 2026", content)

    def test_body_dominates(self):
        """正文应占输出绝大部分。"""
        content, _ = extract_readability(_article_html(), max_chars=8000)
        # 导航 8 项 + 侧栏 6 项都该被滤掉；正文 3 段全保留
        self.assertGreater(len(content), 120)

    def test_empty_html_returns_empty(self):
        content, title = extract_readability("<html><body></body></html>")
        self.assertEqual(content, "")
        self.assertEqual(title, "")

    def test_short_noise_only_page_returns_empty(self):
        """纯按钮/面包屑页（无正文）不应产出内容。"""
        html = "<html><body>" + "".join(
            f'<button>按钮{i} 提交</button>' for i in range(10)
        ) + "</body></html>"
        content, _ = extract_readability(html)
        self.assertEqual(content, "")

    def test_link_heavy_nav_beats_plain_text(self):
        """链接密集的导航块分数必须低于正文块。"""
        html = """<html><body>
        <div class="nav"><a href="/1">首页 链接文字很长很长很长</a><a href="/2">产品 链接文字很长很长很长</a></div>
        <div class="body"><p>这是唯一的一篇真正的正文内容，长度足够长，
        包含足够多的有效信息量，用来验证链接密集的导航块不会被误判为正文章节。</p></div>
        </body></html>"""
        content, _ = extract_readability(html)
        self.assertIn("唯一的一篇真正的正文", content)
        self.assertNotIn("首页", content)
        self.assertNotIn("产品", content)

    def test_score_blocks_placeholder_keeps_order(self):
        """P1 占位：无 query 精排时保持原顺序。"""
        parts = ["第一段", "第二段", "第三段"]
        ranked = score_blocks("", parts)
        self.assertEqual([p for _, p in ranked], parts)


if __name__ == "__main__":
    unittest.main()
