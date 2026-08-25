#!/usr/bin/env python3
"""recompute.py — 可复算执行器：本地数据 + 计算脚本 → 可核查数值。

设计（P0-2，对齐「结论可重算」）：

fail-closed：
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

_TAIL_STDOUT = 3000  # 超时/被杀时保留尾部输出（大任务不至于白跑全丢）
_TAIL_STDERR = 2000

# 注入到用户代码执行前的防护段：Python 层断网 + 强制文件白名单
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
# 保留 _NetworkDisabled：_no_dns / _BlockedSocket.__init__ 的函数体还要 raise 它，
# 若一并 del 则 getaddrinfo 被触发时全局名已被删，抛 NameError 而非预期错误。
del _socket, _s2, _BlockedSocket, _no_dns
"""


def _allowed_paths(inputs: list[dict[str, Any]]) -> list[str]:
    return [str(Path(i["path"]).expanduser().resolve())
            for i in inputs if isinstance(i, dict) and i.get("path")]


def _env_allowed() -> bool:
    raw = (os.environ.get("ARGO_ALLOW_RECOMPUTE") or "").strip().lower()
    return raw not in {"", "0", "false", "off", "no"}


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
            "skipped_reason": "fail-closed：未显式授权（--allow-exec / "
                              "ARGO_ALLOW_RECOMPUTE=1）",
        }
    allowed = _allowed_paths(inputs or [])
    if not allowed:
        return {"ok": False, "skipped_reason": "无白名单输入文件（file_inputs 未声明）"}

    prelude = (
        _NET_DISABLE_PRELUDE
        + f"\n_ALLOWED = {json.dumps(allowed)}\n"
        + "def _assert_allowed(p):\n"
        "    import os\n"
        "    rp = os.path.realpath(p)\n"
        "    if rp not in _ALLOWED:\n"
        "        raise PermissionError(f'recompute 只读白名单输入: {p}')\n"
        "_guard_open = open\n"
        "def _guarded_open(p, *a, **k):\n"
        "    if isinstance(p, str):\n"
        "        _assert_allowed(p)\n"
        "    return _guard_open(p, *a, **k)\n"
        "import builtins, io\n"
        "builtins.open = _guarded_open\n"
        "io.open = _guarded_open\n"
        "del builtins, io\n"
    )
    full_code = prelude + "\n" + script

    py = python or sys.executable
    env = dict(os.environ)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)

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
                [py, "-I", "-c", full_code],
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,  # 独立进程组，便于整组击杀
                preexec_fn=_limits if hasattr(os, "fork") else None,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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

    parser = argparse.ArgumentParser(description="可复算执行器（fail-closed）")
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
    print(json.dumps(result, ensure_ascii=False, indent=2))
