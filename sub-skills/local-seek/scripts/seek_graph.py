#!/usr/bin/env python3
"""seek_graph.py — 零依赖 AST 符号引用图与 PageRank 核心定义排序（P1）。

设计原则（第一性原理 + 奥卡姆剃刀）：
- 纯 Python 标准库（ast + re + os），零外部依赖；
- 仅对检索候选集内的文件（上限 60 个）按 (mtime_ns, size) 增量缓存解析，
  冷启动毫秒级、热查询 <1ms；
- 用 Python ast 提取 .py 的定义符号与导入/引用符号，用轻量正则覆盖 JS/TS/Go/Rust；
- 在局部引用子图上跑 15 轮阻尼迭代 PageRank（d=0.85），识别被广泛引用的
  核心定义真源，压制测试文件与孤立顺带提及。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

_CACHE: dict[str, tuple[int, int, frozenset[str], frozenset[str]]] = {}
_CACHE_MAX = 512
_MAX_FILES = 60
_MAX_FILE_BYTES = 128 * 1024

_GENERIC_SYMS = frozenset({
    "main", "run", "get", "set", "init", "test", "setup", "teardown",
    "load", "save", "read", "write", "parse", "format", "check", "build",
    "create", "update", "delete", "list", "str", "int", "dict", "len",
})

_JS_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum|const)\s+([A-Za-z_]\w{2,})",
    re.M,
)
_GO_DEF_RE = re.compile(r"^\s*(?:func|type)\s+([A-Za-z_]\w{2,})", re.M)
_RS_DEF_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|type)\s+([A-Za-z_]\w{2,})", re.M
)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def _extract_py(text: str) -> tuple[frozenset[str], frozenset[str]]:
    defs: set[str] = set()
    refs: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return frozenset(), frozenset()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if len(node.name) >= 3 and node.name not in _GENERIC_SYMS:
                defs.add(node.name.lower())
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for part in alias.name.split("."):
                    if len(part) >= 3:
                        refs.add(part.lower())
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for part in node.module.split("."):
                    if len(part) >= 3:
                        refs.add(part.lower())
            for alias in node.names:
                if alias.name != "*" and len(alias.name) >= 3:
                    refs.add(alias.name.lower())
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if len(node.id) >= 3 and node.id not in _GENERIC_SYMS:
                refs.add(node.id.lower())
    return frozenset(defs), frozenset(refs)


def _extract_other(text: str, ext: str) -> tuple[frozenset[str], frozenset[str]]:
    if ext in (".js", ".jsx", ".ts", ".tsx"):
        defs = {m.group(1).lower() for m in _JS_DEF_RE.finditer(text)}
    elif ext == ".go":
        defs = {m.group(1).lower() for m in _GO_DEF_RE.finditer(text)}
    elif ext == ".rs":
        defs = {m.group(1).lower() for m in _RS_DEF_RE.finditer(text)}
    else:
        return frozenset(), frozenset()
    defs -= _GENERIC_SYMS
    refs = {m.group(0).lower() for m in _IDENT_RE.finditer(text)} - _GENERIC_SYMS
    return frozenset(defs), frozenset(refs - defs)


def _extract_file_symbols(fp: str) -> tuple[frozenset[str], frozenset[str]]:
    try:
        st = os.stat(fp)
    except OSError:
        return frozenset(), frozenset()
    if st.st_size == 0 or st.st_size > _MAX_FILE_BYTES:
        return frozenset(), frozenset()
    cached = _CACHE.get(fp)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2], cached[3]
    ext = os.path.splitext(fp)[1].lower()
    if ext not in (".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs"):
        return frozenset(), frozenset()
    try:
        text = Path(fp).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset(), frozenset()
    if ext in (".py", ".pyi"):
        defs, refs = _extract_py(text)
    else:
        defs, refs = _extract_other(text, ext)
    stem = Path(fp).stem.lower()
    if len(stem) >= 3 and stem not in _GENERIC_SYMS and not stem.startswith("test_"):
        defs = defs | {stem}
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[fp] = (st.st_mtime_ns, st.st_size, defs, refs)
    return defs, refs


def rank_files_by_pagerank(
    file_paths: list[str],
    *,
    damping: float = 0.85,
    iterations: int = 15,
) -> dict[str, float]:
    """在候选文件集合上构建局部符号引用图并计算归一化 PageRank（[0, 1]）。"""
    unique: list[str] = []
    seen: set[str] = set()
    for fp in file_paths:
        if fp not in seen:
            seen.add(fp)
            unique.append(fp)
            if len(unique) >= _MAX_FILES:
                break
    n = len(unique)
    if n < 2:
        return {fp: 0.0 for fp in unique}

    defs_map: dict[str, frozenset[str]] = {}
    refs_map: dict[str, frozenset[str]] = {}
    sym_owners: dict[str, list[str]] = {}
    for fp in unique:
        d, r = _extract_file_symbols(fp)
        defs_map[fp] = d
        refs_map[fp] = r
        for sym in d:
            sym_owners.setdefault(sym, []).append(fp)

    # 构建有向边 A -> B：A 引用了 B 定义的符号/模块名
    out_edges: dict[str, list[str]] = {fp: [] for fp in unique}
    has_any_edge = False
    for fp in unique:
        targets: set[str] = set()
        for sym in refs_map[fp]:
            owners = sym_owners.get(sym)
            if not owners or len(owners) > 4:
                continue  # 过滤过于泛化的公共词
            for owner in owners:
                if owner != fp:
                    targets.add(owner)
        if targets:
            out_edges[fp] = sorted(targets)
            has_any_edge = True

    if not has_any_edge:
        return {fp: 0.0 for fp in unique}

    pr = {fp: 1.0 / n for fp in unique}
    base = (1.0 - damping) / n
    for _ in range(iterations):
        nxt = {fp: base for fp in unique}
        dangling_sum = sum(pr[fp] for fp in unique if not out_edges[fp])
        dangling_share = damping * dangling_sum / n
        for fp in unique:
            nxt[fp] += dangling_share
        for src in unique:
            outs = out_edges[src]
            if outs:
                share = damping * pr[src] / len(outs)
                for dst in outs:
                    nxt[dst] += share
        pr = nxt

    mx = max(pr.values()) or 1.0
    mn = min(pr.values())
    span = mx - mn
    if span <= 1e-12:
        return {fp: 0.0 for fp in unique}
    return {fp: (v - mn) / span for fp, v in pr.items()}
