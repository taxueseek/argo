#!/usr/bin/env python3
"""research_notes.py — 研究笔记归档（判断稿的一等公民化）。

为什么单独一层（2026-11-11）：
  `argo research --archive` 只存 dossier（来源、覆盖、缺口、门禁），判断稿由
  Agent 手写、散落在工作区某处，两者对不上。后果是「成果复用」只能复用证据，
  复用不了结论——下一次研究仍从零开始。Hyra 2.0 称之为「工具不形成智能复利」。

本模块把判断稿变成一等公民：
  - 目录：<root>/<kind>/<slug>/（kind = study | explore，slug 由调用方给）
    同 slug 重跑时为目录名加 `-<时分秒>` 后缀，绝不覆盖旧目录
  - 文件：原文存档 sources/<slug>_原文版_<date>.md、笔记 <slug>-研究.md 或
    <slug>-探索.md、规范化判断稿 00_判断稿.md、证据账本 manifest.json
  - 索引：<root>/research-index.jsonl，一行一条，含 slug / kind / url / sha256 /
    body_excerpt / related，供 search_archive 与 list 检索（body_excerpt 让
    `note list` 能命中笔记正文里的结论，不只是元数据）

与 archive_run 的分工（不重叠）：
  archive_run 是**搜索取证 run**（envelope + candidates + results + coverage），
  面向「证据怎么来的」；本模块是**研究判断 run**（原文 + 笔记 + 契约），
  面向「结论是什么」。两者共用 <root> 但索引文件不同，互不写入对方目录。

边界：
  - 只搬运与登记，**不**校验笔记内容质量（那是判断，不是机器）
  - sha256 只对原文存档算，笔记是易改的产物
  - 不覆盖同名 slug：重跑时新建带时间戳的目录，旧条目保留
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from cli_io import dumps

# 笔记类型：决定目录名与索引 kind。两类都是「判断稿」，形态不同：
#   study  = 单篇精研（一篇长文 → 一份判断）
#   explore = 方向探索（一个开放问题 → 一组候选方向）
KINDS = ("study", "explore")

# 契约五段式（research/_契约.md 的唯一真源，这里只用于骨架生成）
SECTIONS = [
    ("0. 结论速览", "3-7 条，每条一句话；读完能回答「所以呢」"),
    ("1. 关键事实", "带来源的事实，标注 [已验证] / [推断]；每条可回源"),
    ("2. 机制拆解", "原文/对象怎么做到的，拆到可复用粒度；不是摘要，是结构"),
    ("3. 可迁移判断", "对我们工作有什么用，写条件与风险；面向决策，不空谈"),
    ("4. 边界与后续", "未解决 / 待核验 / 后续动作；未知项写补证动作"),
]

# 头部固定字段模板（契约规定，机器只负责排版）
HEADER_TEMPLATE = """# {title}

研究对象：{url}
原文存档：{archive_ref}
调研日期：{date}
证据口径：`[已验证]` = 直接读到原文内容；`[推断]` = 基于原文事实的判断，未直接证实
关联：{related}
"""

EXPLORE_HEADER_TEMPLATE = """# {title}

研究对象：{url_or_problem}
调研日期：{date}
证据口径：`[已验证]` = 直接读到原文内容；`[推断]` = 基于原文事实的判断，未直接证实
关联：{related}
"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _slugify(text: str, fallback: str = "note") -> str:
    """URL 路径段 → 安全 slug：保留中文，去掉路径分隔与空白。"""
    tail = text.rstrip("/").rsplit("/", 1)[-1] or text
    tail = re.sub(r"[?#].*$", "", tail)
    tail = re.sub(r"\s+", "-", tail.strip())
    tail = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", tail).strip("-.")
    return tail[:80] or fallback


# 公开别名：跨模块（research_cli --auto-note）调用，不再导入下划线私有名。
slugify = _slugify


