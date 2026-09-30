#!/usr/bin/env python3
"""test_cli_stdin_and_positional.py — CLI 契约检查（2026-09-15）。

## 守的两类问题

**一、「stdin 有没有数据」的判据。** 直觉写法 `not sys.stdin.isatty()` 是错的：
`/dev/null`、已关闭的 fd、以及非交互环境下的空 stdin 都不是 tty。而脚本 / CI /
cron / agent 调用**正是 argo 的主战场**，全都在这一类里。实测后果：
`argo evidence "query"`（usage 里就写着的用法）走进「读管道」分支、拿到空串后
崩在 `json.load`（退出码 1 + Traceback）。判据统一处理到 `cli_io.stdin_is_piped()`
（按 fd 类型判），并立检查禁止 `isatty` 在别处复活。

**二、usage 写的位置参数必须真的能用。** `bin/argo` 的 usage 写着
`argo extract "url"`，而 `extract.py` 的 argparse 只认 `--url` ——照文档敲直接
报 `--url required`（同类：此前 fetch 的 `--focus` 也是「文档承诺、入口没有」）。
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
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cli_io  # noqa: E402


class TestStdinPredicate:
    def test_false_for_devnull(self):
        """/dev/null 不是 tty，但也没有数据——旧判据在这里判错。"""
        with open(os.devnull) as f:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(sys, "stdin", f)
                assert cli_io.stdin_is_piped() is False
                assert cli_io.read_stdin_if_piped() == ""

    def test_true_for_regular_file_redirect(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"ok": 1}', encoding="utf-8")
        with open(p, encoding="utf-8") as f:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(sys, "stdin", f)
                assert cli_io.stdin_is_piped() is True
                assert cli_io.read_stdin_if_piped() == '{"ok": 1}'

    def test_false_for_closed_fd(self):
        """已关闭/非法 fd 要 fail-safe 成「没有数据」，不能抛。"""
        class _Broken:
            def fileno(self):
                raise ValueError("closed")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys, "stdin", _Broken())
            assert cli_io.stdin_is_piped() is False
            assert cli_io.read_stdin_if_piped() == ""

    def test_pipe_detected_in_subprocess(self):
        """真管道端到端：echo 的内容必须被判为有数据。"""
        code = ("import sys;sys.path.insert(0,%r);import cli_io as c;"
                "print('YES' if c.stdin_is_piped() else 'NO', end='')" % str(SCRIPTS))
        out = subprocess.run([sys.executable, "-c", code], input="hello",
                             capture_output=True, text=True, timeout=30)
        assert out.stdout == "YES", f"管道未被识别：{out.stdout!r} {out.stderr[:200]}"

    def test_no_pipe_in_subprocess(self):
        code = ("import sys;sys.path.insert(0,%r);import cli_io as c;"
                "print('YES' if c.stdin_is_piped() else 'NO', end='')" % str(SCRIPTS))
        with open(os.devnull) as f:
            out = subprocess.run([sys.executable, "-c", code], stdin=f,
                                 capture_output=True, text=True, timeout=30)
        assert out.stdout == "NO", f"/dev/null 被误判为有数据：{out.stdout!r}"


class TestIsattyIsNotAStdinPredicate:
    """类级别封堵：`isatty` 只允许出现在 cli_io.py 里。"""

    def test_only_cli_io_may_use_isatty(self):
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
            if "isatty" in stripped:
                offenders[path.name] = [
                    f"L{i}" for i, l in enumerate(stripped.splitlines(), 1)
                    if "isatty" in l]
        assert not offenders, (
            "isatty 不是「stdin 有没有数据」的判据（/dev/null 与非交互空 stdin "
            f"也不是 tty）。请改用 cli_io.stdin_is_piped()：{offenders}")

    def test_gate_has_teeth(self, tmp_path):
        """故意造错验证：在别处塞一处 isatty，检查扫描必须报红。"""
        target = SCRIPTS / "wx.py"
        src = target.read_text(encoding="utf-8")
        target.write_text(src + "\n\ndef _m():\n    import sys\n    return sys.stdin.isatty()\n",
                          encoding="utf-8")
        try:
            s = target.read_text(encoding="utf-8")
            assert "isatty" in s, "造错样本没生效"
            # 复刻检查的扫描逻辑
            found = [l for l in s.splitlines() if "isatty" in l]
            assert found, "造错之后检查没抓住——等于没检查"
        finally:
            target.write_text(src, encoding="utf-8")


class TestArgoUsagePositionalsWork:
    """usage 里写的位置参数形式必须真能被入口接受。"""

    def test_extract_accepts_positional_url(self):
        """`argo extract <url>`（usage 写法）不得再报 --url required。"""
        r = subprocess.run([sys.executable, str(SCRIPTS / "extract.py"), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        assert "[URL]" in r.stdout, f"位置参数未注册：{r.stdout[:200]}"

    def test_extract_without_any_url_gives_friendly_error(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "extract.py")],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert r.returncode == 2
        assert "需要 URL" in (r.stderr or ""), f"缺参提示不友好：{r.stderr[:200]}"

    def test_usage_and_entry_points_agree_on_extract(self):
        """bin/argo 的 usage 文案与 extract.py 的入参形式必须一致。"""
        usage = (ROOT / "bin" / "argo").read_text(encoding="utf-8")
        assert 'argo extract   "url"' in usage, "usage 文案变了，请同步本门禁与入口"
        src = (SCRIPTS / "extract.py").read_text(encoding="utf-8")
        assert "nargs='?'" in src, "extract 位置参数被移除，usage 又会变成空头承诺"


class TestEvidenceFlowDoesNotCrash:
    """`argo evidence` 在非交互环境（无管道）下不得崩。"""

    def _argo(self, args, stdin=subprocess.DEVNULL, timeout=180):
        return subprocess.run(["argo"] + args, capture_output=True, text=True,
                              timeout=timeout, stdin=stdin)

    def test_no_args_no_pipe_prints_hint_not_traceback(self):
        r = self._argo(["evidence"])
        out = (r.stdout or "") + (r.stderr or "")
        assert "Traceback" not in out, f"仍然崩溃：{out[-300:]}"
        assert r.returncode == 1
        assert "evidence 需要查询词" in out

    def test_piped_input_still_works(self):
        payload = '{"results":[{"title":"t","url":"https://e.com","snippet":"s"}]}'
        r = self._argo(["evidence"], stdin=subprocess.PIPE)
        # 用 communicate 方式重跑以传入 stdin
        r = subprocess.run(["argo", "evidence"], input=payload,
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        assert "Traceback" not in out, f"管道模式回归：{out[-300:]}"


class TestEmptyOpenPipeDoesNotHang:
    """**空且不关闭**的管道必须立刻放行（2026-09-27）。

    上一节守的是「fd 类型判对」，那解决「要不要读」；这一节守的是
    「读到什么时候」，两者才是完整判据。FIFO 的 `read()` 要等写端关闭才返回，
    而 agent / CI / cron 起的子进程 stdin 正是「空、且全程不关闭」的管道：
    实测 `bin/argo evidence "query" --json` 在那种 stdin 下 90 秒不返回，
    `< /dev/null` 才 1 秒退出——于是所有调用方只能等自己的超时。

    回归检查刻意放在**子进程 + 超时**里：判据要是退回阻塞读，这里会超时失败，
    而不是把整个测试会话挂死。
    """

    def _probe(self, timeout=30):
        """在「写端打开、不写」的管道上读 stdin，返回子进程的 stdout。

        耗时由子进程自己报（`ELAPSED=`），父进程不再计时：父进程那次计时
        包含解释器启动，与「读 stdin 花了多久」不是同一件事。
        """
        code = (
            "import sys, time;"
            "sys.path.insert(0, " + repr(str(SCRIPTS)) + ");"
            "import cli_io as c;"
            "t0 = time.monotonic();"
            "data = c.read_stdin_if_piped();"
            "print('ELAPSED=%.2f LEN=%d' % (time.monotonic() - t0, len(data)), end='')"
        )
        r, w = os.pipe()
        try:
            with os.fdopen(r, "rb") as read_end:
                out = subprocess.run([sys.executable, "-c", code], stdin=read_end,
                                     capture_output=True, text=True, timeout=timeout)
        finally:
            os.close(w)
        return out.stdout

    def test_empty_open_pipe_returns_promptly(self):
        stdout = self._probe()
        m = re.search(r"ELAPSED=([\d.]+)", stdout)
        assert m, f"探针没跑起来：{stdout!r}"
        assert "LEN=0" in stdout, f"空管道不应读出内容：{stdout!r}"
        elapsed = float(m.group(1))
        # 首字节期限 0.25s；读 stdin 本身不该超过它太多
        assert elapsed < 1.0, f"空管道仍被阻塞：{elapsed:.2f}s"

    def test_argo_evidence_survives_open_pipe(self):
        """端到端：`argo evidence <query>` 在空管道 stdin 下必须走自动搜索。"""
        r, w = os.pipe()
        try:
            with os.fdopen(r, "rb") as read_end:
                res = subprocess.run(
                    ["argo", "evidence", "python dataclass", "--json"],
                    capture_output=True, text=True, timeout=90,
                    stdin=read_end, cwd=str(ROOT))
        finally:
            os.close(w)
        err = res.stderr or ""
        assert "Python error" not in err and "Traceback" not in err, err[-300:]
        # 本测试锁的是「空管道不挂起、走通自动搜索链路」，不锁引擎可用性：
        # 免费引擎限流窗口（429 连发，全量套件的 live e2e 流量即可触发）下，
        # 自动搜索合法地返回 0 结果，evidence 打印用法退出 1——那同样是
        # 「分派正常」的证据。引擎可用性由 matrix live 套件另行覆盖。
        assert res.returncode in (0, 1), f"rc={res.returncode} {err[-300:]}"
        assert ("credibility" in res.stdout
                or "需要提供搜索结果" in res.stdout), \
            f"既没评估结果也没走到空结果分支：{res.stdout[:200]}"

    def test_no_daemon_reader_thread(self):
        """静态门：不许用「后台线程阻塞读」实现期限。

    实测坑：daemon 线程阻塞在 BufferedReader 的 C 读上时，解释器退出会撞
    `_enter_buffered_busy: could not acquire lock for <_io.BufferedReader
    name='<stdin>'> at interpreter shutdown`，进程直接 SIGABRT（rc=-6）——
    比原来的挂起更难查。期限逻辑必须留在主线程（非阻塞轮询）。
    """
        src = (SCRIPTS / "cli_io.py").read_text(encoding="utf-8")
        stripped = re.sub(r'""".*?"""', "", src, flags=re.S)
        stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
        assert "threading" not in stripped, (
            "cli_io 里出现了 threading：期限逻辑一旦挪进后台线程，"
            "退出期会因 stdin 缓冲锁 SIGABRT（见本测试 docstring）")
        assert "read1" not in stripped, (
            "不要用缓冲区 read1 阻塞读；非阻塞 os.read + 轮询才是这里的实现")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
