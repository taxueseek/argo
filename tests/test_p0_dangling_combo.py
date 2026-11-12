#!/usr/bin/env python3
"""P0 回归门：域 combo 不得引用已停用的引擎。

## 守的是什么缺陷

2026-11-11 实测：`config.yaml` 的 99 个域里有 9 处 engines_combo / primary
引用了 spec 里 `enabled: false` 的引擎。`get_engines()` 会把停用源从字典里
剔掉，运行时不报错，只是静默少一个源——`--list-engines` 全程显示 ready，
与实际参与检索的引擎集合不一致。

2026-09-19 报告点出 3 处（gleif / jikan / soilgrids），三个月后涨到 9 处。
根因是「修的人删了声明，没删 combo 引用」，且没有任何门禁拦截。

## 判据

从 config.yaml + engines/specs/*.yaml 的合并结果读 enabled，逐个域的
primary / fallback / engines_combo / combo / engines 检查：
1. 引用的引擎必须已声明（config engines 或外置 spec）；
2. 已声明的引擎不得是显式 enabled: false。

外置 spec 覆盖 config 的 enabled 时以合并结果为准（config.py 的
_merge_external_engines 语义）。故意停用的源若仍要留在 combo，必须先在
spec 里恢复 enabled 或把引用摘掉，二者选一，不允许「静默少源」。

注意：`realtime_index` 的 enabled 由 `_validate_engine_paths` 运行时按 CLI
是否存在翻转，spec 静态值可能为 true，本门只按静态声明判。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def _declared_engines() -> dict[str, dict]:
    """config engines + 外置 spec，按 config.py 的合并语义（spec 覆盖）。"""
    cfg = _load_cfg()
    declared: dict[str, dict] = {}
    for name, spec in (cfg.get("engines") or {}).items():
        if isinstance(spec, dict):
            declared[name] = dict(spec)
    for f in sorted((ROOT / "engines" / "specs").glob("*.yaml")):
        try:
            spec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(spec, dict):
            continue
        name = spec.get("engine_id") or f.stem
        base = declared.get(name, {})
        merged = dict(base)
        merged.update(spec)
        declared[name] = merged
    return declared


def _combo_refs(cfg: dict) -> dict[str, list[str]]:
    """域 → 它引用的全部引擎（primary / fallback / engines_combo / combo / engines）。"""
    out: dict[str, list[str]] = {}
    for d in cfg.get("domains") or []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or d.get("id") or "")
        refs: list[str] = []
        for key in ("primary", "fallback", "engines_combo", "combo", "engines"):
            val = d.get(key)
            if isinstance(val, str):
                refs.append(val)
            elif isinstance(val, list):
                refs.extend(x for x in val if isinstance(x, str))
        out[name] = refs
    return out


@pytest.fixture(scope="module")
def registry() -> tuple[dict, dict[str, list[str]]]:
    return _declared_engines(), _combo_refs(_load_cfg())


def test_every_combo_ref_is_declared(registry):
    """域引用的引擎必须已声明——删除声明时不能漏删 combo。"""
    declared, combos = registry
    missing: list[str] = []
    for domain, refs in combos.items():
        for ref in refs:
            if ref.startswith("local_"):
                continue  # local_* 由 local_search 聚合，不走独立声明
            if ref not in declared:
                missing.append(f"{domain} → {ref}")
    assert not missing, "域 combo 引用了未声明的引擎：\n  " + "\n  ".join(sorted(set(missing)))


def test_every_combo_ref_is_enabled(registry):
    """域引用的引擎不得是显式 enabled: false——静默少源的直接拦截。"""
    declared, combos = registry
    disabled: list[str] = []
    for domain, refs in combos.items():
        for ref in refs:
            spec = declared.get(ref)
            if spec is None:
                continue  # 由 test_every_combo_ref_is_declared 负责
            if spec.get("enabled") is False:
                disabled.append(f"{domain} → {ref}")
    assert not disabled, (
        "域 combo 引用了已停用引擎（运行时会静默少源）：\n  "
        + "\n  ".join(sorted(set(disabled)))
    )


def test_registry_is_not_empty(registry):
    """门禁自校验：注册表读空了必须失败，不能让上面两条假绿。"""
    declared, combos = registry
    assert len(declared) > 200, f"引擎注册表异常稀疏：{len(declared)}"
    assert len(combos) > 80, f"域清单异常稀疏：{len(combos)}"
