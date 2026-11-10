#!/usr/bin/env python3
"""check_commit_report.py — 高风险提交的检查清单检查。

## 为什么需要

argo 的 pre-commit 注释自己记着两起真实事故：`f702ee2` 把 SKILL.md 压到
8049B、`4a71693` 让 deepseek_search 成死源，「两笔都过了 hook 却在全量回归里
红」。跑了的门禁全绿，**没跑的那条门没人知道它该跑**——因为提交信息里没有
「未跑什么」这个位置，省略不写是默认行为。

本脚本给高风险提交加一道**轻量提示**：检查清单缺段时打警告，不硬拦。
理由与 pre-commit 自己的边界一致：hook 慢于 20s 就会被 `--no-verify` 绕过，
而一道会被绕过的硬门比没有门更坏。它只做「让省略变得可见」。

## 判定口径

高风险 = 提交标题命中 `docs/COMMIT_STANDARD.md` 里列的模式。清单三段固定
用 `跑了` / `在 commit` / `未跑` 三个名字；`未跑: 无` 是合法写法（确实没漏），
但整段省略不合法。

用法：
  python3 scripts/check_commit_report.py --message-file .git/COMMIT_EDITMSG
  python3 scripts/check_commit_report.py --message "feat(engines): 接入 X 源"
  python3 scripts/check_commit_report.py --last        # 检查最近一次提交
  python3 scripts/check_commit_report.py --last --strict  # 缺段即非零退出

退出码：0 通过（或仅警告）；1 `--strict` 下缺段；2 读不到提交信息。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 高风险模式（与 docs/COMMIT_STANDARD.md 的「哪些提交算高风险」一一对应）。
# 顺序无关，命中任一即高风险。
#
# scope 用白名单而非通配：2026-11-10 首次落地时漏了 `agents`，于是本次这笔
# 改动自己的提交信息没被门禁检查——门禁管不到自己，正是它最该管的地方。但
# 改成 `[a-z_]+` 通配又太宽（`feat(local-seek)` 这类子技能改动被误判），
# 所以此处是**显式 scope 白名单**：新增一类受影响面时往这里加，并在同一次
# 改动里更新 docs/COMMIT_STANDARD.md。tests/test_check_commit_report.py
# 的 test_standard_doc_lists_the_same_high_risk_patterns 会机械对拍两者。
_HIGH_RISK_SCOPES = frozenset({
    "engines", "engine", "route", "search", "rank", "states", "state",
    "agents", "agent", "release", "deps",
})
_HIGH_RISK = (
    # 无 scope 的性能改动：无论改哪里都要给实测数字
    re.compile(r"^perf\("),
    # 发版与依赖链：动的是发布路径，错了影响所有用户
    re.compile(r"^(fix|chore)\(release\)"),
    re.compile(r"^build\(deps\)"),
)

# 检查清单三段。`在 commit` 允许写成 `在 commit:` 或 `在commit:`。
_SEGMENTS = {
    "跑了": re.compile(r"^\s*[-*]?\s*跑了\s*[:：]", re.M),
    "在 commit": re.compile(r"^\s*[-*]?\s*在\s*commit\s*[:：]", re.M),
    "未跑": re.compile(r"^\s*[-*]?\s*未跑\s*[:：]", re.M),
}

# 标题与正文的分界：第一个空行。标题不进正文检查。
_TITLE_RE = re.compile(r"^\s*([a-z]+)(\(([^)]*)\))?!?:\s*(.*)$")


_SCOPE_RE = re.compile(r"^([a-z]+)(?:\(([^)]*)\))?!?:\s*(.*)$")


def is_high_risk(title: str) -> bool:
    """标题是否命中高风险模式。

    判据有两条，命中任一即高风险：
      1. scope 在 `_HIGH_RISK_SCOPES` 白名单里（带 scope 的常规提交）
      2. 命中 `_HIGH_RISK` 里的特殊模式（无 scope 的 perf/build 等）
    """
    m = _SCOPE_RE.match(title.strip())
    if m and m.group(2) and m.group(2).strip() in _HIGH_RISK_SCOPES:
        return True
    return any(p.search(title.strip()) for p in _HIGH_RISK)


def missing_segments(body: str) -> list[str]:
    """检查清单里缺哪几段（跑了 / 在 commit / 未跑）。"""
    return [name for name, pat in _SEGMENTS.items() if not pat.search(body)]


def parse_message(text: str) -> tuple[str, str]:
    """拆成 (标题, 正文)。注释行（# 开头）剔除。"""
    lines = [ln for ln in text.splitlines()
             if not ln.lstrip().startswith("#")]
    if not lines:
        return "", ""
    title = lines[0].strip()
    body = "\n".join(lines[1:])
    return title, body


def check(text: str) -> dict:
    """检查一条提交信息，返回结构化结果。"""
    title, body = parse_message(text)
    high = is_high_risk(title)
    missing = missing_segments(body) if high else []
    return {
        "title": title,
        "high_risk": high,
        "missing": missing,
        "ok": not missing,
    }


def _read_git_message(ref: str) -> str | None:
    r = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%B", ref],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="高风险提交的检查清单检查")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--message-file", help="提交信息文件（如 .git/COMMIT_EDITMSG）")
    g.add_argument("--message", help="直接给提交信息文本")
    g.add_argument("--last", action="store_true", help="检查最近一次提交")
    ap.add_argument("--strict", action="store_true",
                    help="缺段即非零退出（默认只警告）")
    args = ap.parse_args(argv)

    if args.message_file:
        p = Path(args.message_file)
        if not p.exists():
            print(f"[commit-report] 读不到 {p}", file=sys.stderr)
            return 2
        text = p.read_text(encoding="utf-8")
    elif args.message is not None:
        text = args.message
    elif args.last:
        text = _read_git_message("HEAD") or ""
        if not text:
            print("[commit-report] 读不到 HEAD 的提交信息", file=sys.stderr)
            return 2
    else:  # pragma: no cover - 互斥组保证三者必居其一
        print("[commit-report] 未指定提交信息来源", file=sys.stderr)
        return 2

    report = check(text)
    if not report["high_risk"]:
        print(f"[commit-report] 非高风险提交，跳过：{report['title'][:60]}")
        return 0
    if report["ok"]:
        print(f"[commit-report] 检查清单完整：{report['title'][:60]}")
        return 0
    print(f"[commit-report] 高风险提交缺检查清单段：{report['missing']}",
          file=sys.stderr)
    print("  按 docs/COMMIT_STANDARD.md，三段固定为：跑了 / 在 commit / 未跑。",
          file=sys.stderr)
    print("  没漏就写「未跑: 无」——省略不写是默认行为，而默认行为正是两起",
          file=sys.stderr)
    print("  「过了 hook 却在全量回归里红」事故的成因。", file=sys.stderr)
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
