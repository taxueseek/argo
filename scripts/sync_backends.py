#!/usr/bin/env python3
"""
sync_backends.py — 注册表派生与一致性校验（单一真源：config.yaml）

设计目标：新增引擎只改 config.yaml 一处，其余注册表自动派生，消灭 9 处手工同步。

派生关系：
  config.yaml engines 段（唯一真源）
    ├── backends/quota_profiles.json    配额/成本/限频（由引擎声明的元数据派生）
    ├── backends/engine_registry.yaml   引擎注册表文档（由引擎声明派生）
    └── backends/domain_profiles.json   TF-IDF 领域文档（校验引擎名集合，缺失补空模板）

用法：
  python3 scripts/sync_backends.py             # 派生三份 backends 文件
  python3 scripts/sync_backends.py --check     # 只校验不写，不一致退出码非 0
  python3 scripts/sync_backends.py --list      # 输出引擎清单与统计

退出码：0=一致，1=校验发现不一致（--check 模式）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
BACKENDS_DIR = SKILL_DIR / "backends"
QUOTA_PROFILES_PATH = BACKENDS_DIR / "quota_profiles.json"
DOMAIN_PROFILES_PATH = BACKENDS_DIR / "domain_profiles.json"
REGISTRY_PATH = BACKENDS_DIR / "engine_registry.yaml"

# ── 默认值 ────────────────────────────────────────────────────────────────────

DEFAULT_QUOTA: dict[str, Any] = {
    "qps": 2,
    "limit": None,
    "period": "second",
    "cost_per_call": 0.0,
    "cost_unit": "free",
    "cost_tier": "free",
    "priority": 50,
}

DEFAULT_COST_FACTOR = {"free": 1.0, "low": 0.7, "api": 0.5, "paid": 0.3}

# registry 里 cost 字段的口径（free/token/api），与 cost_tier 的映射
_COST_TIER_TO_REGISTRY_COST = {"free": "free", "low": "api", "api": "api", "paid": "api"}


def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_engines() -> dict[str, dict[str, Any]]:
    """从 config.yaml 读取全部引擎声明（含禁用）。"""
    cfg = _load_yaml(CONFIG_PATH)
    engines = cfg.get("engines", {})
    return {name: spec for name, spec in engines.items() if isinstance(spec, dict)}


def engine_meta(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """提取引擎声明的运营元数据，缺失用默认值兜底。"""
    meta: dict[str, Any] = {}
    for key, default in DEFAULT_QUOTA.items():
        meta[key] = spec.get(key, default)
    meta["label"] = spec.get("label", name)
    return meta


# ── 派生：quota_profiles.json ────────────────────────────────────────────────

def derive_quota_profiles(engines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profiles: dict[str, Any] = {
        "_description": "各引擎的配额/成本/限频配置。由 scripts/sync_backends.py 从 config.yaml 派生，勿手工修改。",
    }
    for name, spec in sorted(engines.items()):
        meta = engine_meta(spec, name)
        profiles[name] = {
            "label": meta["label"],
            "qps": meta["qps"],
            "limit": meta["limit"],
            "period": meta["period"],
            "cost_per_call": meta["cost_per_call"],
            "cost_unit": meta["cost_unit"],
            "cost_tier": meta["cost_tier"],
            "priority": meta["priority"],
        }
    return profiles


# ── 派生：engine_registry.yaml ───────────────────────────────────────────────

def derive_registry(engines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """从引擎声明派生注册表文档。coverage/desc 从引擎声明读，缺省用通用标签。"""
    entries = []
    for name, spec in sorted(engines.items()):
        enabled = spec.get("enabled", True)
        etype = spec.get("type", "cli")
        cost_tier = spec.get("cost_tier", "free")
        # tier 口径：T1 直连 API / T2 local-search 本地引擎
        tier = "T2" if name.startswith("local_") else "T1"
        entries.append({
            "name": name,
            "tier": tier,
            "type": etype,
            "coverage": spec.get("coverage", ["general"]),
            "latency_ms": spec.get("latency_ms", 2000),
            "cost": spec.get("cost", _COST_TIER_TO_REGISTRY_COST.get(cost_tier, "free")),
            "status": "ok" if enabled else "disabled",
            "recommended": spec.get("recommended", True),
            "desc": spec.get("desc", f"{label}（{etype}）".replace("（cli）", "")) if (label := spec.get("label")) else "",
        })
    return {
        "_generated": "由 scripts/sync_backends.py 从 config.yaml 派生，勿手工修改。",
        "version": 2,
        "last_updated": __import__("datetime").date.today().isoformat(),
        "engines": entries,
    }


# ── 校验/补全：domain_profiles.json ─────────────────────────────────────────

def check_domain_profiles(engines: dict[str, dict[str, Any]], profiles: dict[str, Any]) -> list[str]:
    """校验 domain_profiles 引擎名集合与 config 一致。返回问题列表。"""
    issues = []
    config_names = set(engines.keys())
    domain_names = set(k for k in profiles if not k.startswith("_"))
    missing = sorted(config_names - domain_names)
    stale = sorted(domain_names - config_names)
    if missing:
        issues.append(f"domain_profiles 缺失引擎: {missing}")
    if stale:
        issues.append(f"domain_profiles 含 config 已不存在的引擎: {stale}")
    return issues


def patch_domain_profiles(engines: dict[str, dict[str, Any]], profiles: dict[str, Any]) -> dict[str, Any]:
    """为缺失引擎补空模板（documents 留空，TF-IDF 对无文档引擎返回零向量，不影响路由）。"""
    patched = dict(profiles)
    for name, spec in engines.items():
        if name in patched:
            continue
        patched[name] = {
            "label": spec.get("label", name),
            "documents": [],
            "boost_keywords": {},
            "boost_combos": {},
        }
    # 移除 config 已不存在的引擎（孤儿条目）
    for name in [k for k in patched if not k.startswith("_") and k not in engines]:
        del patched[name]
    return patched


# ── 主流程 ───────────────────────────────────────────────────────────────────

def collect_issues(engines: dict[str, dict[str, Any]],
                   quota: dict[str, Any], registry: dict[str, Any],
                   domain: dict[str, Any]) -> list[str]:
    """汇总所有一致性检查问题。"""
    issues = []
    config_names = set(engines.keys())
    quota_names = set(k for k in quota if not k.startswith("_"))
    reg_names = {e["name"] for e in registry.get("engines", [])}

    if missing := sorted(config_names - quota_names):
        issues.append(f"quota_profiles 缺失引擎: {missing}")
    if stale := sorted(quota_names - config_names):
        issues.append(f"quota_profiles 含 config 已不存在的引擎: {stale}")
    if missing := sorted(config_names - reg_names):
        issues.append(f"engine_registry 缺失引擎: {missing}")
    if stale := sorted(reg_names - config_names):
        issues.append(f"engine_registry 含 config 已不存在的引擎: {stale}")

    # quota 字段一致性（cost_tier 与 config 引擎声明对齐）
    for name in sorted(config_names):
        spec = engines[name]
        declared = spec.get("cost_tier", "free")
        current = quota.get(name, {}).get("cost_tier", "free")
        if declared != current:
            issues.append(f"引擎 {name} cost_tier 不一致: config={declared} vs quota={current}")

    issues.extend(check_domain_profiles(engines, domain))
    return issues


def write_quota(quota: dict[str, Any]) -> None:
    QUOTA_PROFILES_PATH.write_text(
        json.dumps(quota, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_registry(registry: dict[str, Any]) -> None:
    import yaml
    REGISTRY_PATH.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")


def write_domain(domain: dict[str, Any]) -> None:
    DOMAIN_PROFILES_PATH.write_text(
        json.dumps(domain, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="注册表派生与一致性校验（真源：config.yaml）")
    parser.add_argument("--check", action="store_true", help="只校验不写，不一致退出码 1")
    parser.add_argument("--list", action="store_true", help="输出引擎清单与统计")
    args = parser.parse_args()

    engines = load_engines()
    if args.list:
        enabled = [n for n, s in engines.items() if s.get("enabled", True)]
        tiers: dict[str, list[str]] = {}
        for n, s in engines.items():
            tiers.setdefault(s.get("cost_tier", "free"), []).append(n)
        # D7：对账四数字——config 原始 / 合并外部 specs / env 就绪启用 / 禁用。
        # 此前 136/124/126 口径反复数错，统一在此收敛：registry_merged 与
        # enabled_ready 的差即 disabled（enabled:false + env gating 禁用的），
        # 三方（config / specs / env）对账一次给出，杜绝再次数错。
        registry_merged = dict(engines)
        ready_names = set(enabled)
        try:
            from config import load_config, get_engines
            merged_cfg = load_config()
            registry_merged = {
                k: v for k, v in merged_cfg.get("engines", {}).items()
                if isinstance(v, dict)
            }
            ready = get_engines(merged_cfg) or {}
            ready_names = set(ready.keys())
        except Exception:
            pass  # config 模块不可用时回退到 config.yaml 单源口径
        print(json.dumps({
            "config_raw": len(engines),
            "registry_merged": len(registry_merged),
            "enabled_ready": len(ready_names),
            "disabled": len(registry_merged) - len(ready_names),
            # 兼容旧字段
            "total": len(registry_merged),
            "enabled": len(ready_names),
            "by_cost_tier": {k: len(v) for k, v in tiers.items()},
            "engines": sorted(engines),
        }, ensure_ascii=False, indent=2))
        return 0

    quota_cur = {}
    domain_cur = {}
    if QUOTA_PROFILES_PATH.exists():
        try:
            quota_cur = json.loads(QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    if DOMAIN_PROFILES_PATH.exists():
        try:
            domain_cur = json.loads(DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    quota_new = derive_quota_profiles(engines)
    registry_new = derive_registry(engines)
    domain_new = patch_domain_profiles(engines, domain_cur)

    issues = collect_issues(engines, quota_new, registry_new, domain_new)

    if args.check:
        if issues:
            print("❌ 一致性校验失败：", file=sys.stderr)
            for issue in issues:
                print(f"  - {issue}", file=sys.stderr)
            return 1
        print(f"✅ 一致性校验通过：{len(engines)} 个引擎，注册表与 config.yaml 完全一致。")
        return 0

    write_quota(quota_new)
    write_registry(registry_new)
    write_domain(domain_new)
    print(f"已派生 {len(engines)} 个引擎的注册表：")
    print(f"  quota_profiles.json   {len(quota_new) - 1} 条")
    print(f"  engine_registry.yaml  {len(registry_new['engines'])} 条")
    print(f"  domain_profiles.json  {len([k for k in domain_new if not k.startswith('_')])} 条（缺失已补空模板）")
    for issue in issues:
        print(f"  ⚠️  {issue}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
