#!/usr/bin/env python3
"""recompute.py — 可复算执行器：本地数据 + 计算脚本 → 可核查数值。

设计（P0-2，保持一致「结论可重算」）：

默认拒绝：
  - 默认拒绝运行，需显式 `--allow-exec` 或环境 ARGO_ALLOW_RECOMPUTE=1
  - 输入文件白名单（仅工作包 file_inputs 声明的路径可读，通过
    --inputs JSON 传入）
  - 断网：执行前置注入 Python 层网络禁用（socket/urllib/http.client/
    requests 抛 NetworkDisabledError），C 扩展/外部进程不覆盖但计算场景
    以 Python 数据栈为主
  - 超时硬杀（进程组 killpg）、内存软限（RLIMIT_AS 尽力而为）
  - 工作目录为全新临时目录（无预置物、无写权限到用户目录的语义，
    仅显式白名单输入可读）

输出：{ok, exit_code, stdout(尾部 ≤3000), stderr(尾部 ≤2000),
      elapsed_ms, skipped_reason?}
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from cli_io import dumps
from engine_env import build_child_env

_TAIL_STDOUT = 3000  # 超时/被杀时保留尾部输出（大任务不至于白跑全丢）
_TAIL_STDERR = 2000


def _kill_process_group(proc: subprocess.Popen) -> None:
    """跨平台击杀进程组。

    POSIX：start_new_session=True 时 proc 为进程组组长，用 killpg 杀掉整组
    （含子进程），避免仅杀父进程留下孤儿子任务。
    Windows：无 killpg/getpgid（POSIX-only），os.kill 也只能杀单进程，
    taskkill /T 才可递归，但依赖外部命令；此处退化为 proc.kill()，仍保证
    「父进程被杀、communicate 退出」的默认拒绝语义（子进程虽可能残留，
    但由独立 temp 工作目录 + 断网防护保底，不扩散）。
    """
    try:
        # POSIX-only：Windows 抛 AttributeError
        if hasattr(os, "killpg") and hasattr(os, "getpgid") and os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
    except (ProcessLookupError, OSError, ValueError, AttributeError):
        pass
    proc.kill()

# 注入到用户代码执行前的防护段：Python 层断网 + 强制文件白名单 + 禁外部进程
_NET_DISABLE_PRELUDE = """
import socket as _socket
import sys as _s2
class _NetworkDisabled(Exception):
    pass
class _BlockedSocket(_socket.socket):
    def __init__(self, *a, **k):
        raise _NetworkDisabled("recompute 禁止网络访问")
def _no_dns(*a, **k):
    raise _NetworkDisabled("recompute 禁止网络访问")
_socket.socket = _BlockedSocket
_socket.getaddrinfo = _no_dns
_socket.create_connection = _no_dns
for _m in ("requests", "urllib3", "httpx"):
    _s2.modules.pop(_m, None)

