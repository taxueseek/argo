#!/usr/bin/env python3
"""子进程与文本 IO 编码门禁：拦住「locale 依赖的解码」这一类静默崩溃。

## 为什么需要这道检查

`subprocess.run(..., text=True)` 不写 `encoding=` 时，子进程输出按**进程 locale**
解码（Windows 上是 GBK/cp936，POSIX 上可能是 ASCII）。而本仓的子进程（xhs / tw /
rdt / ego-browser / node 脚本）输出的是 UTF-8 的中文内容——在 Windows 上要么
`UnicodeDecodeError` 当场崩，要么拿到乱码后解析成空结果，且**没有任何信号**：

- social_engines 三个引擎此前就是这个形态（查询词经 URL 编码后 CLI 回显中文标题，
  GBK 解码器读 UTF-8 字节流直接抛异常，被上层 `except (FileNotFoundError,
  TimeoutExpired)` 之外的路径吞掉后表现为「这个引擎永远没结果」）。
- 仓内其余位置早已逐个补过同样的修复（engines_base / mcp_handlers / http_client /
  job / engine_requires / link_source / local_image / local_seek，各有
  「Windows GBK 防线」注释），但没有门禁——每新增一个 `text=True` 就可能漏一个。

同理，内置 `open()` 不写 `encoding=` 按 locale 读写：`fetch_v3` 的 identity 文件
由 `atomic_write_text` 恒以 UTF-8 写入，读侧却走 locale——Windows 上非 ASCII 主机名
一次就炸；`mcp_diag` 的日志 argv 里含中文查询，写侧走 GBK、下次读成乱码。

## 判据

1. `subprocess.run/Popen/check_output/check_call/call` 带 `text=True` 或
   `universal_newlines=True` → 必须带 `encoding=`。
2. 内置 `open()` 以文本模式调用 → 必须带 `encoding=`。

范围：全仓 `.py`（排除 tests 目录——测试夹具里的裸 `text=True` 另行处理，
本门禁只兜**运行时代码**，测试进程跑在开发机 locale 下、不产生用户可见故障）。

判据本体用 ast 而非正则：`text=True` 可能出现在注释里（http_client.py 的 bytes
模式处就有一行注释提到 text=True），正则会误报。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SKIP_PARTS = {"tests", "__pycache__", "node_modules", ".git", ".venv", ".trash",
               "dist", "build"}
_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _runtime_py_files() -> list[Path]:
    files: list[Path] = []
    for p in sorted(ROOT.rglob("*.py")):
        if _SKIP_PARTS & set(p.parts):
            continue
        files.append(p)
    # 无 .py 后缀的 Python 脚本（bin/argo 等，靠 shebang 认）——只扫后缀会漏掉
    # 主入口本身，它是被用户直接敲的那条命令。
    bin_dir = ROOT / "bin"
    if bin_dir.is_dir():
        for p in sorted(bin_dir.iterdir()):
            if not p.is_file() or p.suffix or _SKIP_PARTS & set(p.parts):
                continue
            try:
                first = p.read_text(encoding="utf-8",
                                    errors="replace").splitlines()[0]
            except (OSError, IndexError):
                continue
            if first.startswith("#!") and "python" in first:
                files.append(p)
    return files


def _scan(paths: list[Path]) -> dict[str, list[int]]:
    """返回 {相对路径: [违规行号]}。只收「调了进程/文件却把编码交给 locale」。"""
    offenders: dict[str, list[int]] = {}
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue  # 语法/读取问题由 static_lint_gate 负责，这里不越界
        hits: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else "")
            kws = {kw.arg for kw in node.keywords}
            if (name in _SUBPROCESS_FUNCS
                    and ("text" in kws or "universal_newlines" in kws)
                    and "encoding" not in kws):
                hits.append(node.lineno)
            elif name == "open" and isinstance(f, ast.Name):
                mode = "r"
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) \
                        and isinstance(node.args[1].value, str):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if "b" not in mode and "encoding" not in kws:
                    hits.append(node.lineno)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    return offenders


class TestSubprocessEncodingGate(unittest.TestCase):
    def test_no_locale_dependent_decode(self):
        offenders = _scan(_runtime_py_files())
        self.assertEqual(
            offenders, {},
            "运行时代码把解码交给 locale——Windows GBK 下 UTF-8 输出直接崩或静默"
            "乱码。加 encoding=\"utf-8\", errors=\"replace\"（与仓内既有修复同形）："
            f"{offenders}")

    def test_gate_has_teeth_subprocess(self):
        bad = (
            "import subprocess\n"
            "def f():\n"
            "    return subprocess.run(['xhs'], capture_output=True, text=True)\n"
        )
        good = (
            "import subprocess\n"
            "def f():\n"
            "    return subprocess.run(['xhs'], capture_output=True, text=True,\n"
            "                          encoding='utf-8', errors='replace')\n"
        )
        self.assertEqual(
            _scan_source("_m.py", bad),
            {"_m.py": [3]}, "造错样本没被抓住，判据失效")
        self.assertEqual(_scan_source("_m.py", good), {}, "判据对正常源码误报")

    def test_gate_has_teeth_open(self):
        bad = "def f(p):\n    with open(p) as fh:\n        return fh.read()\n"
        good = ("def f(p):\n"
                "    with open(p, encoding='utf-8') as fh:\n"
                "        return fh.read()\n")
        self.assertEqual(_scan_source("_m2.py", bad), {"_m2.py": [2]},
                         "文本 open 无 encoding 没被抓住")
        self.assertEqual(_scan_source("_m2.py", good), {}, "判据误报正常 open")


def _scan_source(name: str, src: str) -> dict[str, list[int]]:
    """对内存里的源码跑同一套判据（teeth 用例不落盘）。"""
    tree = ast.parse(src, filename=name)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fname = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else "")
        kws = {kw.arg for kw in node.keywords}
        if (fname in _SUBPROCESS_FUNCS
                and ("text" in kws or "universal_newlines" in kws)
                and "encoding" not in kws):
            hits.append(node.lineno)
        elif fname == "open" and isinstance(f, ast.Name):
            mode = "r"
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if "b" not in mode and "encoding" not in kws:
                hits.append(node.lineno)
    return {name: hits} if hits else {}


if __name__ == "__main__":
    unittest.main()
