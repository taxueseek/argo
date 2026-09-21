"""BM25 聚焦提取 — 从长文本中提取与查询最相关的段落。

移植自 Hound 的 focus.py，简化为独立函数。
纯 Python 实现（re + math），无外部依赖。

BM25 参数：k1=1.5, b=0.75，使用 BM25+ 风格的正 IDF，
确保单个匹配词也能获得正分数。
"""

from __future__ import annotations

import math
import re

# 版本号优先：`6.2.2` 这类章节号必须整体成词。
# 原式 `[a-z0-9一-鿿]+` 会把 "6.2.2" 切成 "6"/"2"/"2"，再被长度下限逐个滤掉，
# 于是查询里的章节号**完全没参与打分**——实测对 RFC 查「6.2.2 Syntax-Based
# Normalization」时，最有区分度的那个词等于没写。可选版本号放在首分支，
# 交替匹配按分支顺序取，因此 "6.2.2" 会整体命中而不是退化成数字。
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)+|[a-z0-9一-鿿]+")
# 单个 CJK 字符是词（汉语词多为 1–2 字），单个拉丁字母/数字才是噪声
_CJK_CHAR_RE = re.compile(r"^[一-鿿]$")

# BM25 参数
_K1 = 1.5
_B = 0.75
# 默认评分阈值
_THRESHOLD = 1.0
# 当没有任何段落超过阈值时，回退保留的段落数
_FALLBACK_TOP = 5


def _tokens(text: str) -> list[str]:
    """分词：版本号整体成词、CJK 按字、拉丁按空白词，滤掉单字母噪声。

    长度下限只针对拉丁/数字：单个汉字本身就是一个词，用 `len >= 2` 一刀切
    会把「茶」「书」这类查询词整个丢掉——与质量评分里中文被系统性低评
    是同一类语种偏差。
    """
    out: list[str] = []
    for t in _TOKEN_RE.findall((text or "").lower()):
        if len(t) >= 2 or _CJK_CHAR_RE.match(t):
            out.append(t)
    return out


def _is_heading(block: str) -> bool:
    """判断块是否为 Markdown 标题。"""
    for line in block.splitlines():
        if line.strip():
            return line.lstrip().startswith("#")
    return False


# 目录块识别。目录把一个文档里所有章节标题聚在一个块里，查询命中章节标题时
# 它的词密度天然碾压正文段落——实测对 RFC 3986 查「6.2.2 Syntax-Based
# Normalization」，返回的是整篇目录，目标小节一个字没有。
#
# 两种 TOC 方言各给一个信号：
#   纯文本（RFC/论文/书籍）：引导点，如 "6.2.2.  Syntax-Based Normalization . . . 40"
#   Markdown：整行是一条指向本文锚点的列表链接，如 "- [Getting Started](#getting-started)"
_TOC_LEADER = re.compile(r"\.\s*\.\s*\.")          # ". . ." / "..." 引导点
_TOC_MD_LINK = re.compile(r"^\s*[-*+]\s*\[[^\]]+\]\(#")  # "- [标题](#锚)"
_TOC_MIN_LINES = 4      # 少于 4 行不成目录
_TOC_LINE_RATIO = 0.6   # 索引行占比达到此值即判为目录


def _is_toc_block(block: str) -> bool:
    """块是否为目录/索引（导航性质，不参与 focus 选块）。"""
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if len(lines) < _TOC_MIN_LINES:
        return False
    hits = sum(1 for ln in lines
               if _TOC_LEADER.search(ln) or _TOC_MD_LINK.match(ln))
    return hits / len(lines) >= _TOC_LINE_RATIO


# 重复块识别——页面样板（运行页眉/页脚/页码栏）。
#
# 这类块文本几乎逐字重复，且往往很短；BM25 的长度归一化偏爱短块，于是
# 「RFC 3986  URI Generic Syntax  January 2005」这种每页顶头的运行页眉，
# 会因为含有查询词 "syntax" 而集体胜出，把真正的正文段落挤出去——实测对
# RFC 3986 查「6.2.2 Syntax-Based Normalization」，输出几乎全是页眉副本。
# 一个文本重复出现这么多次，它承载的信息量必然接近于零。
_DUP_MIN_REPEAT = 3      # 同一文本出现次数达到此值即判为样板
_DUP_MAX_CHARS = 300     # 只对短文本判重（长段落重复更可能是合法引用）


def _boilerplate_blocks(blocks: list[str]) -> set[int]:
    """返回样板块索引集合（文本重复出现、且长度较短的块）。"""
    norm: dict[str, list[int]] = {}
    for i, b in enumerate(blocks):
        if len(b) > _DUP_MAX_CHARS:
            continue
        key = " ".join(b.split()).lower()
        if key:
            norm.setdefault(key, []).append(i)
    return {i for idxs in norm.values() if len(idxs) >= _DUP_MIN_REPEAT
            for i in idxs}


