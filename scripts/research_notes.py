#!/usr/bin/env python3
"""research_notes.py — 研报标准化落盘与轻量索引。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from cli_io import dumps

KINDS = ("study", "explore")
_KIND_SUFFIX = {"study": "研究", "explore": "探索"}

SECTIONS = [
    ("0. 结论速览", "3-7 条，每条一句话；读完能回答「所以呢」"),
    ("1. 关键事实", "带来源的事实，标注 [已验证] / [推断]；每条可回源"),
    ("2. 机制拆解", "原文/对象怎么做到的，拆到可复用粒度；不是摘要，是结构"),
    ("3. 可迁移判断", "对我们工作有什么用，写条件与风险；面向决策，不空谈"),
    ("4. 边界与后续", "未解决 / 待核验 / 后续动作；未知项写补证动作"),
]

_STRONG_MARKERS = (".git", "AGENTS.md", "WORKSPACE")
_WEAK_MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
WORKSPACE_MARKERS = _STRONG_MARKERS + _WEAK_MARKERS


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, fallback: str = "note") -> str:
    """URL 或问题标题 → 安全文件名 slug。"""
    tail = text.rstrip("/").rsplit("/", 1)[-1] or text
    tail = re.sub(r"[?#].*$", "", tail)
    tail = re.sub(r"\s+", "-", tail.strip())
    tail = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", tail).strip("-.")
    return tail[:80] or fallback


def find_workspace_root(start_path: Path | None = None) -> Path:
    """从当前路径向上寻找工作区根目录。"""
    curr = (start_path or Path.cwd()).resolve()
    parents = [curr, *curr.parents]
    for markers in (_STRONG_MARKERS, _WEAK_MARKERS):
        for parent in parents:
            if any((parent / marker).exists() for marker in markers):
                return parent
            if parent == parent.parent:
                break
    return curr


def resolve_research_base(workspace: Path | None = None) -> Path:
    """研究与归档共用的基目录。"""
    ws = workspace or find_workspace_root()
    if (ws / "docs" / "research").is_dir():
        return ws / "docs" / "research"
    if (ws / "research").is_dir():
        return ws / "research"
    if (ws / "docs").is_dir():
        return ws / "docs" / "research"
    return ws / "research"


_ARGO_SELF_ROOT = Path(__file__).resolve().parent.parent


def is_argo_self_workspace(workspace: Path | None = None) -> bool:
    """判断工作区是否为 argo 工具自身仓库，避免把个人探索产物落进工具仓。"""
    try:
        ws = (workspace or find_workspace_root()).resolve()
        return ws == _ARGO_SELF_ROOT
    except OSError:
        return False


def workspace_anchorable(workspace: Path | None = None) -> bool:
    """当前工作区是否可锚定（argo 工具自身仓库不作为个人研究落盘工作区）。"""
    try:
        ws = workspace or find_workspace_root()
        if is_argo_self_workspace(ws):
            return False
        return any((ws / m).exists() for m in WORKSPACE_MARKERS)
    except Exception:
        return False


def should_anchor_workspace(workspace: Path | None = None) -> bool:
    """统一判断默认解析是否应落到工作区（而非用户级 research_root）。"""
    ws = workspace or find_workspace_root()
    if is_argo_self_workspace(ws):
        return False
    base = resolve_research_base(ws)
    try:
        if base.is_dir():
            return True
    except OSError:
        pass
    return workspace_anchorable(ws)


def resolve_root(explicit: str | Path | None = None) -> Path:
    """解析研报保存根目录。"""
    return resolve_root_with_source(explicit)[0]


def resolve_root_with_source(explicit: str | Path | None = None) -> tuple[Path, str]:
    """解析研报根目录并返回来源说明。"""
    if explicit:
        return Path(explicit).expanduser().resolve(), f"--root/--dir={explicit}"
    for env in ("ARGO_RESEARCH_ROOT", "ARGO_ARCHIVE_ROOT"):
        val = os.environ.get(env, "").strip()
        if val:
            return Path(val).expanduser().resolve(), f"{env}={val}"

    ws = find_workspace_root()
    base = resolve_research_base(ws)
    if should_anchor_workspace(ws):
        return base, f"工作区锚定 {base}"

    try:
        from argo_paths import research_root
        user_root = research_root().expanduser().resolve()
        return user_root, f"用户级兜底 {user_root}"
    except Exception:
        return base, f"工作区锚定 {base}"


def index_path(root: Path | None = None) -> Path:
    return (root or resolve_root()) / "research-index.jsonl"


def append_index(row: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """追加一行研究笔记索引。"""
    p = index_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return {"indexed": False, "path": str(p), "error": "write_failed"}
    return {"indexed": True, "path": str(p)}


def _body_excerpt(note_path: Path, limit: int = 200) -> str:
    """提取 Markdown 正文首行非标题内容用于检索。"""
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    # 纯横线行（frontmatter 结束后的水平线 "---"）不是正文——实测骨架与填实
    # 两种形态都会在标题块后紧跟一条水平线，不跳过它 excerpt 恒为 "---"。
    skip_prefixes = ("#", "<!--", "研究对象", "调研日期", "证据口径", "关联", "---")
    for line in text.splitlines():
        s = line.strip()
        if s and not any(s.startswith(p) for p in skip_prefixes):
            return s[:limit]
    return ""


def search_index(
    query: str,
    root: Path | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """检索研报索引（支持 slug / title / url / related / 研报正文内容匹配）。"""
    p = index_path(root)
    if not p.is_file():
        return []
    kw = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if kind and row.get("kind") != kind:
                continue
            hay = " ".join(
                str(row.get(k) or "") for k in ("slug", "title", "url", "related", "body_excerpt")
            ).lower()
            matched = not kw or kw in hay
            if not matched:
                note_file = row.get("note_file")
                if note_file and Path(note_file).is_file():
                    try:
                        matched = kw in Path(note_file).read_text(encoding="utf-8", errors="replace").lower()
                    except OSError:
                        pass
            if matched:
                out.append(row)
    return out[-limit:][::-1]


def build_markdown_report(
    *,
    kind: str,
    title: str,
    slug: str,
    url_or_problem: str,
    date: str,
    related: str = "无",
    archive_text: str = "",
    output_path: Path,
) -> Path:
    """生成标准化单一 Markdown 研报（含标准 YAML Frontmatter 与五段式结构）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "title": title, "slug": slug, "kind": kind,
        "target": url_or_problem, "date": date,
        "created_at": _now_iso(), "related": related,
    }
    lines = [
        "---",
        *[f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()],
        "---", "",
        f"# {title}", "",
        f"研究对象：{url_or_problem}",
        f"调研日期：{date}",
        "证据口径：`[已验证]` = 直接读到原文内容；`[推断]` = 基于原文事实的判断，未直接证实",
        f"关联：{related}", "", "---", "",
        *[f"## {heading}\n\n<!-- {hint} -->\n" for heading, hint in SECTIONS],
    ]
    if kind == "explore":
        lines.append("## 5. 候选方向\n\n<!-- 每条含：问题陈述 / 为什么值得 / 怎么证伪 / 第一步取证动作 -->\n")
    if archive_text.strip():
        lines.append("---\n\n## 附录：参考原文\n\n" + archive_text.strip() + "\n")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _create_note_bundle(
    kind: str,
    *,
    root: Path | None = None,
    slug: str,
    title: str,
    target: str,
    related: str = "无",
    archive_text: str = "",
    output: Path | None = None,
) -> dict[str, Any]:
    rt = root or resolve_root()
    out_file = output or (rt / kind / slug / f"{slug}-{_KIND_SUFFIX[kind]}.md")
    build_markdown_report(
        kind=kind, title=title or slug, slug=slug, url_or_problem=target,
        date=datetime.now().strftime("%Y%m%d"), related=related,
        archive_text=archive_text, output_path=out_file,
    )
    row: dict[str, Any] = {
        "schema_version": 2, "kind": kind, "slug": slug,
        "title": title or slug, "url": target, "note_file": str(out_file),
        "related": related or "", "body_excerpt": _body_excerpt(out_file),
        "created_at": _now_iso(),
    }
    if kind == "study":
        row["archive_sha256"] = "embedded" if archive_text else ""
    meta = append_index(row, root=rt)
    return {
        "kind": kind, "slug": slug, "note": str(out_file),
        "output": str(out_file), "bundle_dir": str(out_file.parent), "index": meta,
    }


