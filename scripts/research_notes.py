#!/usr/bin/env python3
"""research_notes.py — 研报标准化落盘与极简轻量索引。

遵循奥卡姆剃刀与通行 Agent 规范：
  - 研报以单一自包含 Markdown 文件落地（内嵌 YAML Frontmatter），不搞过度工程的
    多层嵌套子目录、sources/ 原文副本搬运、相对软链别名与 manifest.json；
  - 默认落盘至约定标准目录（工作区 docs/research/ 或当前 research/，可通过 -o/--output 显式指定）；
  - 保留轻量索引与子串检索，为上层 Agent 提供清晰正交的研报生成与消费接口。
"""

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

SECTIONS = [
    ("0. 结论速览", "3-7 条，每条一句话；读完能回答「所以呢」"),
    ("1. 关键事实", "带来源的事实，标注 [已验证] / [推断]；每条可回源"),
    ("2. 机制拆解", "原文/对象怎么做到的，拆到可复用粒度；不是摘要，是结构"),
    ("3. 可迁移判断", "对我们工作有什么用，写条件与风险；面向决策，不空谈"),
    ("4. 边界与后续", "未解决 / 待核验 / 后续动作；未知项写补证动作"),
]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, fallback: str = "note") -> str:
    """URL 或问题标题 → 安全文件名 slug。"""
    tail = text.rstrip("/").rsplit("/", 1)[-1] or text
    tail = re.sub(r"[?#].*$", "", tail)
    tail = re.sub(r"\s+", "-", tail.strip())
    tail = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", tail).strip("-.")
    return tail[:80] or fallback


_slugify = slugify  # 兼容历史私有别名


_STRONG_MARKERS = (".git", "AGENTS.md", "WORKSPACE")
_WEAK_MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
WORKSPACE_MARKERS = _STRONG_MARKERS + _WEAK_MARKERS


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


def workspace_anchorable() -> bool:
    """当前工作区是否可锚定。"""
    try:
        ws = find_workspace_root()
        return any((ws / m).exists() for m in _STRONG_MARKERS + _WEAK_MARKERS)
    except Exception:
        return False


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

    base = resolve_research_base()
    try:
        if base.is_dir() or workspace_anchorable():
            return base, f"工作区锚定 {base}"
    except OSError:
        pass

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
    # 去掉 Frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    skip_prefixes = ("#", "<!--", "研究对象", "调研日期", "证据口径", "关联")
    for line in text.splitlines():
        s = line.strip()
        if not s or any(s.startswith(p) for p in skip_prefixes):
            continue
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
            excerpt = str(row.get("body_excerpt") or "")
            note_file = row.get("note_file")
            hay = " ".join([
                str(row.get("slug") or ""),
                str(row.get("title") or ""),
                str(row.get("url") or ""),
                str(row.get("related") or ""),
                excerpt,
            ]).lower()
            if kw and kw not in hay:
                # 现场查验研报正文是否命中
                if note_file and Path(note_file).is_file():
                    try:
                        content = Path(note_file).read_text(encoding="utf-8", errors="replace").lower()
                        if kw in content:
                            out.append(row)
                            continue
                    except OSError:
                        pass
                continue
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
    created_at = _now_iso()

    # 标准 YAML Frontmatter
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"slug: {json.dumps(slug, ensure_ascii=False)}",
        f"kind: {json.dumps(kind, ensure_ascii=False)}",
        f"target: {json.dumps(url_or_problem, ensure_ascii=False)}",
        f"date: {json.dumps(date, ensure_ascii=False)}",
        f"created_at: {json.dumps(created_at, ensure_ascii=False)}",
        f"related: {json.dumps(related, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        f"研究对象：{url_or_problem}",
        f"调研日期：{date}",
        "证据口径：`[已验证]` = 直接读到原文内容；`[推断]` = 基于原文事实的判断，未直接证实",
        f"关联：{related}",
        "",
        "---",
        "",
    ]

    for heading, hint in SECTIONS:
        lines.append(f"## {heading}\n\n<!-- {hint} -->\n")

    if kind == "explore":
        lines.append("## 5. 候选方向\n\n<!-- 每条含：问题陈述 / 为什么值得 / 怎么证伪 / 第一步取证动作 -->\n")

    if archive_text.strip():
        lines.append("---\n\n## 附录：参考原文\n\n" + archive_text.strip() + "\n")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def create_study_bundle(
    *,
    root: Path | None = None,
    slug: str,
    title: str,
    url: str,
    archive_text: str = "",
    related: str = "无",
    output: Path | None = None,
) -> dict[str, Any]:
    """单篇精研研报标准化生成。"""
    rt = root or resolve_root()
    date = datetime.now().strftime("%Y%m%d")
    out_file = output or (rt / "study" / slug / f"{slug}-研究.md")
    build_markdown_report(
        kind="study",
        title=title or slug,
        slug=slug,
        url_or_problem=url,
        date=date,
        related=related,
        archive_text=archive_text,
        output_path=out_file,
    )

    meta = append_index({
        "schema_version": 2,
        "kind": "study",
        "slug": slug,
        "title": title or slug,
        "url": url,
        "note_file": str(out_file),
        "related": related or "",
        "body_excerpt": _body_excerpt(out_file),
        "archive_sha256": "embedded" if archive_text else "",
        "created_at": _now_iso(),
    }, root=rt)

    return {
        "kind": "study",
        "slug": slug,
        "note": str(out_file),
        "output": str(out_file),
        "bundle_dir": str(out_file.parent),
        "index": meta,
    }


