#!/usr/bin/env python3
"""bm25.py — local-search 的相关度打分（BM25Okapi + 中英混合切词）。

问题（第一性）：local-search 此前的打分是**纯位次衰减**
（`score = max(0.7 - idx*0.05, 0.1)`）——「第 3 条结果」永远比「第 1 条
不相关结果」分高，无论它是否真的包含查询词。位次是引擎给的先验（有价值，
点击数据比词面匹配强），但把它当唯一依据等于宣布「词面相关度不重要」：
实测查询词在第 3 条标题里完整出现、第 1 条只是泛相关页时，排序无动于衷。

设计（MECE，两层信号各司其职）：
  - BM25 相关度：title + snippet 对查询的词面匹配（TF 饱和 + 长文档惩罚 +
     IDF  Rare-term 加权）。回答「这条内容是否真的关于这个查询」。
  - 引擎位次先验：引擎自己的排序（它的点击/质量信号）。回答「源站认为
    谁更好」。
  两者按权重混合（默认 BM25 0.6 / 位次 0.4）：任一侧无信号时另一侧兜底——
  查询词切不出 token（纯停用词）或引擎不给 snippet 时，BM25 侧为 0，
    混合分退化为旧位次序，行为与升级前逐位一致（fail-soft）。

切词（与 local_image._tokens 同源思路，按 BM25 的需要分叉）：
  - ASCII 连续串 → 小写词元（python / 3.14 / node.js）；
  - CJK 连续串 → 字符二元组（「异步编程」→ 异步/步编/编程）；
    单字 CJK 串 → 单字（「酒」→ 酒，二元无从切起）。
  为什么 CJK 用二元而非单字：单字在任何中文文档里都泛滥（的/是/了），
  IDF 失去鉴别力，BM25 会退化成「文档越长分越低」。二元组是中文无分词器
  场景下的标准近似（信息检索文献的标准做法）。

BM25 口径（Okapi，k1=1.5, b=0.75，Lucene 式 IDF 保证小语料非负）：
  score(q,d) = Σ_t IDF(t) · f(t,d)·(k1+1) / (f(t,d) + k1·(1-b+b·|d|/avgdl))
  IDF(t)     = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5))
N 为语料文档数（一次搜索采集到的全部结果），n(t) 为含 t 的文档数。
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

# ASCII 词元 / CJK 连续串。CJK 必须匹配**整个串**（不是单字）——二元组在
# _tokenize 里对连续 CJK 串二次生成；正则若按单字匹配，二元逻辑永远走不到。
_TOKEN_RE = re.compile(r"[A-Za-z0-9_+\-.#]+|[\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_K1 = 1.5
_B = 0.75
_DELTA = 0.5          # BM25+ 下界 (Lv & Zhai 2011)：长文档命中词保底，消除过惩罚
# 混合权重：BM25 侧。位次先验占 1 - _W_BM25。
_W_BM25 = 0.6
# 旧位次衰减口径（max(0.7 - idx*0.05, 0.1)）——先验的归一化基准。
_PRIOR_BASE = 0.7


def tokenize(text: str) -> list[str]:
    """查询/文档统一切词（两侧必须同函数，否则 TF/IDF 对不上）。"""
    if not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if _CJK_RE.fullmatch(tok):
            # CJK 串：二元组 + （单字串时）单字本身
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok.lower())
    return out


def _position_prior(idx: int) -> float:
    """引擎位次先验（旧打分口径，保持行为连续）。"""
    return max(_PRIOR_BASE - idx * 0.05, 0.1)


class BM25:
    """一次搜索语料上的 BM25 打分器。

    语料 = 本次采集到的全部结果（跨引擎池化）：IDF 在池上计算比在单个
    引擎的 5 条结果上稳定得多——「含查询词的文档有多罕见」是跨源问题。
    """

    def __init__(self, docs: list[list[str]], k1: float = _K1, b: float = _B, delta: float = _DELTA):
        self.k1 = k1
        self.b = b
        self.delta = delta
        self.n_docs = len(docs)
        self.doc_len = [len(d) for d in docs]
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        # term -> [doc 频次, {doc_idx: tf}]
        self.tf: dict[str, dict[int, int]] = {}
        for i, d in enumerate(docs):
            for t in d:
                self.tf.setdefault(t, {}).setdefault(i, 0)
                self.tf[t][i] += 1
        self._idf_cache: dict[str, float] = {}

    def idf(self, term: str) -> float:
        cached = self._idf_cache.get(term)
        if cached is not None:
            return cached
        n = len(self.tf.get(term, {}))
        # Lucene 式：+1 在 ln 内，n=N（语料内全含）时仍为正（小语料常见）
        v = math.log(1.0 + (self.n_docs - n + 0.5) / (n + 0.5))
        self._idf_cache[term] = v
        return v

    def score(self, query_terms: list[str], doc_idx: int) -> float:
        if self.n_docs == 0 or not query_terms:
            return 0.0
        dl = self.doc_len[doc_idx] or 1
        denom_norm = self.k1 * (1.0 - self.b + self.b * dl / (self.avgdl or 1.0))
        total = 0.0
        for t in query_terms:
            f = self.tf.get(t, {}).get(doc_idx, 0)
            if not f:
                continue
            sat = (f * (self.k1 + 1.0)) / (f + denom_norm)
            # BM25+ 饱和项下界增益（Lv & Zhai 2011）
            total += self.idf(t) * (sat + self.delta)
        return total


# 权威站点与官方开发者/百科/学术域（白名单适度加成）
_AUTHORITATIVE_DOMAINS: frozenset[str] = frozenset({
    "github.com", "gitlab.com", "stackoverflow.com", "wikipedia.org",
    "arxiv.org", "developer.mozilla.org", "docs.python.org", "python.org",
    "golang.org", "go.dev", "rust-lang.org", "kernel.org", "w3.org",
    "learn.microsoft.com", "pypi.org", "npmjs.com", "baike.baidu.com",
    "zhihu.com", "segmentfault.com", "juejin.cn", "v2ex.com",
    "nature.com", "science.org", "acm.org", "ieee.org",
})

# 常见低质采集站/垃圾农场特征（微幅降权）
_NOISE_DOMAIN_PATTERNS: tuple[str, ...] = (
    "csdn.net", "jb51.net", "iteye.com",
)


def _domain_quality_prior(url: str) -> float:
    """基于 URL 域名的先验质量分（-0.3 ~ +0.5）。

    零依赖纯规则：高信噪比技术、百科、学术域给予正向增益，低信噪比/采集站特征适度降权。
    仅用于在词面相关（BM25 > 0）的前提下微调，不破坏 fail-soft 契约。
    """
    if not url or "://" not in url:
        return 0.0
    try:
        domain = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    except Exception:
        return 0.0
    for auth in _AUTHORITATIVE_DOMAINS:
        if domain == auth or domain.endswith("." + auth):
            return 0.5
    for noise in _NOISE_DOMAIN_PATTERNS:
        if noise in domain:
            return -0.3
    return 0.0


def _url_tokens_text(url: str) -> str:
    """提取 URL path 中的 slug 词元作为弱辅助匹配信号。"""
    if not url or "://" not in url:
        return ""
    try:
        path = url.split("://", 1)[1].split("?", 1)[0].split("#", 1)[0]
        slugs = re.findall(r"[A-Za-z0-9_-]{2,}", path)
        return " ".join(slugs)
    except Exception:
        return ""


def _phrase_proximity_bonus(query: str, title: str, snippet: str) -> float:
    """词邻近度与短语连续匹配加成（归一化为 0.0~1.0）。

    当完整查询短语或紧邻词元在标题或摘要逐字紧密出现时给予增益，提升复合词精准命中率。
    """
    q_low = (query or "").lower().strip()
    if not q_low or len(q_low) < 2:
        return 0.0
    t_low = (title or "").lower()
    s_low = (snippet or "").lower()

    # 1. 完整短语匹配优先
    if q_low in t_low:
        return 1.0  # 标题完整覆盖查询短语
    if q_low in s_low:
        return 0.6  # 摘要完整覆盖查询短语

    # 2. 多词紧邻跨度匹配
    sub_terms = [t for t in re.split(r"[\s\-_,.:;!?，。！？]+", q_low) if len(t) >= 2]
    if len(sub_terms) >= 2:
        if all(st in t_low for st in sub_terms):
            positions = [t_low.find(st) for st in sub_terms]
            if max(positions) - min(positions) <= 50:
                return 0.85
        if all(st in s_low for st in sub_terms):
            positions = [s_low.find(st) for st in sub_terms]
            if max(positions) - min(positions) <= 100:
                return 0.4

    return 0.0


def _doc_text(r: dict[str, Any]) -> str:
    """参与相关度计算的文档文本：title 权重高于 snippet。
    BM25F 近似加权：标题 3 遍，正文 1 遍，URL slug 辅助字段 1 遍。"""
    title = (r.get("title") or "").strip()
    snippet = (r.get("snippet") or "").strip()
    url_slug = _url_tokens_text(r.get("url") or "")
    parts = []
    if title:
        parts.extend([title, title, title])
    if snippet:
        parts.append(snippet)
    if url_slug:
        parts.append(url_slug)
    return " ".join(parts) if parts else ""


def rerank(by_engine: dict[str, list[dict[str, Any]]], query: str,
           w_bm25: float = _W_BM25) -> dict[str, list[dict[str, Any]]]:
    """按「BM25 相关度 × 引擎位次先验」混合分重排每个引擎的结果列表。

    原地修改（results 的 `score` 字段更新为混合分并重排），返回同一 dict。
    调用点：search_v3 采集完成后、RRF 融合前——RRF 的输入序即相关度序。

    Fail-soft：查询切不出 token、或全部文档零 BM25（无任何词面重叠）时，
    混合分退化为位次先验，排序与升级前逐位一致。
    """
    q_terms = tokenize(query)
    flat: list[tuple[str, int, dict[str, Any]]] = []
    for eng, results in by_engine.items():
        for i, r in enumerate(results):
            if not isinstance(r, dict):
                continue  # 防御：单条畸形结果不应杀死整个重排
            flat.append((eng, i, r))
    if not flat:
        return by_engine

    docs = [tokenize(_doc_text(r)) for _, _, r in flat]
    scorer = BM25(docs)
    raw = [scorer.score(q_terms, i) for i in range(len(flat))]
    max_raw = max(raw) if raw else 0.0

    for (eng, idx, r), bm in zip(flat, raw):
        bm25_norm = (bm / max_raw) if max_raw > 0 else 0.0
        prior_norm = _position_prior(idx) / _PRIOR_BASE
        prox_val = _phrase_proximity_bonus(query, r.get("title", ""), r.get("snippet", ""))
        domain_val = _domain_quality_prior(r.get("url", ""))
        rel_score = min(max(bm25_norm * 0.70 + prox_val * 0.18 + domain_val * 0.12, 0.0), 1.0) if bm25_norm > 0 else 0.0
        r["score"] = round(w_bm25 * rel_score + (1.0 - w_bm25) * prior_norm, 3)

    for eng in by_engine:
        by_engine[eng].sort(
            key=lambda r: (r.get("score", 0.0) if isinstance(r, dict) else -1.0),
            reverse=True)
    return by_engine
