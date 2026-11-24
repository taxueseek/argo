#!/usr/bin/env python3
"""seek_inspect.py — local-seek 精读层（文件结构大纲、符号体一跳直读、流式按行读取）。

从 seek.py 拆出：纯标准库实现，职责严格限定在「已知单文件后的局部结构与源码读取」。
支持文本与结构化 JSON 双形态输出（供 --agent / DSH 工作台直接消费）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_OUTLINE_RULES: dict[str, list[re.Pattern[str]]] = {
    ".py": [re.compile(r"^\s*(async\s+)?(def|class)\s+\w+")],
    ".pyi": [re.compile(r"^\s*(async\s+)?(def|class)\s+\w+")],
    ".js": [
        re.compile(r"^\s*(export\s+)?(async\s+)?(function|class)\s+\w+"),
        re.compile(r"^\s*(export\s+)?(const|let|var)\s+\w+"),
    ],
    ".jsx": [
        re.compile(r"^\s*(export\s+)?(async\s+)?(function|class)\s+\w+"),
        re.compile(r"^\s*(export\s+)?(const|let|var)\s+\w+"),
    ],
    ".ts": [
        re.compile(r"^\s*(export\s+)?(async\s+)?(function|class|interface|type|enum)\s+\w+"),
        re.compile(r"^\s*(export\s+)?(const|let|var)\s+\w+"),
    ],
    ".tsx": [
        re.compile(r"^\s*(export\s+)?(async\s+)?(function|class|interface|type|enum)\s+\w+"),
        re.compile(r"^\s*(export\s+)?(const|let|var)\s+\w+"),
    ],
    ".go": [
        re.compile(r"^\s*func\s+\w+"),
        re.compile(r"^\s*type\s+\w+\s+(struct|interface)\b"),
    ],
    ".rs": [re.compile(r"^\s*(pub\s+)?(fn|struct|enum|impl|trait|mod|type)\s+\w+")],
    ".java": [
        re.compile(r"^\s*(public|private|protected|static|\s)*(class|interface|enum)\s+\w+"),
        re.compile(r"^\s*(public|private|protected|static|\s)*[\w<>,\[\] ]+\s+\w+\s*\("),
    ],
    ".sh": [
        re.compile(r"^\s*function\s+\w+"),
        re.compile(r"^\s*[a-zA-Z_]\w*\s*\(\)\s*\{?"),
    ],
    ".bash": [
        re.compile(r"^\s*function\s+\w+"),
        re.compile(r"^\s*[a-zA-Z_]\w*\s*\(\)\s*\{?"),
    ],
    ".zsh": [
        re.compile(r"^\s*function\s+\w+"),
        re.compile(r"^\s*[a-zA-Z_]\w*\s*\(\)\s*\{?"),
    ],
    ".md": [re.compile(r"^#{1,6}\s+")],
    ".mdx": [re.compile(r"^#{1,6}\s+")],
    ".json": [re.compile(r'^\s*"[^"]+"\s*:')],
    ".yaml": [re.compile(r"^[a-zA-Z_][\w.-]*\s*:")],
    ".yml": [re.compile(r"^[a-zA-Z_][\w.-]*\s*:")],
}


def _outline_level(line: str, suffix: str) -> int:
    """返回结构行的层级：Markdown 按 # 数量，其余按前导空白长度。"""
    if suffix in (".md", ".mdx"):
        m = re.match(r"^(#{1,6})\s+", line)
        return len(m.group(1)) if m else 99
    return len(line) - len(line.lstrip(" \t"))


def _compute_outline_entries(
    lines: list[str], rules: list[re.Pattern[str]], suffix: str
) -> list[tuple[int, int, int, str]]:
    """计算文件结构项及其精确起止行 [(start, end, span, text), ...]。"""
    raw_hits = []
    for i, ln in enumerate(lines, 1):
        if any(r.match(ln) for r in rules):
            raw_hits.append((i, _outline_level(ln, suffix), ln.strip()))
    total = len(lines)
    entries = []
    for idx, (start, lvl, text) in enumerate(raw_hits):
        end = total
        for nxt_start, nxt_lvl, _ in raw_hits[idx + 1:]:
            if nxt_lvl <= lvl:
                end = nxt_start - 1
                break
        while end > start and not lines[end - 1].strip():
            end -= 1
        entries.append((start, end, end - start + 1, text))
    return entries


