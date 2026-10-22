#!/usr/bin/env python3
"""cli_runner.py — local-search CLI 工具引擎调度（ddgs 等）。

拆分自 search_v3.py：负责外部命令行搜索工具执行、临时结构化输出文件管理、
错误标记探测与快速失败重试策略。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import time
from typing import Any

# ddgs -o 的结构化输出临时文件清理
_TMP_FILES: set[str] = set()


def _cleanup_tmp_files() -> None:
    for p in list(_TMP_FILES):
        try:
            os.unlink(p)
        except OSError:
            pass
        _TMP_FILES.discard(p)


atexit.register(_cleanup_tmp_files)

# ddgs CLI 可用性检查缓存
_CLI_AVAIL_CACHE: dict[str, tuple[float, bool]] = {}
_CLI_AVAIL_TTL = 60.0

_DDGS_TIMELIMITS: dict[str, set[str]] = {
    "text": {"d", "w", "m", "y"},
    "news": {"d", "w", "m", "y"},
    "images": {"d", "w", "m", "y"},
    "videos": {"d", "w", "m"},
}

_CLI_ERROR_MARKERS: tuple[str, ...] = (
    "DDGSException", "No results", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "Timeout", "backends do not exist",
)
_CLI_RETRIES = 1
_RETRY_FAST_S = 2.5


def _since_to_ddgs_timelimit(since: str | None, until: str | None) -> str | None:
    """Convert since/until to ddgs timelimit argument."""
    if not since:
        return None
    match = re.match(r"^(\d+)([dwmy])$", since.lower())
    if match:
        num, unit = match.groups()
        num = int(num)
        if unit == "y":
            return "y"
        elif unit == "m":
            return "m"
        elif unit == "w":
            return "w"
        elif unit == "d":
            if num >= 365:
                return "y"
            elif num >= 30:
                return "m"
            elif num >= 7:
                return "w"
            else:
                return "d"
    return None


def _check_cli_available(cmd: str) -> bool:
    """Check if a CLI command is available（结果缓存 TTL=_CLI_AVAIL_TTL）。"""
    import shutil
    now = time.time()
    hit = _CLI_AVAIL_CACHE.get(cmd)
    if hit is not None and now - hit[0] < _CLI_AVAIL_TTL:
        return hit[1]
    ok = shutil.which(cmd) is not None
    _CLI_AVAIL_CACHE[cmd] = (time.time(), ok)
    return ok


def _parse_cli_json(data: Any, engine_name: str) -> list[dict[str, Any]]:
    """解析 ddgs 结构化 JSON 输出（-o json 写入文件）。"""
    if not isinstance(data, list):
        return []
    results: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url = item.get("href", item.get("url", item.get("link", "")))
        snippet = item.get("body", item.get("snippet",
                                            item.get("description", item.get("content", ""))))
        if not title and not url:
            continue
        r: dict[str, Any] = {
            "title": str(title),
            "url": str(url),
            "snippet": str(snippet),
            "source": engine_name,
        }
        for key in ("image", "thumbnail", "source", "duration", "provider",
                    "height", "width"):
            if key in item and item[key] not in (None, ""):
                r[key] = item[key]
        date = item.get("date")
        if date:
            r["published_at"] = str(date)[:64]
        meta = " · ".join(
            str(item[k]) for k in ("author", "publisher", "info")
            if item.get(k)
        )
        if meta:
            r["snippet"] = (str(snippet) + " · " + meta)[:400] if snippet else meta[:400]
        results.append(r)
    return results


def _parse_cli_output(stdout: str, engine_name: str) -> tuple[list[dict[str, Any]], str]:
    """Parse output from CLI search tools like ddgs."""
    results = []
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            for item in data:
                result = {
                    "title": item.get("title", ""),
                    "url": item.get("href", item.get("url", item.get("link", ""))),
                    "snippet": item.get("body", item.get("snippet", item.get("description", item.get("content", "")))),
                    "source": engine_name,
                }
                for key in ["date", "image", "thumbnail", "source"]:
                    if key in item:
                        result[key] = item[key]
                results.append(result)
            return results, ""
    except json.JSONDecodeError:
        pass

    current_result = {}
    current_field = None

    for line in stdout.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if re.fullmatch(r"\d+\.\s*", line_stripped):
            if current_result:
                results.append(current_result)
                current_result = {}
                current_field = None
            continue

        field_matched = False
        for field, target_key in [
            ("title", "title"),
            ("href", "url"),
            ("url", "url"),
            ("body", "snippet"),
            ("snippet", "snippet"),
            ("description", "snippet"),
            ("content", "snippet"),
            ("date", "date"),
            ("image", "image"),
            ("thumbnail", "thumbnail"),
            ("source", "source"),
            ("duration", "duration"),
            ("provider", "provider"),
            ("height", "height"),
            ("width", "width"),
        ]:
            if line_stripped.startswith(field) and len(line_stripped) > len(field):
                value = line_stripped[len(field):].strip()
                if value:
                    current_result[target_key] = value
                    current_field = field
                    field_matched = True
                    break

        if not field_matched and current_field:
            if current_field in ("body", "snippet", "description", "content"):
                current_result.setdefault("snippet", "")
                current_result["snippet"] += " " + line_stripped

    if current_result:
        results.append(current_result)

    for i, r in enumerate(results):
        r.setdefault("source", engine_name)
        r.setdefault("title", "")
        r.setdefault("url", "")
        r.setdefault("snippet", "")
        r.setdefault("score", round(0.9 - (i * 0.05), 2))
    return results, ""


def _attempt_loop(cmd: list[str], cli_cmd: str, key: str,
                  tmp_path: str | None, timeout: float):
    """CLI 引擎的单引擎尝试循环。"""
    last_err = ""
    for attempt in range(_CLI_RETRIES + 1):
        t_attempt = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [], f"{cli_cmd} timed out after {timeout}s"
        except FileNotFoundError:
            return [], f"{cli_cmd} not found"
        except Exception as e:
            return [], f"{cli_cmd} error: {e}"

        combined_output = result.stdout + result.stderr

        if tmp_path is not None:
            json_ok = False
            try:
                with open(tmp_path, encoding="utf-8") as f:
                    data = json.load(f)
                parsed = _parse_cli_json(data, key)
                if parsed:
                    return parsed, ""
                json_ok = True
            except (OSError, ValueError):
                json_ok = False
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if json_ok:
                return [], ""
        else:
            json_ok = False

        err_marker = next(
            (m for m in _CLI_ERROR_MARKERS if m in combined_output), None
        )
        if err_marker:
            detail = combined_output.strip().replace("\n", " ")[:120]
            last_err = f"{cli_cmd}: {err_marker}: {detail}"
            if attempt < _CLI_RETRIES and time.monotonic() - t_attempt <= _RETRY_FAST_S:
                time.sleep(0.3)
                continue
            return [], last_err
        if result.returncode != 0:
            last_err = f"{cli_cmd} exit {result.returncode}: {combined_output[:200]}"
            if attempt < _CLI_RETRIES and time.monotonic() - t_attempt <= _RETRY_FAST_S:
                time.sleep(0.3)
                continue
            return [], last_err

        parsed, parse_err = _parse_cli_output(result.stdout, key)
        if parsed:
            return parsed, parse_err
        last_err = f"{cli_cmd}: empty output"
        if attempt < _CLI_RETRIES and time.monotonic() - t_attempt <= _RETRY_FAST_S:
            time.sleep(0.3)
            continue
        return [], last_err
    return [], last_err


def _run_cli_engine(spec: dict[str, Any], query: str, n: int, timeout: float,
                    since: str | None = None, until: str | None = None,
                    lang_param_fn=None) -> tuple[list[dict[str, Any]], str]:
    """Run a CLI-based search engine (e.g., ddgs)."""
    cli_cmd = spec.get("cli_command", "")
    cli_args_template = spec.get("cli_args", [])
    key = spec.get("key", cli_cmd)

    if not cli_cmd:
        return [], "CLI engine missing cli_command"

    if not _check_cli_available(cli_cmd):
        return [], f"{cli_cmd} not available"

    time_limit = _since_to_ddgs_timelimit(since, until)

    cli_args = []
    for arg in cli_args_template:
        formatted = arg.format(query=query, n=n)
        cli_args.append(formatted)

    if time_limit and "-t" not in cli_args and "--timelimit" not in cli_args:
        sub = next((a for a in cli_args_template if not str(a).startswith("-")), None)
        allowed = _DDGS_TIMELIMITS.get(str(sub), _DDGS_TIMELIMITS["text"])
        if time_limit in allowed:
            cli_args.extend(["-t", time_limit])

    if spec.get("use_lang_region") and not any(a in ("-r", "--region") for a in cli_args) and lang_param_fn:
        region = lang_param_fn("region", query)
        if region:
            cli_args.extend(["-r", region])

    output_format = spec.get("output_format", "")
    tmp_path: str | None = None
    if output_format == "json" and not any(
        a in ("-o", "--output") for a in cli_args
    ):
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="ddgs_")
        os.close(fd)
        _TMP_FILES.add(tmp_path)
        cli_args.extend(["-o", tmp_path])

    cmd = [cli_cmd] + cli_args

    try:
        return _attempt_loop(cmd, cli_cmd, key, tmp_path, timeout)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            _TMP_FILES.discard(tmp_path)
