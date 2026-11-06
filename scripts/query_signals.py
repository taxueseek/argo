#!/usr/bin/env python3
"""query_signals.py — 查询改写与「结果够不够」的判定。

从 search.py 中外提。这里放的全是**本地启发式**判断，不发起网络请求、
不依赖搜索编排状态，因此可以被搜索之外的层（评测、路由、批量任务）复用。

两类信号：
  - 查询改写（apply_query_rewrite）：把口语化查询换成更适合检索的形式
  - 早停判定（results_sufficient / cumulative_sufficient / query_coverage_ok）：
    结果是否已经够用、可以不再等慢源
"""

from __future__ import annotations

import math
import re
from typing import Any

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD_RE = re.compile(r"[a-z0-9]+")

# 查询意图词：描述「怎么问」而不是「问什么」，不参与区分性判定。
# 只收高频且跨查询无区分度的词；保留 review/practice 这类可能承载主题的词，
# 避免把正常的结果集误判成「只命中了泛词」。
_INTENT_STOPWORDS = frozenset({
    "how", "what", "why", "when", "where", "which", "who",
    "the", "and", "or", "for", "with", "without", "vs", "versus",
    "to", "of", "in", "on", "at", "by", "from", "about", "is", "are",
    "best", "top", "good", "guide", "tutorial", "recommend",
    "怎么", "如何", "什么", "为什么", "哪些", "哪个", "推荐", "教程",
})


def _query_terms(query: str) -> list[str]:
    """把查询切成「主题词」：CJK 按连续片段（≥2 字），拉丁按词（≥2 字符）。

    与 `relevance_units` 的区别：那个返回 bigram，用于连续覆盖率；这里返回
    完整片段，用于回答「这条结果到底命中了查询里的几个词」。中文「钙钛矿
    太阳能 稳定性」是三个主题词，不是六个 bigram——按 bigram 数判定会把
    「只命中太阳能」也算成命中两次。
    """
    q = (query or "").lower()
    terms: list[str] = []
    for seg in _CJK_RUN_RE.findall(q):
        # 中文意图词同样要滤：「如何 部署」若把「如何」当主题词，整批结果
        # 都不命中它，区分性判据会误拒一条本来就答对的部署结果。
        if len(seg) >= 2 and seg not in _INTENT_STOPWORDS:
            terms.append(seg)
    for word in _LATIN_WORD_RE.findall(q):
        if len(word) >= 2 and word not in _INTENT_STOPWORDS:
            terms.append(word)
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


def _term_covered(term: str, text: str,
                  tokens: set[str] | None = None) -> bool:
    """主题词是否被文本命中：CJK 按 bigram 过半，拉丁按词干前缀。

    `tokens` 由调用方按结果预算一次（同一条文本要判多个主题词），
    不传时自行分词，保持单测可直接调用。
    """
    if _CJK_RUN_RE.fullmatch(term):
        units = [term[i:i + 2] for i in range(len(term) - 1)]
        if not units:
            return term in text
        hits = sum(1 for unit in units if unit in text)
        # 词序/分隔（「记忆 插件」vs「记忆插件」）不应让整词判负：
        # 过半 bigram 命中即算这个词出现。
        return hits >= max(1, math.ceil(len(units) * 0.5))
    if tokens is None:
        try:
            from tfidf_router import tokenize
            tokens = set(tokenize(text))
        except ImportError:
            return term in text
    if term in tokens:
        return True
    # 英文词形变化：commit / commits / committed 视为同一主题词。
    return len(term) >= 4 and any(tok.startswith(term) for tok in tokens)


