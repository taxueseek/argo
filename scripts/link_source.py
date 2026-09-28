#!/usr/bin/env python3
"""
link_source.py — 把「消费者入口」指回本仓库来源（符号链接，不复制）。

标准化原则：
  1. 磁盘上只应有一份 argo 代码：本仓库（scripts/ 的上一级）。
  2. Skill 目录 / 文档入口若需要出现在主机约定位置，用 **symlink** 指向来源，
     禁止 rsync/cp 出第二份业务树。
  3. **链接必须直连来源（1 跳）**。宿主入口挂到另一个入口上（如
     `~/.workbuddy/skills/argo -> ~/.claude/skills/argo -> 来源`）语义上能解析，
     但多出一环依赖：中间那环被删改或指偏，入口就成断链，而工具此前对此
     完全无感。间接链接会被本脚本**重建为直连**（只替换 symlink 本身）。
  4. **目标路径绝不写死在代码里**。来源优先级：
       CLI `--to PATH`（可重复）
       → 环境变量 `ARGO_LINK_TARGETS`（os.pathsep 分隔，如 `:` / `;`）
       → 来源根目录下的 `installs.local.yaml`（本机声明，应 gitignore）
  5. 注册表派生仍只走 `sync_backends.py`（config.yaml → backends/*），与链接无关。

用法：
  # 本机 installs.local.yaml 示例见 installs.local.yaml.example
  python3 scripts/link_source.py
  python3 scripts/link_source.py --to /any/consumer/path/argo
  ARGO_LINK_TARGETS="$HOME/.claude/skills/argo:$HOME/.agents/skills/argo" \\
    python3 scripts/link_source.py
  python3 scripts/link_source.py --check        # 只校验；间接链接记 warn 并非零退出
  python3 scripts/link_source.py --dry-run

退出码：0 成功；1 无目标 / 链接失败 / 校验不一致（含「只经多跳间接指向来源」）。
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
        from yaml_load import load as _yaml_load
    except ImportError:
        print("[warn] 需要 PyYAML 才能读 installs.local.yaml", file=sys.stderr)
        return []
    data = _yaml_load(LOCAL_INSTALLS) or {}
    raw = data.get("link_targets") or data.get("targets") or []
    if not isinstance(raw, list):
        return []
    out: list[Path] = []
    for item in raw:
        if not item:
            continue
        p = Path(str(item)).expanduser()
        # 相对路径相对来源根；规范化但不跟随符号链接（见 _normalize_target）
        out.append(_normalize_target(SOURCE / p if not p.is_absolute() else p))
    return out


def _normalize_target(p: Path) -> Path:
    """规范化目标路径：展开 ~、补成绝对路径、消掉 . 与 ..，**但不跟随符号链接**。

    为什么不用 resolve()：宿主入口本来就应该是一条 symlink。resolve() 会先把
    「已经指向来源的链接」折叠成来源路径，于是 link_one 看到的目标是「来源本体」
    而直接跳过重建，check_targets 也无法区分「直连」与「隔了一层的间接链接」——
    校验常年报 ok，实际入口却挂在中转链接上（2026-09-14 实测踩到）。
    """
    p = Path(p).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return Path(os.path.normpath(str(p)))


def _hops_to_source(target: Path) -> int:
    """目标到来源的符号链接跳数。

    0 = 目标就是来源本体；1 = 直连；>1 = 间接链接（可解析但多一跳依赖）；
    -1 = 未指向来源 / 断链 / 成环（超过 8 跳按未指向处理，避免死循环）。
    """
    src = SOURCE.resolve()
    cur = target
    for hops in range(8):
        try:
            resolved = cur.resolve()
        except (OSError, RuntimeError):
            # 断链 / 符号链接成环（py3.13 的 resolve() 对环抛 RuntimeError）
            return -1
        if resolved != src:
            return -1
        if not cur.is_symlink():
            return hops
        try:
            raw = os.readlink(cur)
        except OSError:
            return -1
        nxt = Path(raw)
        if not nxt.is_absolute():
            nxt = cur.parent / nxt
        cur = Path(os.path.normpath(str(nxt)))
    return -1


def _env_targets() -> list[Path]:
    raw = os.environ.get("ARGO_LINK_TARGETS", "").strip()
    if not raw:
        return []
    return [_normalize_target(p) for p in raw.split(os.pathsep) if p.strip()]


def resolve_targets(cli: list[Path] | None) -> list[Path]:
    """无默认路径：没有 CLI / env / local 文件则空列表。"""
    seen: set[str] = set()
    ordered: list[Path] = []
    for group in (cli or [], _env_targets(), _load_local_targets()):
        for p in group:
            key = _normalize_target(p)
            if str(key) in seen:
                continue
            seen.add(str(key))
            ordered.append(key)
    return ordered


# 判定「目录像 argo 来源/旧副本」的特征文件。用于 --force 迁走目录前的软校验，
# 防止用户把 --to 错指到任意数据目录，--force 下整目录被 rename 迁走的误伤。
_ARGO_MARKERS = ("scripts/search.py", "SKILL.md")


def _looks_like_argo_copy(path: Path) -> bool:
    """路径是否为 argo 来源/旧副本（含脚本与 SKILL 文档），而非任意数据目录。"""
    if not path.is_dir():
        return False
    return any((path / marker).exists() for marker in _ARGO_MARKERS)


def link_one(target: Path, *, dry_run: bool, force: bool) -> int:
    """将 target 设为指向 SOURCE 的 symlink（间接链接会被重建为直连）。"""
    source = SOURCE.resolve()
    depth = _hops_to_source(target)

    if depth == 0:
        print(f"[skip] 目标就是真源本体: {target}")
        return 0

    if depth == 1:
        print(f"[ok]   已指向真源: {target} -> {source}")
        return 0

    if depth > 1:
        # 间接链接：能解析到来源，但中间还挂了一环。重建为直连只替换符号链接
        # 本身（不触碰任何目录内容），因此无需 --force。
        if dry_run:
            print(f"[dry]  将把 {depth} 跳间接链接重建为直连: {target}")
            return 0
        # 先建同目录临时链接再原子替换：避免「已 unlink、symlink 又失败」的
        # 窗口里宿主入口凭空消失（入口消失会让 Skill/工具调用直接找不到 argo）。
        tmp = target.with_name(target.name + ".relink-tmp")
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(source, tmp, target_is_directory=True)
            os.replace(tmp, target)
        except OSError as e:
            try:
                if tmp.is_symlink() or tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            print(f"[fail] 重建直连失败: {target} ({e})", file=sys.stderr)
            return 1
        print(f"[relink] {target} -> {source}（原为 {depth} 跳间接链接，已重建为直连）")
        return 0

    if target.exists() or target.is_symlink():
        if not force:
            print(
                f"[fail] 目标已存在且不是指向真源的链接: {target}\n"
                f"       若确认可替换，加 --force（会先移走/删除该路径）",
                file=sys.stderr,
            )
            return 1
        # 软校验（dry-run 与实际执行都触发）：目录若不像 argo 来源/旧副本
        # （无 scripts/search.py 或 SKILL.md），拒绝 --force 迁走，防止 --to
        # 误指用户数据目录被整目录 rename 的误伤。
        if target.is_dir() and not _looks_like_argo_copy(target):
            print(
                f"[fail] 目录不像 argo 真源/旧副本，拒绝 --force 迁走: {target}\n"
                f"       若确要替换，先手动备份该目录，或删掉后再执行。",
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
    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError:
        # Windows 无开发者模式/管理员权限时 symlink 会失败：
        # 目录链接退化为 junction（mklink /J，不需要管理员权限），
        # 语义与 symlink 基本一致（resolve() 同样指向来源）。
        if os.name == "nt":
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            # junction 不是 symlink（is_symlink() 为 False，_hops_to_source 会
            # 把它当普通目录），此处按「解析后是否等于来源」独立判定。
            try:
                linked = target.resolve() == SOURCE.resolve()
            except OSError:
                linked = False
            if r.returncode == 0 and linked:
                print(f"[link] {target} -> {source} (junction)")
                return 0
        print(f"[fail] 创建链接失败: {target} -> {source}", file=sys.stderr)
        return 1
    print(f"[link] {target} -> {source}")
    return 0


def check_targets(targets: list[Path]) -> int:
    bad = 0
    print(f"真源: {SOURCE.resolve()}")
    if not targets:
        print("[fail] 无校验目标（请 --to / ARGO_LINK_TARGETS / installs.local.yaml）")
        return 1
    for t in targets:
        depth = _hops_to_source(t)
        if depth == 0:
            print(f"[ok]   {t} 即真源本体")
        elif depth == 1:
            print(f"[ok]   {t} -> {SOURCE.resolve()}")
        elif depth > 1:
            # 间接链接仍能解析到来源，但多了一环依赖——按「校验不一致」计失败，
            # 提示直接重跑（不带 --check）即可重建为直连。
            print(f"[warn] {t} 经 {depth} 跳间接指向真源（非直连）；"
                  f"去掉 --check 重跑即可重建为直连")
            bad += 1
        else:
            print(f"[miss] {t} 未指向真源（exists={t.exists()} symlink={t.is_symlink()}）")
            bad += 1
    return 1 if bad else 0


def maybe_sync_backends() -> int:
    script = SOURCE / "scripts" / "sync_backends.py"
    if not script.exists():
        return 0
    # timeout（2026-09-29 补）：sync_backends 是纯本地文件操作，正常秒回；
    # 但无 timeout 的 subprocess 一旦被挂住（NFS/锁/杀软扫描），本命令会
    # 无限期卡死。60s 对本地脚本是极宽裕的上界。
    r = subprocess.run([sys.executable, str(script)], cwd=str(SOURCE), timeout=60)
    if r.returncode != 0:
        return r.returncode
    return subprocess.run(
        [sys.executable, str(script), "--check"], cwd=str(SOURCE),
        timeout=60).returncode


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
