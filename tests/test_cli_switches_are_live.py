#!/usr/bin/env python3
"""test_cli_switches_are_live.py — 每个 `add_argument` 声明的开关都必须真被读取（2026-09-27）。

## 守的问题

「开关声明了、参数表里有、文档里还教着，代码里却没人读」是这一类缺陷：
用户照文档敲，命令 rc=0、输出正常，**只是那个开关什么都没做**。它比崩溃难查，
因为没有异常、没有日志、没有任何信号。

本仓已经踩过两次，两次都是靠人翻出来的：

- `--domain` / `--sub_domain`（2026-09-27，提交 a2e0666）：argparse 接下、SKILL.md
  参数表写着，没有任何一层读取——请求照发，`domain` 从未到达引擎，静默退化成
  通用搜索。修的时候顺手加了 cache_key_vdom 与契约测试。
- `--progress`（search_cli）与 `--explain`（clarify）：本次实测发现，同样是
  声明 + 文档双份承诺、零读取。前者可从 argparse 直接删掉；后者更阴——SKILL.md
  与 references/usage.md 都教着 `clarify.py "查询" --explain --json`，
  跟着敲的人以为自己拿到了「详细解释」。

两次都是「补一个开关的守卫」，于是第三次还会发生。真正的根治是**把判据变成
全量扫描**：谁声明谁就得读，不读的必须显式登记在下面的白名单里并写清理由。

## 判据

扫 `scripts/*.py` 与 `bin/argo` 里所有 `add_argument` 的 dest，再扫同一文件里
所有读法：`args.<dest>`、`getattr(args, "<dest>"`、`["<dest>"]`（`vars(args)`
下标）。**声明了但三种读法都不出现**即判红。

白名单只收「刻意不读」的兼容项，且每条都必须写出**为什么无害**。这是刻意的
人工闸：白名单是这份门禁唯一的松口，它必须显式、可读、可审计，而不是靠
「这个文件跳过」把问题整片藏起来。
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# 声明了但刻意不读的开关：dest → 无害理由。
# 判据是「读了会更有害」或「读了没有可读的东西」，不是「历史遗留、先放着」。
_ACKNOWLEDGED_NOOPS: dict[str, dict[str, str]] = {
    "search_cli.py": {
        "no_envelope": (
            "兼容保留的等价项：默认档已不再附加 envelope，此开关等同默认，"
            "读了也不会改变行为。tests/test_envelope_default.py 反向锁着"
            "「use_envelope 的计算里不得出现 no_envelope」，删掉会打断既有脚本。"),
    },
    "wide_research.py": {
        "json": (
            "兼容项：该命令输出本来就是 JSON，帮助文案里已写明这一点。"),
    },
    "plan.py": {
        "json_output": (
            "兼容项：default=True 且输出恒为 JSON（人类档位不存在）。"),
    },
}


def _parser_dests(src: str) -> dict[str, int]:
    """源码里 `add_argument` 声明的 dest → 行号。"""
    tree = ast.parse(src)
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not flags:
            continue
        kw = {k.arg: k.value for k in node.keywords}
        dest = None
        if isinstance(kw.get("dest"), ast.Constant):
            dest = kw["dest"].value
        if dest is None:
            longs = [f for f in flags if f.startswith("--")]
            dest = (longs[0][2:] if longs
                    else flags[0].lstrip("-")).replace("-", "_")
        out[str(dest)] = node.lineno
    return out


def _read_dests(src: str) -> set[str]:
    """源码里所有「读 args 上的 dest」的写法。"""
    read = set(re.findall(r"\bargs\.([A-Za-z_]\w*)", src))
    read |= set(re.findall(r"getattr\(\s*args\s*,\s*['\"](\w+)", src))
    # `vars(args)` / `args.__dict__` 之后按下标取
    if "vars(args)" in src or "args.__dict__" in src:
        read |= set(re.findall(r"\[\s*['\"](\w+)['\"]\s*\]", src))
    return read


def _dead_switches(filename: str, src: str) -> dict[str, int]:
    """该文件里「声明了但没人读」的开关（已扣掉白名单）。"""
    acked = _ACKNOWLEDGED_NOOPS.get(filename, {})
    read = _read_dests(src)
    return {dest: line for dest, line in _parser_dests(src).items()
            if dest not in read and dest not in acked}


def _cli_sources() -> list[tuple[str, str]]:
    out = []
    for path in sorted(SCRIPTS.glob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "add_argument" in src:
            out.append((path.name, src))
    dispatcher = ROOT / "bin" / "argo"
    if dispatcher.exists():
        out.append(("bin/argo", dispatcher.read_text(encoding="utf-8")))
    return out


class TestEveryDeclaredSwitchIsRead(unittest.TestCase):
    def test_no_dead_switches(self):
        offenders: dict[str, dict[str, int]] = {}
        for name, src in _cli_sources():
            dead = _dead_switches(name, src)
            if dead:
                offenders[name] = dead
        self.assertEqual(
            offenders, {},
            "有开关声明了却没有任何一层读取——用户照文档敲不生效且无任何信号。"
            "要么接上实现，要么从 argparse 删掉（并同步文档 / --help），"
            f"要么写进本文件的白名单并说明为什么无害：{offenders}")

    def test_acknowledged_noops_still_exist(self):
        """反向守卫：白名单不许腐烂——登记项如果已被删除，条目也必须删掉。"""
        stale: dict[str, list[str]] = {}
        for name, dests in _ACKNOWLEDGED_NOOPS.items():
            src = next((s for n, s in _cli_sources() if n == name), None)
            if src is None:
                stale[name] = ["文件不存在"]
                continue
            missing = [d for d in dests if d not in _parser_dests(src)]
            if missing:
                stale[name] = missing
        self.assertEqual(
            stale, {},
            "白名单里的开关已经不在声明里了——条目本身成了误导，必须一并删除"
            f"（否则下一个人会以为它还在）：{stale}")

    def test_gate_has_teeth(self):
        """变异：造一处「声明了不读」，判据必须报红；正常源码不得误报。"""
        mutated = (
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--ghost', action='store_true')\n"
            "    args = p.parse_args()\n"
            "    print(args.query)\n"
        )
        self.assertIn("ghost", _dead_switches("_mutant.py", mutated),
                      "造错样本没被抓住，判据失效")
        clean = (
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--real', action='store_true')\n"
            "    args = p.parse_args()\n"
            "    print(args.real)\n"
        )
        self.assertEqual(_dead_switches("_mutant.py", clean), {},
                         "判据对正常源码误报")

    def test_gate_would_have_caught_the_two_real_cases(self):
        """回归锚点：两次真实事故的形态必须在本判据下报红。"""
        domain_case = (
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--domain', default='')\n"
            "    p.add_argument('--sub_domain', default='')\n"
            "    args = p.parse_args()\n"
            "    print(args.query)\n"
        )
        dead = _dead_switches("_mutant.py", domain_case)
        self.assertIn("domain", dead)
        self.assertIn("sub_domain", dead)
        progress_case = (
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--progress', action='store_true')\n"
            "    args = p.parse_args()\n"
            "    print(args.query)\n"
        )
        self.assertIn("progress", _dead_switches("_mutant.py", progress_case))


if __name__ == "__main__":
    sys.path.insert(0, str(SCRIPTS))
    unittest.main()
