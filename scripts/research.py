#!/usr/bin/env python3
"""
research.py — 深度研究工具（wigolo research 理念移植）

核心能力：
  1. 问题分解：将复杂查询拆分为 3-5 个子查询
  2. 多源采集：对每个子查询并行执行搜索
  3. 综合报告：合并去重 + 来源标注 + 知识缺口识别
  4. 引用追踪：每个结论可追溯到具体搜索结果

用法：
  python3 research.py "CRISPR-Cas9 脱靶效应的 AI 预测方法综述"
  python3 research.py "CVE-2024-6387 生产环境影响评估" --depth deep
  python3 research.py "台积电财报分歧分析" --sub-queries 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from search import super_search, rrf_merge, deduplicate_by_url


# ── 问题分解 ──────────────────────────────────────────────────────────────────

def decompose_query(query: str, num_sub: int = 4) -> list[dict[str, str]]:
    """将复杂查询分解为子查询。

    策略：基于关键词特征自动分解，不依赖 LLM。
    """
    sub_queries = []

    # 策略 1：中英文混合 → 分语言搜索
    has_chinese = any("\u4e00" <= c <= "\u9fff" for c in query)
    has_english = any(c.isascii() and c.isalpha() for c in query)

    if has_chinese and has_english:
        # 提取英文核心词
        eng_words = " ".join(w for w in query.split() if w.isascii() and len(w) > 2)
        if eng_words:
            sub_queries.append({
                "query": eng_words,
                "intent": "英文核心概念搜索",
                "strategy": "english_focused"
            })

    # 策略 2：包含年份/时间 → 补充时效性搜索
    import re
    year_match = re.search(r"20\d{2}", query)
    if year_match:
        year = year_match.group()
        sub_queries.append({
            "query": f"{query} {year} latest update",
            "intent": f"{year}年最新进展",
            "strategy": "temporal"
        })

    # 策略 3：包含对比词 → 分别搜索各对象
    compare_match = re.search(r"(?:vs| versus |对比|比较|和|与|及)", query, re.I)
    if compare_match:
        parts = re.split(r"(?:vs| versus |对比|比较|和|与|及)", query, flags=re.I)
        for part in parts[:2]:
            part = part.strip()
            if part and len(part) > 2:
                sub_queries.append({
                    "query": part,
                    "intent": f"独立搜索：{part[:20]}",
                    "strategy": "split_compare"
                })

    # 策略 4：包含「如何/怎么/why」→ 补充教程/方案搜索
    how_match = re.search(r"(?:如何|怎么|how|why|为什么|最佳实践|best practice)", query, re.I)
    if how_match:
        sub_queries.append({
            "query": f"{query} tutorial guide best practices",
            "intent": "教程/最佳实践",
            "strategy": "tutorial"
        })

    # 策略 5：包含「问题/bug/错误」→ 补充社区讨论搜索
    bug_match = re.search(r"(?:bug|error|问题|报错|故障|issue|panic|crash|exception)", query, re.I)
    if bug_match:
        sub_queries.append({
            "query": f"{query} solution fix workaround community",
            "intent": "社区解决方案",
            "strategy": "community_fix"
        })

    # 策略 6：包含「论文/学术」→ 补充学术搜索
    academic_match = re.search(r"(?:论文|paper|arxiv|学术|综述|review|survey|研究)", query, re.I)
    if academic_match:
        sub_queries.append({
            "query": f"{query} arxiv semantic scholar 2024 2025",
            "intent": "学术文献补充",
            "strategy": "academic"
        })

    # 策略 7：包含「安全/CVE」→ 补充安全源
    security_match = re.search(r"(?:CVE|漏洞|vulnerability|security|exploit|PoC)", query, re.I)
    if security_match:
        sub_queries.append({
            "query": f"{query} NVD exploit PoC advisory",
            "intent": "安全数据源补充",
            "strategy": "security"
        })

    # 策略 8：包含「金融/股票/财报」→ 补充金融源
    finance_match = re.search(r"(?:股价|财报|基金|股票|行情|金融|financial|earnings|stock)", query, re.I)
    if finance_match:
        sub_queries.append({
            "query": f"{query} 东方财富 雪球 研报",
            "intent": "金融数据补充",
            "strategy": "finance"
        })

    # 确保至少有原始查询
    if not sub_queries:
        sub_queries.append({
            "query": query,
            "intent": "原始查询",
            "strategy": "direct"
        })

    # 补充通用搜索
    if len(sub_queries) < num_sub:
        sub_queries.append({
            "query": query,
            "intent": "综合搜索",
            "strategy": "general"
        })

    return sub_queries[:num_sub]


# ── 多源采集 ──────────────────────────────────────────────────────────────────

def collect_sources(sub_queries: list[dict[str, str]], max_results: int = 5,
                    timeout: int = 15, depth: str = "balanced",
                    mode: str = "auto") -> dict[str, Any]:
    """对每个子查询并行执行搜索，返回聚合结果。"""
    all_results = []
    engines_used = set()
    sub_results = []
    t0 = time.time()

    def _search_one(sq: dict[str, str]) -> dict[str, Any]:
        result = super_search(
            sq["query"], n=max_results, timeout=timeout,
            depth=depth, mode=mode, skip_cache=False
        )
        return {
            "sub_query": sq["query"],
            "intent": sq["intent"],
            "strategy": sq["strategy"],
            "results": result.get("results", []),
            "engines_used": result.get("engines_used", []),
            "elapsed_ms": result.get("elapsed_ms", 0),
        }

    with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as ex:
        futures = {ex.submit(_search_one, sq): sq for sq in sub_queries}
        for fut in as_completed(futures, timeout=timeout * 2 + 5):
            try:
                sr = fut.result()
                sub_results.append(sr)
                all_results.extend(sr["results"])
                engines_used.update(sr["engines_used"])
            except Exception as e:
                sq = futures[fut]
                sub_results.append({
                    "sub_query": sq["query"],
                    "intent": sq["intent"],
                    "strategy": sq["strategy"],
                    "results": [],
                    "engines_used": [],
                    "error": str(e),
                    "elapsed_ms": 0,
                })

    # RRF 融合
    result_lists = [sr["results"] for sr in sub_results if sr["results"]]
    if len(result_lists) > 1:
        merged = rrf_merge(result_lists)
    elif result_lists:
        merged = deduplicate_by_url(result_lists[0])
    else:
        merged = []

    elapsed = int((time.time() - t0) * 1000)

    return {
        "merged_results": merged[:max_results * 3],
        "sub_results": sub_results,
        "engines_used": sorted(engines_used),
        "total_results": len(merged),
        "elapsed_ms": elapsed,
    }


# ── 知识缺口识别 ──────────────────────────────────────────────────────────────

def identify_gaps(sub_results: list[dict[str, Any]], query: str) -> list[str]:
    """识别搜索结果中的知识缺口。"""
    gaps = []

    # 检查是否有子查询完全失败
    for sr in sub_results:
        if not sr["results"]:
            gaps.append(f"子查询「{sr['intent']}」无结果：{sr['sub_query'][:40]}")

    # 检查是否有子查询结果过少
    for sr in sub_results:
        if sr["results"] and len(sr["results"]) < 2:
            gaps.append(f"子查询「{sr['intent']}」结果稀少（仅 {len(sr['results'])} 条）")

    # 检查来源多样性
    all_sources = set()
    for sr in sub_results:
        for r in sr["results"]:
            src = r.get("source", "")
            if src:
                all_sources.add(src)
    if len(all_sources) < 3:
        gaps.append(f"来源多样性不足：仅 {len(all_sources)} 个引擎有结果（{', '.join(all_sources)}）")

    # 检查时间覆盖
    import re
    year_match = re.search(r"20\d{2}", query)
    if year_match:
        target_year = year_match.group()
        has_recent = False
        for sr in sub_results:
            for r in sr["results"]:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if target_year in title or target_year in snippet:
                    has_recent = True
                    break
        if not has_recent:
            gaps.append(f"未找到 {target_year} 年的直接相关内容")

    return gaps


# ── 综合报告 ──────────────────────────────────────────────────────────────────

def synthesize_report(query: str, collection: dict[str, Any],
                      gaps: list[str]) -> dict[str, Any]:
    """生成综合研究报告。"""
    merged = collection["merged_results"]
    sub_results = collection["sub_results"]

    # 按子查询分组的关键发现
    key_findings = []
    for sr in sub_results:
        if sr["results"]:
            best = sr["results"][0]
            key_findings.append({
                "aspect": sr["intent"],
                "strategy": sr["strategy"],
                "top_result": {
                    "title": best.get("title", ""),
                    "url": best.get("url", ""),
                    "snippet": (best.get("snippet", "") or "")[:200],
                    "score": best.get("score", 0),
                    "source": best.get("source", ""),
                },
                "result_count": len(sr["results"]),
            })

    # 来源统计
    source_counts = {}
    for r in merged:
        src = r.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    # 引用列表
    citations = []
    for i, r in enumerate(merged[:15]):
        citations.append({
            "id": f"[{i+1}]",
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": r.get("source", ""),
            "score": r.get("score", 0),
        })

    return {
        "query": query,
        "key_findings": key_findings,
        "total_sources": collection["total_results"],
        "engines_used": collection["engines_used"],
        "source_distribution": source_counts,
        "citations": citations,
        "gaps": gaps,
        "elapsed_ms": collection["elapsed_ms"],
        "sub_query_count": len(sub_results),
    }


# ── 主入口 ────────────────────────────────────────────────────────────────────

def deep_research(query: str, num_sub_queries: int = 4, max_results: int = 5,
                  timeout: int = 15, depth: str = "balanced",
                  mode: str = "auto") -> dict[str, Any]:
    """执行深度研究。"""
    # 1. 问题分解
    sub_queries = decompose_query(query, num_sub_queries)

    # 2. 多源采集
    collection = collect_sources(sub_queries, max_results, timeout, depth, mode)

    # 3. 知识缺口
    gaps = identify_gaps(collection["sub_results"], query)

    # 4. 综合报告
    report = synthesize_report(query, collection, gaps)

    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="深度研究工具")
    parser.add_argument("query", help="研究查询")
    parser.add_argument("--sub-queries", type=int, default=4, help="子查询数量")
    parser.add_argument("-n", "--max-results", type=int, default=5, help="每个子查询最大结果数")
    parser.add_argument("--timeout", type=int, default=15, help="超时秒数")
    parser.add_argument("--depth", choices=["fast", "balanced", "deep"], default="balanced")
    parser.add_argument("--mode", choices=["fast", "auto", "deep", "budget"], default="auto")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    report = deep_research(
        args.query, args.sub_queries, args.max_results,
        args.timeout, args.depth, args.mode
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # 人类可读输出
        print(f"\n{'='*60}")
        print(f"深度研究报告：{report['query']}")
        print(f"{'='*60}")
        print(f"子查询数：{report['sub_query_count']} | 引擎：{', '.join(report['engines_used'])}")
        print(f"总结果：{report['total_sources']} | 耗时：{report['elapsed_ms']}ms")
        print()

        for f in report["key_findings"]:
            print(f"▸ {f['aspect']}")
            print(f"  策略：{f['strategy']} | 结果数：{f['result_count']}")
            if f["top_result"]:
                tr = f["top_result"]
                print(f"  最佳：{tr['title'][:60]}")
                print(f"  来源：{tr['source']} | 分数：{tr['score']}")
            print()

        if report["citations"]:
            print("── 引用列表 ──")
            for c in report["citations"]:
                print(f"  {c['id']} {c['title'][:50]} ({c['source']})")
            print()

        if report["gaps"]:
            print("── 知识缺口 ──")
            for g in report["gaps"]:
                print(f"  ⚠ {g}")
            print()


if __name__ == "__main__":
    main()
