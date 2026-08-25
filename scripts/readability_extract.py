#!/usr/bin/env python3
"""
readability_extract.py — 正文提取（readability 密度法，纯标准库）

设计（P0，自研实现，思路对标 Mozilla readability / crawl4ai fit-markdown，
不复制任何第三方源码）：
  1. 块级密度评分：score = (文本 - 2×链接文本) / (块字符数 + 1)
     链接越多的块（导航/侧栏/页脚）分数越低，无需 query 即可区分正文与噪音。
  2. 标签权重：article/main 加权，li/h1-h4 降权，div/p 基准。
  3. 容器归并：同深度的相邻块合并为同一正文段，保持文档顺序输出
     （旧实现 sort-by-density 会把页脚插进正文中间）。
  4. 可选第二级精排接口（P1）：score_blocks(query, blocks) 占位，
     query 缺失时纯第一级生效。

零外部依赖，单次调用毫秒级。供 fetch_v3 等接入。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Callable, Sequence

SKIP_TAGS = {
    "script", "style", "nav", "header", "footer", "aside", "noscript",
    "iframe", "form", "button", "dialog",
}
# 容器级标签：块归并的候选父级（出现在 starttag 时开启一个新容器层）
CONTAINER_TAGS = {"div", "article", "main", "section", "blockquote"}
# 段落级标签：正文块的边界（td 列其中：单元格是块级单元，结束即 flush，
# 否则纯文本表格（无 p/li）的数据会随容器清空逻辑整块丢失）
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "td"}

# 标签权重：正文候选得分乘数
TAG_WEIGHT = {
    "article": 1.5, "main": 1.5, "blockquote": 1.2, "p": 1.1,
    "td": 1.0, "div": 1.0, "section": 0.9, "pre": 1.0,
    "h1": 0.6, "h2": 0.6, "h3": 0.6, "h4": 0.6, "h5": 0.6, "h6": 0.6,
    "li": 0.5, "tr": 0.5,
}
MIN_BLOCK_CHARS = 30  # 短块直接丢弃（按钮/面包屑/导航项）
MAX_BLOCK_CHARS = 6000  # 超过视为容器包全页，不额外加分（防 div 吞全页）
# 长文本块需要的密度下限（去空字符比例）：正文段落通常 >0.6
MIN_DENSITY = 0.55


class TextBlock:
    """一个正文候选块。"""

    __slots__ = ("text", "link_chars", "depth", "weight", "start", "end", "is_heading")

    def __init__(self, text: str, link_chars: int, depth: int, weight: float, is_heading: bool = False):
        self.text = text
        self.link_chars = link_chars
        self.depth = depth
        self.weight = weight
        self.start = -1  # 文档序占位，由收集器赋值
        self.end = -1
        self.is_heading = is_heading

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def density(self) -> float:
        """去空格字符占比（块文本的紧凑度）。"""
        non_space = len(self.text.replace(" ", "").replace("\n", ""))
        return non_space / max(self.chars, 1)

    @property
    def score(self) -> float:
        """readability 密度分：惩罚链接文本，加权标签。"""
        base = (self.chars - 2 * self.link_chars) / max(self.chars + 1, 1)
        # 密度惩罚：导航/侧栏通常文本稀疏（链接密集），正文段落紧凑
        base *= self.density / (MIN_DENSITY * 1.2)
        base *= self.weight
        if self.chars < 80 and not self.is_heading:
            base *= 0.6  # 短块降权（可能是导航项）；标题短但信息密度高，不降权
        if self.chars > MAX_BLOCK_CHARS:
            base *= MAX_BLOCK_CHARS / self.chars  # 超长块降权（容器吞全页）
        return base


class ReadabilityExtractor(HTMLParser):
    """基于 readability 密度法的正文提取器（保持文档顺序）。"""

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._link_depth = 0
        self._current: list[str] = []
        self._current_link: list[str] = []
        self._depth = 0
        self._blocks: list[TextBlock] = []
        self._container_stack: list[int] = []  # 当前容器层级的块索引起点
        self.title = ""
        self._in_title = False

    # ── HTMLParser 回调 ──────────────────────────────────────────────

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIP_TAGS:
            self._in_skip += 1
            return
        if self._in_skip:
            return
        if tag in CONTAINER_TAGS | BLOCK_TAGS:
            self._depth += 1
        if tag == "a":
            self._link_depth += 1
        if tag in CONTAINER_TAGS:
            self._container_stack.append(len(self._blocks))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS:
            self._in_skip = max(0, self._in_skip - 1)
            return
        if self._in_skip:
            return
        if tag == "a":
            self._link_depth = max(0, self._link_depth - 1)
        if tag in CONTAINER_TAGS:
            start = self._container_stack.pop() if self._container_stack else None
            # 仅当容器内**没有产出任何正文块**时（纯链接侧栏/导航），丢弃累积文本，
            # 避免污染后续同深度块的 link_chars（负数 score 会拖垮正文组）。
            # 容器内已有正文块（如表格 td 数据）必须保留累积，否则 td/p 之外的
            # 剩余文本（残句、表格数据）被整个丢掉。
            if start is not None and len(self._blocks) == start:
                self._current = []
                self._current_link = []
        if tag in BLOCK_TAGS:
            self._flush_block(tag)
        if tag in CONTAINER_TAGS | BLOCK_TAGS:
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._in_skip or not data.strip():
            return
        if self._link_depth > 0:
            self._current_link.append(data)
        else:
            self._current.append(data)

    # ── 内部 ─────────────────────────────────────────────────────────

    def _flush_block(self, tag: str) -> None:
        text = "".join(self._current).strip()
        link_chars = len("".join(self._current_link).strip())
        self._current = []
        self._current_link = []
        # 标题类标签（h1-h6）放宽短块阈值：文章标题/小节标题信息密度高，
        # 30 字符下限会把「第一段正文标题内容」这类短标题丢掉。
        # td 单元格同理：表格是原子数据单元（「型号」「15999 元」），
        # 30 字符下限会把整张表格的数据全部丢光；2 字符下限只丢单字噪声。
        is_heading = tag.startswith("h") and len(tag) == 2 and tag[1].isdigit()
        min_chars = 2 if tag == "td" else (6 if is_heading else MIN_BLOCK_CHARS)
        if len(text) < min_chars:
            return
        weight = TAG_WEIGHT.get(tag, 1.0)
        block = TextBlock(text, link_chars, self._depth, weight, is_heading=is_heading)
        block.start = len(self._blocks)
        block.end = block.start + 1
        self._blocks.append(block)

    # ── 结果组装 ─────────────────────────────────────────────────────

    def extract(self, max_chars: int = 8000) -> list[str]:
        """按文档顺序返回正文段（容器归并后）。"""
        if not self._blocks:
            return []
        # 按容器深度分组归并：同一深度且相邻的块合并成一段
        merged: list[tuple[float, list[TextBlock]]] = []
        current_group: list[TextBlock] = []
        current_depth: int | None = None

        for block in self._blocks:
            if current_depth is None or block.depth == current_depth:
                current_group.append(block)
                current_depth = block.depth
            else:
                merged.append((self._group_score(current_group), current_group))
                current_group = [block]
                current_depth = block.depth
        if current_group:
            merged.append((self._group_score(current_group), current_group))

        # 保留与最高分组同量级的组：导航/页脚得分通常差 3-6 倍，
        # 用 max*0.6 比固定分位稳（分位会把「第二高但仍是噪音」的组带上）。
        if not merged:
            return []
        max_score = max(s for s, _ in merged)
        threshold = max_score * 0.6
        kept = [g for s, g in merged if s >= threshold]
        kept.sort(key=lambda g: g[0].start)  # 文档顺序
        parts: list[str] = []
        total = 0
        for group in kept:
            text = "\n".join(b.text for b in group)
            if not text.strip():
                continue
            if total + len(text) > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    parts.append(text[:remaining].rstrip())
                break
            parts.append(text)
            total += len(text)
        return parts

    @staticmethod
    def _group_score(blocks: Sequence[TextBlock]) -> float:
        """一组块的总分：求和归一化到块数，避免容器内块数虚高。"""
        if not blocks:
            return 0.0
        return sum(b.score for b in blocks) / max(len(blocks) ** 0.5, 1)


# ── 模块级接口 ─────────────────────────────────────────────────────────


def extract_readability(html: str, max_chars: int = 8000) -> tuple[str, str]:
    """提取正文（readability 密度法）与标题。"""
    ext = ReadabilityExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    parts = ext.extract(max_chars=max_chars)
    return "\n\n".join(parts), ext.title.strip()


def score_blocks(query: str, parts: Sequence[str]) -> list[tuple[float, str]]:
    """P1 预留：按 query 对正文段精排（BM25/余弦占位，未实现）。

    P0 阶段保持原顺序；接入 query 精排时在此实现并替换调用点。
    """
    _ = query
    return [(0.0, p) for p in parts]


if __name__ == "__main__":
    import sys
    import urllib.request

    url = sys.argv[1] if len(sys.argv) > 1 else "https://docs.python.org/3/"
    req = urllib.request.Request(url, headers={"User-Agent": "unified-search/2.5"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read(400000).decode("utf-8", errors="replace")
    content, title = extract_readability(raw, max_chars=2000)
    print(f"标题: {title}")
    print(f"正文长度: {len(content)}")
    print(content[:600])
