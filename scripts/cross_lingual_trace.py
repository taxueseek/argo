#!/usr/bin/env python3
"""cross_lingual_trace.py — 跨语言长程搜索与多源任务溯源引擎。

架构定位：
  解耦自 research.py 与 wide_research.py。当单语言（如中文或特定小语种）
  信息贫瘠、被低质农场包围，或任务明确要求事实溯源（源头论文、首发漏洞、
  规范草案、最初作者、原始出处）时，自动触发跨语言长程追踪（Pivot & Trace），
  并进行跨语言证据交叉比对（Reconciliation & Disagreement Check）。

核心阶段：
  1. 需求与充分性判定（assess_cross_lingual_need）：
     检测结果稀疏度、权威源覆盖率以及溯源关键词（起源/出处/最早/first proposed 等）。
  2. 跨语言实体锚点与溯源子查询生成（build_cross_lingual_trace_queries）：
     将查询投影至目标语种（以英文为主，或日/德/法垂直语境），生成多阶段溯源子查询。
  3. 跨语言多源证据对账与综合（synthesize_cross_lingual_evidence）：
     将跨语言采集回来的结果与原语言结果按一手源等级、时序和互补关系对齐，
     沉淀出 primary_origins（一手源）、complements（互补证据）与 disagreements（口径差异）。
"""

from __future__ import annotations

import re
from typing import Any


# 溯源与跨语言强触发词
_TRACING_KEYWORDS = (
    "溯源", "起源", "出处", "最初", "源头", "提出者", "发明", "草案",
    "原型", "首发", "首个", "第一篇", "历史沿革", "来龙去脉",
    "origin", "provenance", "root cause", "first proposed", "author of",
    "initial draft", "whitepaper", "specification", "cve-", "rfc "
)

_SOURCE_TYPE_RANKS = {
    "official": 4,
    "academic": 4,
    "standard": 4,
    "repository": 3,
    "reference": 3,
    "news": 2,
    "community": 2,
    "general": 1,
}


def assess_cross_lingual_need(
    query: str,
    results: list[dict[str, Any]] | None,
    domain: str = "",
    min_sufficient_results: int = 3,
    force_trace: bool = False,
) -> dict[str, Any]:
    """评估是否需要启动跨语言长程搜索与任务溯源。

    触发条件（满足任意一条）：
      1. 显式开启 force_trace 或查询包含溯源关键词；
      2. 结果稀疏（有效结果 < min_sufficient_results）；
      3. 高置信度/权威一手信源缺失（authority_level >= 3 的结果为 0）；
      4. 强跨语言领域（security_search, academic, package_intel, rfc）。
    """
    q_low = (query or "").lower()
    has_tracing_keyword = any(kw in q_low for kw in _TRACING_KEYWORDS)

    valid_results = [r for r in (results or []) if isinstance(r, dict)]
    res_count = len(valid_results)

    high_auth_count = sum(
        1 for r in valid_results
        if float(r.get("authority") or r.get("score") or 0.0) >= 0.7
        or int(r.get("authority_level") or 0) >= 3
    )

    domain_low = (domain or "").lower()
    is_cross_lingual_domain = any(
        d in domain_low for d in ("security", "academic", "package", "rfc", "macro", "us_stock")
    )

    is_sparse = res_count < min_sufficient_results
    no_authoritative = (res_count > 0 and high_auth_count == 0)

    needed = force_trace or has_tracing_keyword or is_sparse or no_authoritative or (
        is_cross_lingual_domain and high_auth_count < 2
    )

    reasons: list[str] = []
    if force_trace:
        reasons.append("显式指定跨语言长程溯源")
    if has_tracing_keyword:
        reasons.append("命中溯源意图关键词")
    if is_sparse:
        reasons.append(f"原语言结果不足（仅 {res_count} 条，门槛 {min_sufficient_results}）")
    if no_authoritative:
        reasons.append("原语言缺乏高置信度一手权威信源")
    if is_cross_lingual_domain:
        reasons.append(f"领域「{domain}」天然具备跨语言溯源需求")

    target_langs = ["en"]
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", query):
        target_langs = ["en", "zh"]
    elif re.search(r"(日企|日本|动漫|acg|丰田|索尼|任天堂)", query, re.I):
        target_langs = ["en", "ja"]

    return {
        "needed": bool(needed),
        "target_langs": target_langs,
        "is_tracing": bool(has_tracing_keyword or force_trace),
        "is_sparse": bool(is_sparse),
        "reasons": reasons,
        "high_auth_count": high_auth_count,
        "result_count": res_count,
    }


def _extract_core_entity(query: str) -> str:
    """从复杂查询中提取核心溯源实体（英文锚点或关键名词）。"""
    q = (query or "").strip()
    # 优先抽取其中的西文标识（如 CVE-2024-1234, React, Transformer, SQLite 等）
    ascii_matches = re.findall(r"[A-Za-z0-9_\-\.\:\/]{3,}", q)
    if ascii_matches:
        # 过滤掉通用词
        clean_terms = [m for m in ascii_matches if m.lower() not in ("what", "how", "why", "when", "with")]
        if clean_terms:
            return " ".join(clean_terms[:3])

    # 如果纯 CJK，剥离常见的动词与提问词
    cleaned = re.sub(
        r"(如何|怎么|为什么|溯源|起源|出处|最初|源头|提出者|发明|的来历|是谁|什么时候|最新|评价)",
        " ",
        q,
    ).strip()
    words = [w for w in re.split(r"\s+", cleaned) if len(w) >= 2]
    return words[0] if words else q