# ── 断网加固（2026-08）：纯 Python socket 层断网挡不住
#    `subprocess.run(['curl',…])` / `os.system(...)` 等「出网通道」。
#    这里 1) meta_path 拦截危险模块导入 2) 覆盖 os 的进程/执行入口，
#    封死外部进程投退，让「recompute 断网」承诺真正成立。 ──
import importlib.abc as _abc
import importlib.machinery as _mach
import os as _os_mod
_BLOCKED_MODS = {"subprocess", "multiprocessing", "ctypes", "pty", "pexpect"}
class _BlockImporter(_abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _BLOCKED_MODS or fullname.split(".")[0] in _BLOCKED_MODS:
            raise _NetworkDisabled("recompute 禁止导入 %s" % fullname)
        return None
_s2.meta_path.insert(0, _BlockImporter())
def _no_exec(*a, **k):
    raise _NetworkDisabled("recompute 禁止外部进程/系统调用")
for _f in ("system", "popen", "popen2", "popen3", "popen4", "spawnl", "spawnle",
           "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
           "fork", "forkpty", "posix_spawn", "posix_spawnp", "execv", "execve",
           "execl", "execle", "execlp", "execlpe", "execvp", "execvpe", "startfile"):
    if hasattr(_os_mod, _f):
        setattr(_os_mod, _f, _no_exec)
del _abc, _mach, _os_mod

# 保留 _NetworkDisabled：_no_dns / _BlockedSocket.__init__ / _no_exec /
# _BlockImporter.find_spec 的函数体还要 raise 它，若一并 del 则被触发时
# 全局名已被删，抛 NameError 而非预期错误。
del _socket, _s2, _BlockedSocket, _no_dns
"""


# 注入到用户代码执行前的**文件白名单**防护段。
#
# 两条入口都要闸住，且共用同一把尺：
#   * builtins.open / io.open —— 常规读取入口
#   * os.open / os.fdopen    —— 拿原始 fd 的底层入口，不闸就能绕过上面那条
#
# 为什么 os.open 是「白名单门」而不是「一律禁」（2026-09-16 修）：
# 原实现在这里把 os.open/os.fdopen 直接替换成抛异常。安全性没问题（实测
# `os.open`+`os.read`、`os.open`+`os.fdopen`、`open(opener=os.open)` 三条绕过
# 都被堵死），但它超出了必要的封堵面：**Python 3.11 之前，pathlib 的
# `Path.read_text()/read_bytes()` 恰好经由 os.open**——
#
#     class _NormalAccessor(_Accessor):
#         open = os.open                      # 直接别名
#     Path._opener = lambda self, n, f, m=0o666: self._accessor.open(self, f, m)
#
# 于是 3.9/3.10 上「用 pathlib 读一个白名单内的文件」会被当成拿原始 fd 攻击
# 而被拒绝（报 "recompute 禁止外部进程/系统调用"，与真实原因毫无关系）。
# 3.11+ 改用 io.open 才不再经过 os.open，所以这个缺陷只在老解释器上出现。
#
# 改成白名单门后，安全性不降反升：原来只保证「读不到非白名单」，现在还额外
# 禁止以写模式打开（白名单语义本就是只读），且非路径入参一律拒绝。
_FS_GUARD_PRELUDE = """
def _is_fs_path(p):
    \"\"\"入参像不像文件系统路径（供 3.9 pathlib 的错位实参识别用）。\"\"\"
    if isinstance(p, bool) or isinstance(p, int):
        return False
    return isinstance(p, (str, bytes)) or hasattr(p, "__fspath__")


def _fs_path(p):
    \"\"\"把入参归一成文件系统路径字符串；不像路径的直接拒绝。

    拒绝而非放过是关键：3.9 的 pathlib 会把 accessor 实例塞进第一个实参，
    若不校验类型，白名单检查会拿到一个非路径对象、realpath 抛 TypeError，
    表现为「白名单内文件也读不了」。
    \"\"\"
    import os
    if isinstance(p, bool) or isinstance(p, int):
        # 整数是裸 fd：拿它去绕白名单正是要防的事
        raise PermissionError("recompute 拒绝裸文件描述符")
    if isinstance(p, str):
        return p
    if isinstance(p, bytes):
        return os.fsdecode(p)
    if hasattr(p, "__fspath__"):
        return os.fsdecode(p.__fspath__())
    raise PermissionError(
        "recompute 拒绝非常规路径入参: %s" % type(p).__name__)


def _assert_allowed(p):
    \"\"\"路径必须在白名单内（realpath 后精确匹配）。

    用 realpath 而非 abspath：`link -> /etc/passwd` 这类软链必须按真实目标判定，
    否则白名单里放一个软链就等于放行它指向的任意文件。
    \"\"\"
    import os
    rp = os.path.realpath(_fs_path(p))
    if rp not in _ALLOWED:
        raise PermissionError("recompute 只读白名单输入: %s" % (p,))


_WRITE_MODES = set("wxa+")
_WRITE_FLAGS = 0
import os as _os_g
for _fl in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
    if hasattr(_os_g, _fl):
        _WRITE_FLAGS |= getattr(_os_g, _fl)
del _fl

# 先抓住原始实现，再安装包装（顺序不能颠倒：包装体引用这两个名字）
_guard_open = open
_guard_os_open = _os_g.open


def _assert_read_only(mode):
    # 白名单是「只读输入」：写模式一律拒绝。
    # 这里挡的是把人给的原始数据改掉——重算的语义是「读输入、算结果」，
    # 允许写会污染调用方的一手数据，且让「白名单=只读」的承诺不成立。
    if isinstance(mode, str) and (_WRITE_MODES & set(mode)):
        raise PermissionError("recompute 白名单为只读，拒绝写模式打开: %r" % (mode,))


def _guarded_open(p, *a, **k):
    _assert_allowed(p)
    mode = k.get("mode", a[0] if a else "r")
    _assert_read_only(mode)
    return _guard_open(p, *a, **k)


def _guarded_fdopen(fd, *a, **k):
    # os.fdopen 接的是**已打开的描述符**（int），不是路径，所以不能走 _assert_allowed。
    #
    # 这里保持拒绝，与改动前一致（那时它被整个替换成 _no_exec，同样不可用）。
    # 不引入 fd 记账表去放行「os.fdopen(os.open(白名单))」：那需要模块级状态，
    # 而 fd 在 close 后会被系统复用，记账会失真——为一个此前从未可用的能力
    # 增加有状态复杂度不划算。真正的读取入口是 open / io.open / pathlib，
    # 它们都已可用。
    raise PermissionError("recompute 不允许 os.fdopen（请用 open 或 pathlib 读取）")


def _guarded_os_open(path, flags, *a, **k):
    # os.open 的白名单门（也是 3.11 以前 pathlib 的真实入口）。
    #
    # 3.9/3.10 的 pathlib 以 self._accessor.open(self, flags, mode) 调用，而
    # _NormalAccessor.open 就是 os.open 的别名——实测第一个实参落在 accessor
    # 实例上、真正路径在第二位（这与「accessor 被当成绑定 self 吃掉」的直觉
    # 相反，靠实测确认）。所以这里先判形参是不是路径，不是则把 flags 当路径、
    # 原 flags 顺延，再做白名单与只读校验。
    if not _is_fs_path(path) and _is_fs_path(flags):
        path, flags = flags, (a[0] if a else 0)
        a = a[1:]
    _assert_allowed(path)
    if flags & _WRITE_FLAGS:
        raise PermissionError("recompute 白名单为只读，拒绝写模式打开: %s" % (path,))
    return _guard_os_open(path, flags, *a, **k)


import builtins, io
builtins.open = _guarded_open
io.open = _guarded_open
_os_g.open = _guarded_os_open
_os_g.fdopen = _guarded_fdopen
del builtins, io, _os_g
"""


def _allowed_paths(inputs: list[dict[str, Any]]) -> list[str]:
    return [str(Path(i["path"]).expanduser().resolve())
            for i in inputs if isinstance(i, dict) and i.get("path")]


def _env_allowed() -> bool:
    # 默认关（该开关是授权语义，不是能力开关）：未设置或为空 → 不放行。
    # 统一走 env_flag，与全仓其他布尔开关同一计算方式（0/false/no/off 都算关）；
    # expand=False 是授权位专有的计算方式：只认 ARGO_ALLOW_RECOMPUTE 这一字面名，
    # 不认裸名 ALLOW_RECOMPUTE。此前走别名展开，环境里任何工具设一个少写前缀
    # 的同名变量就等于替用户放行了「受限子进程执行脚本」——授权只认明确信号。
    from engine_env import env_flag
    return env_flag("ARGO_ALLOW_RECOMPUTE", default=False,
                    expand=False, strict=True)


def run_recompute(
    script: str,
    inputs: list[dict[str, Any]] | None,
    *,
    timeout_s: int = 30,
    max_mem_mb: int = 512,
    allow_exec: bool = False,
    python: str | None = None,
) -> dict[str, Any]:
    """受限执行计算脚本，返回结构化结果（永不抛异常）。"""
    if not script or not script.strip():
        return {"ok": False, "skipped_reason": "script 为空"}
    if not (allow_exec or _env_allowed()):
        return {
            "ok": False,
            "skipped_reason": "默认拒绝：未显式授权（--allow-exec / "
                              "ARGO_ALLOW_RECOMPUTE=1）",
        }
    allowed = _allowed_paths(inputs or [])
    if not allowed:
        return {"ok": False, "skipped_reason": "无白名单输入文件（file_inputs 未声明）"}

    prelude = (
        _NET_DISABLE_PRELUDE
        + f"\n_ALLOWED = {json.dumps(allowed)}\n"
        + _FS_GUARD_PRELUDE
    )
    full_code = prelude + "\n" + script

    py = python or sys.executable
    # 子进程环境走最小集，不再继承全量 os.environ。
    # 为什么：这里执行的是研究包携带的脚本（非本仓代码），脚本可自由
    # os.environ 读取；全量继承等于把父进程的全部 API 密钥交给一段
    # 来源不可控的代码（与 CLI 引擎白名单同一判据，见 engine_env
    # BASE_CHILD_ENV_ALLOWLIST）。断网意图保留：代理类一律不进子进程，
    # 与既有 pop 行为等价（-I 隔离模式下 PYTHONPATH/PYTHONUTF8 本就失效，
    # 脚本只依赖 stdlib，编码由 -X utf8 显式指定）。
    env = build_child_env(
        "recompute", None,
        extra_env={
            "HTTP_PROXY": None, "HTTPS_PROXY": None,
            "http_proxy": None, "https_proxy": None,
        },
    )

    def _limits():
        try:
            import resource
            mem = max_mem_mb * 1024 * 1024
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            resource.setrlimit(resource.RLIMIT_AS, (mem, hard or mem))
        except Exception:
            pass  # RLIMIT_AS 不可用时尽力而为

    start = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="argo-recompute-") as workdir:
            proc = subprocess.Popen(
                # -I 隔离模式会忽略 PYTHONUTF8 环境变量，故编码须用 -X utf8
                # 显式指定：Windows 下子进程按 GBK 编码 stdout，打印 emoji 等
                # 非 GBK 字符会 UnicodeEncodeError 误判为任务失败
                [py, "-I", "-X", "utf8", "-c", full_code],
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8", errors="replace",
                env=env,
                start_new_session=True,  # 独立进程组，便于整组击杀
                preexec_fn=_limits if hasattr(os, "fork") else None,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                try:
                    _kill_process_group(proc)
                except Exception:
                    proc.kill()
                stdout, stderr = proc.communicate()
                return {
                    "ok": False,
                    "exit_code": None,
                    "timed_out": True,
                    "stdout": (stdout or "")[-_TAIL_STDOUT:],
                    "stderr": (stderr or "")[-_TAIL_STDERR:],
                    "elapsed_ms": int((time.time() - start) * 1000),
                }
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "timed_out": False,
            "stdout": (stdout or "")[-_TAIL_STDOUT:],
            "stderr": (stderr or "")[-_TAIL_STDERR:],
            "elapsed_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"{type(e).__name__}: {e}"[-_TAIL_STDERR:],
            "elapsed_ms": int((time.time() - start) * 1000),
        }


def load_table(path: str, **kw: Any) -> list[list[Any]]:
    """表格读取辅助（调试/外部复用）：csv/tsv stdlib；xlsx 需 openpyxl 可选依赖。

    返回行列表（首行为表头）。注意：recompute 子进程内白名单由注入的
    _guarded_open 强制，脚本请直接用 open(...) 读 _ALLOWED[0]；
    本函数仅适合容器外调试。
    """
    p = Path(path).expanduser().resolve()
    suffix = p.suffix.lower()
    if suffix in (".csv", ".tsv"):
        import csv as _csv
        delim = "\t" if suffix == ".tsv" else ","
        with open(p, newline="", encoding="utf-8", **kw) as f:
            return list(_csv.reader(f, delimiter=delim))
    if suffix in (".xlsx", ".xls"):
        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "读取 xlsx 需要 openpyxl（可选依赖）：pip install openpyxl"
            ) from e
        if suffix == ".xlsx":
            import openpyxl as _xl
            wb = _xl.load_workbook(p, read_only=True, data_only=True)
            ws = wb.active
            return [list(row) for row in ws.iter_rows(values_only=True)]
        # .xls（老格式）：openpyxl 不支持，提示转换
        raise RuntimeError(".xls 旧格式请先转 .xlsx 或 csv")
    raise RuntimeError(f"load_table 不支持: {suffix}")


def extract_values(text: str) -> list[float]:
    """从 stdout 提取数值（含千分位/百分比/负号），供冲突对照。"""
    import re
    if not text:
        return []
    out = []
    for m in re.finditer(r"(?<![\w.])(-?\d[\d,]*\.?\d*)(\s*%?)", text):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if m.group(2).strip() == "%":
            v = v / 100.0
        out.append(round(v, 6))
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="可复算执行器（不授权就不执行）")
    parser.add_argument("--script", required=True, help="计算代码（Python）")
    parser.add_argument("--inputs", default="[]",
                        help="file_inputs JSON 数组（白名单输入）")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-mem-mb", type=int, default=512)
    parser.add_argument("--allow-exec", action="store_true",
                        help="显式授权运行（默认拒绝）")
    args = parser.parse_args()
    try:
        inputs = json.loads(args.inputs)
    except ValueError:
        inputs = []
    result = run_recompute(
        args.script, inputs, timeout_s=args.timeout,
        max_mem_mb=args.max_mem_mb, allow_exec=args.allow_exec,
    )
    print(dumps(result))