def _split_blocks(text: str) -> list[str]:
    """按空行切分文本为段落块（标题、段落、表格、列表）。"""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _fit_to_budget(keep: list[int], blocks: list[str],
                   scores: list[float], budget: int) -> list[int]:
    """按相关度优先裁剪保留集，直到正文总长落入预算。

    为什么需要：结果最终按**文档顺序**输出、再由调用方按 max_chars 截断。
    只按阈值选块时，靠后的高相关块会被整段截掉——实测对 RFC 3986 查
    「6.2.2 Syntax-Based Normalization」，选中的目标小节排在文档十万字符处，
    截断后用户手上只剩题头与目录，看上去像 BM25 偏袒目录。

    按分数降序装填（装不下的跳过而不是中断，好让短块也能补上），
    输出顺序仍由调用方还原为文档序。
    """
    if budget <= 0:
        return keep
    total = sum(len(blocks[i]) for i in keep) + 2 * max(len(keep) - 1, 0)
    if total <= budget:
        return keep
    kept: list[int] = []
    acc = 0
    for i in sorted(keep, key=lambda i: scores[i], reverse=True):
        seg = len(blocks[i]) + 2
        if acc + seg > budget:
            continue
        kept.append(i)
        acc += seg
    if not kept:
        # 一个完整块都装不下时，至少留下最高分的那个（交由调用方截断）。
        # 否则输出只剩头部提示行，而调用方还会以为「聚焦成功」——
        # 交出空正文比不聚焦更糟：实测长小节 + 默认 max_chars=8000 正是这条路径。
        kept = [max(keep, key=lambda i: scores[i])]
    return kept


def focus_extract(content: str, query: str, top_k: int = 5,
                  budget: int = 0) -> str:
    """BM25 聚焦提取：从长文本提取与查询最相关的段落。纯 Python，无依赖。

    budget > 0 时在选块阶段即按相关度装入预算，避免高相关块在后续按
    max_chars 截断时被砍掉（见 _fit_to_budget）。
    """
    # 空查询或短文本直接返回原文
    if not query or not content or not content.strip():
        return content
    if len(content) < 2000:
        return content
    blocks = _split_blocks(content)
    if len(blocks) <= 1:
        return content
    qterms = set(_tokens(query))
    if not qterms:
        return content

    block_tokens = [_tokens(bl) for bl in blocks]
    n = len(blocks)
    # `else 1.0`：没有段落时避免除零。此前写作 `else 0.0 or 1.0`（等价但读起来
    # 像是「0 也当 1 用」），且让紧随其后的 `if avgdl == 0` 看着像死代码——
    # 那条其实是活的：n>0 但所有段落都切不出词元时 avgdl 仍是 0。
    avgdl = (sum(len(t) for t in block_tokens) / n) if n else 1.0
    if avgdl == 0:
        avgdl = 1.0

    # 文档频率（每个词出现在多少个段落中）
    df: dict[str, int] = {}
    for toks in block_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    def idf(term: str) -> float:
        d = df.get(term, 0)
        # +1 保证 IDF 为正（BM25+ 风格）
        return math.log((n - d + 0.5) / (d + 0.5) + 1)

    def score(i: int) -> float:
        toks = block_tokens[i]
        if not toks:
            return 0.0
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        denom_len = _K1 * (1 - _B + _B * dl / avgdl)
        for term in qterms:
            f = tf.get(term)
            if f:
                s += idf(term) * (f * (_K1 + 1)) / (f + denom_len)
        return s

    scores = [score(i) for i in range(n)]
    # 目录与样板块不参与选块——阈值档与兜底档都要排除，否则兜底会把它们
    # 重新捞回来（这两类都不是「内容」，而是导航与重复装饰）
    excluded = _boilerplate_blocks(blocks)
    candidates = [i for i in range(n)
                  if i not in excluded and not _is_toc_block(blocks[i])]
    keep = [i for i in candidates if scores[i] >= _THRESHOLD]

    # 如果没有段落超过阈值，回靠取得分最高的 top_k 个
    if not keep:
        keep = sorted(candidates, key=lambda i: scores[i], reverse=True)[:top_k]

    # 预算内按相关度择优（不改变输出顺序，见 _fit_to_budget）
    if budget:
        keep = _fit_to_budget(keep, blocks, scores, budget)

    keep_set = set(keep)
    # 保留被保留段落前的标题（提供上下文）
    for i in keep:
        if i > 0 and _is_heading(blocks[i - 1]) and not _is_heading(blocks[i]):
            keep_set.add(i - 1)

    # 标题回填可能把总量顶回预算之上，再按分数裁一次（标题不参与裁剪，
    # 它是幸存块的语义锚点）
    if budget:
        total = sum(len(blocks[i]) for i in keep_set) + 2 * max(len(keep_set) - 1, 0)
        if total > budget:
            headings = {i for i in keep_set if _is_heading(blocks[i])}
            body = [i for i in keep_set if i not in headings]
            room = budget - sum(len(blocks[i]) + 2 for i in headings)
            keep_set = headings | set(
                _fit_to_budget(body, blocks, scores, max(room, 0)))

    # 按原始顺序输出
    kept = "\n\n".join(blocks[i] for i in range(n) if i in keep_set)
    header = (
        f"[Focus: {query!r}; showing {len(keep_set)} of {n} blocks "
        f"by BM25 relevance. Pass focus='' for the full page.]"
    )
    return header + "\n\n" + kept