def _discriminative_coverage_ok(
    results: list[dict[str, Any]],
    query: str,
    *,
    _pre_full_tokens: list[tuple[str, set[str]]] | None = None,
) -> bool:
    """至少一条结果同时命中**两个**查询主题词。

    2026-10-26 实测：查询「git commit」时 local_bing 返回 8 条泛化 Git 页
    （Wikipedia / Install for Windows / Documentation），标题只命中 git，
    旧守卫按「过半结果命中任意一个词」判定为够用 → 早停，真正对题的
    git-commit 手册页被取消。只命中泛词（工具名）不等于回答了查询。

    判据取「同一条结果命中 ≥2 个主题词」而不是提高覆盖率阈值：长查询的
    正常结果本就只覆盖一半词，阈值一高就误杀；而「只命中一个泛词」在任何
    查询长度下都是同一种失败。单主题词查询（如「Crawl4AI」）不适用。
    """
    terms = _query_terms(query)
    if len(terms) < 2:
        return True
    if _pre_full_tokens is not None:
        for text, tokens in _pre_full_tokens:
            if sum(1 for term in terms if _term_covered(term, text, tokens)) >= 2:
                return True
        return False
    try:
        from tfidf_router import tokenize
    except ImportError:
        return True  # 分词不可用不设卡（与 query_coverage_ok 同一 fail-open）
    for r in results:
        if not isinstance(r, dict):
            continue
        text = f"{r.get('title') or ''} {r.get('snippet') or ''}".lower()
        tokens = set(tokenize(text))
        covered = sum(1 for term in terms
                      if _term_covered(term, text, tokens))
        if covered >= 2:
            return True
    return False


def apply_query_rewrite(query: str) -> tuple[str, dict | None]:
    """统一查询改写逻辑，返回 (改写后的查询, 改写结果字典)。

    改写失败时静默返回原查询，不影响搜索流程。
    """
    try:
        from query_rewriter import rewrite_query as do_rewrite
        result = do_rewrite(query)
        if result["rewritten"] and result["confidence"] >= 0.7:
            return result["rewritten"], result
    except ImportError:
        pass  # query_rewriter 模块不可用，使用原查询
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询改写跳过: {type(e).__name__}")
    return query, None


def results_sufficient(
    results: list[dict[str, Any]],
    mode: str = "auto",
    min_results: int | None = None,
    query: str = "",
) -> bool:
    return _sufficient_internal(results, mode, min_results, query)


def cumulative_sufficient(raw_results: dict[str, list[dict[str, Any]]],
                          mode: str = "auto",
                          min_results: int | None = None,
                          query: str = "") -> bool:
    """跨引擎累计结果是否已够（wave-2 提前终止判定）。

    与 results_sufficient 同阈值，但把 raw_results 里所有已完成的
    引擎结果合并成一条列表再判，避免单一引擎不足时重复等待慢源。
    壳链不算「够数」的口径在 _sufficient_internal 统一定义
    （2026-10-11 热榜实测，判定与下游 serp_guard 过滤对齐）。
    """
    merged: list[dict[str, Any]] = []
    for res in raw_results.values():
        if not res:
            continue
        merged.extend(
            r for r in res if isinstance(r, dict) and "error" not in r
        )
    return _sufficient_internal(merged, mode, min_results, query)


def is_query_echo(results: list[dict[str, Any]], query: str) -> bool:
    """这批结果是否是「查询词回声」——上游把整句当单词去查，返回查询中
    **某一个词**的词典释义/同主题页。

    2026-10-17 实锤：「keyboard switch lube service 2026」全批命中
    "Online Virtual Keyboard"、「best mechanical keyboard for typing 2026」
    全批命中 "BEST 的词典释义"——整批标题都在回声**同一个**查询词。

    判据只看标题：snippet 里的功能词（for/the/and…）会把单条覆盖抬到 2，
    判据即被击穿——实测 "Shop Best Buy for electronics" 仅因含 for 就
    cov=2，而标题侧仍只有 best 一个词。标题是结果的核心，功能词出现率低。

    只判整批、不判单条：长尾查询本就有弱命中，单条覆盖 1 词完全正常；
    整批都只覆盖同一个词才是「上游降级」的信号。零网络成本（只读文本与
    查询词元），可在早停（rerank 之前，rel 尚不存在）与输出层（恢复链）
    复用——这与 cache_guard.is_degraded_results 的「整批退化」同构，区别是
    那里读 rerank 的 relevance，这里读词面。
    """
    if not query or not query.strip() or not results:
        return False
    try:
        from tfidf_router import tokenize
    except ImportError:
        return False  # 分词不可用不判（fail-open）
    q_set = set(tokenize(query))
    # 查询词 <4 不判：短查询的结果标题天然只命中一两个词，拦它即误伤（实测
    # 「OpenAI MCP specification」的专利/论文结果只命中 specification 一个词，
    # 却被判回声 → 恢复链误触发，白打两个引擎还把金标输出撑大）。
    if len(q_set) < 4:
        return False
    # 至少 3 条结果才判：一两条短标题完全可能是正确答案（实测
    # 「what is the capital of France」→ "Paris capital" 只命中 capital 一个词，
    # 但那就是答案）。
    if len(results) < 3:
        return False
    covered_terms: set[str] = set()
    for r in results:
        if isinstance(r, dict):
            covered_terms |= (q_set & set(tokenize(r.get("title") or "")))
    # 判据取「整批标题覆盖的**不同**查询词数恰好为 1」：
    #   0 个 = 完全无关，归 covered 判据管（算进来会让离线 mock 误触发恢复链，
    #         实测 test_funnel 的 called 从 2 涨到 4）；
    #   1 个 = 整批都在回声同一个词（"Online Virtual Keyboard" 之类）；
    #   ≥2 个 = 结果确实在回答查询的不同侧面，不是回声。
    return len(covered_terms) == 1


