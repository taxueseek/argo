#!/usr/bin/env python3
"""release.py — 一条链发版：改号 → 校验 → 提交打标签 → 发 npm → 轮询核验。

## 为什么要它

版本号散在 11 处机器声明、五语 README 的徽标 / 散文 / 发布表，外加发布说明
与 npm files 白名单。v2.10.0 发版时手改了 26 个文件，v2.9.8 更漏过 README
「最近更新」条目——漏改往往等 npm 包发出去才发现。release_check.py 只能事后
报错，本脚本把「改」和「发」也收成确定性步骤。

## 三步

    python3 scripts/release.py bump 2.10.1       # 改号（要求干净树 + main）
    # 填 docs/RELEASE_NOTES_v2.10.1.md（去掉 TODO 标记）与五语 README 三块
    python3 scripts/release.py check             # 结构 + 内容 + 目录计数全绿
    python3 scripts/release.py publish 2.10.1 --yes

内容段落（最近更新、能力段、发布表行）不自动生成：它们要判断和翻译。脚本
负责卡口——缺一段就不让发，而不是发出去再补。

常用开关：`--dry-run` 只打印；`publish --skip-tests/--skip-npm`；`--wait 600`
调大 registry 传播轮询秒数（npm CDN 3-4 分钟生效是正常现象）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release_check  # noqa: E402

README_LANGS = release_check.README_LANGS
NOTES_TODO = "<!-- release-todo -->"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(RuntimeError):
    """发版前置条件不满足；main() 打到 stderr 并以非零退出。"""


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8", newline="\n")


def current_version(root: Path = ROOT) -> str:
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read(root, "pyproject.toml"))
    if not m:
        raise ReleaseError("pyproject.toml 读不出 version")
    return m.group(1)


def parse_version(v: str) -> tuple[int, int, int]:
    if not VERSION_RE.match(v):
        raise ReleaseError(f"版本号必须是 X.Y.Z 三段数字，收到 {v!r}")
    return tuple(int(x) for x in v.split("."))  # type: ignore[return-value]


# ── 改号规则：唯一一份「哪些地方写着版本号」的清单 ────────────────────────────
# 每条 (相对路径, 旧串, 新串, 期望出现次数)；次数不对直接中止，避免误伤历史文案。
def rules(old: str, new: str) -> list[tuple[str, str, str, int]]:
    o, n = old, new
    out: list[tuple[str, str, str, int]] = [
        ("pyproject.toml", f'version = "{o}"', f'version = "{n}"', 1),
        ("package.json", f'"version": "{o}"', f'"version": "{n}"', 1),
        ("packages/dsh-plugin/package.json", f'"version": "{o}"', f'"version": "{n}"', 1),
        ("packages/marketplace.json", f'"version": "{o}"', f'"version": "{n}"', 1),
        ("packages/argo-zcode/.zcode-plugin/plugin.json",
         f'"version": "{o}"', f'"version": "{n}"', 1),
        ("server.json", f'"version": "{o}"', f'"version": "{n}"', 2),
        ("argo_search/__init__.py", f'__version__ = "{o}"', f'__version__ = "{n}"', 1),
        ("SKILL.md", f"version: {o}", f"version: {n}", 1),
        ("scripts/mcp_transport.py", f'ARGO_MCP_VERSION = "{o}"', f'ARGO_MCP_VERSION = "{n}"', 1),
        ("packages/argo-zcode/server/panel_server.mjs",
         f'version: "{o}"', f'version: "{n}"', 1),
        ("packages/argo-zcode/ui/panel.html", f'version: "{o}"', f'version: "{n}"', 1),
        # 五语徽标
        ("README.md", f"badge/version-{o}-", f"badge/version-{n}-", 1),
        ("README.en.md", f"badge/version-{o}-", f"badge/version-{n}-", 1),
        ("README.ja.md", f"badge/version-{o}-", f"badge/version-{n}-", 1),
        ("README.ko.md", f"badge/version-{o}-", f"badge/version-{n}-", 1),
        ("README.es.md", f"badge/version-{o}-", f"badge/version-{n}-", 1),
        # 五语「当前版本」散文（安装段两处）
        ("README.md", f"（已支持，当前 {o}；按 tag 发布，版本固定）",
         f"（已支持，当前 {n}；按 tag 发布，版本固定）", 1),
        ("README.md", f"**npm 安装已支持，当前版本 {o}**",
         f"**npm 安装已支持，当前版本 {n}**", 1),
        ("README.en.md", f"(supported, currently {o}; released per tag, version pinned)",
         f"(supported, currently {n}; released per tag, version pinned)", 1),
        ("README.en.md", f"**npm install is supported, current version {o}**",
         f"**npm install is supported, current version {n}**", 1),
        ("README.ja.md", f"現在 {o}。tag ごとに公開", f"現在 {n}。tag ごとに公開", 1),
        ("README.ja.md", f"**npm インストールに対応済みで、最新版は {o}**",
         f"**npm インストールに対応済みで、最新版は {n}**", 1),
        ("README.ko.md", f"현재 {o}, tag마다 배포", f"현재 {n}, tag마다 배포", 1),
        ("README.ko.md", f"**npm 설치를 지원하며 최신 버전은 {o}**",
         f"**npm 설치를 지원하며 최신 버전은 {n}**", 1),
        ("README.es.md", f"versión actual {o}; se publica por tag",
         f"versión actual {n}; se publica por tag", 1),
        ("README.es.md", f"**La instalación por npm está soportada y la versión actual es {o}**",
         f"**La instalación por npm está soportada y la versión actual es {n}**", 1),
        # 五语「当前大致能力」标题
        ("README.md", f"### 当前大致能力（v{o}）", f"### 当前大致能力（v{n}）", 1),
        ("README.en.md", f"### Rough capability set (v{o})", f"### Rough capability set (v{n})", 1),
        ("README.ja.md", f"### 現時点の能力概要（v{o}）", f"### 現時点の能力概要（v{n}）", 1),
        ("README.ko.md", f"### 대략적인 능력 세트 (v{o})", f"### 대략적인 능력 세트 (v{n})", 1),
        ("README.es.md", f"### Conjunto aproximado de capacidades (v{o})",
         f"### Conjunto aproximado de capacidades (v{n})", 1),
    ]
    return out


def apply_rules(root: Path, old: str, new: str, dry: bool) -> list[str]:
    """按 rules 改写；任何一处次数不符就整体中止（返回已计划清单）。"""
    planned: list[str] = []
    for rel, o, n, want in rules(old, new):
        path = root / rel
        if not path.is_file():
            raise ReleaseError(f"{rel}: 文件不存在，无法改号")
        text = path.read_text(encoding="utf-8")
        got = text.count(o)
        if got != want:
            raise ReleaseError(f"{rel}: {o!r} 出现 {got} 次（期望 {want}）——先确认模板没变")
        planned.append(f"{rel}: {o} -> {n}")
        if not dry:
            path.write_text(text.replace(o, n), encoding="utf-8", newline="\n")
    return planned


def ensure_whitelist(root: Path, old: str, new: str, dry: bool) -> str:
    """package.json files 白名单补新发布说明（锚在旧版条目上）。"""
    rel = "package.json"
    text = _read(root, rel)
    old_entry = f'    "docs/RELEASE_NOTES_v{old}.md",\n'
    new_entry = f'    "docs/RELEASE_NOTES_v{new}.md",\n'
    if new_entry in text:
        return f"{rel}: 白名单已含 v{new}，跳过"
    if old_entry not in text:
        raise ReleaseError(f"{rel}: 找不到 v{old} 的发布说明条目，无法定位插入点")
    if not dry:
        _write(root, rel, text.replace(old_entry, new_entry + old_entry, 1))
    return f"{rel}: 白名单插入 docs/RELEASE_NOTES_v{new}.md"


def ensure_notes(root: Path, new: str, dry: bool) -> str:
    """发布说明不存在时生成带 TODO 标记的骨架，避免结构性预检红。"""
    rel = f"docs/RELEASE_NOTES_v{new}.md"
    path = root / rel
    if path.is_file():
        return f"{rel}: 已存在"
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Argo v{new} 发布说明\n\n{NOTES_TODO}\n\n"
            "**版本**：%s\n**定位**：\n\n---\n\n"
            "## 这次更新有什么（通俗版）\n\n## 验证\n\n## 兼容性\n" % new,
            encoding="utf-8", newline="\n")
    return f"{rel}: 生成骨架（记得去掉 {NOTES_TODO}）"


# ── 内容清单：脚本不写文案，但缺一段就不让发 ────────────────────────────────
def content_problems(root: Path, ver: str) -> list[str]:
    problems: list[str] = []
    notes = root / f"docs/RELEASE_NOTES_v{ver}.md"
    if not notes.is_file():
        problems.append(f"docs/RELEASE_NOTES_v{ver}.md: 发布说明不存在")
    elif NOTES_TODO in notes.read_text(encoding="utf-8"):
        problems.append(f"docs/RELEASE_NOTES_v{ver}.md: 仍是骨架（{NOTES_TODO} 未删）")
    for rel in README_LANGS:
        text = _read(root, rel)
        if not re.search(rf"(?m)^### v{re.escape(ver)}", text):
            problems.append(f"{rel}: 缺「最近更新」段（### v{ver}）")
        if f"| **v{ver}** |" not in text:
            problems.append(f"{rel}: 发布记录表缺 | **v{ver}** | 行")
        if not re.search(rf"(?m)^### [^\n]*[{re.escape(ver)}）)]", text):
            problems.append(f"{rel}: 「当前大致能力」标题未跟进到 v{ver}")
        if not re.search(rf"(?m)^v{re.escape(ver)}", text):
            problems.append(f"{rel}: 能力段缺以 v{ver} 开头的新段落")
    return problems


def catalog_stale(root: Path) -> str:
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "gen_engine_catalog.py"), "--check"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        return ""
    out = (r.stdout + r.stderr).strip()
    if "No module named 'yaml'" in out or "No module named yaml" in out:
        out += "\n（当前解释器缺 PyYAML；用 python3.14 跑本脚本）"
    return out


def check(root: Path = ROOT) -> list[str]:
    """全量校验：结构（release_check）+ 内容清单 + 目录计数。"""
    ver = current_version(root)
    problems = [f"release_check: {p}" for p in release_check.check(root)]
    problems += content_problems(root, ver)
    stale = catalog_stale(root)
    if stale:
        problems.append(f"目录计数: {stale}")
    return problems


# ── git 前置 ────────────────────────────────────────────────────────────────
def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _is_git_repo(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def preflight(root: Path, new: str, force_branch: bool, git_checks: bool) -> None:
    old = current_version(root)
    if parse_version(new) <= parse_version(old):
        raise ReleaseError(f"新版本 {new} 必须大于当前 {old}")
    if not git_checks or not _is_git_repo(root):
        return
    dirty = _git(root, "status", "--porcelain").stdout.strip()
    if dirty:
        raise ReleaseError("工作区不干净——先提交或 stash，再改号（保证发版 diff 可审）")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main" and not force_branch:
        raise ReleaseError(f"当前分支 {branch}，发版请在 main（或用 --force-branch）")
    if _git(root, "rev-parse", "-q", "--verify", f"refs/tags/v{new}").returncode == 0:
        raise ReleaseError(f"标签 v{new} 已存在")


def bump(root: Path, new: str, dry: bool = False, force_branch: bool = False,
         git_checks: bool = True) -> list[str]:
    old = current_version(root)
    preflight(root, new, force_branch, git_checks)
    msgs = [f"版本 {old} -> {new}"]
    msgs += apply_rules(root, old, new, dry)
    msgs.append(ensure_whitelist(root, old, new, dry))
    msgs.append(ensure_notes(root, new, dry))
    return msgs


# ── 发布 ────────────────────────────────────────────────────────────────────
def _npm_version_exists(pkg: str, ver: str) -> bool:
    r = subprocess.run(["npm", "view", f"{pkg}@{ver}", "version"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def _npm_publish(root: Path, cwd_rel: str, pkg: str, ver: str, extra: list[str]) -> None:
    if _npm_version_exists(pkg, ver):
        print(f"  - {pkg}@{ver} 已在 registry，跳过")
        return
    r = subprocess.run(["npm", "publish", *extra], cwd=root / cwd_rel, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ReleaseError(f"npm publish {pkg} 失败（退出码 {r.returncode}）")


def _wait_npm(pkg: str, ver: str, timeout: int) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        r = subprocess.run(["npm", "view", pkg, "dist-tags.latest"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.stdout.strip() == ver:
            print(f"  - {pkg} latest = {ver}（{int(time.time() - start)}s）")
            return True
        time.sleep(10)
    print(f"  !! {pkg} 的 latest 在 {timeout}s 内未变成 {ver}（CDN 传播，勿重发）")
    return False


def _wait_release(ver: str, timeout: int) -> str:
    start = time.time()
    while time.time() - start < timeout:
        r = subprocess.run(
            ["gh", "release", "view", f"v{ver}", "--json", "url,assets"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            data = json.loads(r.stdout)
            assets = ", ".join(a["name"] for a in data.get("assets", []))
            return f"{data.get('url', '')}（资产：{assets}）"
        time.sleep(10)
    return ""


def publish(root: Path, new: str, yes: bool = False, skip_tests: bool = False,
            skip_npm: bool = False, wait: int = 300, dry: bool = False) -> None:
    ver = current_version(root)
    if ver != new:
        raise ReleaseError(f"仓库当前是 {ver}，与要发布的 {new} 不一致——先 bump")
    problems = check(root)
    if problems:
        raise ReleaseError("校验未过：\n  - " + "\n  - ".join(problems))
    plan = [
        "pytest 全量" if not skip_tests else "pytest 跳过",
        f"git commit + tag v{ver} + push main/tag",
        "npm publish argo-search + @taxueseek/argo-dsh" if not skip_npm else "npm 跳过",
        f"轮询 registry 与 GitHub Release（最多 {wait}s）",
    ]
    if dry:
        print("dry-run，将执行：\n  - " + "\n  - ".join(plan))
        return
    if not yes:
        raise ReleaseError("发布是对外动作，确认后加 --yes 重跑：\n  - " + "\n  - ".join(plan))

    if not skip_tests:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise ReleaseError("pytest 未过，已停止")

    if _is_git_repo(root):
        if _git(root, "status", "--porcelain").stdout.strip():
            _git(root, "add", "-A")
            msg = f"docs(release): v{ver} 发布说明 + 版本号与五语 README 统一 {ver}"
            r = _git(root, "commit", "-m", msg)
            if r.returncode != 0:
                raise ReleaseError(f"git commit 失败：{r.stderr.strip()}")
        if _git(root, "rev-parse", "-q", "--verify", f"refs/tags/v{ver}").returncode != 0:
            r = _git(root, "tag", f"v{ver}")
            if r.returncode != 0:
                raise ReleaseError(f"git tag 失败：{r.stderr.strip()}")
        for args in (("push", "origin", "main"), ("push", "origin", f"v{ver}")):
            r = _git(root, *args)
            if r.returncode != 0:
                raise ReleaseError(f"git {args[0]} {args[-1]} 失败：{r.stderr.strip()}")
        print(f"  - 已推送 main 与 tag v{ver}")

    if not skip_npm:
        _npm_publish(root, ".", "argo-search", ver, [])
        _npm_publish(root, "packages/dsh-plugin", "@taxueseek/argo-dsh",
                     ver, ["--access", "public"])
        _wait_npm("argo-search", ver, wait)
        _wait_npm("@taxueseek/argo-dsh", ver, wait)

    release = _wait_release(ver, wait)
    print(f"  - GitHub Release: {release or '未在等待时间内出现（看 Actions 的 release 工作流）'}")
    print(f"发布完成：v{ver}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Argo 发版一条链")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bump", help="改版本号（含发布说明骨架）")
    b.add_argument("version")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--force-branch", action="store_true")
    b.add_argument("--no-git-checks", action="store_true", help=argparse.SUPPRESS)

    c = sub.add_parser("check", help="结构 + 内容 + 目录计数校验")

    p = sub.add_parser("publish", help="测试 → 提交 → 打标签 → 发 npm → 轮询核验")
    p.add_argument("version")
    p.add_argument("--yes", action="store_true", help="确认对外发布")
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--skip-npm", action="store_true")
    p.add_argument("--wait", type=int, default=300)
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "bump":
            for line in bump(ROOT, args.version, args.dry_run,
                             args.force_branch, not args.no_git_checks):
                print(f"  - {line}")
            if args.dry_run:
                print("dry-run，未写入")
            else:
                print("\n下一步：填发布说明与五语 README 三块，然后 release.py check")
        elif args.cmd == "check":
            problems = check(ROOT)
            if problems:
                print(f"release.py check: {len(problems)} 项待修", file=sys.stderr)
                for x in problems:
                    print(f"  !! {x}", file=sys.stderr)
                return 1
            print(f"release.py check: v{current_version(ROOT)} 全绿")
        else:
            publish(ROOT, args.version, args.yes, args.skip_tests,
                    args.skip_npm, args.wait, args.dry_run)
    except ReleaseError as e:
        print(f"release.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
