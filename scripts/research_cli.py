#!/usr/bin/env python3
"""research_cli.py — 深度研究 CLI（从 research.py 拆出，压文件规模）。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _load_work_packages(raw: str | None) -> Any:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("[") or text.startswith("{"):
        return text
    with open(text, encoding="utf-8") as fh:
        return fh.read()


def main() -> None:
    parser = argparse.ArgumentParser(description="深度研究取证")
    parser.add_argument("query", nargs="?", default=None, help="研究查询（--topic help 时可省略）")
    parser.add_argument("--sub-queries", type=int, default=None, help="扩词数量（有工作包时忽略）")
    parser.add_argument("-n", "--max-results", type=int, default=None, help="每个子查询最大结果数")
    parser.add_argument("--timeout", type=int, default=15, help="超时秒数")
    parser.add_argument("--depth", choices=["fast", "balanced", "deep"], default=None)
    parser.add_argument(
        "--mode",
        choices=["fast", "auto", "deep", "budget", "social-sentiment"],
        default="auto",
    )
    parser.add_argument("--budget", type=int, default=None, help="子查询工具调用上限")
    parser.add_argument(
        "--route-strategy",
        choices=["local_first", "cost_aware", "full"],
        default=None,
    )
    parser.add_argument(
        "--work-packages",
        default=None,
        help="工作包 JSON 文件路径或内联 JSON 数组；有则跳过扩词、按 depends_on 分阶段",
    )
    parser.add_argument("--platforms", type=str, default=None, help="社交平台，逗号分隔")
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="选题类型。--topic help 列出全部",
    )
    parser.add_argument("--no-auto-topic", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument("--archive-dir", type=str, default=None)
    parser.add_argument("--archive-tag", default=None)
    parser.add_argument("--archive-note", default=None)
    parser.add_argument(
        "--verify", nargs="?", const=3, type=int, default=None, metavar="TOP_K",
    )
    parser.add_argument(
        "--allow-recompute", action="store_true",
        help="授权执行工作包 recompute 计算脚本（fail-closed，默认拒绝）",
    )
    parser.add_argument(
        "--search-archive", metavar="主题词",
        help="检索已有研究/搜索归档（成果复用）：按主题词列出历史 run",
    )
    parser.add_argument("--archive-since", default=None, help="归档检索起始日期")
    parser.add_argument("--archive-until", default=None, help="归档检索截止日期")
    args = parser.parse_args()
    do_archive = not args.no_archive

    if args.search_archive:
        try:
            from archive_run import search_archive
            hits = search_archive(
                args.search_archive,
                since=args.archive_since, until=args.archive_until,
            )
        except Exception as e:
            print(f"[search-archive] {type(e).__name__}: {e}")
            return
        if not hits:
            print(
                f"未找到归档命中：{args.search_archive}"
                f"（根目录见 archive_run.resolve_archive_root；"
                "先跑带 --archive 的研究/搜索生成索引）"
            )
            return
        print(f"归档命中 {len(hits)} 条（主题：{args.search_archive}）\n")
        for h in hits:
            print(f"[{h['run_id']}] {h['packed_at']}_{h['query']}")
            print(f"    tag={h['tag']} source={h['source']} "
                  f"counts={h['counts']}")
            print(f"    → {h['run_dir']}")
        return

    from research import (
        deep_research,
        social_sentiment_research,
        _print_social_report,
    )

    profile_obj: dict[str, Any] | None = None
    profile_applied = None
    profile_key = None
    try:
        from topic_research_profiles import (
            get_profile, list_profiles, detect_topic_from_query, list_triggers,
        )
    except ImportError:
        get_profile = list_profiles = detect_topic_from_query = list_triggers = None  # type: ignore

    if args.topic and str(args.topic).lower() in ("help", "list", "--help"):
        if list_profiles:
            print("可用选题类型（--topic）：")
            print(f"  {'键名':15s} {'名称':14s} {'深度':8s} {'领域':10s} {'引擎':36s}")
            print(f"  {'-'*15} {'-'*14} {'-'*8} {'-'*10} {'-'*36}")
            for p in list_profiles():
                engines = ", ".join(p["engines"][:3])
                print(
                    f"  {p['key']:15s} {p['name']:14s} {p['depth']:8s} "
                    f"{p.get('discipline', ''):10s} {engines}"
                )
            print()
            if list_triggers:
                trig = list_triggers()
                print("深度研究触发词（示例）：", "、".join(trig["deep_research_triggers"][:8]), "…")
                print("主斜杠：", trig.get("main_slash") or "/argo")
                print("子技能斜杠：", " ".join(trig.get("slash_commands") or []))
                print("深度研究斜杠：", " ".join(trig.get("research_slash_commands") or [])[:120])
            print()
            print("示例：")
            print("  python3 research.py \"Claude Opus 5\" --topic ai")
            print("  python3 research.py \"固态电池\" --work-packages '[{\"id\":\"d\",\"question\":\"定义\"}]'")
        sys.exit(0)

    if not args.query:
        parser.error("需要研究查询；或使用 --topic help 查看选题与触发词")

    if args.topic and get_profile and list_profiles:
        profile_obj = get_profile(args.topic)
        if not profile_obj:
            available = ", ".join(p["key"] for p in list_profiles())
            print(f"⚠️  未知选题类型 '{args.topic}'。可用: {available}", file=sys.stderr)
            sys.exit(1)
        profile_key = args.topic.strip().lower()
        from topic_research_profiles import ALIASES
        profile_key = ALIASES.get(args.topic.strip()) or ALIASES.get(profile_key) or profile_key
    elif not args.no_auto_topic and detect_topic_from_query and get_profile:
        auto_key = detect_topic_from_query(args.query)
        if auto_key:
            profile_obj = get_profile(auto_key)
            profile_key = auto_key
            if profile_obj and not args.json:
                print(f"  [topic auto] → {auto_key}（{profile_obj['name']}）", file=sys.stderr)

    if profile_obj:
        if args.sub_queries is None:
            args.sub_queries = int(profile_obj.get("sub_queries") or 4)
        if args.max_results is None:
            args.max_results = int(profile_obj.get("max_results") or 5)
        if args.depth is None:
            args.depth = str(profile_obj.get("depth") or "balanced")
        profile_applied = profile_obj["name"]

    if args.sub_queries is None:
        args.sub_queries = 4
    if args.max_results is None:
        args.max_results = 5
    if args.depth is None:
        args.depth = "balanced"

    try:
        work_packages = _load_work_packages(args.work_packages)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        parser.error(f"work-packages 无法解析: {e}")

    if args.mode == "social-sentiment":
        platforms = [p.strip() for p in args.platforms.split(",")] if args.platforms else None
        report = social_sentiment_research(args.query, platforms, args.max_results)
    else:
        report = deep_research(
            args.query, args.sub_queries, args.max_results,
            args.timeout, args.depth, args.mode,
            profile=profile_obj,
            budget=args.budget,
            route_strategy=args.route_strategy,
            work_packages=work_packages,
            allow_recompute=args.allow_recompute,
        )

    if profile_applied:
        report["topic_profile"] = profile_applied
        if profile_key:
            report["topic_profile_key"] = profile_key

    if args.verify:
        try:
            from evidence_loop import verify_results
            targets = []
            for c in report.get("citations") or report.get("sources") or []:
                if isinstance(c, dict) and c.get("url"):
                    targets.append({
                        "url": c.get("url"),
                        "title": c.get("title") or "",
                        "snippet": c.get("snippet") or "",
                    })
            if not targets:
                for block in report.get("key_findings") or []:
                    if not isinstance(block, dict):
                        continue
                    top = block.get("top_result") or {}
                    if top.get("url"):
                        targets.append({
                            "url": top.get("url"),
                            "title": top.get("title") or "",
                            "snippet": top.get("snippet") or "",
                        })
            v = verify_results(targets[:args.verify], args.query, top_k=args.verify)
            report["verify"] = v
            from research_gates import evaluate_dossier_gates
            report["quality_gate_results"] = evaluate_dossier_gates(report)
            report["conclusion_cap"] = report["quality_gate_results"]["conclusion_cap"]
            if not args.json:
                rs = v.get("revision_summary") or {}
                print(
                    f"  [verify] 核验 {rs.get('n', 0)} 条，"
                    f"improved={rs.get('improved', 0)} unchanged={rs.get('unchanged', 0)} "
                    f"degraded={rs.get('degraded', 0)} mean_delta={rs.get('mean_delta', 0)}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"  [verify error] {type(e).__name__}: {e}", file=sys.stderr)

    if do_archive:
        try:
            from archive_run import write_search_archive, resolve_archive_root
            flat_results = []
            for c in report.get("citations") or report.get("sources") or []:
                if isinstance(c, dict) and c.get("url"):
                    flat_results.append({
                        "title": c.get("title") or c.get("url"),
                        "url": c.get("url"),
                        "snippet": c.get("snippet") or "",
                        "source": c.get("source") or c.get("engine") or "research",
                        "score": c.get("score"),
                    })
            if not flat_results:
                for block in report.get("key_findings") or []:
                    if not isinstance(block, dict):
                        continue
                    top = block.get("top_result") or {}
                    if top.get("url"):
                        flat_results.append({
                            "title": top.get("title") or top.get("url"),
                            "url": top.get("url"),
                            "snippet": top.get("snippet") or "",
                            "source": top.get("source") or "research",
                        })
            envelope = {
                "query": report.get("query") or args.query,
                "status": "completed",
                "mode": report.get("mode") or args.mode,
                "depth": args.depth,
                "engine": "research",
                "engines": report.get("engines_used") or ["research"],
                "results": flat_results,
                "count": len(flat_results),
                "sources": report.get("sources") or [],
                "citations": report.get("citations") or [],
                "input_kind": "keyword",
                "limitations": [
                    "research archive: dossier; snippets not verified body",
                ],
                "research_meta": {
                    "kind": report.get("kind"),
                    "sub_queries": report.get("sub_queries"),
                    "work_packages": report.get("work_packages"),
                    "gaps": report.get("gaps"),
                    "quality_gate_results": report.get("quality_gate_results"),
                    "topic_profile": report.get("topic_profile"),
                    "key_findings": report.get("key_findings"),
                },
                "errors": report.get("errors") or [],
            }
            root = resolve_archive_root(args.archive_dir) if args.archive_dir else None
            meta = write_search_archive(
                envelope,
                root=root,
                tag=args.archive_tag or "research",
                note=args.archive_note,
                source="argo_research",
            )
            report["archive"] = meta
            if not args.json:
                print(f"  [archive] {meta.get('run_id')} → {meta.get('run_dir')}", file=sys.stderr)
        except Exception as e:
            print(f"  [archive error] {type(e).__name__}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if profile_applied:
            print(f"📋 选题类型：{profile_applied}", file=sys.stderr)
        if report.get("mode") == "social-sentiment":
            _print_social_report(report)
        else:
            from research_report import print_deep_report
            print_deep_report(report)


if __name__ == "__main__":
    main()