def outline_file(path: str | Path, *, as_json: bool = False) -> tuple[str, int]:
    """输出代码/文档文件结构（含精确起止行跨度），替代整文件读取。返回 (输出文本, 退出码)。"""
    p = Path(path)
    if not p.is_file():
        msg = f"local-seek: {path} 不是文件"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        msg = f"local-seek: 读取失败 {e}"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    suffix = p.suffix.lower()
    rules = _OUTLINE_RULES.get(suffix)
    if not rules:
        if as_json:
            return json.dumps({
                "ok": True, "path": str(p), "total_lines": len(lines),
                "entries": [], "note": f"{p.suffix or '无扩展名'} 暂无结构规则",
            }, ensure_ascii=False), 0
        return (f"local-seek: {p.name} 共 {len(lines)} 行"
                f"（{p.suffix or '无扩展名'} 暂无结构规则）"), 0
    entries = _compute_outline_entries(lines, rules, suffix)
    if as_json:
        return json.dumps({
            "ok": True,
            "path": str(p),
            "total_lines": len(lines),
            "count": len(entries),
            "entries": [
                {"start": s, "end": e, "span": span, "signature": txt[:120]}
                for s, e, span, txt in entries
            ],
        }, ensure_ascii=False), 0
    hits = [f"{s}-{e} ({span}L): {txt[:100]}" for s, e, span, txt in entries]
    head = f"local-seek: {p.name} 结构 {len(hits)} 处 / 共 {len(lines)} 行"
    return "\n".join([head] + hits), 0


def read_symbol_body(
    path: str | Path,
    symbol_name: str,
    max_lines: int = 200,
    *,
    as_json: bool = False,
) -> tuple[str, int]:
    """按符号名一跳读取定义区间源码（带真实行号，默认 ≤ 200 行封顶）。返回 (输出文本, 退出码)。"""
    symbol_name = (symbol_name or "").strip()
    if not symbol_name:
        msg = "local-seek: --symbol-body 需要指定符号名"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    p = Path(path)
    if not p.is_file():
        msg = f"local-seek: {path} 不是文件"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        msg = f"local-seek: 读取失败 {e}"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    suffix = p.suffix.lower()
    rules = _OUTLINE_RULES.get(suffix)
    if not rules:
        msg = f"local-seek: {p.name} 暂无结构规则（{p.suffix or '无扩展名'}），请改用 --lines"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    entries = _compute_outline_entries(lines, rules, suffix)
    pat = re.compile(r"(?<!\w)" + re.escape(symbol_name) + r"(?!\w)")
    matched = [item for item in entries if pat.search(item[3])]
    if not matched:
        low = symbol_name.lower()
        matched = [item for item in entries if low in item[3].lower()]
    if not matched:
        msg = f"local-seek: {p.name} 中未找到符号 {symbol_name!r}（可先运行 --outline 查看结构）"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1

    start, end, span, sig = matched[0]
    cap = max(1, int(max_lines))
    shown_end = min(end, start + cap - 1)
    body = [f"{i}: {lines[i - 1]}" for i in range(start, shown_end + 1)]
    if as_json:
        payload: dict[str, Any] = {
            "ok": True,
            "path": str(p),
            "symbol": symbol_name,
            "signature": sig[:160],
            "start": start,
            "end": shown_end,
            "symbol_end": end,
            "span": span,
            "total_lines": len(lines),
            "truncated": shown_end < end,
            "content": "\n".join(body),
        }
        if shown_end < end:
            payload["next_range"] = f"{shown_end + 1}-{end}"
        return json.dumps(payload, ensure_ascii=False), 0

    extra = ""
    if len(matched) > 1:
        others = ", ".join(f"{s}-{e}" for s, e, _, _ in matched[1:5])
        extra = f"；另有 {len(matched) - 1} 处同名: {others}"
    head = (
        f"local-seek: {p.name} 符号 {symbol_name} 第 {start}-{shown_end} 行"
        f"（共 {span} 行 / 文件 {len(lines)} 行{extra}）"
    )
    if shown_end < end:
        body.append(
            f"... 已截断（上限 {cap} 行），继续读取：argo seek --lines {shown_end + 1}-{end} {path}"
        )
    return "\n".join([head] + body), 0


def read_lines(path: str | Path, spec: str, *, as_json: bool = False) -> tuple[str, int]:
    """按行读取文件（单遍流式，不加载全文）。返回 (输出文本, 退出码)。"""
    m = re.fullmatch(r"(\d+)-(\d+)", spec)
    if not m:
        msg = "local-seek: --lines 格式应为 N-M（如 10-20）"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        msg = f"local-seek: 行范围无效 {spec}"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    p = Path(path)
    if not p.is_file():
        msg = f"local-seek: {path} 不是文件"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    picked, total = [], 0
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f, 1):
                total = i
                if a <= i <= b:
                    picked.append(f"{i}: {ln.rstrip()}")
    except OSError as e:
        msg = f"local-seek: 读取失败 {e}"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    if a > total:
        msg = f"local-seek: {p.name} 只有 {total} 行，请求从 {a} 行开始"
        return (json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if as_json else msg), 1
    end = min(b, total)
    if as_json:
        return json.dumps({
            "ok": True,
            "path": str(p),
            "start": a,
            "end": end,
            "total_lines": total,
            "content": "\n".join(picked),
        }, ensure_ascii=False), 0
    out = [f"local-seek: {p.name} 第 {a}-{end} 行 / 共 {total} 行"]
    return "\n".join(out + picked), 0
