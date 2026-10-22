#!/usr/bin/env python3
"""mcp_local.py — MCP 本地文件检索、受限预览与可复算沙箱。

拆分自 mcp_handlers.py：隔离本地文件系统访问、白名单审查、seek 定位与安全执行沙箱。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from mcp_compact import _trim_snippet

_LOCAL_READ_ALLOWED_EXTS = {
    ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".xml", ".rst", ".ipynb",
}


def _local_read_whitelist() -> list[str]:
    """白名单目录：ARGO_LOCAL_READ_DIRS（逗号分隔）；没配置就返回空，不放行。"""
    raw = (os.environ.get("ARGO_LOCAL_READ_DIRS") or "").strip()
    if not raw:
        return []
    return [os.path.realpath(os.path.expanduser(d))
            for d in raw.split(",") if d.strip()]


def _local_read_preview(
    path: str,
    max_chars: int = 4000,
    line_start: int | None = None,
    line_end: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """白名单内文本文件预览（行窗口或字符截断）；越权/二进制拒绝。"""
    root = os.path.realpath(os.path.expanduser(path))
    allowed = _local_read_whitelist()
    if not allowed:
        raise PermissionError(
            "本地读取未配置白名单：设 ARGO_LOCAL_READ_DIRS=目录1,目录2（没配置就不放行）"
        )
    if not any(root == d or root.startswith(d + os.sep) for d in allowed):
        raise PermissionError(f"路径不在白名单目录内: {path}")
    if not os.path.isfile(root):
        raise FileNotFoundError(f"文件不存在: {path}")
    ext = os.path.splitext(root)[1].lower()
    if ext not in _LOCAL_READ_ALLOWED_EXTS:
        raise PermissionError(f"仅支持文本类文件预览（{ext} 不支持；pdf 走其他路径）")
    size = os.path.getsize(root)
    if size > 4 * 1024 * 1024:
        raise PermissionError("文件超过 4MB，拒绝预览（请缩小范围或改用搜索）")

    with open(root, encoding="utf-8", errors="replace") as f:
        full = f.read()
    total_lines = full.count("\n") + (1 if full and not full.endswith("\n") else 0)
    if line_start is not None or line_end is not None:
        start = max(1, int(line_start or 1))
        end = int(line_end) if line_end else start + 200
        lines = full.splitlines(keepends=True)[(start - 1): end]
        content = "".join(lines)
        lines_slice = (start, start + len(lines) - 1)
    else:
        content = full
        lines_slice = None

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return content, {
        "path": root,
        "chars": len(content),
        "lines": (lines_slice[1] - lines_slice[0] + 1) if lines_slice else None,
        "total_lines": total_lines,
        "is_preview": truncated or lines_slice is not None,
    }


def _seek_py() -> str:
    """定位 local-seek 的 seek.py（安装感知、唯一来源，见 seek_locator）。"""
    from seek_locator import resolve_seek_py
    return resolve_seek_py()


def handle_local_search(
    arguments: dict[str, Any],
    pretty: bool = False,
    ok_fn=None,
    clamp_int_fn=None,
    lazy_cached_fn=None,
) -> dict[str, Any]:
    """argo_local_search 实现：本地文件搜索。"""
    query = str(arguments.get("query", "")).strip()

    def _local_err(msg: str) -> dict:
        return ok_fn({"query": query, "engine": "local_files", "count": 0, "results": [],
                     "errors": [msg]}, pretty=pretty)

    if not query:
        return _local_err("query 不能为空")
    path = str(arguments.get("path", "~"))
    max_results = clamp_int_fn(arguments.get("max_results", 5), 5, 1, 20)
    exact = bool(arguments.get("exact", False))

    # 宽泛根守卫
    try:
        if lazy_cached_fn("local_seek")._is_broad_local_root(os.path.expanduser(path)):
            return _local_err("路径过宽（home / 根目录），扫全盘既慢又失准，已拒绝；请传具体目录")
    except Exception as e:
        sys.stderr.write(
            f"[argo-mcp] local_search 宽泛根守卫不可用（{type(e).__name__}），fail-open\n")

    payload = None
    try:
        from local_seek import seek_query_payload
        payload = seek_query_payload(query, os.path.expanduser(path),
                                     max_results, exact=exact, time_budget=15)
    except Exception:
        payload = None

    if payload is None:
        seek_py = _seek_py()

        cmd = [sys.executable, seek_py, query, "--path", path, "--json",
               "--max", str(max_results)]
        if exact:
            cmd.append("--exact")
        try:
            # 兼容测试 patch mcp_handlers.subprocess.run
            runner = subprocess.run
            try:
                import mcp_handlers
                runner = getattr(mcp_handlers.subprocess, "run", subprocess.run)
            except Exception:
                pass

            proc = runner(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
                env={**os.environ, "PYTHONUTF8": "1"})
        except Exception as e:
            return _local_err(f"本地搜索执行失败: {e}")
        if proc.returncode != 0:
            msg = (proc.stdout or proc.stderr or "").strip() or "本地搜索无结果"
            return _local_err(msg)
        try:
            payload = json.loads(proc.stdout)
        except Exception:
            return _local_err("本地搜索输出解析失败")

    mode = payload.get("mode", "fast")
    score = 0.9 if mode == "fast" else 0.7
    results = []
    for r in payload.get("results") or []:
        abspath = os.path.abspath(r.get("path", ""))
        line = r.get("line") or 0
        results.append({
            "title": abspath,
            "url": f"file://{abspath}" + (f"#L{line}" if line else ""),
            "snippet": _trim_snippet(r.get("snippet"), 120),
            "source": "local_files", "score": score,
        })
    return ok_fn({
        "query": query, "engine": "local_files", "engines_used": ["local_files"],
        "count": len(results), "elapsed_ms": payload.get("elapsed_ms"),
        "cached": False, "mode": mode, "depth": None,
        "results": results, "errors": [],
    }, pretty=pretty)


def handle_local_read(
    arguments: dict[str, Any],
    pretty: bool = False,
    ok_fn=None,
    int_param_fn=None,
) -> dict[str, Any]:
    """argo_local_read 实现：白名单本地文本预览。"""
    raw_path = str(arguments.get("path", "")).strip()
    max_chars = max(200, min(int_param_fn(arguments, "max_chars", 4000), 20000))
    line_start = arguments.get("line_start")
    line_end = arguments.get("line_end")
    if not raw_path:
        return ok_fn({"path": "", "content": "", "errors": ["path 不能为空"]}, pretty=pretty)
    try:
        content, meta = _local_read_preview(
            raw_path, max_chars=max_chars,
            line_start=line_start, line_end=line_end,
        )
    except Exception as e:
        return ok_fn({"path": raw_path, "content": "",
                     "errors": [str(e)]}, pretty=pretty)
    return ok_fn({
        "path": meta["path"],
        "content": content,
        "chars": meta["chars"],
        "lines": meta["lines"],
        "total_lines": meta["total_lines"],
        "is_preview": meta["is_preview"],
        "errors": [],
    }, pretty=pretty)


def handle_recompute(
    arguments: dict[str, Any],
    pretty: bool = False,
    ok_fn=None,
    clamp_int_fn=None,
) -> dict[str, Any]:
    """argo_recompute 实现：可复算执行器。"""
    script = str(arguments.get("script", "")).strip()
    inputs_raw = arguments.get("file_inputs")
    inputs = []
    if isinstance(inputs_raw, str) and inputs_raw.strip():
        try:
            inputs = json.loads(inputs_raw)
        except ValueError:
            inputs = []
    elif isinstance(inputs_raw, list):
        inputs = inputs_raw
    elif inputs_raw:
        try:
            inputs = json.loads(inputs_raw)
        except (ValueError, TypeError):
            inputs = []
    timeout_s = clamp_int_fn(arguments.get("timeout_s", 30), 30, 1, 120)
    max_mem_mb = clamp_int_fn(arguments.get("max_mem_mb", 512), 512, 64, 4096)
    allow_exec = bool(arguments.get("allow_exec", False))
    if not script:
        return ok_fn({"ok": False, "skipped_reason": "script 为空"}, pretty=pretty)
    try:
        from recompute import run_recompute
        result = run_recompute(
            script, inputs or None,
            timeout_s=timeout_s, max_mem_mb=max_mem_mb,
            allow_exec=allow_exec,
        )
    except Exception as e:
        result = {"ok": False, "skipped_reason": f"执行器异常: {e}"}
    return ok_fn(result, pretty=pretty)
