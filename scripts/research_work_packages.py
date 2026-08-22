#!/usr/bin/env python3
"""research_work_packages.py — Agent 工作包交接。

工作包是可验证问题，不是扩词。depends_on 决定阶段；同阶段才并行。
"""

from __future__ import annotations

import json
from typing import Any


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
