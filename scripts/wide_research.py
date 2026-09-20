#!/usr/bin/env python3
"""wide_research.py — 广泛研究的机器侧原语（skill 形态）。

与 DSH 插件 `@taxueseek/argo-dsh` 的 wide_research 共用同一套证据语义，
但宿主能力不同：插件靠 ctx.subagents 派 worker、outputSchema 拿结构化
结果；skill 形态的宿主只有 bash，所以这里只做**确定性机器侧**：

  stage  — 轨道按 depends_on 分阶段（复用 research_work_packages 的分层）
  merge  — 各轨道 worker 产出 → 跨轨道来源账本（URL 去重、http(s) 门）
           → 宽表门禁（failures→low / warnings→medium / 干净→high）
           → Markdown 报告落盘

规划（拆轨道）、worker 取证、综合写作这三步是模型判断，不属于机器，
协议见 references/research-protocol.md 的「广泛研究（skill 形态）」一节。

用法：
  python3 scripts/wide_research.py stage --tracks '<json 数组>'
  python3 scripts/wide_research.py merge --run-dir <dir> [--question ...] [--write-report]

merge 的输入是 <run-dir>/tracks/<track-id>.json，每个文件形如：
  {"track_id": "...", "summary": "...", "findings": [...],
   "sources": [{"title","url","source_type","claim","excerpt",
                "confidence","limitations"}],
   "disagreements": [...], "gaps": [...], "error": 可选}
输出（stdout，--json）：账本 + stats + quality_gate_results + report_path。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_io import dumps  # noqa: E402
from research_work_packages import stage_work_packages  # noqa: E402

MAX_SOURCES_PER_TRACK = 8
MAX_FINDINGS_PER_TRACK = 8
MAX_DISAGREEMENTS = 6
MAX_GAPS = 6
CLIP = {
    "title": 240,
    "url": 2000,
    "source_type": 80,
    "claim": 1000,
    "excerpt": 1200,
    "limitations": 480,
    "summary": 2500,
    "finding": 1200,
    "disagreement": 1000,
    "gap": 1000,
}


def _clip(value: Any, limit: int, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text[:limit]


def normalize_track(raw: Any, index: int) -> dict[str, Any] | None:
    """对齐插件 normalizeTrack：id/title/question 必需，rationale/depends_on 可选。"""
    if not isinstance(raw, dict):
        return None
    title = _clip(raw.get("title"), 120)
    question = _clip(raw.get("question"), 800)
    if not title or not question:
        return None
    track_id = _clip(raw.get("id") or f"track-{index + 1}", 64)
    track_id = re.sub(r"[^a-z0-9_-]+", "-", track_id.lower()).strip("-") or f"track-{index + 1}"
    return {
        "id": track_id,
        "title": title,
        "question": question,
        "rationale": _clip(raw.get("rationale"), 360, "Independent evidence-collection angle."),
        "depends_on": [str(d).strip() for d in (raw.get("depends_on") or []) if str(d).strip()][:8],
    }


def normalize_tracks(raw: Any, maximum: int = 9) -> list[dict[str, Any]]:
    """接受 list / JSON 字符串 / {"tracks": [...]}，去重、截断到 maximum。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tracks 不是合法 JSON: {exc}") from exc
    if isinstance(raw, dict):
        raw = raw.get("tracks") or []
    if not isinstance(raw, list):
        raise ValueError("tracks 须为数组")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if len(out) >= maximum:
            break
        track = normalize_track(item, i)
        if not track or track["id"] in seen:
            continue
        seen.add(track["id"])
        out.append(track)
    return out