# 聚焦场景的抓取额度放大系数——聚焦必须能看到整篇。
#
# 若 max_chars 在聚焦前生效，BM25 就只能在文档开头那一段里挑段落。实测对
# RFC 3986 查「6.2.2 Syntax-Based Normalization」：目标小节在十万字符处，
# 抓取时早被截掉，聚焦结果退化成题头与目录，看起来像「BM25 偏袒目录」，
# 真正的原因是截断早于选块。
#
# 所以聚焦时先按放大额度取回全文，选块后再裁回用户额度：
# 内存与缓存条目的代价只在显式使用 --focus 时付出，而它换来的是「能选中」。
FOCUS_FETCH_MULTIPLIER = 8
FOCUS_MIN_FETCH_CHARS = 100_000
# 上限：额度会随抓取结果一起进正文缓存（键含 _max_chars），无上限时
# 调用方传 max_chars=50000 就能把 400KB 正文写进共享缓存，内存与磁盘都失控。
# 20 万字符已覆盖绝大多数长文档（RFC 全文约 14 万）。
FOCUS_MAX_FETCH_CHARS = 200_000


def focus_fetch_chars(max_chars: int, query: str | None) -> int:
    """聚焦场景实际使用的抓取额度；无 query 时原样返回 max_chars。"""
    if not query:
        return max_chars
    return min(max(max_chars * FOCUS_FETCH_MULTIPLIER, FOCUS_MIN_FETCH_CHARS),
               FOCUS_MAX_FETCH_CHARS)


def apply_focus(result: dict, query: str, top_k: int = 5,
                max_chars: int | None = None) -> dict:
    """把 BM25 聚焦提取应用到抓取结果——CLI 与 MCP 共用的单一入口。

    背景：`--focus` / `focus` 这个能力同时暴露在 CLI（`argo fetch URL --focus`）
    与 MCP（`argo_fetch` 的 `focus` 参数）两处。两处各写一套裁剪逻辑时，
    文档描述的语义与实际行为容易分叉（历史 bug：CLI 侧压根没有该参数）。
    统一到这里，任何一侧新增调用方都自动继承同一语义。

    契约：
      - query 为空串或正文为空 → 原样返回，不写任何字段
      - 正文过短（focus_extract 内部按 2000 字符阈值判定）→ 不裁剪，
        focus_applied=False，调用方能看出「省 token 没生效」而不是静默当成功
      - 发生裁剪 → content/length 更新，focus_applied=True
    """
    if not query or not (result.get("content") or "").strip():
        return result
    original = result["content"]
    # 告知选块阶段的可用额度：留出头部提示行的位置，其余全部给正文，
    # 这样选出并输出的块恰好落在 max_chars 内，不会在最后一步被截掉。
    budget = 0
    if max_chars:
        header_len = len(
            f"[Focus: {query!r}; showing 000 of 000 blocks by BM25 relevance. "
            f"Pass focus='' for the full page.]"
        ) + 2
        budget = max(max_chars - header_len, 0)
    focused = focus_extract(original, query, top_k=top_k, budget=budget)
    result["focus_query"] = query
    # 正文为空（只剩头部提示行）时退回全文并如实标注未生效——交出空正文
    # 比不聚焦更糟，且不能让调用方以为省到了 token。
    body = focused.split("\n\n", 1)[1] if "\n\n" in focused else ""
    if focused == original or not body.strip():
        result["focus_applied"] = False
        return result
    result["content"] = focused
    result["length"] = len(focused)
    result["focus_applied"] = True
    # 聚焦前用的是放大额度，此处裁回用户额度（见 focus_fetch_chars）
    if max_chars and len(result["content"]) > max_chars:
        result["content"] = result["content"][:max_chars]
        result["length"] = len(result["content"])
    return result


if __name__ == "__main__":
    # 简单测试：构造超过 2000 字符的文本
    block = """
## 异步编程

Python asyncio 提供 async/await 语法，事件循环调度协程。
适合 IO 密集型任务，比多线程更轻量。
""".strip()
    long_text = ("# 编程指南\n\n" + block * 30)  # >2000 字符

    # 正常过滤
    result = focus_extract(long_text, "异步编程 事件循环", top_k=2)
    print(result[:600])

    # 空查询
    assert focus_extract(long_text, "") == long_text

    # 短文本直接返回
    assert focus_extract("短文本", "查询") == "短文本"

    # apply_focus：发生裁剪 → 记账为已应用
    res = {"content": long_text, "length": len(long_text)}
    out = apply_focus(res, "异步编程 事件循环")
    assert out["focus_applied"] is True
    assert out["length"] == len(out["content"])

    # apply_focus：短文本不裁剪 → 诚实记账为未应用（而非假装成功）
    short = {"content": "短文本", "length": 3}
    short_out = apply_focus(short, "查询")
    assert short_out["focus_applied"] is False
    assert short_out["content"] == "短文本" and short_out["length"] == 3

    print("\n测试通过。")
