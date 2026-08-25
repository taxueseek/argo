#!/usr/bin/env python3
"""research_work_packages.py — Agent 工作包交接。

工作包是可验证问题，不是扩词。depends_on 决定阶段；同阶段才并行。
file_inputs 声明本地一手数据文件（白名单制：工作包显式声明才可入账）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 允许入账的本地文件类型（kind 未给时按扩展名推断）
_ALLOWED_KINDS = frozenset({
    "csv", "tsv", "xlsx", "xls", "parquet", "json", "md", "txt", "pdf",
})
_EXT_KIND = {
    ".csv": "csv", ".tsv": "tsv", ".xlsx": "xlsx", ".xls": "xls",
    ".parquet": "parquet", ".json": "json", ".md": "md", ".txt": "txt",
    ".pdf": "pdf",
}


def normalize_recompute(raw: Any) -> dict[str, Any] | None:
    """校验工作包 recompute 契约：{script 必填, budget 可选}。

    不在此处执行（fail-closed 由 recompute.run_recompute 授权门负责）。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("recompute 须为对象")
    script = str(raw.get("script") or "").strip()
    if not script:
        raise ValueError("recompute 缺少 script")
    budget = raw.get("budget") or {}
    if not isinstance(budget, dict):
        raise ValueError("recompute.budget 须为对象")
    return {
        "script": script,
        "expect": str(raw.get("expect") or "").strip(),
        "budget": {
            "timeout_s": max(5, min(int(budget.get("timeout_s") or 30), 300)),
            "max_mem_mb": max(64, min(int(budget.get("max_mem_mb") or 512), 4096)),
        },
    }


def normalize_file_inputs(items: Any) -> list[dict[str, Any]]:
    """校验并规范化工作包的 file_inputs（fail-closed）。

    白名单 = 工作包显式声明：每条必须有非空 path；文件必须存在、
    是普通文件、可读；kind 未给时按扩展名推断，不在白名单扩展名内拒绝。
    输出：{path(绝对规范路径), kind, role, size}。
    """
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("file_inputs 须为数组")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"file_inputs[{i}] 须为对象")
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            raise ValueError(f"file_inputs[{i}] 缺少 path")
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"file_inputs[{i}] 文件不存在: {raw_path}")
        if not path.is_file():
            raise ValueError(f"file_inputs[{i}] 不是普通文件: {raw_path}")
        if not os.access(path, os.R_OK):
            raise ValueError(f"file_inputs[{i}] 不可读: {raw_path}")
        kind = str(item.get("kind") or "").strip().lower()
        if not kind:
            kind = _EXT_KIND.get(path.suffix.lower(), "")
        if kind not in _ALLOWED_KINDS:
            raise ValueError(
                f"file_inputs[{i}] 类型不受支持: {path.suffix or kind}"
                f"（允许: {', '.join(sorted(_ALLOWED_KINDS))}）"
            )
        role = str(item.get("role") or "data").strip()
        out.append({
            "path": str(path),
            "kind": kind,
            "role": role,
            "size": path.stat().st_size,
        })
    return out


def parse_work_packages(raw: Any) -> list[dict[str, Any]]:
    """接受 list[dict]、JSON 数组字符串或 JSON 文件内容。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text[0] not in "[{":
            raise ValueError("work_packages 须为 JSON 数组或对象")
        raw = json.loads(text)
    if isinstance(raw, dict):
        raw = raw.get("work_packages") or raw.get("packages") or [raw]
    if not isinstance(raw, list):
        raise ValueError("work_packages 须为数组")
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"work_packages[{i}] 须为对象")
        question = str(item.get("question") or item.get("query") or "").strip()
        if not question:
            raise ValueError(f"work_packages[{i}] 缺少 question")
        pid = str(item.get("id") or f"wp-{i + 1}").strip()
        if pid in seen:
            raise ValueError(f"重复工作包 id: {pid}")
        seen.add(pid)
        deps_raw = item.get("depends_on") or item.get("dependsOn") or []
        if isinstance(deps_raw, str):
            deps = [d.strip() for d in deps_raw.split(",") if d.strip()]
        elif isinstance(deps_raw, list):
            deps = [str(d).strip() for d in deps_raw if str(d).strip()]
        else:
            deps = []
        packages.append({
            "id": pid,
            "question": question,
            "query": str(item.get("query") or question).strip(),
            "intent": str(item.get("intent") or question[:40]).strip(),
            "priority_sources": [
                str(s) for s in (item.get("priority_sources") or []) if s
            ],
            "depends_on": deps,
            "file_inputs": normalize_file_inputs(item.get("file_inputs")),
            "recompute": normalize_recompute(item.get("recompute")),
        })
    return packages


def stage_work_packages(
    packages: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """按 depends_on 分层。成环的剩余包并入最后一阶段并记 warning。"""
    by_id = {p["id"]: p for p in packages}
    remaining = set(by_id)
    stages: list[list[dict[str, Any]]] = []
    warnings: list[str] = []
    known = set(by_id)
    for p in packages:
        missing = [d for d in p["depends_on"] if d not in known]
        if missing:
            warnings.append(f"{p['id']} 依赖不存在: {', '.join(missing)}")

    while remaining:
        ready = []
        for pid in list(remaining):
            deps = [d for d in by_id[pid]["depends_on"] if d in known]
            if all(d not in remaining for d in deps):
                ready.append(by_id[pid])
        if not ready:
            leftover = [by_id[pid] for pid in sorted(remaining)]
            warnings.append(
                "工作包依赖成环，剩余包并入末阶段: "
                + ", ".join(p["id"] for p in leftover)
            )
            stages.append(leftover)
            break
        ready.sort(key=lambda p: p["id"])
        stages.append(ready)
        for p in ready:
            remaining.discard(p["id"])
    return stages, warnings


def packages_to_sub_queries(
    packages: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """转成 collect_sources 的输入形状。"""
    out: list[dict[str, str]] = []
    for p in packages:
        sq: dict[str, str] = {
            "query": str(p.get("query") or p.get("question") or ""),
            "intent": p.get("intent") or "",
            "strategy": "work_package",
            "package_id": p.get("id") or "",
        }
        prefs = p.get("priority_sources") or []
        if prefs:
            sq["preferred_engine"] = str(prefs[0])
            sq["preferred_engines"] = [str(s) for s in prefs if s]
        out.append(sq)
    return out
