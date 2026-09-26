#!/usr/bin/env python3
"""
archive_run.py — 搜索/研究 run 工作区归档

职责边界：
  - 搜索只做发现；本模块把**当次 envelope** 写入文件，便于复用与分析
  - **不**抓取正文、**不**下载媒体、**不**覆盖旧 run
  - known-url / 正文吸收仍走 fetch / extract / 其他打包工具

目录约定（默认）::

  <root>/
    index.jsonl                 # 全局索引（一行一个 run）
    runs/
      YYYY-MM-DD/
        <run_id>/
          run-summary.json      # 元数据 + 计数 + 路径
          envelope.json         # 完整可公开搜索结果（去 _ 私有键）
          candidates.jsonl      # 候选一行一条
          results.jsonl         # 原始 results 一行一条
          coverage.json         # 后端覆盖
          INDEX.md              # 人读摘要

环境变量：
  ARGO_ARCHIVE_ROOT  覆盖默认归档根目录

默认根目录解析顺序：
  1. ARGO_ARCHIVE_ROOT
  2. 工作区存在 AGENTS.md 时 → <workspace>/数据/argo-search-archive
  3. ./数据/argo-search-archive（若父级 数据/ 可建）
  4. ./argo-search-archive
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from cli_io import dumps, dumps_pretty

_TZ_CN = timezone(timedelta(hours=8))
SCHEMA = "argo-search-archive/1.0"


def _now() -> datetime:
    return datetime.now(_TZ_CN)


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    s = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", s, flags=re.U)
    s = s.strip("._") or "query"
    return s[:max_len]


def _public_payload(result: dict[str, Any]) -> dict[str, Any]:
    """去掉私有键与过大字段，避免凭证泄漏。"""
    out: dict[str, Any] = {}
    for k, v in result.items():
        if k.startswith("_"):
            continue
        if k in {"raw_html", "headers", "cookie", "cookies", "authorization"}:
            continue
        out[k] = v
    return out


# ── 凭证值级脱敏（写入时生效）────────────────────────────────────────────────
# _public_payload 删的是「整个字段」（headers/cookie）；这里补的是「值里的片段」：
# URL 查询串里嵌的密钥（很多引擎把 key 放 query）、正文里贴的凭证。脱敏必须
# 发生在写入时，而不是读取/导出时——密钥一旦以明文写入文件，就已经泄漏给所有
# 能读到该文件的人，事后清理依赖「有人记得在出口调用」，不可靠。
_SECRET_QUERY_KEYS = (
    "key", "keys", "api_key", "apikey", "api-key", "access_key",
    "access_token", "token", "auth", "authorization", "credential",
    "password", "passwd", "pwd", "secret", "sig", "signature", "sign",
    "session", "sessionid", "sessdata", "cookie", "bili_jct", "d_c0",
)
_SECRET_QUERY_RE = re.compile(
    r"((?:[?&;]|\b)(?:%s)=)([^&;\s\"']+)" % "|".join(
        re.escape(k) for k in _SECRET_QUERY_KEYS), re.IGNORECASE)
_BEARER_RE = re.compile(r"(bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
# 厂商密钥前缀：不少上游在错误体里回显密钥片段
# （如 OpenAI `Incorrect API key provided: sk-...`），而它既不是 query 参数
# 也不是 Bearer 头，前两类规则都漏。前缀取主流按段可辨识者。
# Google/AWS/Slack 三家的 key 不带统一分隔符（实测 AIzaSy…、AKIA…、xoxb-…
# 在上面「前缀+分隔符」规则下全部漏过），按各家官方形态单列：AIza 起头总长
# 39 字符、AKIA 后接 16 位大写数字、xoxb- 后接数字段。同一文本里兄弟 key
# 各自独立命中，re.sub 全量替换。
_VENDOR_KEY_RE = re.compile(
    r"\b(?:sk|rk|pk|gsk|xai|hf|ghp|gho|github_pat|glpat|ya29)"
    r"[-_][A-Za-z0-9._\-]{8,}"
    r"|\bAIza[A-Za-z0-9_\-]{30,}"
    r"|\bAKIA[A-Z0-9]{16}"
    r"|\bxoxb-[A-Za-z0-9\-]{20,}")
_HOME_PATH_RE = re.compile(
    r"([A-Za-z]:\\Users\\[^\\\s:\"]+"      # Windows：C:\Users\<name>
    r"|/Users/[^/\s:\"]+"                  # macOS
    r"|/home/[^/\s:\"]+)")                 # Linux / BSD


def redact_secrets(text: str) -> str:
    """字符串里的凭证值改写为 [REDACTED]，家目录改写为 ~。

    按类别正则实现，逐类有测试锁定；不加白名单——宁可多脱一个无害参数
    （多脱只损失一点调试信息），不可漏一个真密钥。
    """
    if not text:
        return text
    out = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", text)
    out = _BEARER_RE.sub(r"\1[REDACTED]", out)
    out = _VENDOR_KEY_RE.sub("[REDACTED]", out)
    out = _HOME_PATH_RE.sub("~", out)
    return out


def _redact_deep(obj: Any) -> Any:
    """递归脱敏：dict/list 逐值处理，str 应用规则，其余原样。"""
    if isinstance(obj, dict):
        return {k: _redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_deep(v) for v in obj]
    if isinstance(obj, str):
        return redact_secrets(obj)
    return obj


def resolve_archive_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("ARGO_ARCHIVE_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    # 向上找带 AGENTS.md 的工作区
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if (p / "AGENTS.md").is_file():
            root = p / "数据" / "argo-search-archive"
            return root
        if p == p.parent:
            break

    data_local = cwd / "数据" / "argo-search-archive"
    return data_local


def make_run_id(query: str, when: datetime | None = None) -> str:
    when = when or _now()
    stamp = when.strftime("%Y%m%dT%H%M%S")
    qh = hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:8]
    return f"{stamp}_{_slug(query)}_{qh}"


def search_archive(query: str, root: Path | None = None,
                   since: str | None = None, until: str | None = None,
                   limit: int = 10) -> list[dict[str, Any]]:
    """检索归档索引（成果复用）：主题词命中的历史研究/搜索 run。

    - 数据源：{root}/index.jsonl（无索引时返回空）
    - 过滤：query/tag/note 子串命中（query 必需）；packed_at 日期窗可选
    - 返回：run_id / packed_at / query / tag / source / counts / run_dir
    """
    archive_root = (root or resolve_archive_root()).expanduser().resolve()
    index_path = archive_root / "index.jsonl"
    if not index_path.exists():
        return []
    kw = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    try:
        with index_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                hay = " ".join([
                    str(row.get("query") or ""),
                    str(row.get("tag") or ""),
                    str(row.get("note") or ""),
                ]).lower()
                if kw and kw not in hay:
                    continue
                packed = str(row.get("packed_at") or "")
                day = packed[:10]
                if since and day and day < since:
                    continue
                if until and day and day > until:
                    continue
                out.append({
                    "run_id": row.get("run_id"),
                    "packed_at": packed,
                    "query": row.get("query"),
                    "tag": row.get("tag"),
                    "source": row.get("source"),
                    "status": row.get("status"),
                    "counts": row.get("counts") or {},
                    "run_dir": row.get("run_dir"),
                })
                if len(out) >= limit:
                    break
    except OSError:
        return []
    return out


def write_search_archive(
    result: dict[str, Any],
    *,
    root: Path | None = None,
    tag: str | None = None,
    note: str | None = None,
    source: str = "argo_search",
) -> dict[str, Any]:
    """将搜索 envelope 写入新 run 目录。永不覆盖已有路径。

    Returns:
        handoff 摘要：{run_id, root, paths, counts, schema_version}
    """
    archive_root = root or resolve_archive_root()
    archive_root = archive_root.expanduser().resolve()
    when = _now()
    query = str(result.get("query") or result.get("query_original") or "")
    run_id = make_run_id(query, when)
    day = when.strftime("%Y-%m-%d")
    run_dir = archive_root / "runs" / day / run_id
    if run_dir.exists():
        # 碰撞：追加序号
        for i in range(2, 100):
            cand = archive_root / "runs" / day / f"{run_id}-r{i}"
            if not cand.exists():
                run_dir = cand
                run_id = run_dir.name
                break
        else:
            raise RuntimeError(f"无法分配 run 目录: {run_dir}")

    run_dir.mkdir(parents=True, exist_ok=False)
    public = _public_payload(result)
    # 凭证脱敏在写入时生效：URL 查询串密钥 / Bearer 头 / 家目录路径，
    # 覆盖 envelope 与三类 jsonl 的全部下游数据
    public = _redact_deep(public)
    candidates = public.get("candidates") if isinstance(public.get("candidates"), list) else []
    results = public.get("results") if isinstance(public.get("results"), list) else []
    coverage = public.get("coverage") if isinstance(public.get("coverage"), list) else []
    sources = public.get("sources") if isinstance(public.get("sources"), list) else []
    if not sources and results:
        # 归档时补全 sources，与 search.build_sources 语义一致
        try:
            from search import build_sources  # type: ignore
            sources = build_sources(results)
            public["sources"] = sources
        except Exception:
            sources = [
                {
                    "ref": i + 1,
                    "title": (r.get("title") or "")[:160],
                    "url": r.get("url"),
                    "engine": r.get("source"),
                }
                for i, r in enumerate(results)
                if isinstance(r, dict) and r.get("url")
            ]
            public["sources"] = sources

    # 若无 candidates 但有 results，做最小投影（不 import 循环依赖时的保底）
    if not candidates and results:
        try:
            from candidate_envelope import result_to_candidate  # type: ignore
            candidates = [
                result_to_candidate(r, query, rank=i + 1)
                for i, r in enumerate(results)
                if isinstance(r, dict) and "error" not in r
            ]
        except Exception:
            # 兜底投影：candidate_envelope 不可导入时才会走到。verification 的
            # 判据**只有一处定义**（candidate_envelope.verification_of），这里
            # 单独再试一次导入；连它都拿不到就取保守值（candidate / 未打开）——
            # 判据不可得时不能凭空断言「已打开原文」。此前这里硬编码 False，
            # 于是在「已抓过正文」的行上与本文件同源的 results 自相矛盾。
            try:
                from candidate_envelope import verification_of  # type: ignore
            except Exception:
                def verification_of(_item):  # type: ignore[misc]
                    return {"status": "candidate", "opened_original": False,
                            "checked_at": None}
            candidates = [
                {
                    "candidate_id": f"web:rank-{i+1}",
                    "query": query,
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "snippet": (r.get("snippet") or "")[:300],
                    "backend": r.get("source") or r.get("_engine"),
                    "rank": i + 1,
                    "verification": verification_of(r),
                }
                for i, r in enumerate(results)
                if isinstance(r, dict)
            ]

    envelope_path = run_dir / "envelope.json"
    candidates_path = run_dir / "candidates.jsonl"
    results_path = run_dir / "results.jsonl"
    sources_path = run_dir / "sources.jsonl"
    coverage_path = run_dir / "coverage.json"
    summary_path = run_dir / "run-summary.json"
    index_md_path = run_dir / "INDEX.md"

    envelope_path.write_text(
        dumps_pretty(public) + "\n",
        encoding="utf-8",
    )
    with candidates_path.open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with results_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with sources_path.open("w", encoding="utf-8") as f:
        for s in sources:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    coverage_path.write_text(
        dumps_pretty(coverage) + "\n",
        encoding="utf-8",
    )

    engines = public.get("engines_used") or public.get("engines_combo") or public.get("engines") or []
    if isinstance(engines, str):
        engines = [engines]

    summary = {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "source": source,
        "packed_at": when.isoformat(),
        "query": query,
        "query_original": public.get("query_original"),
        "input_kind": public.get("input_kind"),
        "status": public.get("status") or "completed",
        "mode": public.get("mode"),
        "depth": public.get("depth"),
        "engine": public.get("engine"),
        "engines": engines,
        "domain": public.get("domain"),
        "tag": tag,
        "note": note,
        "counts": {
            "results": len(results),
            "candidates": len(candidates),
            "sources": len(sources),
            "coverage_backends": len(coverage),
            "errors": len(public.get("errors") or []),
        },
        "limitations": public.get("limitations") or [],
        "paths": {
            "run_dir": str(run_dir),
            "envelope": str(envelope_path),
            "candidates": str(candidates_path),
            "results": str(results_path),
            "sources": str(sources_path),
            "coverage": str(coverage_path),
            "summary": str(summary_path),
            "index_md": str(index_md_path),
        },
        "reuse": {
            "candidates_jsonl": str(candidates_path),
            "sources_jsonl": str(sources_path),
            "load_hint": "sources.jsonl 为编号信源；candidates.jsonl 为完整候选；snippet 非正文",
            "analysis_hint": "可按 platform/backend/canonical_url 聚合；勿把 snippet 当正文事实",
        },
        "boundaries": {
            "discovery_only": True,
            "body_fetched": False,
            "media_downloaded": False,
            "overwrite": False,
        },
    }
    summary_path.write_text(
        dumps_pretty(summary) + "\n",
        encoding="utf-8",
    )

    # 人读 INDEX
    lines = [
        f"# 搜索归档 · {run_id}",
        "",
        f"- **时间**: {when.isoformat()}",
        f"- **查询**: {query}",
        f"- **状态**: {summary['status']}",
        f"- **引擎**: {public.get('engine')} / {engines}",
        f"- **结果**: results={len(results)} candidates={len(candidates)}",
        f"- **模式**: mode={public.get('mode')} depth={public.get('depth')} kind={public.get('input_kind')}",
    ]
    if tag:
        lines.append(f"- **标签**: {tag}")
    if note:
        lines.append(f"- **备注**: {note}")
    lines += ["", "## 边界", "", "- 本包仅为**发现候选**，未抓正文、未下媒体",
              "- 引用前应对 candidates/sources 做 fetch/extract 核验",
              "- `verification.status=candidate` 时禁止把 snippet 当事实",
              "", "## 相关信源", ""]
    preview = sources[:20] if sources else candidates[:15]
    for i, c in enumerate(preview, 1):
        if not isinstance(c, dict):
            continue
        ref = c.get("ref") or i
        title = (c.get("title") or "")[:80]
        url = c.get("url") or ""
        backend = c.get("engine") or c.get("backend") or ""
        lines.append(f"[{ref}] [{backend}] {title}" if backend else f"[{ref}] {title}")
        if url:
            lines.append(f"    {url}")
    extra_n = max(0, (len(sources) if sources else len(candidates)) - len(preview))
    if extra_n:
        lines.append(f"\n… 另有 {extra_n} 条，见 sources.jsonl / candidates.jsonl")
    lims = summary.get("limitations") or []
    if lims:
        lines += ["", "## limitations", ""]
        for lim in lims[:8]:
            lines.append(f"- {lim}")
    lines.append("")
    index_md_path.write_text("\n".join(lines), encoding="utf-8")

    # 全局 index.jsonl 追加
    index_path = archive_root / "index.jsonl"
    archive_root.mkdir(parents=True, exist_ok=True)
    index_row = {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "packed_at": when.isoformat(),
        "query": query,
        "tag": tag,
        "status": summary["status"],
        "engine": public.get("engine"),
        "counts": summary["counts"],
        "run_dir": str(run_dir),
        "source": source,
    }
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(index_row, ensure_ascii=False) + "\n")

    # 附回结果对象（不破坏调用方）
    result["archive"] = {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "root": str(archive_root),
        "run_dir": str(run_dir),
        "paths": summary["paths"],
        "counts": summary["counts"],
    }
    return result["archive"]


def list_runs(
    root: Path | None = None,
    *,
    limit: int = 20,
    tag: str | None = None,
    query_substr: str | None = None,
) -> list[dict[str, Any]]:
    archive_root = root or resolve_archive_root()
    index_path = archive_root / "index.jsonl"
    if not index_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with index_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tag and row.get("tag") != tag:
                continue
            if query_substr and query_substr not in str(row.get("query") or ""):
                continue
            rows.append(row)
    return rows[-limit:]


def load_run(run_dir: str | Path) -> dict[str, Any]:
    p = Path(run_dir)
    summary = json.loads((p / "run-summary.json").read_text(encoding="utf-8"))
    envelope = json.loads((p / "envelope.json").read_text(encoding="utf-8"))
    return {"summary": summary, "envelope": envelope, "run_dir": str(p.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Argo 搜索 run 工作区归档")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_write = sub.add_parser("write", help="从 stdin JSON 写入归档")
    p_write.add_argument("--root", type=Path, default=None)
    p_write.add_argument("--tag", default=None)
    p_write.add_argument("--note", default=None)
    p_write.add_argument("--source", default="argo_search")

    p_list = sub.add_parser("list", help="列出近期 run")
    p_list.add_argument("--root", type=Path, default=None)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--tag", default=None)
    p_list.add_argument("--query", default=None, help="query 子串过滤")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="展示某 run 摘要")
    p_show.add_argument("run_dir", type=Path)
    p_show.add_argument("--json", action="store_true")

    p_root = sub.add_parser("root", help="打印默认归档根目录")
    p_root.add_argument("--root", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.cmd == "root":
        print(resolve_archive_root(args.root))
        return 0

    if args.cmd == "write":
        raw = sys.stdin.read()
        if not raw.strip():
            print("stdin 为空，需要搜索 JSON", file=sys.stderr)
            return 1
        data = json.loads(raw)
        if not isinstance(data, dict):
            print("stdin 须为 JSON object", file=sys.stderr)
            return 1
        meta = write_search_archive(
            data,
            root=args.root,
            tag=args.tag,
            note=args.note,
            source=args.source,
        )
        print(dumps(meta))
        return 0

    if args.cmd == "list":
        rows = list_runs(
            args.root,
            limit=args.limit,
            tag=args.tag,
            query_substr=args.query,
        )
        if args.json:
            print(dumps(rows))
        else:
            if not rows:
                print("(empty)")
                return 0
            for r in rows:
                c = r.get("counts") or {}
                print(
                    f"{r.get('packed_at', '?'):<25} "
                    f"cand={c.get('candidates', 0):>3} "
                    f"[{r.get('engine') or '?'}] "
                    f"{(r.get('query') or '')[:50]}  "
                    f"→ {r.get('run_dir')}"
                )
        return 0

    if args.cmd == "show":
        data = load_run(args.run_dir)
        if args.json:
            print(dumps(data["summary"]))
        else:
            s = data["summary"]
            print(f"run_id: {s.get('run_id')}")
            print(f"query:  {s.get('query')}")
            print(f"counts: {s.get('counts')}")
            print(f"paths:  {s.get('paths', {}).get('run_dir')}")
            for lim in (s.get("limitations") or [])[:5]:
                print(f"  ! {lim}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