def _unique_dir(base: Path) -> Path:
    """分配一个未占用的归档目录，避免同 slug 重跑覆盖旧笔记。

    首次仍用裸 slug（保持既有路径可读）；同 slug 已存在时加 `-<时分秒>` 后缀，
    再冲突则追加序号，最后退回微秒后缀，保证任何情况下都不覆盖既有目录。
    模块 docstring 承诺「不覆盖同名 slug：重跑时新建带时间戳的目录」，此前
    `_cmd_study` / `_cmd_explore` 直接复用 `<root>/<kind>/<slug>/`，同 slug 同日
    重跑会静默覆盖旧笔记、sources 原文与 manifest（2026-11-11 实测）。
    """
    if not base.exists():
        return base
    stem = base.name
    stamp = datetime.now().strftime("%H%M%S")
    for i in range(100):
        cand = base.with_name(f"{stem}-{stamp}" if i == 0 else f"{stem}-{stamp}-{i}")
        if not cand.exists():
            return cand
    return base.with_name(f"{stem}-{datetime.now().strftime('%H%M%S%f')}")


def _body_excerpt(note_path: Path, limit: int = 200) -> str:
    """笔记正文首段摘录（头部固定字段之后的第一段正文），供 search_index 命中结论。

    只索引元数据时 `argo note list` 搜不到笔记结论（2026-11-11 实测：骨架里
    首段是「## 0. 结论速览」的提示注释，索引不含正文就等于没有可检索的结论）。
    头部（`# 标题` 到第一个 `---` 之间）是「研究对象 / 调研日期 / 证据口径 /
    关联」这类固定字段，不算正文，跳过；正文里的标题与 HTML 提示注释也跳过。
    """
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 先切掉头部固定字段（第一个 `---` 之前）
    parts = text.split("\n---\n", 1)
    body = parts[1] if len(parts) > 1 else text
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--"):
            continue
        return s[:limit]
    return ""