def create_explore_bundle(
    *,
    root: Path | None = None,
    slug: str,
    title: str,
    problem: str,
    related: str = "无",
    output: Path | None = None,
) -> dict[str, Any]:
    """方向探索研报标准化生成。"""
    rt = root or resolve_root()
    date = datetime.now().strftime("%Y%m%d")
    out_file = output or (rt / "explore" / slug / f"{slug}-探索.md")
    build_markdown_report(
        kind="explore",
        title=title or slug,
        slug=slug,
        url_or_problem=problem,
        date=date,
        related=related,
        output_path=out_file,
    )

    meta = append_index({
        "schema_version": 2,
        "kind": "explore",
        "slug": slug,
        "title": title or slug,
        "url": problem,
        "note_file": str(out_file),
        "related": related or "",
        "body_excerpt": _body_excerpt(out_file),
        "created_at": _now_iso(),
    }, root=rt)

    return {
        "kind": "explore",
        "slug": slug,
        "note": str(out_file),
        "output": str(out_file),
        "bundle_dir": str(out_file.parent),
        "index": meta,
    }


def _cmd_study(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    slug = args.slug or slugify(args.url)
    out_path = Path(args.output).expanduser().resolve() if args.output else None

    archive_text = args.archive_text or ""
    if not archive_text and args.archive and Path(args.archive).is_file():
        try:
            archive_text = Path(args.archive).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    res = create_study_bundle(
        root=root,
        slug=slug,
        title=args.title or slug,
        url=args.url,
        archive_text=archive_text,
        related=args.related or "无",
        output=out_path,
    )
    print(dumps(res))
    return 0


def _cmd_explore(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    slug = args.slug or slugify(args.problem)
    out_path = Path(args.output).expanduser().resolve() if args.output else None

    res = create_explore_bundle(
        root=root,
        slug=slug,
        title=args.title or slug,
        problem=args.problem,
        related=args.related or "无",
        output=out_path,
    )
    print(dumps(res))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    root, root_source = resolve_root_with_source(args.root)
    rows = search_index(
        args.query or "",
        root=root,
        kind=args.kind,
        limit=args.limit,
    )
    out: dict[str, Any] = {
        "count": len(rows),
        "notes": rows,
        "root": str(root),
        "root_source": root_source,
    }
    if not rows:
        out["hint"] = (
            f"当前归档根 {root} 下没有命中。可指定 --dir 或设置 ARGO_RESEARCH_ROOT。"
        )
    print(dumps(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="argo note",
        description="研究笔记生成与归档：单一自包含 Markdown 研报输出",
    )
    sub = p.add_subparsers(dest="command", required=True)

    ps = sub.add_parser("study", help="生成单篇精研研报（标准 Markdown + Frontmatter）")
    ps.add_argument("url", help="研究对象 URL")
    ps.add_argument("--title", help="标题（缺省用 slug）")
    ps.add_argument("--slug", help="标识符（缺省从 URL 推导）")
    ps.add_argument("-o", "--output", help="直接指定研报输出文件路径")
    ps.add_argument("--archive", help="原文文件（直接作为附录内嵌）")
    ps.add_argument("--archive-text", help="直接提供原文文本（作为附录内嵌）")
    ps.add_argument("--related", help="关联引用")
    ps.add_argument("--root", "--dir", dest="root", help="归档根目录（缺省自动锚定工作区）")
    ps.set_defaults(func=_cmd_study)

    pe = sub.add_parser("explore", help="生成方向探索研报（标准 Markdown + Frontmatter）")
    pe.add_argument("problem", help="探索问题或主题")
    pe.add_argument("--title", help="标题（缺省用 slug）")
    pe.add_argument("--slug", help="标识符（缺省从问题推导）")
    pe.add_argument("-o", "--output", help="直接指定研报输出文件路径")
    pe.add_argument("--related", help="关联引用")
    pe.add_argument("--root", "--dir", dest="root", help="归档根目录（缺省自动锚定工作区）")
    pe.set_defaults(func=_cmd_explore)

    pl = sub.add_parser("list", help="检索研报索引")
    pl.add_argument("query", nargs="?", help="检索关键词")
    pl.add_argument("--kind", choices=KINDS, help="只看 study 或 explore")
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--root", "--dir", dest="root", help="归档根目录")
    pl.set_defaults(func=_cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