def create_study_bundle(
    *, root: Path | None = None, slug: str, title: str, url: str,
    archive_text: str = "", related: str = "无", output: Path | None = None,
) -> dict[str, Any]:
    """单篇精研研报标准化生成。"""
    return _create_note_bundle(
        "study", root=root, slug=slug, title=title, target=url,
        related=related, archive_text=archive_text, output=output,
    )


def create_explore_bundle(
    *, root: Path | None = None, slug: str, title: str, problem: str,
    related: str = "无", output: Path | None = None,
) -> dict[str, Any]:
    """方向探索研报标准化生成。"""
    return _create_note_bundle(
        "explore", root=root, slug=slug, title=title, target=problem,
        related=related, output=output,
    )


def _cmd_create(args: argparse.Namespace) -> int:
    kind = args.command
    target = args.url if kind == "study" else args.problem
    slug = args.slug or slugify(target)
    out_path = Path(args.output).expanduser().resolve() if args.output else None
    archive_text = ""
    if kind == "study":
        archive_text = args.archive_text or ""
        if not archive_text and args.archive and Path(args.archive).is_file():
            try:
                archive_text = Path(args.archive).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    res = _create_note_bundle(
        kind, root=resolve_root(args.root), slug=slug, title=args.title or slug,
        target=target, related=args.related or "无",
        archive_text=archive_text, output=out_path,
    )
    print(dumps(res))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    root, root_source = resolve_root_with_source(args.root)
    rows = search_index(args.query or "", root=root, kind=args.kind, limit=args.limit)
    out: dict[str, Any] = {"count": len(rows), "notes": rows, "root": str(root), "root_source": root_source}
    if not rows:
        out["hint"] = f"当前归档根 {root} 下没有命中。可指定 --dir 或设置 ARGO_RESEARCH_ROOT。"
    print(dumps(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="argo note", description="研究笔记生成与归档：单一自包含 Markdown 研报输出")
    sub = p.add_subparsers(dest="command", required=True)
    for name, arg_name, arg_help, desc in (
        ("study", "url", "研究对象 URL", "生成单篇精研研报（标准 Markdown + Frontmatter）"),
        ("explore", "problem", "探索问题或主题", "生成方向探索研报（标准 Markdown + Frontmatter）"),
    ):
        sp = sub.add_parser(name, help=desc)
        sp.add_argument(arg_name, help=arg_help)
        sp.add_argument("--title", help="标题（缺省用 slug）")
        sp.add_argument("--slug", help="标识符（缺省从对象推导）")
        sp.add_argument("-o", "--output", help="直接指定研报输出文件路径")
        if name == "study":
            sp.add_argument("--archive", help="原文文件（直接作为附录内嵌）")
            sp.add_argument("--archive-text", help="直接提供原文文本（作为附录内嵌）")
        sp.add_argument("--related", help="关联引用")
        sp.add_argument("--root", "--dir", dest="root", help="归档根目录（缺省自动锚定工作区）")
        sp.set_defaults(func=_cmd_create)

    pl = sub.add_parser("list", help="检索研报索引")
    pl.add_argument("query", nargs="?", help="检索关键词")
    pl.add_argument("--kind", choices=KINDS, help="只看 study 或 explore")
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--root", "--dir", dest="root", help="归档根目录")
    pl.set_defaults(func=_cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
