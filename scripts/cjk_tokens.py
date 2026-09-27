#!/usr/bin/env python3
"""cjk_tokens — 中英混排查询的信号切分（长中文串按重叠 2-gram 展开）。

## 为什么需要它

全仓有多处「取查询 token，再用 `token in blob` 判断结果与查询是否相关」的门。
它们此前都用同一个正则：

    re.findall(r"[A-Z]{2,}|[a-zA-Z]{3,}|[\\u4e00-\\u9fff]{2,}", q)

对**无空格中文**，`[\\u4e00-\\u9fff]{2,}` 会把整句吃成一个 token
（「年最好看的科幻电影」→ `['年最好看的科幻电影']`）。结果里永远不会出现整句
原样，于是 `token in blob` 恒假、**真实结果一条都过不了门**。实测每查询取通用
引擎 5 条实回：

    查询                 实回  旧判据通过  本模块
    年最好看的科幻电影      5        0        5
    量子计算最新进展        5        0        5
    怎么学好深度学习        5        0        5
    冬奥会金牌榜            5        1        5
    东京旅游攻略推荐        5        0        5
    新能源汽车电池技术      5        2        5

失效面不止 recovery 兜底：百度百科相关度门、化学 token 交集、V2EX 中文节点
反查用的是同一个正则，四处一起静默失效——中文查询的兜底链路整体形同不存在。

## 判据

长中文串展开为**重叠 2-gram**（「科幻电影」→ 科幻/幻电/电影），再按调用方给的
停用词表逐个过滤。等价于「查询与文本的最长公共中文子串 ≥ 2 字」：比整段包含
宽松（可满足），比单字严格（挡掉「上」「大」这类噪声）。

停用词过滤是判据的另一半，不能省：不滤的话「最新」会在任何含「最新的XX」的
无关页面上命中，门就从「恒假」翻到「恒真」。

英文沿用原语义：`[A-Z]{2,}` 整词大写缩写、`[a-zA-Z]{3,}` 三字母以上词，一律
小写后比对。**但缩写不受停用词限制**——「who」本身就在停用词表里，滤掉会让
「WHO headquarters」一个信号都不剩，反而把门放成恒真。单字中文不成词
（与原 `{2,}` 一致）。
"""

from __future__ import annotations

import re

__all__ = ["CJK_RE", "SIGNAL_RE", "cjk_ngrams", "cjk_term_grams", "signal_tokens"]

#: 信号切分：大写缩写整词 / 三字母以上词 / 中文连续串。
SIGNAL_RE = re.compile(r"[A-Z]{2,}|[a-zA-Z]{3,}|[\u4e00-\u9fff]+")
#: 仅中文连续串。
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

#: 中文成词下限，与历史判据 `[\u4e00-\u9fff]{2,}` 一致——单字不成词。
MIN_CJK = 2


def cjk_ngrams(run: str, n: int = MIN_CJK) -> list[str]:
    """中文串 → 重叠 n-gram；不长于 n 时原样返回（长度门槛由调用方把）。"""
    if len(run) <= n:
        return [run]
    return [run[i:i + n] for i in range(len(run) - n + 1)]


def cjk_term_grams(text: str, n: int = MIN_CJK) -> list[str]:
    """只取中文串的 n-gram——供「用中文查中文标题」的调用方（如 V2EX 节点反查）。"""
    return [g for run in CJK_RE.findall(text or "")
            if len(run) >= MIN_CJK for g in cjk_ngrams(run, n)]


def signal_tokens(text: str, stop: "frozenset | set | None" = None,
                  n: int = MIN_CJK) -> list[str]:
    """查询/文本 → 信号 token 列表（全部小写，已按 `stop` 过滤）。

    Args:
        text: 查询或待比对文本。
        stop: 停用词表；命中即丢弃。中文按 n-gram 逐个比对，所以停用词写
            「最新」能同时挡掉 bigram「最新」。
        n: 中文 n-gram 宽度。
    """
    stop = stop or frozenset()
    out: list[str] = []
    for tok in SIGNAL_RE.findall(text or ""):
        if CJK_RE.fullmatch(tok):
            if len(tok) < MIN_CJK:
                continue  # 单字不成词
            out.extend(g for g in cjk_ngrams(tok, n) if g not in stop)
            continue
        low = tok.lower()
        # 大写缩写（WHO/NASA）不受停用词限制：历史语义如此，而且不能改——
        # 「who」本身就在停用词表里，滤掉会让「WHO headquarters」一个信号都不剩，
        # 于是 `if not keys: return True` 把门放成恒真。test_p0_v25 的 Chegg
        # 反例锁的就是这一点。
        if (tok.isupper() and len(tok) >= 2) or low not in stop:
            out.append(low)
    return out