def query_coverage_ok(results: list[dict[str, Any]], query: str,
                      *, strict: bool = False) -> bool:
    """查询-结果词面覆盖守卫（fast/auto 早停质量门槛）。

    2026-09-02 实测教训：fast 早停原只看计数+snippet，首引擎上游波动时
    返回 5 条高计数但不相关结果（查询 Crawl4AI 却返回无关 MDN 页），
    早停吞掉 wave-2，单引擎垃圾即成最终答案。本守卫只在极端场景（多数
    结果与查询零词面交集）拒绝早停，让既有 wave-2/串行次引擎补跑——
    只影响「是否停」，不丢弃任何结果，最坏代价是多跑一个引擎。

    CJK 说明：中文查询分词为单字，覆盖判定在字符级——结果与查询零字符
    交集同样会被拒早停（弱信号但非空）；同义词改写场景（如「电脑」vs
    「计算机」）可能多跑一个引擎，属可接受代价：宁可多一次调用，不放走
    单引擎垃圾。

    2026-10-26 补区分性判据（_discriminative_coverage_ok）：只命中泛词
    不算够用——「git commit」接受「Git - Install for Windows」是本守卫的
    真实漏网案例。该判据只在「一条结果也没同时命中两个主题词」时拒绝，
    不改变多数覆盖的口径，最坏代价仍是多跑一个引擎。

    `strict=True`：没有任何权威信号时（authority_level/upstream_authority
    全空或全 0），要求覆盖「明确过半」而不是「达到一半」。免费源的平庸结果
    常在 4 条里只中 2 条就触发早停；收紧后多跑一个引擎，换更高的准确率。
    """
    if not query or not query.strip():
        return True
    try:
        from tfidf_router import tokenize
    except ImportError:
        return True  # 分词不可用不设卡（fail-open）
    q_set = set(tokenize(query))
    if not q_set:
        return True
    # PB-HOS 摊销分词：单轮早停守卫内对每条结果的 title 与全文只分词一次，
    # 供单 token 噪声、回声检测、区分性覆盖与多数覆盖四道判据共享。
    title_tok_sets: list[set[str]] = []
    full_tok_pairs: list[tuple[str, set[str]]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        t_str = r.get("title") or ""
        s_str = r.get("snippet") or ""
        t_set = set(tokenize(t_str))
        s_set = set(tokenize(s_str)) if s_str else set()
        title_tok_sets.append(t_set)
        full_tok_pairs.append((f"{t_str} {s_str}".lower(), t_set | s_set))

    single_token_titles = sum(1 for ts in title_tok_sets if len(ts) <= 1)
    if (len(results) >= 3 and len(q_set) >= 3
            and single_token_titles * 2 > len(results)):
        return False
    if len(q_set) >= 4 and len(results) >= 3:
        covered_title_terms: set[str] = set()
        for ts in title_tok_sets:
            covered_title_terms |= (q_set & ts)
        if len(covered_title_terms) == 1:
            return False
    if not _discriminative_coverage_ok(
            results, query, _pre_full_tokens=full_tok_pairs):
        return False
    covered = sum(1 for _, f_set in full_tok_pairs if q_set & f_set)
    if strict:
        # 明确过半：2 条要 2 条都命中，4 条要 3 条命中。没有权威信号时
        # 不用「刚好一半」换早停。
        return covered > len(results) / 2
    return covered >= max(1, (len(results) + 1) // 2)


def result_relevance(query: str, results: list[dict[str, Any]],
                     top_k: int = 3) -> float | None:
    """单个引擎这次结果的相关度代理 ∈ [0,1]；**拿不到信号时返回 None**。

    用途：给 `adaptive` 学习器回写质量维度（见 adaptive.quality_factor），
    让引擎排序不再只学「快慢/成败」而能学「好坏」。零网络成本——只读结果
    文本与查询词元。

    与 `query_coverage_ok` 的区别是本末颠倒：那个问「够不够好到可以停」
    （二元判定，判据保守到宁可多跑一个引擎），这个问「有多好」
    （连续量，用于跨引擎、跨时间比较）。两者共用词面覆盖的直觉，但口径必须
    分开：早停判据要求高敏感度（漏放=放出垃圾），质量分要求可比较
    （量纲稳定，否则引擎之间的分数不可比）。

    取「前 top_k 条里每条对查询的覆盖率，再取均值」，而不是全量平均：
    一条半相关文档不该替整个结果集背书，反之亦然。

    返回 None 而非 0.0 是关键：0.0 在自适应评分里是「测过了、很差」，
    None 才是「本次没测」。两者混同会让「无法判定」把引擎打落
    （`adaptive.record` 负责把 None 落成不参与判定的 0.0）。
    """
    if not query or not query.strip():
        return None
    try:
        from tfidf_router import tokenize
    except ImportError:
        return None  # 分词不可用 → 无信号（fail-open，不影响主路径）
    q_set = set(tokenize(query))
    if not q_set:
        return None  # 查询无内容词元（纯符号）→ 无法判定
    scored: list[float] = []
    for r in results:
        if not isinstance(r, dict) or "error" in r:
            continue
        text = f"{r.get('title') or ''} {r.get('snippet') or ''}"
        d_set = set(tokenize(text))
        if not d_set:
            scored.append(0.0)  # 有结果却零词元：结构化源常见，计为不相关
            continue
        scored.append(len(q_set & d_set) / len(q_set))
        if len(scored) >= top_k:
            break
    if not scored:
        return None  # 无有效结果（全部 error / 空列表）→ 无信号
    return sum(scored) / len(scored)


def score_clarity_ok(results: list[dict[str, Any]]) -> bool:
    """无标注 QPP 信号（2026-09-16）：结果分平坦且数量仅达下限时拒绝早停。

    方法论：无 relevance judgments 的查询性能预测（QPP）——NQC/Clarity 一族
    的共同直觉是「分数分布越尖锐（有明确赢家），查询表现越可预期；分布
    平坦说明检索器对查询拿不准」。这里取最保守的判据：

      - 只在**计数恰好等于下限**时参与判定（_sufficient_internal 传入
        barely）：结果富余本身就是信心，不必再问分数分布；
      - 至少 2 个数值 score 才参与（单条无从谈分布），否则 fail-open
        （放行早停）——下限取 2 而非 3 是刻意的：fast 档计数下限就是 2，
        取 3 会让 fast 档永远够不着信号；两条全等的占位分与三条同样可疑；
      - 全体 score 完全相等（std=0，结构化源常见的占位分）视为最平坦。

    「拒绝早停」的代价实打实是多跑一个引擎（成本语义见 engine_dispatch
    wave-1 注释），所以阈值取最极端的平坦而非「偏平坦」；宁可多一次调用，
    不放走「字段齐全、计数达标、分数无区分度」的单引擎垃圾（2026-09-02
    实测教训的分数维度补丁，与词面覆盖守卫互补——那个管文本，这个管排序）。
    """
    scores: list[float] = []
    for r in results:
        try:
            v = r.get("score")
            if v is None:
                continue
            v = float(v)
        except (TypeError, ValueError):
            continue
        scores.append(v)
    if len(scores) < 2:
        return True  # 信号不可用：fail-open
    mean = sum(scores) / len(scores)
    if mean <= 0:
        return True
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = var ** 0.5
    return (std / mean) > 0.02  # 变异系数 ≤2% 视为平坦（实测占位分全等=0）


def _authority_level_of(r: dict[str, Any]) -> int | None:
    """读单条结果的权威等级（0-4）：精排写 authority_level，引擎原生写
    upstream_authority。解析不了/越界返回 None（「没信号」不是「0」）。"""
    for key in ("authority_level", "upstream_authority"):
        value = r.get(key)
        if value in (None, ""):
            continue
        try:
            level = int(float(str(value).strip()))
        except (TypeError, ValueError):
            continue
        if 0 <= level <= 4:
            return level
    return None


def _sufficient_internal(
    results: list[dict[str, Any]],
    mode: str,
    min_results: int | None,
    query: str = "",
) -> bool:
    """渐进检索 early-stop：结果是否已够用。

    轻量启发式（不依赖网络/LLM）：
      - 默认 auto：至少 3 条非错误 + 2 条有 snippet
      - 默认 fast：至少 2 条 + 1 个 snippet
      - min_results：域配置覆盖（答案型源 1 条快照即够用，计数语义不动）
      - 通用路径叠加词面覆盖守卫（query_coverage_ok）：结果与查询几乎
        无交集时不许早停
      - 两条路径都叠加平坦分守卫（score_clarity_ok）：计数恰好压线且
        分数无区分度时不许早停
      - 权威信号（2026-10-26）：任一条 authority≥3 时允许少一条即停；
        全批无权威信号时覆盖要求收紧到「明确过半」，避免平庸免费结果早停

    壳链不算「够数」（2026-10-11 热榜实测）：early-stop 的两条调用
    路径（wave-1 的 _wave1_stop → results_sufficient、wave-2 的
    cumulative_sufficient）都在此判定，与下游 serp_guard 过滤共用
    同一口径。此前壳链被算作有效结果——wave-1「先合格者赢」时
    baidu_hot 交 10 条 baidu.com/s?wd= 壳即触发早停、同域真榜单
    引擎（toutiao_hot）被 cancelled，壳随后被 serp_guard 整批过滤
    清零、merged 归零触发 2.3s 恢复链。判定与过滤对齐后：壳从一
    开始就不算「够」，早停只在交付真结果的引擎上触发。无 url 的
    结构化快照（行情/天气）不受影响——壳判定只看 URL。
    """
    try:
        from serp_guard import drop_shell_urls as _drop_shells
    except ImportError:
        _drop_shells = None  # 守卫不可用 → 壳不过滤（fail-open，与模块容错一致）
    goods: list[dict[str, Any]] = [
        r for r in results if isinstance(r, dict) and "error" not in r]
    if _drop_shells is not None:
        # 壳过滤唯一出口（serp_guard.drop_shell_urls）：与下游漏斗过滤、
        # 恢复链 L0/L3 收口共用同一判定，口径不再各自内联漂移。
        goods = _drop_shells(goods)
    if not goods:
        return False
    with_snippet = sum(
        1 for r in goods
        if (r.get("snippet") or r.get("title") or "").strip()
    )
    authority_max = None
    for r in goods:
        level = _authority_level_of(r)
        if level is not None and (authority_max is None or level > authority_max):
            authority_max = level
    if min_results is not None:
        try:
            need = max(1, int(min_results))
        except (TypeError, ValueError):
            need = 1
        # 答案型 1 条要求有可展示正文；≥3 条时至少 2 条有 snippet
        need_snip = 1 if need <= 2 else max(2, need - 1)
        ok = len(goods) >= need and with_snippet >= min(need_snip, len(goods))
        # 词面覆盖守卫不适用于答案型（1 条快照无「多数覆盖」可言），
        # 平坦分守卫适用：占位分快照同样可能是垃圾
        return ok and (len(goods) > need or score_clarity_ok(goods))
    threshold = 2 if mode == "fast" else 3
    # 高权威结果可以少一条：auto 默认 3 条，2 条 authority≥3 且覆盖通过
    # 就停。这是 DeepSeek/官方源的结构化字段第一次参与「停不停」的判定。
    if authority_max is not None and authority_max >= 3:
        need = max(2, threshold - 1)
        return (len(goods) >= need and with_snippet >= 1
                and query_coverage_ok(goods, query))
    ok = len(goods) >= threshold and with_snippet >= (1 if mode == "fast" else 2)
    # 全批没有权威信号（None 或 0）时，覆盖要求明确过半；有 1-2 级弱信号
    # 的批不额外收紧，避免把大量正常免费结果误伤。
    strict = authority_max in (None, 0)
    return (ok
            and query_coverage_ok(goods, query, strict=strict)
            and (len(goods) > threshold or score_clarity_ok(goods)))
