#!/usr/bin/env python3
"""test_stdio_encoding.py — 本进程 stdout/stderr 的编码契约（2026-09-27）。

## 守的问题

本仓给**子进程**注入 `PYTHONUTF8=1` 是既有做法（mcp_server / search.py 的
local-seek / recompute），但**自家进程**从没做过同一件事。实测：

    PYTHONIOENCODING=ascii python3 scripts/search.py "贵州茅台" --json
    → UnicodeEncodeError + Traceback，退出码 1

同一个故障形态出现在 Windows 老控制台（cp1252/cp936 之外无法编码中文的代码页）。
而 stdout 的契约是 UTF-8 JSON（`cli_io.dumps`），进程自己的流必须跟上，否则
「能力都在、结果出不来」，而且崩在输出阶段、与查询本身无关，最难归因。

修法只有一处实现（`cli_io.ensure_utf8_stdio`），四个 CLI 入口的 `main()` 各调一次。
本文件锁两件事：

1. **锚点**：文档里写着的直接调用形式（`python3 scripts/search.py`）与
   `bin/argo` 两条路，在 ASCII 输出编码下都必须 rc=0 且中文可解码；
2. **单一来源**：不许在别处再写一套编码处理（`reconfigure` 只允许出现在 cli_io.py）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

_CHINESE_QUERY = "贵州茅台"


def _run(args, cwd=ROOT, timeout=180):
    """在「输出编码不是 UTF-8」的环境里跑一次，返回 CompletedProcess。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii"
    return subprocess.run(args, cwd=str(cwd), env=env, capture_output=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


class TestAsciiOutputEncodingDoesNotCrash:
    def test_direct_script_entry(self):
        """文档推荐形式：`python3 scripts/search.py "查询" --json`。"""
        r = _run([sys.executable, str(SCRIPTS / "search.py"), _CHINESE_QUERY,
                  "--json", "--fields", "agent"])
        err = (r.stderr or b"").decode("utf-8", "replace")
        assert "UnicodeEncodeError" not in err, err[-400:]
        assert "Traceback" not in err, err[-400:]
        assert r.returncode == 0, f"rc={r.returncode} {err[-400:]}"
        out = r.stdout.decode("utf-8", "replace")
        assert _CHINESE_QUERY in out, f"输出里的中文没活下来：{out[:200]}"

    def test_argo_dispatcher_entry(self):
        """`bin/argo` 入口（子命令 + 中文 usage）同一条路。"""
        r = _run([str(ROOT / "bin" / "argo"), "search", _CHINESE_QUERY,
                  "--json", "--fields", "agent"])
        err = (r.stderr or b"").decode("utf-8", "replace")
        assert "UnicodeEncodeError" not in err and "Traceback" not in err, err[-400:]
        assert r.returncode == 0, f"rc={r.returncode} {err[-400:]}"
        assert _CHINESE_QUERY in r.stdout.decode("utf-8", "replace")

    def test_usage_text_survives(self):
        """`argo --help` 的中文 usage 在选解释器之前就打印，必须同样安全。"""
        r = _run([str(ROOT / "bin" / "argo"), "--help"], timeout=90)
        assert r.returncode == 0
        out = r.stdout.decode("utf-8", "replace")
        assert "统一搜索" in out, f"usage 中文没活下来：{out[:120]}"


class TestStdioPinHasSingleSource:
    """`reconfigure` 只允许出现在 cli_io.py：判据与实现都不许长出第二套。"""

    def test_only_cli_io_reconfigures_stdio(self):
        offenders = {}
        targets = list(SCRIPTS.glob("*.py")) + [ROOT / "bin" / "argo"]
        for path in targets:
            if path.name == "cli_io.py":
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except OSError:
                continue
            stripped = re.sub(r'""".*?"""', "", src, flags=re.S)
            stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
            if "reconfigure" in stripped:
                offenders[path.name] = [
                    f"L{i}" for i, line in enumerate(stripped.splitlines(), 1)
                    if "reconfigure" in line]
        assert not offenders, (
            "stdio 编码的判据只有一处（cli_io.ensure_utf8_stdio）；"
            f"别处再写一套必然漂移：{offenders}")

    def test_entry_points_call_the_pin(self):
        """四个 CLI 入口 + bin/argo 必须真的调用它，否则上面那条锚点测试会漏。"""
        wanted = {
            "search_cli.py": "ensure_utf8_stdio()",
            "research_cli.py": "ensure_utf8_stdio()",
            "evidence.py": "ensure_utf8_stdio()",
            "clarify.py": "ensure_utf8_stdio()",
        }
        for name, needle in wanted.items():
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            assert needle in src, f"{name} 没有调用 {needle}"
        dispatcher = (ROOT / "bin" / "argo").read_text(encoding="utf-8")
        assert "_pin_stdio()" in dispatcher, "bin/argo 入口没有钉住 stdio 编码"

    def test_gate_has_teeth(self):
        """变异：在别处塞一处 reconfigure，扫描必须报红。"""
        target = SCRIPTS / "wx.py"
        src = target.read_text(encoding="utf-8")
        target.write_text(
            src + "\n\ndef _mut():\n    import sys\n"
                  "    sys.stdout.reconfigure(encoding='utf-8')\n",
            encoding="utf-8")
        try:
            text = target.read_text(encoding="utf-8")
            stripped = re.sub(r'""".*?"""', "", text, flags=re.S)
            stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
            assert "reconfigure" in stripped, "造错样本没生效——等于没检查"
        finally:
            target.write_text(src, encoding="utf-8")


class TestUtf8PinKeepsNativeTerminalEncoding:
    """终端档只放宽 errors，不改 encoding：中文 Windows 的 cp936 终端本来就能
    正确显示中文，硬换 UTF-8 反而乱码。这里用假流锁住这条分档。"""

    def _fake(self, is_tty):
        class _Stream:
            def __init__(self):
                self.calls = []
                self._tty = is_tty

            def isatty(self):
                return self._tty

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        return _Stream()

    def test_pipe_forces_utf8(self, monkeypatch):
        sys.path.insert(0, str(SCRIPTS))
        import cli_io
        out, err = self._fake(False), self._fake(False)
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        cli_io.ensure_utf8_stdio()
        assert out.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]
        assert err.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]

    def test_terminal_keeps_encoding(self, monkeypatch):
        sys.path.insert(0, str(SCRIPTS))
        import cli_io
        out = self._fake(True)
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", self._fake(True))
        cli_io.ensure_utf8_stdio()
        assert out.calls == [{"errors": "backslashreplace"}], (
            "终端档不许改 encoding：那会把本来正常的中文终端变成乱码")

    def test_unsupported_stream_is_skipped(self, monkeypatch):
        """不支持 reconfigure 的流（测试替身/已被包过的流）静默跳过，不抛。"""
        sys.path.insert(0, str(SCRIPTS))
        import cli_io

        class _Bare:
            pass

        monkeypatch.setattr(sys, "stdout", _Bare())
        monkeypatch.setattr(sys, "stderr", _Bare())
        cli_io.ensure_utf8_stdio()  # 不抛即通过


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
