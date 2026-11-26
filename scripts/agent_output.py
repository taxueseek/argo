"""Agent-Native 输出瘦身、YAML-Compact 序列化与自描述 Schema 契约。

独立模块职责：
1. compact_for_yaml：在 _strip_for_agent 基础上做无损去重（消除 web_results 与 results
   完全重复的副本、省略空容器与冗余 profiling），压缩上下文体积；
2. dumps_yaml_compact：零外部依赖的紧凑 YAML 序列化器，省去 JSON 标点符号 Token；
3. build_search_schema：毫秒级返回 argo search 的自描述参数与契约，减少误调用。
"""
from __future__ import annotations

import json
import re
from typing import Any

_YAML_SPECIAL_PREFIX = set("-?:,[]{}#&*!|>'\"%@`")
_YAML_RESERVED = {
    "true", "false", "null", "none", "yes", "no", "on", "off",
    "y", "n", "~",
}
_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _needs_quotes(s: str) -> bool:
    if not s:
        return True
    if s[0].isspace() or s[-1].isspace():
        return True
    if s[0] in _YAML_SPECIAL_PREFIX:
        return True
    if s.lower() in _YAML_RESERVED or _NUM_RE.match(s):
        return True
    if any(ch in s for ch in ("\n", "\r", "\t", ": ", " #", "\x00")):
        return True
    if s.endswith(":"):
        return True
    return False


def _format_scalar(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    if not _needs_quotes(s):
        return s
    # 单引号在无需转义控制字符时最省 Token；含控制字符退回 JSON 双引号
    if not any(ord(ch) < 32 for ch in s):
        return "'" + s.replace("'", "''") + "'"
    return json.dumps(s, ensure_ascii=False)


def _is_scalar(val: Any) -> bool:
    return val is None or isinstance(val, (bool, int, float, str))


def _dump_lines(val: Any, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if _is_scalar(val):
        out.append(f"{pad}{_format_scalar(val)}")
        return
    if isinstance(val, list):
        if not val:
            out.append(f"{pad}[]")
            return
        for item in val:
            if _is_scalar(item):
                out.append(f"{pad}- {_format_scalar(item)}")
            elif isinstance(item, dict):
                if not item:
                    out.append(f"{pad}- {{}}")
                    continue
                first = True
                for k, v in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    _emit_kv(prefix, str(k), v, indent + 2, out)
            else:
                out.append(f"{pad}-")
                _dump_lines(item, indent + 2, out)
        return
    if isinstance(val, dict):
        if not val:
            out.append(f"{pad}{{}}")
            return
        for k, v in val.items():
            _emit_kv(pad, str(k), v, indent, out)
        return
    out.append(f"{pad}{_format_scalar(str(val))}")


def _emit_kv(prefix: str, key: str, val: Any, base_indent: int, out: list[str]) -> None:
    k_str = _format_scalar(key)
    if _is_scalar(val):
        out.append(f"{prefix}{k_str}: {_format_scalar(val)}")
        return
    if isinstance(val, list):
        if not val:
            out.append(f"{prefix}{k_str}: []")
            return
        if all(_is_scalar(x) for x in val) and len(val) <= 6:
            inline = ", ".join(_format_scalar(x) for x in val)
            if len(inline) <= 72:
                out.append(f"{prefix}{k_str}: [{inline}]")
                return
        out.append(f"{prefix}{k_str}:")
        _dump_lines(val, base_indent + 2, out)
        return
    if isinstance(val, dict):
        if not val:
            out.append(f"{prefix}{k_str}: {{}}")
            return
        out.append(f"{prefix}{k_str}:")
        _dump_lines(val, base_indent + 2, out)
        return
    out.append(f"{prefix}{k_str}: {_format_scalar(str(val))}")


def dumps_yaml_compact(data: Any) -> str:
    """将结构化数据序列化为紧凑、零额外依赖的 YAML 文本。"""
    out: list[str] = []
    _dump_lines(data, 0, out)
    return "\n".join(out)


def compact_for_yaml(agent_payload: dict[str, Any]) -> dict[str, Any]:
    """在 _strip_for_agent 基础上执行无损去重与紧凑化。

    1. 当 local_results 为空且 web_results 与 results 的 URL 序一致时，省略重复的
       web_results、空的 local_results 与冗余 facets；
    2. 省略空列表/空字典（errors/failures）；
    3. 将详细 timing profiling 折叠为单行关键摘要（elapsed_ms/engines_run）。
    """
    out: dict[str, Any] = {}
    results = agent_payload.get("results") or []
    local_results = agent_payload.get("local_results")
    web_results = agent_payload.get("web_results")

    has_local = bool(local_results)
    same_web = (
        isinstance(web_results, list)
        and len(web_results) == len(results)
        and [r.get("url") for r in web_results if isinstance(r, dict)]
        == [r.get("url") for r in results if isinstance(r, dict)]
    )

    for k, v in agent_payload.items():
        if v is None:
            continue
        if k in ("errors", "failures") and not v:
            continue
        if k == "local_results" and not has_local:
            continue
        if k == "web_results" and not has_local and same_web:
            continue
        if k == "facets" and not has_local and same_web:
            continue
        if k == "timing" and isinstance(v, dict):
            compact_t: dict[str, Any] = {}
            if "elapsed_ms" in v:
                compact_t["elapsed_ms"] = v["elapsed_ms"]
            disp = v.get("dispatch")
            if isinstance(disp, dict) and "engines_run" in disp:
                compact_t["engines_run"] = disp["engines_run"]
            if compact_t:
                out[k] = compact_t
            continue
        out[k] = v
    return out


def build_search_schema() -> dict[str, Any]:
    """返回 argo search 的自描述契约（零网络开销，供 Agent 首轮自检）。"""
    return {
        "command": "argo search <query>",
        "output_modes": {
            "--yaml": "Agent 推荐：紧凑 YAML + 无损去重（比 --agent 省约 60% Token）",
            "--agent": "等价于 --json --fields agent（向后兼容 JSON 管道）",
            "--json --envelope": "全量溯源视图（含 candidates/sources/coverage）",
        },
        "key_flags": {
            "-n": "结果数（建议 <=10）",
            "--mode": ["fast", "auto", "deep", "budget"],
            "--intent": ["news", "academic", "social", "image-reverse", "calc"],
            "--verify [N]": "抓取 top-N 正文并回填证据分",
            "--numverify [N]": "比对摘要数字与原文一致性",
            "--since / --until": "时间窗过滤（如 7d / 2026-08-01）",
            "--no-cache": "跳过缓存强制实时查询",
        },
        "mutex_rules": [
            "--yaml 与 --json / --envelope / --fields full 互斥",
            "--agent 与 --envelope / --fields full 互斥",
        ],
        "result_fields": [
            "title", "url", "snippet", "source", "score",
            "published_at", "fetch_suggested", "upstream",
        ],
    }
