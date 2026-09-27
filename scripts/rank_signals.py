#!/usr/bin/env python3
"""rank_signals.py — 相关性/完整性算子与 SEO 对抗信号（纯函数层）。

2026-09-27 从 search_rank.py 拆出（该文件因本次改造触及 1000 行硬上限）。
拆分理由不是「文件太长」，而是**职责本就独立**：本模块只回答「这条结果
与该查询有多匹配、这份内容有多可信」，不碰融合、去重、语言偏好、截断等
排序流程——那些留在 search_rank.py。两者是「打分」与「调度」的分工。

设计约束（与 search_rank 一致）：
  - 零外部依赖，只用 stdlib + re，单次调用微秒级
  - 纯函数，无全局可变状态（延迟编译的正则除外，只写一次）
  - 所有开关走 ARGO_* 环境变量，默认开启，=0 退回旧行为

文献依据见 research/argo-低质内容检测调研.md；实证数据见
tests/test_relevance_cjk.py 的用例注释。
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

try:
    from engine_env import get_env
except ImportError:  # pragma: no cover - 独立导入时的兜底
    def get_env(name):
        if isinstance(name, str):
            return os.environ.get(name)
        for n in name:
            v = os.environ.get(n)
            if v:
                return v
        return None


def _env_flag(name: str, default: bool = True) -> bool:
    """读 ARGO_* 开关：未设置取 default，=0/off/no/false 判 False。"""
    try:
        v = get_env(name)
    except Exception:
        v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "off", "no", "false", "disable", "disabled")


# 中文停用字：高频但无区分度。内容信号模块（content_signals._ZH_STOPCHARS）
# 有一份同表副本——刻意独立维护以避开反向依赖（search_rank / rank_signals
# 在 import 链上游），两表分叉由 tests/test_relevance_cjk.py 的一致性用例发现。
CJK_STOPCHARS: frozenset[str] = frozenset(
    "的了是和与或在有为被把对从到就都也还很更最之其此这那一个"
    "个们我你他她它上下中里外前后时和及等所可能够会要"
    "怎幺么样如选哪个多少何呢吗吧啊哦呀嘛"
)


# ── 相关性算子（2026-09-27 重写）─────────────────────────────────────────────
#
# 旧实现把查询与文档都按「中文单字」切分后算覆盖率，三个结构性缺陷：
#
#   1. **关键词堆砌得分更高**。「颈椎病 枕头 推荐」切成 颈/椎/病/枕/头/推/荐
#      七个单字后，堆砌标题「颈椎枕头推荐_颈椎病枕头怎么选_枕头推荐颈椎病」
#      能全覆盖拿 0.650，而正常表述「颈椎病患者如何选择合适的枕头」只有
#      0.464 —— 奖励的正是内容农场唯一要刷的指标。
#   2. **单字命中即得分**。任何含「股」「价」二字的页面，在「贵州茅台股价」
#      这个查询上白拿 2/6 覆盖率，而「贵州」「茅台」作为一个词与单字「价」
#      等权。
#   3. **堆砌的长标题反而拉低不了分**。覆盖率的分母是查询 token 数、不惩罚
#      文档侧的冗余，所以「把查询词重复十遍」与「自然用一次」同分。
#
# 新算子：查询侧取**字符二元组（CJK）+ 小写词（拉丁数字）**，文档侧同样
# 处理，用**非对称覆盖率**（分母只算查询单元）。停用字不参与，避免
# 「怎么选」这类无信息词撑起分数。堆砌由 _title_stuffing_penalty 单独处理。

_WORD_RE = None
_CJK_RUN_RE = None


def relevance_units(text: str) -> list[str]:
    """相关性比对单元（有序、含重复）：CJK 字符二元组 + 小写拉丁/数字词。

    **返回 list 而非 set**：`title_stuffing_penalty` 要数「同一单元在标题里
    出现了几次」，set 会把重复折叠掉，堆砌检测就永不触发。需要集合语义的
    调用方自己 `set(...)`。

    与 serp_guard._extract_tokens 同口径（CJK bigram + 拉丁词），但那里是
    「有无交集」的粗判，这里要算覆盖率，故独立成实现——serp_guard 刻意
    不 import search_rank（冷启动热路径），与本模块也不共享代码。
    """
    global _WORD_RE, _CJK_RUN_RE
    if _WORD_RE is None:
        _WORD_RE = re.compile(r"[a-zA-Z0-9]+")
        _CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
    low = (text or "").lower()
    units: list[str] = []
    for run in _CJK_RUN_RE.findall(low):
        # 先剔停用字再取 bigram：否则「怎么选枕头」里的字会与相邻字组成
        # bigram 混进单元集合。
        kept = [ch for ch in run if ch not in CJK_STOPCHARS]
        if len(kept) == 1:
            units.append(kept[0])       # 单字残段：补一个单元，避免整段丢失
        else:
            units.extend(kept[i] + kept[i + 1] for i in range(len(kept) - 1))
    units.extend(_WORD_RE.findall(low))
    return units


def relevance_v2_enabled() -> bool:
    """相关性算子开关：ARGO_RELEVANCE_V2=0 退回旧单字覆盖率。"""
    return _env_flag("ARGO_RELEVANCE_V2")


def _coverage(units: set[str], title: str, snippet: str) -> tuple[float, float]:
    """返回 (title 覆盖率, snippet 覆盖率)。"""
    t_units = set(relevance_units(title))
    s_units = set(relevance_units(snippet))
    t_cov = len(units & t_units) / len(units) if units else 0.0
    s_cov = len(units & s_units) / len(units) if units else 0.0
    return t_cov, s_cov


def title_stuffing_penalty(title: str, units: set[str]) -> float:
    """标题堆砌惩罚：同一查询单元在标题里重复出现时的折扣系数（0.5-1.0）。

    内容农场的标题形态是「颈椎枕头推荐_颈椎病枕头怎么选_枕头推荐颈椎病」——
    查询单元全部命中，但重复了五六遍。覆盖率看不见这种堆砌（分子封顶在
    查询单元数），所以必须单独量「文档侧冗余」：命中次数 / 查询单元数。
    自然表述的标题里，各单元通常各出现一次。
    """
    if not units or not title:
        return 1.0
    t_units_list = relevance_units(title)
    if not t_units_list:
        return 1.0
    hits = sum(1 for u in t_units_list if u in units)
    if hits <= len(units):
        return 1.0
    # 允许上限 = 查询单元数（自然标题一次覆盖即达标）；超出越多折扣越大。
    # 斜率 0.30/倍、下限 0.35：实测「颈椎病 枕头 推荐」上，2.5 倍堆砌的标题
    # 原始覆盖率 1.0 而正常表述只有 0.75，若惩罚不够陡则堆砌仍然胜出——
    # 这正是本次改造要消灭的方向（0.30 斜率下 2.5 倍堆砌 ≈ ×0.55，
    # 0.65→0.36 < 正常型的 0.49）。
    excess = hits / max(len(units), 1)
    return round(max(0.35, 1.0 - 0.30 * (excess - 1.0)), 4)


def score_relevance(query_units: set[str], title: str, snippet: str) -> float:
    """相关性：查询单元在 title/snippet 的覆盖率 × 标题堆砌惩罚。

    非对称覆盖率——分母只算查询侧单元，文档侧的冗长不摊薄分数（这是
    「长文档天然占优」的对立面）。堆砌由 title_stuffing_penalty 单独处理。
    """
    if not query_units:
        return 0.5
    if not relevance_v2_enabled():
        # 逃生门：旧单字覆盖率。注意调用方需相应地传单字集合
        # （见 search_rank.local_five_dim_rerank 的开关分支）。
        t_tokens = set(re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")
                       .findall((title or "").lower()))
        s_tokens = set(re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")
                       .findall((snippet or "").lower()))
        title_cov = len(query_units & t_tokens) / len(query_units)
        snip_cov = len(query_units & s_tokens) / len(query_units)
        return round(min(1.0, 0.65 * title_cov + 0.35 * snip_cov), 4)
    t_cov, s_cov = _coverage(query_units, title, snippet)
    raw = 0.65 * t_cov + 0.35 * s_cov
    return round(min(1.0, raw * title_stuffing_penalty(title, query_units)), 4)


# ── 完整性维度（2026-09-27 重写：QSDM 信息/噪声比）───────────────────────────
#
# 旧实现 0.6×snippet 长度 + 0.2×有数字 + 0.2×有标题。长度占大头，而「把
# 摘要写长、塞进数字」正是内容农场唯一需要刷的指标——实测 SEO 软文得
# 0.556、正经来源只得 0.239（2.3 倍反向激励）。
#
# 换成 Bendersky/Croft/Diao, WSDM 2011（QSDM）的质量特征族。选它的理由：
# 该论文的核心结果是这套特征**在几乎无 spam 的 GOV2 上也显著有效**
# （MRR +8%、nDCG@5 +9%、Wikipedia 页检索率 5 倍），说明它抓的是普适的
# 文档质量而非可被针对性绕过的「spam 特征」。
#
# 论文要求特征「线性时间可算、可 aggregate」，以下全部满足。

COMPLETENESS_IDEAL_TITLE_CHARS = (8, 40)

_STRUCTURE_MARK_RE = None
_TITLE_SEP_RE = None


def completeness_v2_enabled() -> bool:
    """完整性算子开关：ARGO_COMPLETENESS_V2=0 退回旧长度启发式。"""
    return _env_flag("ARGO_COMPLETENESS_V2")


def title_quality_term(title: str) -> float:
    """标题质量项（0.2-1.0）：长度规范度 × 分隔式堆砌惩罚。

    两道独立扣分正交叠加：
      ① 长度：<8 字信息不足、>40 字快速衰减（论文实测长标题更可能是 spam）
      ② 分隔符：标题被切成 3 段以上短语 → SEO 堆砌形态
    自然标题两项都不触发。
    """
    global _TITLE_SEP_RE
    t = (title or "").strip()
    if not t:
        return 0.0
    tlen = len(t)
    lo, hi = COMPLETENESS_IDEAL_TITLE_CHARS
    if tlen < lo:
        length_term = tlen / lo
    elif tlen <= hi:
        length_term = 1.0
    else:
        # 超出每 10 字扣 0.2，下限 0.2（比初版 20 字/0.15 更陡）
        length_term = max(0.2, 1.0 - 0.2 * ((tlen - hi) / 10.0))

    if _TITLE_SEP_RE is None:
        # 中文逗号/顿号/竖线/分号/方括号 + 英文逗号/竖线
        _TITLE_SEP_RE = re.compile(r"[，,、|｜;；【】\[\]]")
    segs = len([s for s in _TITLE_SEP_RE.split(t) if s.strip()])
    # 2 段以内视为自然；3 段起每多一段扣 0.12，下限 0.5
    sep_term = 1.0 if segs <= 2 else max(0.5, 1.0 - 0.12 * (segs - 2))

    return max(0.2, length_term * sep_term)


def score_completeness(title: str, snippet: str) -> float:
    """完整性：QSDM 式信息/噪声比（标题质量 + 实义密度 + 数字密度 + 结构）。

    与旧版的关键差别：**长度不再直接给分**，只通过标题质量与实义密度间接
    体现，且两个方向都能扣分。长而空的软文摘要因此拿不到高分。
    """
    if not completeness_v2_enabled():
        length = len(snippet or "")
        length_score = min(length / 200.0, 1.0)
        has_digit = 1.0 if any(c.isdigit() for c in (snippet or "")) else 0.0
        has_title = 1.0 if (title or "").strip() else 0.0
        return round(min(1.0, 0.6 * length_score + 0.2 * has_digit + 0.2 * has_title), 4)

    global _STRUCTURE_MARK_RE
    if _STRUCTURE_MARK_RE is None:
        # 可读性/结构标记：列表、序号、定义、对比、步骤（中英）
        _STRUCTURE_MARK_RE = re.compile(
            r"(?:^|\n)\s*(?:\d+[.、)]|[一二三四五六七八九十][.、)]|[-*•])"
            r"|(?:是指|定义为|包括|分为|例如|比如|综上|因此|however|defined as|such as)",
            re.I,
        )

    s = snippet or ""
    title_term = title_quality_term(title)

    # 实义密度（fracVisText 的文本级近似）：去空白后非标点字符占比
    stripped = "".join(s.split())
    if not stripped:
        density_term = 0.0
        digit_term = 0.0
        struct_term = 0.0
    else:
        substantive = sum(1 for c in stripped if c.isalnum())
        density_term = substantive / len(stripped)

        # 数字密度**归一化**：有数字只是入场券，占比过多（堆砌「10款/20款/
        # 90%/5000元」的软文话术）同样扣分。2%-15% 为合理区间。
        digits = sum(1 for c in stripped if c.isdigit())
        dr = digits / len(stripped)
        if dr == 0:
            digit_term = 0.35          # 无数字：信息量偏低但不等于低质
        elif dr <= 0.15:
            digit_term = 1.0
        elif dr <= 0.30:
            digit_term = 0.7
        else:
            digit_term = 0.4

        struct_term = 1.0 if _STRUCTURE_MARK_RE.search(s) else 0.4

    return round(min(1.0, 0.35 * title_term + 0.30 * density_term
                     + 0.20 * digit_term + 0.15 * struct_term), 4)


# ── 域级聚合惩罚（2026-09-27 新增）───────────────────────────────────────────
#
# 仿 Google Helpful Content Update 的 site-wide 信号。现有 authority 只看
# 域名查表，**不看在这次查询里这个域名实际返回了什么**。实测「空气净化器
# 推荐」返回 5 条结果全部来自 zhuanlan.zhihu.com，且标题清一色是
# 「2026年9月最新实测」「自费5000元实测20款」的软文形态。
#
# 本函数是该思想在**单次查询粒度**上的落地（argo 无跨查询的站点级统计）：
# 只惩罚「高度集中」的情形（单一域名占比过半），正常的多结果场景不触发。
#
# 与 MMR/新颖性的分工：新颖性按**内容**去冗余（bigram 相似度），本函数按
# **来源**去集中，两者正交——同一个域名可以发多篇不同内容（都被压），
# 同一篇内容也可以跨多个域名转载（由 minhash + novelty 负责）。


def domain_penalty_enabled() -> bool:
    """域级聚合惩罚开关：ARGO_DOMAIN_CONCENTRATION=0 关闭。"""
    return _env_flag("ARGO_DOMAIN_CONCENTRATION")


def host_of(url: str) -> str:
    """取规范化 host（www 折叠为裸域）。"""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_concentration_penalty(results: list[dict[str, Any]]) -> dict[str, float]:
    """按「同一域名的占比」给出惩罚系数（乘子 ≤1，只降不升）。

    返回 {host: factor}；不触发时返回空表（调用方据此完全跳过，保证
    「无惩罚时与旧实现逐位一致」这条既有纪律）。
    """
    if not results:
        return {}
    from collections import Counter
    counts: Counter = Counter()
    for r in results:
        if not isinstance(r, dict):
            continue
        host = host_of(r.get("url") or "")
        if host:
            counts[host] += 1
    total = sum(counts.values())
    if total < 3 or not counts:
        # 结果太少时占比噪声太大（2 条里 1 条就是 50%），不判定
        return {}
    if len(counts) == 1:
        # 全部结果来自同一域名：这不是「某站从多源里占榜」，而是「本次召回
        # 本来就只有一个来源」（单引擎查询、或该域确实是唯一相关源）。
        # 罚它等于对所有单源结果无条件降权——既打不出多样性（没有别的源
        # 可让位），又破坏了「无融合信息时与旧实现确定性」这条既有不变式
        # （由 tests/test_ranking_contract.py 锁定）。故只在**多域并存**时判定。
        return {}
    penalty: dict[str, float] = {}
    for host, n in counts.items():
        share = n / total
        if share <= 0.5:
            continue
        # 占比过半起罚：0.5 → 1.0（不罚），1.0 → 0.75（满罚 25%）
        # 用意是「打散占榜」而非「删除结果」——同域仍可按内容质量排序。
        penalty[host] = round(1.0 - 0.25 * (share - 0.5) / 0.5, 4)
    return penalty


# ── 域级源保底分（config.yaml 声明驱动）─────────────────────────────────────

_SCORE_FLOORS_CACHE: dict[str, dict[str, dict[str, float]]] | None = None


# ── 跨域内容同质（2026-09-27 新增）────────────────────────────────────────────
#
# 补 Google「Scaled content abuse」政策（2024-03 起）对应的信号：
#   "Creating many pages where the content makes little or no sense..."
#   "Scraping feeds... including through automated transformations like
#    synonymizing, translating, or other obfuscation techniques"
#
# 与上面 domain_concentration_penalty 的分工（两者正交，不是加强关系）：
#   - 域级惩罚管「**一个**站占榜」：同一 host 占比过半
#   - 本信号管「**很多**站各发一篇同款」：host 各不相同、内容高度相似
# 后者正是前者的结构性盲区：几十个小站发同一篇软文时，每个 host 都只占
# 1/N，域级惩罚一条都不触发。
#
# 为什么这条比继续在内容上加信号更值得做：ACL 2024「AI News Content
# Farms Are Easy to Make and Hard to Detect」(arXiv 2406.12128) 的结论是
# 内容农场对**基于内容**的检测器天然鲁棒——改写成本极低。来源侧的证据
# （同一批内容出现在互不相干的域名下）不随改写而消失，判别力更稳。
#
# 复用 search_rank._content_similarity（minhash + Jaccard 兜底）而非另造
# 指纹：同一套口径，避免「去重用一套、惩罚用另一套」导致的自相矛盾——
# 同一对内容不会既被判为近重复而删除、又被判为不相似而不罚。


def cross_domain_homogeneity_enabled() -> bool:
    """跨域同质惩罚开关：ARGO_CROSS_DOMAIN_HOMOGENEITY=0 关闭。"""
    return _env_flag("ARGO_CROSS_DOMAIN_HOMOGENEITY")


def _result_content_sig(r: dict[str, Any]) -> str:
    """结果的内容签名：title + snippet。取不到就返回空串（不参与判定）。"""
    parts = [str(r.get("title") or ""), str(r.get("snippet") or "")]
    return " ".join(p for p in parts if p).strip()


def cross_domain_homogeneity_penalty(
    results: list[dict[str, Any]],
    similarity_threshold: float = 0.82,
    min_cluster: int = 3,
) -> dict[str, float]:
    """同质内容簇的降权系数（按 url 索引，乘子 ≤1，只降不升）。

    判据：一条结果与**其它域名**下的结果内容相似度 ≥ 阈值，且这样的同伙
    总数 ≥ min_cluster（含自己），就认定为「同质簇」。

    三个刻意的保守设计（每条都对应一类误伤）：
    1. **只降不升**，且强度按「簇内排名」递减——簇里最像原创源的那条（分
       最高者）几乎不受罚，罚的是随大流。farm 的危害不是「存在」而是
       「占榜」，把位次让出来就达到了目的。
    2. **要求 ≥2 个不同 host**：同域多篇不是「跨域」问题，那是
       domain_concentration_penalty 的职责，两者不重复计罚。
    3. **相似度阈值走既有 minhash 口径**（0.82 对齐 minhash_dedupe 的
       0.85 略松一点，因为惩罚比删除温和，可以更早介入）。

    正常场景不触发：多域并存的正常结果，内容本就该不同；真出现同一新闻
    多家转载时，簇内只有 2-3 条且相似度中等，且最像原创的那条不受罚。
    """
    if not results:
        return {}
    try:
        similarity_threshold = float(similarity_threshold)
        min_cluster = int(min_cluster)
    except (TypeError, ValueError):
        return {}
    if not (0.0 < similarity_threshold <= 1.0) or min_cluster < 2:
        return {}

    # 延迟导入：search_rank 在 import 链上游，rank_signals 不能反向依赖它
    try:
        from search_rank import _content_similarity
    except Exception:
        return {}

    rows: list[tuple[int, str, str, str]] = []   # (原始下标, host, sig, url)
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            continue
        host = host_of(r.get("url") or "")
        sig = _result_content_sig(r)
        if not host or len(sig) < 40:
            # 过短的签名（标题+摘要不足 40 字符）相似度噪声太大，不判定
            continue
        rows.append((i, host, sig, str(r.get("url") or "")))

    if len(rows) < min_cluster:
        return {}

    penalty: dict[str, float] = {}
    for ai, ahost, asig, aurl in rows:
        mates = 0
        for bi, bhost, bsig, _ in rows:
            if bi == ai or bhost == ahost:
                continue          # 只数异域同伙
            try:
                if _content_similarity(asig, bsig) >= similarity_threshold:
                    mates += 1
            except Exception:
                continue
        if mates + 1 < min_cluster:
            continue
        # 同伙越多罚得越重，但留 0.45 下限：单点判据的假阳性代价高于漏放，
        # 且不把任何一条打到 0——最终排序还会叠加其它维度。
        severity = min(1.0, (mates + 1 - min_cluster) / float(max(min_cluster, 1)))
        penalty[aurl] = round(1.0 - 0.55 * severity, 4)
    return penalty


def combined_source_penalties(results: list[dict[str, Any]]) -> tuple[
        dict[str, float], dict[str, float]]:
    """两个来源侧惩罚的合取：返回 (按 host 的域级系数, 按 url 的跨域同质系数)。

    合并成一个入口有两个理由：
      - 调用点（search_rank）只关心「把所有来源侧惩罚都算出来」，不必知道
        现在有几类、各自的键是什么——再加一类惩罚时不必改调用点；
      - 开关判断也收在这里，调用点不再出现 if/else 分支。

    两类惩罚键不同（host vs url）故分开返回，调用点各自查各自的表。
    """
    domain_pen: dict[str, float] = (
        domain_concentration_penalty(results) if domain_penalty_enabled() else {})
    cross_pen: dict[str, float] = (
        cross_domain_homogeneity_penalty(results)
        if cross_domain_homogeneity_enabled() else {})
    return domain_pen, cross_pen


def domain_score_floors() -> dict[str, dict[str, dict[str, float]]]:
    """域级源保底分（config.yaml 各域的 score_floors），进程内缓存。

    这些分值是「域对源的先验信任」，属于引擎/域声明而非排序算法——
    此前硬编码在 local_five_dim_rerank 里，每接一个新源都可能要改排序
    代码（2026-09-13 审查 P1-2）。声明形态：

      score_floors:
        sina_quote: {relevance: 1.0, authority: 0.85, freshness: 0.85}

    生效时机：relevance 在相关性评分后立即生效；authority/freshness 仅在
    evidence 评分可用时生效（无 evidence 时两维本就恒 0.5，保底无意义，
    与旧实现逐位一致）。源匹配按「/」切分成员判断（rrf 合并源
    "local_bing/sina_quote" 也要吃到保底）。
    """
    global _SCORE_FLOORS_CACHE
    if _SCORE_FLOORS_CACHE is None:
        try:
            from config import load_config
            floors: dict[str, dict[str, dict[str, float]]] = {}
            for d in (load_config().get("domains") or []):
                if isinstance(d, dict) and d.get("score_floors"):
                    floors[d["name"]] = {
                        str(src): dict(fl)
                        for src, fl in d["score_floors"].items()
                        if isinstance(fl, dict)
                    }
            _SCORE_FLOORS_CACHE = floors
        except Exception:
            _SCORE_FLOORS_CACHE = {}
    return _SCORE_FLOORS_CACHE
