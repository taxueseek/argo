#!/usr/bin/env python3
"""release_check.py — 发布一致性预检（10 秒回答「这棵树能不能发」）。

## 为什么要这道预检

版本号散在 16 处声明（pyproject / package.json / dsh 插件包 / 插件市场清单 /
MCP Registry 清单 / ZCode 插件清单 / argo_search.__version__ / SKILL.md 前置
字段 / MCP 与面板源码常量 + 五语 README 的徽标），外加五语 README 的发布记录
行、docs/RELEASE_NOTES_v<ver>.md 与 package.json 的 files 白名单（带版本号的
文件名，漏改就是 npm 包里静默缺文件）。历史上 v2.9.8 漏过 README「最近更新」
条目、版本号清单靠人肉逐处核对——每次发版三十到四十分钟，且漏改不一定当场
发现（npm 包发出去了才看到 README 还是旧版）。

## 检什么（全部是结构化事实，不做散文匹配）

1. 十一处机器声明与 pyproject.toml 的 version 完全一致；
2. 五语 README 的徽标 `badge/version-<v>-` 与发布表行 `| **v<v>** |` 都在；
3. `docs/RELEASE_NOTES_v<v>.md` 存在；
4. package.json 的 files 白名单包含当前发布说明，且**盘上每份**发布说明都在
   白名单里（新增 RELEASE_NOTES 忘登记 = npm 包缺文件，与版本号无关）。

不检查散文里的版本提法（「当前 2.9.9」这类句子五语各有写法，正则匹配只会
制造假红；结构化位置全绿即可保证对外可见的版本事实一致）。

用法：
    python3 scripts/release_check.py          # 预检，非零退出码 = 不能发
    python3 scripts/release_check.py --root . # 指定检出根（测试用）

被 tests/test_release_consistency.py 锁进 pre-commit 快门禁：版本漂移会在
提交时就被拦下，而不是等发版当天。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 十一处机器声明的相对路径（顺序即报错输出顺序）
JSON_DECLS = (
    "package.json",
    "packages/dsh-plugin/package.json",
    "server.json",
    "packages/argo-zcode/.zcode-plugin/plugin.json",
)
README_LANGS = ("README.md", "README.en.md", "README.ja.md", "README.ko.md", "README.es.md")

# 源码里的版本常量（正则取，避免 import 副作用）
SRC_DECLS = (
    ("scripts/mcp_transport.py", r'ARGO_MCP_VERSION\s*=\s*"([^"]+)"'),
    ("packages/argo-zcode/server/panel_server.mjs",
     r'SERVER_INFO\s*=\s*\{[^}]*version:\s*"([^"]+)"'),
    ("packages/argo-zcode/ui/panel.html",
     r'new App\(\{\s*name:\s*"argo-panel",\s*version:\s*"([^"]+)"'),
)


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _safe_read(root: Path, rel: str, problems: list[str]) -> str | None:
    """读文件；缺失/不可读记一条问题而不是抛 traceback。

    --root 指错目录（或检出被裁剪）是常见的误用，预检工具必须给一句可读的
    话，而不是让 FileNotFoundError 穿出去——那会让「预检」本身看起来像坏了。
    """
    try:
        return _read(root, rel)
    except OSError as e:
        problems.append(f"{rel}: 读取失败（{e.strerror or e}）")
        return None


def _pyproject_version(root: Path) -> str:
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read(root, "pyproject.toml"))
    return m.group(1) if m else ""


def check(root: Path) -> list[str]:
    """返回问题清单；空列表 = 一致。"""
    problems: list[str] = []
    if not (root / "pyproject.toml").is_file():
        return [f"{root} 不像 argo 检出（缺 pyproject.toml）"]
    ver = _pyproject_version(root)
    if not ver:
        return ["pyproject.toml 读不出 version（真源缺失，预检无法进行）"]

    # 1) JSON 声明
    for rel in JSON_DECLS:
        text = _safe_read(root, rel, problems)
        if text is None:
            continue
        try:
            got = str(json.loads(text).get("version") or "")
        except ValueError as e:
            problems.append(f"{rel}: JSON 解析失败（{e}）")
            continue
        if got != ver:
            problems.append(f"{rel}: {got} ≠ pyproject {ver}")

    # 2) 插件市场清单（plugins[] 每个都要对上）
    text = _safe_read(root, "packages/marketplace.json", problems)
    if text is not None:
        try:
            market = json.loads(text)
            for plugin in market.get("plugins") or []:
                got = str(plugin.get("version") or "")
                if got != ver:
                    problems.append(
                        f"packages/marketplace.json[{plugin.get('name')}]: {got} ≠ {ver}")
        except (OSError, ValueError) as e:
            problems.append(f"packages/marketplace.json: 读取失败（{e}）")

    # 2b) MCP Registry 清单（packages[] 每条的版本也要对上）
    text = _safe_read(root, "server.json", problems)
    if text is not None:
        try:
            for pkg in json.loads(text).get("packages") or []:
                got = str(pkg.get("version") or "")
                if got != ver:
                    problems.append(
                        f"server.json[{pkg.get('identifier')}]: {got} ≠ {ver}")
        except ValueError as e:
            problems.append(f"server.json: JSON 解析失败（{e}）")

    # 2c) 源码常量（MCP 握手自报版本、ZCode 面板版本）
    for rel, pat in SRC_DECLS:
        if not (root / rel).is_file():
            continue
        m = re.search(pat, _read(root, rel))
        if not m:
            problems.append(f"{rel}: 找不到版本声明")
        elif m.group(1) != ver:
            problems.append(f"{rel}: {m.group(1)} ≠ {ver}")

    # 3) Python 包内声明（MCP/库调用方读它）
    text = _safe_read(root, "argo_search/__init__.py", problems)
    if text is not None:
        m = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', text)
        if not m:
            problems.append("argo_search/__init__.py: 找不到 __version__")
        elif m.group(1) != ver:
            problems.append(f"argo_search/__init__.py: {m.group(1)} ≠ {ver}")

    # 4) SKILL.md 前置字段
    text = _safe_read(root, "SKILL.md", problems)
    if text is not None:
        m = re.search(r"(?m)^version:\s*(\S+)\s*$", text[:2000])
        if not m:
            problems.append("SKILL.md: 前置字段缺 version")
        elif m.group(1) != ver:
            problems.append(f"SKILL.md: {m.group(1)} ≠ {ver}")

    # 5) 五语 README：徽标 + 发布表行
    for rel in README_LANGS:
        text = _safe_read(root, rel, problems)
        if text is None:
            continue
        if f"badge/version-{ver}-" not in text:
            problems.append(f"{rel}: 徽标不是 {ver}")
        if f"| **v{ver}** |" not in text:
            problems.append(f"{rel}: 发布记录表缺 | **v{ver}** | 行")

    # 6) 发布说明文件 + npm files 白名单
    notes = f"docs/RELEASE_NOTES_v{ver}.md"
    if not (root / notes).is_file():
        problems.append(f"{notes}: 发布说明不存在")
    text = _safe_read(root, "package.json", problems)
    files = []
    if text is not None:
        try:
            files = json.loads(text).get("files") or []
        except ValueError:
            files = []
    if notes not in files:
        problems.append(f"package.json files 白名单缺 {notes}（npm 包会缺发布说明）")
    for path in sorted((root / "docs").glob("RELEASE_NOTES_v*.md")):
        rel = f"docs/{path.name}"
        if rel not in files:
            problems.append(f"package.json files 白名单缺 {rel}（盘上有、包里没有）")

    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布一致性预检")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="检出根目录（默认本仓库）")
    args = ap.parse_args(argv)
    root = Path(args.root)
    problems = check(root)
    try:
        ver = _pyproject_version(root) or "?"
    except OSError:
        ver = "?"  # 非检出目录：check() 已记问题，这里只负责别在报错路径上再炸
    if problems:
        print(f"release_check: v{ver} 不一致，{len(problems)} 项待修：", file=sys.stderr)
        for p in problems:
            print(f"  !! {p}", file=sys.stderr)
        return 1
    print(f"release_check: v{ver} 一致（11 处机器声明 + 五语 README + 发布说明与白名单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