def build_cross_lingual_trace_queries(
    query: str,
    target_lang: str = "en",
    focus: str = "comprehensive",
) -> list[dict[str, str]]:
    """生成分阶段跨语言长程溯源子查询。

    返回结构化的子查询列表，标注意图（intent）、策略（strategy）与语言标记。
    """
    entity = _extract_core_entity(query)
    subs: list[dict[str, str]] = []

    if target_lang == "en":
        # 阶段 1：一手原始出处与概念提出（Origin & Inception）
        subs.append({
            "query": f"{entity} original paper first proposed inception origin",
            "intent": "原始论文/源头提出者与发布背景溯源",
            "strategy": "origin_trace",
            "lang": "en",
        })
        # 阶段 2：规范、代码仓与官方权威文档（Specification & Implementation）
        subs.append({
            "query": f"{entity} official repository documentation specification RFC",
            "intent": "官方规范、权威仓库与技术标准追踪",
            "strategy": "primary_spec",
            "lang": "en",
        })
        # 阶段 3：演进考证与关键争议（Evolution & Historical Milestone）
        subs.append({
            "query": f"{entity} milestone evolution history benchmark analysis",
            "intent": "历史演进里程碑与事实交叉考证",
            "strategy": "cross_check",
            "lang": "en",
        })
    elif target_lang == "ja":
        subs.append({
            "query": f"{entity} 開発経緯 歴史 初出 仕様",
            "intent": "日文原始出处与开发历史溯源",
            "strategy": "origin_trace",
            "lang": "ja",
        })
        subs.append({
            "query": f"{entity} 公式ドキュメント 仕様書 論文",
            "intent": "日文官方规范与文档追踪",
            "strategy": "primary_spec",
            "lang": "ja",
        })
    else:
        subs.append({
            "query": f"{entity} origin official source history",
            "intent": f"{target_lang} 语种源头与官方文档追踪",
            "strategy": "origin_trace",
            "lang": target_lang,
        })

    return subs


def _classify_source_nature(url: str, title: str) -> str:
    """根据 URL 和标题对信源进行性质分类。"""
    u_low = (url or "").lower()
    t_low = (title or "").lower()

    if any(h in u_low for h in ("arxiv.org", "doi.org", "acm.org", "ieee.org", "biorxiv.org", "semanticscholar.org")):
        return "academic"
    if any(h in u_low for h in ("github.com", "gitlab.com", "bitbucket.org", "git.kernel.org")):
        return "repository"
    if any(h in u_low for h in ("rfc-editor.org", "w3.org", "iso.org", "ecma-international.org", "cve.mitre.org", "nvd.nist.gov")):
        return "standard"
    if any(h in u_low for h in (".gov", ".edu", "europa.eu", "who.int", "un.org")):
        return "official"
    if any(h in u_low for h in ("wikipedia.org", "wikidata.org", "baike.baidu.com")):
        return "reference"
    if any(k in t_low for k in ("official", "announcement", "release note", "white paper", "specification")):
        return "official"
    return "general"


def synthesize_cross_lingual_evidence(
    primary_results: list[dict[str, Any]],
    cross_results: list[dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    """将原语言与跨语言长程检索证据进行交叉对账。

    产出：
      - primary_origins: 权威一手信源（论文、代码仓、标准机构）
      - complements: 跨语言带来的增量补充事实
      - disagreements: 跨语言比对中发现的时间/口径分歧
      - provenance_chain: 结构化溯源脉络
    """
    seen_urls: set[str] = set()
    primary_origins: list[dict[str, Any]] = []
    complements: list[dict[str, Any]] = []
    disagreements: list[str] = []

    all_cross = [r for r in cross_results if isinstance(r, dict)]

    # 1. 甄选跨语言一手源
    for r in all_cross:
        u = r.get("url") or ""
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        t = r.get("title") or ""
        nature = _classify_source_nature(u, t)
        entry = {
            "title": t,
            "url": u,
            "snippet": r.get("snippet") or "",
            "nature": nature,
            "source": r.get("source") or "",
            "authority_rank": _SOURCE_TYPE_RANKS.get(nature, 1),
        }
        if nature in ("academic", "standard", "official", "repository"):
            primary_origins.append(entry)
        else:
            complements.append(entry)

    # 排序：权威一手源排在最前
    primary_origins.sort(key=lambda x: x["authority_rank"], reverse=True)

    # 2. 检查年份/时间戳分歧
    year_re = re.compile(r"\b(19\d{2}|20\d{2})\b")
    prim_years = set()
    for r in primary_results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        prim_years.update(year_re.findall(text))

    cross_years = set()
    for r in all_cross:
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        cross_years.update(year_re.findall(text))

    if prim_years and cross_years and not (prim_years & cross_years):
        disagreements.append(
            f"跨语言时间线存在差异：原语言提及时段 {sorted(prim_years)}，而外文一手源提及时段 {sorted(cross_years)}"
        )

    # 3. 构造溯源链条
    provenance_chain: list[dict[str, str]] = []
    for origin in primary_origins[:4]:
        provenance_chain.append({
            "stage": "一手信源定位",
            "source_type": origin["nature"],
            "title": origin["title"][:80],
            "url": origin["url"],
            "evidence": origin["snippet"][:140],
        })

    return {
        "status": "completed" if (primary_origins or complements) else "no_incremental_evidence",
        "primary_origins": primary_origins[:6],
        "complements_count": len(complements),
        "complements": complements[:6],
        "disagreements": disagreements,
        "provenance_chain": provenance_chain,
        "total_cross_sources": len(all_cross),
    }