def stage_tracks(tracks: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """轨道分层。work package 的分层逻辑与轨道同构：id/question/depends_on。"""
    packages = [
        {"id": t["id"], "question": t["question"], "depends_on": t["depends_on"]}
        for t in tracks
    ]
    stages, warnings = stage_work_packages(packages)
    by_id = {t["id"]: t for t in tracks}
    return [[by_id[p["id"]] for p in stage if p["id"] in by_id] for stage in stages], warnings


def normalize_source(raw: Any) -> dict[str, Any] | None:
    """对齐插件 normalizeSource：title/url/claim 必需，仅 http(s) 入账。"""
    if not isinstance(raw, dict):
        return None
    title = _clip(raw.get("title"), CLIP["title"])
    url = _clip(raw.get("url"), CLIP["url"])
    claim = _clip(raw.get("claim"), CLIP["claim"])
    if not title or not url or not claim:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    confidence = str(raw.get("confidence") or "low").lower()
    if confidence not in ("high", "medium"):
        confidence = "low"
    return {
        "title": title,
        "url": url,
        "source_type": _clip(raw.get("source_type") or raw.get("sourceType"), CLIP["source_type"], "web"),
        "claim": claim,
        "excerpt": _clip(raw.get("excerpt"), CLIP["excerpt"], "No excerpt supplied."),
        "confidence": confidence,
        "limitations": _clip(raw.get("limitations"), CLIP["limitations"],
                             "Not independently verified by the orchestrator."),
    }


def normalize_track_result(raw: Any, track_id: str) -> dict[str, Any]:
    """对齐插件 normalizeResearch 的字段与截断。error 轨道保留错误信息。"""
    record = raw if isinstance(raw, dict) else {}
    sources: list[dict[str, Any]] = []
    for item in (record.get("sources") or [])[:MAX_SOURCES_PER_TRACK]:
        source = normalize_source(item)
        if source:
            sources.append(source)
    return {
        "track_id": track_id,
        "summary": _clip(record.get("summary"), CLIP["summary"], "No usable summary returned."),
        "findings": [_clip(f, CLIP["finding"]) for f in (record.get("findings") or [])[:MAX_FINDINGS_PER_TRACK]],
        "sources": sources,
        "disagreements": [_clip(d, CLIP["disagreement"]) for d in (record.get("disagreements") or [])[:MAX_DISAGREEMENTS]],
        "gaps": [_clip(g, CLIP["gap"]) for g in (record.get("gaps") or [])[:MAX_GAPS]],
        "error": _clip(record.get("error"), 800) or None,
    }


def load_track_results(run_dir: Path, tracks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """读 <run-dir>/tracks/*.json。给了 --tracks 时交叉校验：缺失产出的轨道记失败行，
    防 worker 静默丢失（没写文件 = 没完成，门禁必须看见）。"""
    tracks_dir = run_dir / "tracks"
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    if tracks_dir.is_dir():
        for path in sorted(tracks_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                results.append(normalize_track_result({"error": f"轨道文件不可读: {exc}"}, path.stem))
                seen.add(path.stem)
                continue
            track_id = str(raw.get("track_id") or path.stem)
            seen.add(track_id)
            results.append(normalize_track_result(raw, track_id))
    elif tracks is None:
        raise FileNotFoundError(f"找不到轨道产出目录: {tracks_dir}")
    if tracks:
        for track in tracks:
            if track["id"] not in seen:
                results.append(normalize_track_result(
                    {"error": f"worker 未产出结果文件（{tracks_dir}/{track['id']}.json 不存在）"},
                    track["id"],
                ))
    if not results:
        raise FileNotFoundError(f"{tracks_dir} 下没有任何轨道产出 (*.json)")
    return results


def build_ledger(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """跨轨道来源账本：URL 归一去重，S1..Sn 全局编号。返回 (sources, ledger)。"""
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    ledger: list[dict[str, Any]] = []
    for result in results:
        keys: list[str] = []
        for source in result["sources"]:
            normalized = source["url"].lower().split("#", 1)[0].rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            key = f"S{len(sources) + 1}"
            sources.append({**source, "key": key})
            keys.append(key)
        ledger.append({
            "track_id": result["track_id"],
            "summary": result["summary"],
            "findings": result["findings"],
            "source_keys": keys,
            "disagreements": result["disagreements"],
            "gaps": result["gaps"],
            "error": result["error"],
        })
    return sources, ledger


def evaluate_gates(
    sources: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    caveats: list[str],
    unanswered: list[str],
) -> dict[str, Any]:
    """对齐插件 evaluateGates 的可判定谓词（recompute 门由 research.py 工作包链路负责）。"""
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    completed = [row for row in ledger if not row["error"]]
    failed = [row for row in ledger if row["error"]]

    if not sources:
        failures.append({"id": "no_sources", "detail": "No usable sources with real URLs were returned."})
    if not completed:
        failures.append({"id": "no_completed_tracks", "detail": "No research track completed."})
    if failed and completed:
        warnings.append({
            "id": "partial_track_failure",
            "detail": f"{len(failed)} of {len(ledger)} track(s) failed; report covers completed tracks only.",
        })
    if sources and all(s["confidence"] != "high" for s in sources):
        warnings.append({"id": "no_high_confidence_sources",
                         "detail": "No source reached high confidence; treat claims as unverified."})
    if len(caveats) + len(unanswered) >= max(3, (len(sources) + 1) // 2):
        warnings.append({"id": "high_uncertainty",
                         "detail": f"{len(caveats)} caveats and {len(unanswered)} unanswered questions exceed the evidence threshold."})
    cap = "low" if failures else ("medium" if warnings else "high")
    return {"passed": not failures, "conclusion_cap": cap, "failures": failures, "warnings": warnings}


def render_report(question: str, summary: str, answer: str,
                  sources: list[dict[str, Any]], warnings: list[str]) -> str:
    """对齐插件 renderReport 的报告骨架。"""
    bibliography = "\n".join(f"- [{s['key']}] {s['title']} — {s['url']}" for s in sources) \
        or "- No valid source entries were returned."
    warning_block = ""
    if warnings:
        warning_block = "\n\n## Execution warnings\n" + "\n".join(f"- {w}" for w in warnings)
    return (f"# Wide Research Report\n\n## Executive summary\n{summary}\n\n"
            f"{answer}\n\n## Evidence ledger\n{bibliography}{warning_block}")


def persist_report(run_dir: Path, question: str, text: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", question[:40]).strip("_") or "research"
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = run_dir / f"{ts}-{slug}.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def cmd_stage(args: argparse.Namespace) -> int:
    tracks = normalize_tracks(args.tracks, args.max_tracks)
    if len(tracks) < 2:
        print("wide_research: 至少需要两条有效轨道（id/title/question）", file=sys.stderr)
        return 2
    stages, warnings = stage_tracks(tracks)
    print(dumps({
        "stages": [[t["id"] for t in stage] for stage in stages],
        "tracks": tracks,
        "warnings": warnings,
        "hint": "每个阶段内的轨道并行取证；下一阶段等上一阶段完成。worker 产出写 <run-dir>/tracks/<track-id>.json。",
    }))
    return 0


def _parse_lines(raw: str) -> list[str]:
    """接受 JSON 数组或每行一条；给 agent 留最低门槛的输入方式。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw[0] == "[":
        try:
            return [str(c) for c in json.loads(raw)][:12]
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in raw.splitlines() if line.strip()][:12]


def cmd_merge(args: argparse.Namespace) -> int:
    tracks = normalize_tracks(args.tracks, 9) if args.tracks else []
    run_dir = Path(args.run_dir).expanduser()
    results = load_track_results(run_dir, tracks or None)
    sources, ledger = build_ledger(results)

    caveats: list[str] = []
    unanswered: list[str] = []
    summary = args.summary or ""
    answer = args.answer or ""
    if args.synthesis:
        try:
            raw = Path(args.synthesis).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            print(f"wide_research: synthesis 文件不可读: {exc}", file=sys.stderr)
            return 2
        try:
            synth = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"wide_research: synthesis 不是合法 JSON: {exc}", file=sys.stderr)
            return 2
        summary = _clip(synth.get("executive_summary") or synth.get("executiveSummary"), CLIP["summary"]) or summary
        answer = str(synth.get("answer") or answer)
        caveats = [str(c)[:CLIP["disagreement"]] for c in (synth.get("caveats") or [])[:12]]
        unanswered = [str(c)[:CLIP["gap"]] for c in (synth.get("unanswered_questions") or synth.get("unansweredQuestions") or [])[:12]]
    else:
        caveats = _parse_lines(args.caveats)
        unanswered = _parse_lines(args.unanswered)

    gates = evaluate_gates(sources, ledger, caveats, unanswered)
    warnings = [w["detail"] for w in gates["warnings"]]
    report = ""
    report_path = None
    if args.write_report:
        if not answer:
            # 没有综合正文时，报告至少承载账本与警告，供人工/后续核验。
            answer = "\n\n".join(
                f"### {row['track_id']}\n{row['summary']}"
                + ("".join(f"\n- {f}" for f in row["findings"]))
                for row in ledger if not row["error"]
            )
        report = render_report(args.question or "Wide Research", summary, answer, sources, warnings)
        report_path = persist_report(run_dir, args.question or "wide-research", report)

    print(dumps({
        "question": args.question or "",
        "stats": {
            "plannedTracks": len(ledger),
            "completedTracks": sum(1 for r in ledger if not r["error"]),
            "failedTracks": sum(1 for r in ledger if r["error"]),
            "sourceCount": len(sources),
        },
        "sources": sources,
        "ledger": ledger,
        "caveats": caveats,
        "unanswered_questions": unanswered,
        "warnings": warnings,
        "quality_gate_results": gates,
        "report": report,
        "report_path": report_path,
    }))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wide_research", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_stage = sub.add_parser("stage", help="轨道按 depends_on 分阶段")
    p_stage.add_argument("--tracks", required=True, help="轨道 JSON 数组（内联或文件路径）")
    p_stage.add_argument("--max-tracks", type=int, default=9)
    p_stage.add_argument("--json", action="store_true", help="兼容仓库 CLI 惯例；输出本就是 JSON")
    p_stage.set_defaults(func=cmd_stage)

    p_merge = sub.add_parser("merge", help="轨道产出 → 账本 + 门禁 + 报告")
    p_merge.add_argument("--run-dir", required=True, help="本次研究运行目录（含 tracks/*.json）")
    p_merge.add_argument("--tracks", default="", help="轨道 JSON（与 stage 同输入），用于交叉校验缺失产出")
    p_merge.add_argument("--question", default="")
    p_merge.add_argument("--summary", default="", help="执行摘要（模型写）")
    p_merge.add_argument("--answer", default="", help="综合正文 Markdown（模型写）")
    p_merge.add_argument("--synthesis", default="", help="综合 JSON 文件（answer/executive_summary/caveats/unanswered_questions）")
    p_merge.add_argument("--caveats", default="", help="JSON 数组或每行一条（无 --synthesis 时生效）")
    p_merge.add_argument("--unanswered", default="", help="JSON 数组或每行一条（无 --synthesis 时生效）")
    p_merge.add_argument("--write-report", action="store_true", help="报告落盘 <run-dir>/<ts>-<slug>.md")
    p_merge.add_argument("--json", action="store_true", help="兼容仓库 CLI 惯例；输出本就是 JSON")
    p_merge.set_defaults(func=cmd_merge)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