def _sha256_file(path: Path, chunk: int = 256 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# 强标记优先：先在所有祖先里找版本库/工作区契约，找不到再认工程文件。
# 只按「就近命中」扫一遍时，repo/frontend/package.json 会先于 repo/.git
# 命中，归档就落进子包，恰好造成「子目录执行碎片化」——这正是要避免的。
_STRONG_MARKERS = (".git", "AGENTS.md", "WORKSPACE")
_WEAK_MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
WORKSPACE_MARKERS = _STRONG_MARKERS + _WEAK_MARKERS  # 兼容既有导入


def find_workspace_root(start_path: Path | None = None) -> Path:
    """从当前路径向上寻找工作区（项目）根目录。

    两轮扫描：强标记（.git / AGENTS.md / WORKSPACE）优先，全部祖先都没有
    才认工程文件（pyproject / package.json / Cargo.toml / go.mod）。
    """
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
    """研究与归档共用的基目录，避免笔记和 archive 落在两棵树。

    优先级：既有 docs/research/ → 既有 research/ → 有 docs/ 时新建
    docs/research/ → 否则新建 research/。调用方只在这一个基目录下分支
    （笔记直接在基目录，归档在 <base>/archive）。
    """
    ws = workspace or find_workspace_root()
    if (ws / "docs" / "research").is_dir():
        return ws / "docs" / "research"
    if (ws / "research").is_dir():
        return ws / "research"
    if (ws / "docs").is_dir():
        return ws / "docs" / "research"
    return ws / "research"


def resolve_root(explicit: str | Path | None = None) -> Path:
    """研究笔记归档根目录。

    优先级：
      1. 显式参数 explicit（--root / --dir）
      2. 环境变量 ARGO_RESEARCH_ROOT / ARGO_ARCHIVE_ROOT
      3. 工作区智能锚定：向上探测项目根（.git / AGENTS.md / pyproject 等），
         优先落地在工程文档规范的 docs/research/ 或既有的 research/ 目录下。
      4. 兜底：用户级文档目录（argo_paths.research_root）——工作区完全不可
         锚定（容器、无持久盘、临时目录）时，结论不至于随容器销毁而丢。
         与缓存分开：缓存可清，结论不可丢。
    """
    return resolve_root_with_source(explicit)[0]


def resolve_root_with_source(explicit: str | Path | None = None) -> tuple[Path, str]:
    """解析归档根，连同决策来源一起返回——路径与 provenance 的单一真源。

    `note list` 等诊断输出直接取这里的 source，禁止再按优先级重推一遍：
    resolve_root 的优先级一改，重推的标签就会撒谎（2026-11-11 评审）。
    """
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
    # 工作区不可写/不可锚定：退回用户级文档目录
    try:
        from argo_paths import research_root
        user_root = research_root().expanduser().resolve()
        return user_root, f"用户级兜底 {user_root}"
    except Exception:
        return base, f"工作区锚定 {base}"


def workspace_anchorable() -> bool:
    """当前是否处于可锚定的工作区（有强标记或工程文件）。

    无标记的临时目录（容器 workdir、/tmp、CI 未检出的目录）在 resolve_research_base
    里会退化成 `<cwd>/research`，写进去的东西随环境消失。有标记才认项目级归档。

    探测自身失败（权限、文件系统异常）按「不可锚定」处理：结论宁可落用户级
    持久区，也不写进身份未知的易失目录。archive_run.resolve_archive_root 用
    同一兜底口径，两处的失败语义由本函数锁死（2026-11-11 评审）。
    """
    try:
        ws = find_workspace_root()
        return any((ws / m).exists() for m in _STRONG_MARKERS + _WEAK_MARKERS)
    except Exception:
        return False


def index_path(root: Path | None = None) -> Path:
    return (root or resolve_root()) / "research-index.jsonl"


def append_index(
    row: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """追加一行研究笔记索引。索引损坏时跳过（不阻断主流程）。"""
    path = index_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return {"indexed": False, "path": str(path), "error": "write_failed"}
    return {"indexed": True, "path": str(path)}


def search_index(
    query: str,
    root: Path | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """按子串检索研究笔记索引（slug / title / url / related / 笔记正文首段）。

    `body_excerpt` 是登记时写进索引行的正文首段（见 `_body_excerpt`），检索
    时一并纳入匹配面——否则笔记写完，`argo note list` 仍搜不到它的结论内容，
    「结论复用」落空。

    摘录为空时（骨架刚生成、结论是后来写进笔记的；或旧索引行没有该字段）
    现场读笔记文件补算，保证「写完就能搜到」，不必重新登记。笔记文件缺失
    时退化为只匹配元数据。
    """
    path = index_path(root)
    if not path.is_file():
        return []
    kw = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
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
            if not excerpt:
                note_file = row.get("note_file")
                if note_file:
                    excerpt = _body_excerpt(Path(note_file))
            hay = " ".join([
                str(row.get("slug") or ""),
                str(row.get("title") or ""),
                str(row.get("url") or ""),
                str(row.get("related") or ""),
                excerpt,
            ]).lower()
            if kw and kw not in hay:
                continue
            out.append(row)
    return out[-limit:][::-1]


def build_study_note(
    *,
    title: str,
    url: str,
    archive_file: str,
    archive_sha256: str,
    date: str,
    related: str = "无",
    output: Path,
) -> Path:
    """生成单篇精研笔记骨架（五段式 + 头部固定字段）。"""
    body = HEADER_TEMPLATE.format(
        title=title,
        url=url,
        archive_ref=f"{archive_file}（sha256 `{archive_sha256[:8]}…`，{date} 抓取）",
        date=date,
        related=related,
    )
    body += "\n---\n\n"
    for heading, hint in SECTIONS:
        body += f"## {heading}\n\n<!-- {hint} -->\n\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return output


def build_explore_note(
    *,
    title: str,
    url_or_problem: str,
    date: str,
    related: str = "无",
    output: Path,
) -> Path:
    """生成方向探索笔记骨架（五段式，头部字段不同）。"""
    body = EXPLORE_HEADER_TEMPLATE.format(
        title=title,
        url_or_problem=url_or_problem,
        date=date,
        related=related,
    )
    body += "\n---\n\n"
    for heading, hint in SECTIONS:
        body += f"## {heading}\n\n<!-- {hint} -->\n\n"
    body += "## 5. 候选方向\n\n"
    body += "<!-- 每条含：问题陈述 / 为什么值得 / 怎么证伪 / 第一步取证动作 -->\n\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return output


def _cmd_study(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    slug = args.slug or _slugify(args.url)
    # 同 slug 重跑换目录，避免覆盖旧笔记、sources 原文与 manifest（见 _unique_dir）
    kind_dir = _unique_dir(root / "study" / slug)
    date = datetime.now().strftime("%Y%m%d")

    # 原文存档：唯一真源沉淀至 sources/ 目录并算 sha256，保持离线自包含
    archive_name = f"{slug}_原文版_{date}.md"
    sources_dir = kind_dir / "sources"
    source_file = sources_dir / archive_name
    sha = ""
    source = "url"
    if args.archive and Path(args.archive).is_file():
        sources_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.archive, source_file)
        sha = _sha256_file(source_file)
        source = "local_archive"
    elif args.archive_text:
        sources_dir.mkdir(parents=True, exist_ok=True)
        source_file.write_text(args.archive_text, encoding="utf-8")
        sha = _sha256_file(source_file)
        source = "text"

    # 兼容根目录原文引用：建立相对软链指向 sources/，消除物理双份副本隐患
    archive_path = kind_dir / archive_name
    if sha:
        if archive_path.is_symlink() or archive_path.exists():
            archive_path.unlink()
        try:
            archive_path.symlink_to(Path("sources") / archive_name)
        except OSError:
            pass

    # 规范化：以 00_判断稿.md 为单一真源实体稿件
    judgement_name = "00_判断稿.md"
    judgement_path = kind_dir / judgement_name
    build_study_note(
        title=args.title or slug,
        url=args.url,
        archive_file=archive_name,
        archive_sha256=sha or "（未登记原文）",
        date=date,
        related=args.related or "无",
        output=judgement_path,
    )

    # 兼容旧版命名：建立相对符号链接，避免两份物理实体造成版本分叉
    note_name = f"{slug}-研究.md"
    note_path = kind_dir / note_name
    if note_path.is_symlink() or note_path.exists():
        note_path.unlink()
    try:
        note_path.symlink_to(judgement_name)
    except OSError:
        shutil.copy2(judgement_path, note_path)

    # 资产包证据账本 manifest.json
    manifest = {
        "schema_version": 1,
        "kind": "study",
        "slug": slug,
        "title": args.title or slug,
        "url": args.url,
        "date": date,
        "note_file": note_name,
        "judgement_file": "00_判断稿.md",
        # 旧名只是指向判断稿的相对软链，不是第二份实体；显式登记便于
        # 备份/打包工具判断是否保留了链接。
        "aliases": [note_name],
        "sources": [
            {
                "name": archive_name,
                "path": f"sources/{archive_name}",
                "sha256": sha,
                "source": source,
            }
        ] if sha else [],
        "created_at": _now_iso(),
    }
    (kind_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = append_index({
        "schema_version": 1,
        "kind": "study",
        "slug": slug,
        "title": args.title or slug,
        "url": args.url,
        "source": source,
        "archive_file": str(source_file if sha else archive_path),
        "archive_sha256": sha,
        "note_file": str(note_path),
        "judgement_file": str(judgement_path),
        "related": args.related or "",
        "body_excerpt": _body_excerpt(note_path),
        "created_at": _now_iso(),
    }, root=root)

    print(dumps({
        "kind": "study",
        "slug": slug,
        "note": str(note_path),
        "judgement": str(judgement_path),
        "bundle_dir": str(kind_dir),
        "manifest": str(kind_dir / "manifest.json"),
        "archive": str(archive_path) if archive_path.exists() else None,
        "archive_sha256": sha or None,
        "index": meta,
    }))
    return 0


def create_explore_bundle(
    *,
    root: Path,
    slug: str,
    title: str,
    problem: str,
    related: str = "无",
) -> dict[str, Any]:
    """创建一个方向探索资产包，供 CLI 与 research --auto-note 共用。

    两条入口此前各写一套目录、软链、manifest 与索引逻辑：`argo note explore`
    有资产包，`research --auto-note` 只有骨架笔记，结构随入口而变。收敛到
    本函数后，两边产出同一套 `00_判断稿.md / manifest.json / 索引行`。
    """
    kind_dir = _unique_dir(root / "explore" / slug)
    date = datetime.now().strftime("%Y%m%d")

    # 规范化：以 00_判断稿.md 为单一真源实体稿件
    judgement_name = "00_判断稿.md"
    judgement_path = kind_dir / judgement_name
    build_explore_note(
        title=title or slug,
        url_or_problem=problem,
        date=date,
        related=related or "无",
        output=judgement_path,
    )

    # 兼容旧版命名：建立相对符号链接，避免两份物理实体造成版本分叉
    note_name = f"{slug}-探索.md"
    note_path = kind_dir / note_name
    if note_path.is_symlink() or note_path.exists():
        note_path.unlink()
    try:
        note_path.symlink_to(judgement_name)
    except OSError:
        shutil.copy2(judgement_path, note_path)

    manifest = {
        "schema_version": 1,
        "kind": "explore",
        "slug": slug,
        "title": title or slug,
        "problem": problem,
        "date": date,
        "note_file": note_name,
        "judgement_file": "00_判断稿.md",
        "aliases": [note_name],
        "created_at": _now_iso(),
    }
    (kind_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = append_index({
        "schema_version": 1,
        "kind": "explore",
        "slug": slug,
        "title": title or slug,
        "url": problem,
        "note_file": str(note_path),
        "judgement_file": str(judgement_path),
        "related": related or "",
        "body_excerpt": _body_excerpt(note_path),
        "created_at": _now_iso(),
    }, root=root)

    return {
        "kind": "explore",
        "slug": slug,
        "note": str(note_path),
        "judgement": str(judgement_path),
        "bundle_dir": str(kind_dir),
        "manifest": str(kind_dir / "manifest.json"),
        "index": meta,
    }


def _cmd_explore(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    slug = args.slug or _slugify(args.problem)
    print(dumps(create_explore_bundle(
        root=root,
        slug=slug,
        title=args.title or slug,
        problem=args.problem,
        related=args.related or "无",
    )))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    root, root_source = resolve_root_with_source(args.root)
    rows = search_index(
        args.query or "",
        root=root,
        kind=args.kind,
        limit=args.limit,
    )
    # 换环境后第一个症状是「笔记都不见了」。把根目录与来源摊进输出，让
    # 「根不对」在第一屏就能看出来，而不是让人怀疑笔记被删了。
    out: dict[str, Any] = {
        "count": len(rows),
        "notes": rows,
        "root": str(root),
        "root_source": root_source,
    }
    if not rows:
        out["hint"] = (
            f"当前归档根 {root} 下没有命中。若刚换过机器或容器，"
            "用 `argo paths` 看 research_root，或设 ARGO_RESEARCH_ROOT 指定位置。"
        )
    print(dumps(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="argo note",
        description="研究笔记归档：把判断稿变成一等公民（study 单篇精研 / explore 方向探索）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    ps = sub.add_parser("study", help="登记一篇长文研究（原文存档 + 五段式笔记骨架）")
    ps.add_argument("url", help="研究对象 URL")
    ps.add_argument("--title", help="标题（缺省用 slug）")
    ps.add_argument("--slug", help="目录名（缺省从 URL 推导）")
    ps.add_argument("--archive", help="本地原文文件路径（复制到归档目录并算 sha256）")
    ps.add_argument("--archive-text", help="直接以文本作为原文存档")
    ps.add_argument("--related", help="关联 slug，多个用逗号分隔")
    ps.add_argument("--root", "--dir", dest="root", help="归档根目录（缺省自动锚定工作区）")
    ps.set_defaults(func=_cmd_study)

    pe = sub.add_parser("explore", help="登记一个方向探索（五段式笔记骨架 + 候选方向段）")
    pe.add_argument("problem", help="开放问题或探索对象")
    pe.add_argument("--title", help="标题（缺省用 slug）")
    pe.add_argument("--slug", help="目录名（缺省从问题推导）")
    pe.add_argument("--related", help="关联 slug，多个用逗号分隔")
    pe.add_argument("--root", "--dir", dest="root", help="归档根目录（缺省自动锚定工作区）")
    pe.set_defaults(func=_cmd_explore)

    pl = sub.add_parser("list", help="检索研究笔记索引")
    pl.add_argument("query", nargs="?", help="子串匹配 slug/title/url/related/正文首段")
    pl.add_argument("--kind", choices=KINDS, help="只看某一类")
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--root", help="归档根目录")
    pl.set_defaults(func=_cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
