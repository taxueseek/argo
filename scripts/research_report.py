#!/usr/bin/env python3
"""
research_report.py — 深度研究报告打印（从 research.py 拆分）

承载 _print_deep_report：终端渲染深度研究报告。
从 research.py 拆分以控制文件规模（code-review：单文件 <1k 行）。
"""

from __future__ import annotations

from typing import Any


def print_deep_report(report: dict):
    """打印深度研究报告：正文用 [n] 标注，链接统一沉底「相关信源」。"""
    elapsed_s = report.get("elapsed_ms", 0) / 1000
    sub_count = report.get("sub_query_count", len(report.get("sub_findings", [])))
    total_sources = report.get("total_sources", 0)
    citations = report.get("citations") or []
    sources = report.get("sources") or []
    if not sources and citations:
        sources = [
            {
                "ref": c.get("ref") or i + 1,
                "title": c.get("title"),
                "url": c.get("url"),
                "engine": c.get("source"),
            }
            for i, c in enumerate(citations)
            if isinstance(c, dict) and c.get("url")
        ]
    citations_count = len(citations) or len(sources)

    print(f"\n{'='*60}")
    print("=== 深度研究报告 ===")
    print(f"{'='*60}")
    print(f"查询：{report.get('query', '')}")
    if report.get("rewritten_query"):
        rq = report["rewritten_query"]
        print(f"改写：{rq.get('original', '')} → {rq.get('rewritten', '')}（置信度 {rq.get('confidence', 0):.2f}）")
    print(f"子查询：{sub_count} 个 | 来源：{total_sources} 个 | 引用：{citations_count} 个 | 耗时：{elapsed_s:.1f}s")
    if report.get("archive"):
        print(f"归档：{report['archive'].get('run_dir', '')}")
    print()

    # 关键发现（链接用 [n]，不在正文刷 URL）
    print("## 关键发现")
    print()
    for i, kf in enumerate(report.get("key_findings", []), 1):
        aspect = kf.get("aspect", kf.get("sub_query", ""))
        refs = kf.get("citation_refs") or []
        ref_s = "".join(f"[{r}]" for r in refs) if refs else ""
        print(f"### {i}. {aspect} {ref_s}".rstrip())
        top = kf.get("top_result", {}) or {}
        if top:
            tref = top.get("ref")
            tmark = f"[{tref}] " if tref else ""
            print(f"  {tmark}{top.get('title', '')}")
            snippet = top.get("snippet", "")
            if snippet:
                print(f"  {snippet[:200]}")
        findings = kf.get("findings", [])
        if findings:
            for f in findings:
                title = f.get("title", "")
                print(f"  - {title}")
        print(f"  （本方面 {kf.get('result_count', 0)} 条结果）")
        print()

    # 知识缺口
    gaps = report.get("gaps", [])
    if gaps:
        print("## 知识缺口")
        for g in gaps:
            print(f"  - {g}")
        print()

    # 交叉引用
    cross_refs = report.get("cross_references", [])
    if cross_refs:
        print("## 交叉验证")
        for cr in cross_refs[:5]:
            print(f"  - 「{cr.get('ngram', '')}」被 {cr.get('source_count', 0)} 个来源佐证：{', '.join(cr.get('domains', [])[:5])}")
        print()

    # Verify 阶段：可信度评分 + 佐证强度（Selection×Absorption）
    cv = report.get("cross_verification") or {}
    if cv.get("credibility_available"):
        print("## 证据核验（Verify）")
        print(f"  佐证强度：{cv.get('corroboration_level', 'unknown')} | "
              f"交叉分：{cv.get('cross_score', 0):.2f} | "
              f"可吸收域名：{cv.get('content_domains', 0)}/{cv.get('unique_domains', 0)}")
        for ts in cv.get("top_sources", [])[:3]:
            print(f"  - {ts.get('final', 0):.2f} {ts.get('source', '')} {ts.get('url', '')[:80]}")
        for cf in cv.get("conflicts", [])[:3]:
            print(f"  ⚠ {cf.get('dimension', '')}：{cf.get('detail', '')}")
        if cv.get("unverified_count"):
            print(f"  ⚠ {cv.get('unverified_count')} 个维度无结果可核实")
        print()

    # 事实对齐：跨源事实冲突与印证
    fa = report.get("fact_alignment")
    if fa:
        stats = fa.get("stats") or {}
        conflicts = fa.get("fact_conflicts") or []
        corroborated = fa.get("fact_corroborated") or []
        print("## 事实对齐（Fact Alignment）")
        print(f"  抽取 {stats.get('facts_extracted', 0)} 个事实 | "
              f"冲突 {stats.get('conflicts', 0)} | "
              f"印证 {stats.get('corroborated', 0)}")
        for cf in conflicts[:5]:
            vals = " vs ".join(f"{v['value']}（{'/'.join(v['domains'][:2])}）"
                               for v in cf.get("values", [])[:3])
            print(f"  ⚠ {cf.get('type', '')}: {vals}")
        for co in corroborated[:5]:
            print(f"  ✓ {co.get('type', '')} {co.get('value', '')} "
                  f"（{'/'.join(co.get('domains', [])[:3])}）")
        print()

    # 固定工具预算提示
    budget_info = report.get("budget")
    if budget_info and budget_info.get("exhausted"):
        print(f"## 预算提示")
        print(f"  工具预算 {budget_info.get('limit')} 已用尽，本报告为最佳部分答案；未覆盖维度见知识缺口/盲区。")
        print()

    # 知识缺口
    gaps = report.get("gaps") or []
    if gaps:
        print("## 知识缺口 / 盲区")
        for g in gaps[:8]:
            print(f"  - {g}")
        print()

    # 专业质量门禁（选题 profile）
    gates = report.get("quality_gates") or []
    if gates:
        print("## 质量门禁（交付前自检）")
        for g in gates:
            print(f"  - [ ] {g}")
        print()

    sections = report.get("report_sections") or []
    if sections:
        print("## 建议报告结构")
        print("  → " + " / ".join(sections))
        print()

    grades = report.get("source_grades") or {}
    if grades:
        print("## 信源级别参考")
        for level, items in grades.items():
            if isinstance(items, list):
                print(f"  - {level}：{', '.join(str(x) for x in items[:6])}")
            else:
                print(f"  - {level}：{items}")
        print()

    if report.get("disclaimer"):
        print(f"⚠ {report['disclaimer']}")
        print()

    # 来源分布
    source_dist = report.get("source_distribution", {})
    if source_dist:
        print("## 引擎分布")
        for src, cnt in sorted(source_dist.items(), key=lambda x: x[1], reverse=True):
            print(f"  - {src}: {cnt}")
        print()

    engines = report.get("engines_used", [])
    if engines:
        print(f"使用引擎：{', '.join(engines)}")
        print()

    # 底部相关信源（传统搜索引擎形态；引用与 sources 统一）
    if sources or citations:
        print(f"── 相关信源（{len(sources) or len(citations)}）──")
        rows = sources if sources else citations
        for c in rows:
            if not isinstance(c, dict):
                continue
            ref = c.get("ref") or (c.get("id") or "").strip("[]") or "?"
            title = (c.get("title") or "")[:70]
            eng = c.get("engine") or c.get("source") or ""
            url = c.get("url") or ""
            eng_s = f" · {eng}" if eng else ""
            if title:
                print(f"  [{ref}] {title}{eng_s}")
            if url:
                print(f"      {url}")
        print()
