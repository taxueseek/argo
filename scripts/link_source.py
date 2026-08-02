#!/usr/bin/env python3
"""
link_source.py — 把「消费者入口」指回本仓库真源（符号链接，不复制）。

标准化原则：
  1. 磁盘上只应有一份 argo 代码：本仓库（scripts/ 的上一级）。
  2. Skill 目录 / 文档入口若需要出现在主机约定位置，用 **symlink** 指向真源，
     禁止 rsync/cp 出第二份业务树。
  3. **目标路径绝不写死在代码里**。来源优先级：
       CLI `--to PATH`（可重复）
       → 环境变量 `ARGO_LINK_TARGETS`（os.pathsep 分隔，如 `:` / `;`）
       → 真源根目录下的 `installs.local.yaml`（本机声明，应 gitignore）
  4. 注册表派生仍只走 `sync_backends.py`（config.yaml → backends/*），与链接无关。

用法：
  # 本机 installs.local.yaml 示例见 installs.local.yaml.example
  python3 scripts/link_source.py
  python3 scripts/link_source.py --to /any/consumer/path/argo
  ARGO_LINK_TARGETS="$HOME/.claude/skills/argo:$HOME/.agents/skills/argo" \\
    python3 scripts/link_source.py
  python3 scripts/link_source.py --check
  python3 scripts/link_source.py --dry-run

退出码：0 成功；1 无目标 / 链接失败 / 校验不一致。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
LOCAL_INSTALLS = SOURCE / "installs.local.yaml"


def _load_local_targets() -> list[Path]:
    if not LOCAL_INSTALLS.exists():
        return []
    try:
        import yaml
    except ImportError:
        print("[warn] 需要 PyYAML 才能读 installs.local.yaml", file=sys.stderr)
        return []
    data = yaml.safe_load(LOCAL_INSTALLS.read_text(encoding="utf-8")) or {}
    raw = data.get("link_targets") or data.get("targets") or []
    if not isinstance(raw, list):
        return []
    out: list[Path] = []
    for item in raw:
        if not item:
            continue
        p = Path(str(item)).expanduser()
        # 相对路径相对真源根
        if not p.is_absolute():
            p = (SOURCE / p).resolve()
        else:
            p = p.resolve()
        out.append(p)
    return out


def _env_targets() -> list[Path]:
    raw = os.environ.get("ARGO_LINK_TARGETS", "").strip()
    if not raw:
        return []
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def resolve_targets(cli: list[Path] | None) -> list[Path]:
    """无默认路径：没有 CLI / env / local 文件则空列表。"""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for group in (cli or [], _env_targets(), _load_local_targets()):
        for p in group:
            rp = p.expanduser().resolve() if p.exists() or p.parent.exists() else p.expanduser()
            # normalize for dedupe
            key = rp if rp.is_absolute() else (Path.cwd() / rp).resolve()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(key)
    return ordered


def _is_link_to_source(path: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve() == SOURCE.resolve()
    except OSError:
        return False


def link_one(target: Path, *, dry_run: bool, force: bool) -> int:
    """将 target 设为指向 SOURCE 的 symlink。"""
    source = SOURCE.resolve()
    if target.resolve() == source and target.exists() and not target.is_symlink():
        print(f"[skip] 目标就是真源本体: {target}")
        return 0

    if _is_link_to_source(target):
        print(f"[ok]   已指向真源: {target} -> {source}")
        return 0

    if target.exists() or target.is_symlink():
        if not force:
            print(
                f"[fail] 目标已存在且不是指向真源的链接: {target}\n"
                f"       若确认可替换，加 --force（会先移走/删除该路径）",
                file=sys.stderr,
            )
            return 1
        if dry_run:
            print(f"[dry]  将替换已有路径: {target}")
        else:
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                # 旧多副本：整目录移走备份，避免误删用户未入库文件
                bak = target.with_name(target.name + ".bak-before-link")
                if bak.exists():
                    shutil.rmtree(bak) if bak.is_dir() and not bak.is_symlink() else bak.unlink(missing_ok=True)
                target.rename(bak)
                print(f"[bak]  旧副本已移至 {bak}")
            else:
                target.unlink(missing_ok=True)

    parent = target.parent
    if dry_run:
        print(f"[dry]  ln -sfn {source} {target}")
        return 0
    parent.mkdir(parents=True, exist_ok=True)
    # atomic-ish: symlink in place
    os.symlink(source, target, target_is_directory=True)
    print(f"[link] {target} -> {source}")
    return 0


def check_targets(targets: list[Path]) -> int:
    bad = 0
    print(f"真源: {SOURCE.resolve()}")
    if not targets:
        print("[fail] 无校验目标（请 --to / ARGO_LINK_TARGETS / installs.local.yaml）")
        return 1
    for t in targets:
        if _is_link_to_source(t):
            print(f"[ok]   {t} -> {t.resolve()}")
        elif t.resolve() == SOURCE.resolve() and t.exists():
            print(f"[ok]   {t} 即真源本体")
        else:
            print(f"[miss] {t} 未指向真源（exists={t.exists()} symlink={t.is_symlink()}）")
            bad += 1
    return 1 if bad else 0


def maybe_sync_backends() -> int:
    script = SOURCE / "scripts" / "sync_backends.py"
    if not script.exists():
        return 0
    r = subprocess.run([sys.executable, str(script)], cwd=str(SOURCE))
    if r.returncode != 0:
        return r.returncode
    return subprocess.run([sys.executable, str(script), "--check"], cwd=str(SOURCE)).returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="将消费者入口 symlink 到 argo 真源（无内置默认路径）",
    )
    p.add_argument(
        "--to",
        action="append",
        type=Path,
        default=None,
        help="链接目标路径（可重复）。无内置默认值。",
    )
    p.add_argument("--check", action="store_true", help="只校验目标是否指向真源")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--force",
        action="store_true",
        help="目标已存在时替换：symlink/file 删除；目录改名为 *.bak-before-link",
    )
    p.add_argument(
        "--with-backends",
        action="store_true",
        help="链接前先跑 sync_backends.py 派生 backends",
    )
    args = p.parse_args(argv)

    targets = resolve_targets(args.to)
    if not targets:
        print(
            "未指定任何链接目标。\n"
            "  方式 1: python3 scripts/link_source.py --to <path> [--to <path2>]\n"
            "  方式 2: export ARGO_LINK_TARGETS='path1:path2'\n"
            f"  方式 3: 在真源写 {LOCAL_INSTALLS.name}（见 installs.local.yaml.example）\n"
            "代码内不固化任何主机 skill/MCP 路径。",
            file=sys.stderr,
        )
        return 1

    if args.check:
        return check_targets(targets)

    if args.with_backends:
        print(f"== sync_backends @ {SOURCE} ==")
        rc = maybe_sync_backends()
        if rc != 0:
            return rc

    code = 0
    for t in targets:
        code = link_one(t, dry_run=args.dry_run, force=args.force) or code
    if args.dry_run:
        return code
    return check_targets(targets) if code == 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
