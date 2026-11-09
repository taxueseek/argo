#!/usr/bin/env python3
"""
sync_backends.py — 注册表派生与一致性校验（唯一来源：运行时合并后的引擎声明）

设计目标：新增引擎只改一处声明，其余注册表自动派生，消灭手工同步。

来源计算方式（与运行时一致）：`config.load_config()` 合并后的 engines 段，即
  config.yaml engines + engines/*.yaml + engines/specs/*.yaml
只读 config.yaml 是不够的——外置 spec 声明的引擎运行时可见、可路由，
若派生时看不见，派生件就会与运行时事实脱钩（batch7 收录的 7 个引擎
曾因此只出现在人工维护的 registry 里，而 quota/domain 两份漏侧）。

派生关系：
  运行时合并后的 engines 段（唯一来源）
    ├── backends/quota_profiles.json    配额/成本/限频（由引擎声明的元数据派生）
    ├── backends/engine_registry.yaml   引擎注册表文档（由引擎声明派生）
    ├── backends/domain_profiles.json   TF-IDF 领域文档（校验引擎名集合，缺失补空模板）
    └── backends/env_passthrough.txt    mcp_launch.sh 的密钥透传名单（声明里能读到的全部密钥名）

用法：
  python3 scripts/sync_backends.py             # 派生四份 backends 文件
  python3 scripts/sync_backends.py --check     # 只校验不写，不一致退出码非 0
  python3 scripts/sync_backends.py --list      # 输出引擎清单与统计

退出码：0=一致，1=校验发现不一致（--check 模式）。

--check 读的是磁盘上的三份派生件，与重新派生的结果逐项比对。比对对象必须是
「磁盘现状」而不是「本次派生结果」——后者等于拿结果和自己比，永远一致，
校验形同虚设（本文件 2026-09-12 前的实际状态）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from cli_io import dumps, dumps_pretty
# 派生件一律原子写（2026-11-08 审查收口）：原地 write_text 先截断后写，并发读者
# （如 mcp_launch.sh 读 env_passthrough.txt）会拿到半截文件，随后被熔断器按 auth 误禁。
from argo_paths import atomic_write_text

# ── 路径 ──────────────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
BACKENDS_DIR = SKILL_DIR / "backends"
QUOTA_PROFILES_PATH = BACKENDS_DIR / "quota_profiles.json"
DOMAIN_PROFILES_PATH = BACKENDS_DIR / "domain_profiles.json"
REGISTRY_PATH = BACKENDS_DIR / "engine_registry.yaml"
PASSTHROUGH_PATH = BACKENDS_DIR / "env_passthrough.txt"
# DSH 设置面板「密钥」页的清单：client.js 跑在浏览器里读不了仓库文件，
# 所以把清单写进 client.js 里一段由本脚本维护的标记区（与 native-tools.mjs
# 由 gen_native_tools.py 生成同一思路），面板不再手写第二份密钥名单。
DSH_CLIENT_PATH = SKILL_DIR / "packages" / "dsh-plugin" / "dsh" / "client.js"
_KEYS_BEGIN = "    // <generated:keys> 由 scripts/sync_backends.py 从引擎声明生成，勿手改\n"
_KEYS_END = "    // </generated:keys>\n"
# 面板常用组：首屏直接显示的那几把 key（其余在「显示全部」里）
# 旧面板手写名单里与候选链规则不一致的 ref（候选链里确实有这个名字，运行时
# get_env 能读到）：保留旧名，否则 DSH 凭证库里已存的 key 改名后面板显示未配置
DSH_LEGACY_REFS = {"ARGO_BYTED_API_KEY": "WEB_SEARCH_API_KEY"}
DSH_COMMON_KEYS = ("DEEPSEEK_API_KEY", "ARGO_YUANBAO_API_KEY", "ARGO_TAVILY_API_KEY",
                   "ARGO_EXA_API_KEY", "ARGO_BOCHA_API_KEY", "ARGO_BRAVE_API_KEY",
                   "ARGO_TINYFISH_API_KEY")
DSH_EN_LABELS: dict[str, str] = {
    "DEEPSEEK_API_KEY": "DeepSeek Search",
    "ARGO_YUANBAO_API_KEY": "Tencent Yuanbao",
    "TAVILY_API_KEY": "Tavily",
    "EXA_API_KEY": "Exa Semantic Search",
    "BOCHA_API_KEY": "Bocha Search",
    "BRAVE_API_KEY": "Brave Search",
    "TINYFISH_API_KEY": "TinyFish Live Search",
    "ANYSEARCH_API_KEY": "AnySearch",
    "ARGO_PIXIV_REFRESH_TOKEN": "Pixiv App OAuth",
    "EASTMONEY_APIKEY": "Eastmoney Miaoxiang",
    "FELO_API_KEY": "Felo AI Search",
    "FIRECRAWL_API_KEY": "Firecrawl",
    "GITHUB_TOKEN": "GitHub",
    "KEENABLE_API_KEY": "Keenable",
    "METASO_API_KEY": "Metaso",
    "OCTEN_API_KEY": "Octen",
    "OPENCORPORATES_API_TOKEN": "OpenCorporates",
    "PARALLEL_API_KEY": "Parallel Search",
    "QWEATHER_KEY": "QWeather",
    "SAUCENAO_API_KEY": "SauceNAO Reverse Image",
    "SCIVERSE_API_TOKEN": "Sciverse",
    "SEARXNG_URL": "SearXNG",
    "SELTZ_API_KEY": "Seltz Search",
    "SERPAPI_API_KEY": "Google Lens (SerpAPI)",
    "UNPAYWALL_EMAIL": "Unpaywall Open Access",
    "WEB_SEARCH_API_KEY": "ByteDance Search",
    "WEREAD_API_KEY": "WeRead",
    "WOLFRAM_APPID": "WolframAlpha",
    "YDC_API_KEY": "You.com Search",
    "ZHIHU_ACCESS_SECRET": "Zhihu",
}

# 不属于任何引擎声明、但 MCP 子进程也要拿到的变量：代理五件套与非引擎通道
# 的凭据（JEV 证据精排、论文 S2 限频、twitter 社交兜底）。新增此类变量只改这里。
EXTRA_PASSTHROUGH: tuple[str, ...] = (
    "ARGO_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "ARGO_JEV_API_KEY", "TYPESAFE_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "TWITTER_AUTH_TOKEN", "TWITTER_CT0",
)

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

# registry 里 cost 字段的计算方式（free/token/api），与 cost_tier 的映射
_COST_TIER_TO_REGISTRY_COST = {"free": "free", "low": "api", "api": "api", "paid": "api"}


def _load_yaml(path: Path) -> dict:
    from yaml_load import load as _yaml_parse
    data = _yaml_parse(path)
    return data if isinstance(data, dict) else {}


def load_engines() -> dict[str, dict[str, Any]]:
    """读取**公共**配置里的全部引擎声明（含禁用）。

    与运行时同一入口（config.load_config 合并 config.yaml + engines/*.yaml +
    engines/specs/*.yaml），但 **include_local=False**：派生件（engine_registry.yaml /
    quota_profiles.json）随公开仓发版，本机覆盖层 config.local.yaml 是本机状态，
    不该进来——否则本机跑一次 sync 就把「parallel 已停用」这类本机状态写进了
    公开仓。config 模块不可用时（缺依赖/语法错）退回只读 config.yaml，并在
    stderr 明示——静默退回会让派生件少一批引擎却不报错。
    """
    try:
        from config import load_config
        merged = load_config(include_local=False).get("engines", {})
        if merged:
            return {n: s for n, s in merged.items() if isinstance(s, dict)}
    except Exception as e:  # noqa: BLE001 — 退回单源并留痕，不吞错误
        print(f"⚠️  config 模块不可用（{type(e).__name__}: {e}），"
              f"退回只读 config.yaml——派生件将不含外置 spec 引擎",
              file=sys.stderr)
    cfg = _load_yaml(CONFIG_PATH)
    engines = cfg.get("engines", {})
    return {name: spec for name, spec in engines.items() if isinstance(spec, dict)}


def engine_meta(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """提取引擎声明的运营元数据，缺失用默认值保底。"""
    meta: dict[str, Any] = {}
    for key, default in DEFAULT_QUOTA.items():
        meta[key] = spec.get(key, default)
    meta["label"] = spec.get("label", name)
    return meta


# ── 派生：quota_profiles.json ────────────────────────────────────────────────

def derive_quota_profiles(engines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profiles: dict[str, Any] = {
        "_description": "各引擎的配额/成本/限频配置。由 scripts/sync_backends.py 从运行时引擎声明派生，勿手工修改。",
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
        # tier 计算方式：T1 直连 API / T2 local-search 本地引擎
        tier = "T2" if name.startswith("local_") else "T1"
        # desc 优先级：声明自述 → label 保底。spec 侧 desc 是权威自述（外置 spec
        # 只声明 engine_id/desc，没有 label），此前只认 label 导致 14 个引擎
        # 的 desc 派生为空——外置声明的引擎在注册表里集体失语。
        label = spec.get("label")
        desc = spec.get("desc") or (f"{label}（{etype}）".replace("（cli）", "") if label else "")
        entries.append({
            "name": name,
            "tier": tier,
            "type": etype,
            "coverage": spec.get("coverage", ["general"]),
            "latency_ms": spec.get("latency_ms", 2000),
            "cost": spec.get("cost", _COST_TIER_TO_REGISTRY_COST.get(cost_tier, "free")),
            "status": "ok" if enabled else "disabled",
            "recommended": spec.get("recommended", True),
            # explicit_only：设计上不进自动路由（需密钥的源 / 输入形态特殊），
            # 按 --engine 显式调用；可达性检查据此区分「有意显式」与「忘了接线」
            "explicit_only": bool(spec.get("explicit_only")),
            "desc": desc,
        })
    return {
        "_generated": "由 scripts/sync_backends.py 从运行时引擎声明派生，勿手工修改。",
        "version": 2,
        # 曾写 date.today()：同一份真源隔天重生成就产出一处无语义 diff，
        # 而 collect_issues 又刻意跳过这个字段（消费者也从不读它），于是
        # 「派生件可手改 + 生成器不确定」叠加出反复的「合并后重生成」提交。
        # 派生件必须是真源的纯函数，不含墙钟。
        "engines": entries,
    }


# ── 派生：env_passthrough.txt（mcp_launch.sh 的 launchctl 透传名单）──────────

def derive_passthrough(engines: dict[str, dict[str, Any]]) -> list[str]:
    """引擎声明能读到的全部密钥名（含 ARGO_ 与裸名两套写法）+ EXTRA_PASSTHROUGH。

    此前名单在 mcp_launch.sh 里手写，测试只对 KNOWN_ENV_ALIASES 校验，
    于是走 required_env / 占位符声明的 12 个引擎（tinyfish、deepseek_search、
    saucenao 等）从未进名单；pixiv 入别名表后也要单独补一笔提交。改为派生后，
    新引擎声明了密钥就自动透传。
    """
    import engine_env as E

    def chain(name: str) -> list[str]:
        return list(E.PLACEHOLDER_ALIASES.get(name) or E._name_variants(name))

    names: set[str] = set(EXTRA_PASSTHROUGH)
    for aliases in E.KNOWN_ENV_ALIASES.values():
        names.update(aliases)
    for aliases in E.PLACEHOLDER_ALIASES.values():
        names.update(aliases)
    for name, spec in engines.items():
        explicit = spec.get("required_env") or []
        for item in explicit:
            if isinstance(item, list):
                names.update(x for x in item if x)
            elif isinstance(item, str) and item:
                names.update(chain(item))
        for p in E._placeholders_in_spec(spec):
            names.update(chain(p))
    return sorted(n for n in names if n)


def render_passthrough(names: list[str]) -> str:
    return ("# 由 scripts/sync_backends.py 从引擎声明派生，勿手工修改。\n"
            "# scripts/mcp_launch.sh 读取本文件，把这些变量从 launchctl 透传给 MCP 子进程。\n"
            + "\n".join(names) + "\n")


def read_passthrough() -> list[str]:
    if not PASSTHROUGH_PATH.exists():
        return []
    return [ln.strip() for ln in PASSTHROUGH_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


# ── 派生：DSH 面板密钥清单（client.js 标记区）──────────────────────────────

def derive_dsh_keys(engines: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """每个引擎声明的主密钥名一行；面板 ref 用宿主凭证库里的名字。

    ref 规则：带 ARGO_ 前缀且候选链第二位是裸名的，面板沿用裸名（DSH 用户
    此前就按裸名在凭证库存过 key，改名会让已存的 key 失联）；其余照用主名。
    标签取声明里的 label（多个引擎共用一把 key 时取第一个启用的）。
    """
    import engine_env as E
    groups: dict[str, dict[str, Any]] = {}
    for name, spec in sorted(engines.items()):
        reqs = list(E.required_env_for(name, spec) or [])
        if not reqs and name in E.OPTIONAL_ENV_ENGINES:
            reqs = [E.KNOWN_ENV_ALIASES[name][0]]
        for primary in reqs:
            g = groups.setdefault(primary, {"primary": primary, "engines": [], "label": "", "enabled": False})
            g["engines"].append(name)
            on = bool(spec.get("enabled", True))
            if not g["label"] or (on and not g["enabled"]):
                g["label"], g["enabled"] = spec.get("label") or name, on
    out = []
    for primary, g in sorted(groups.items()):
        chain = E.KNOWN_ENV_ALIASES.get(g["engines"][0]) or list(
            E.PLACEHOLDER_ALIASES.get(primary) or E._name_variants(primary))
        ref = primary
        if primary.startswith("ARGO_") and len(chain) > 1 and chain[1] == primary[5:]:
            ref = primary[5:]
        # 用户已按旧面板名存进凭证库的 key：沿用旧名，避免改名后失联
        ref = DSH_LEGACY_REFS.get(primary, ref)
        en = DSH_EN_LABELS.get(ref) or g["engines"][0].replace("_", " ").title()
        out.append({"ref": ref, "common": primary in DSH_COMMON_KEYS,
                    "label": g["label"], "en": en, "engines": g["engines"]})
    out.sort(key=lambda r: (not r["common"],
                            DSH_COMMON_KEYS.index(next(p for p in DSH_COMMON_KEYS
                                                       if p in (r["ref"], "ARGO_" + r["ref"])))
                            if r["common"] else 0, r["ref"]))
    return out


def render_dsh_keys(rows: list[dict[str, Any]]) -> str:
    body = "".join(
        "      { ref: %s, common: %s, label: %s, en: %s, engines: %s },\n" % (
            json.dumps(r["ref"]), "true" if r["common"] else "false",
            json.dumps(r["label"], ensure_ascii=False),
            json.dumps(r["en"], ensure_ascii=False),
            json.dumps(r["engines"]))
        for r in rows)
    return _KEYS_BEGIN + "    const KEYS = [\n" + body + "    ];\n" + _KEYS_END


def read_dsh_keys_block() -> str:
    if not DSH_CLIENT_PATH.exists():
        return ""
    s = DSH_CLIENT_PATH.read_text(encoding="utf-8")
    i, j = s.find(_KEYS_BEGIN), s.find(_KEYS_END)
    return s[i:j + len(_KEYS_END)] if i >= 0 and j > i else ""


def write_dsh_keys_block(block: str) -> None:
    s = DSH_CLIENT_PATH.read_text(encoding="utf-8")
    i, j = s.find(_KEYS_BEGIN), s.find(_KEYS_END)
    if i < 0 or j < i:
        raise SystemExit(f"{DSH_CLIENT_PATH} 缺 <generated:keys> 标记区")
    # 整文件原子替换（保持 0644）：再生成时并发读者（DSH 插件运行时）不该看到
    # 半截文件——与仓内其他派生件同一纪律（2026-11-08 审查收口）。
    atomic_write_text(DSH_CLIENT_PATH, s[:i] + block + s[j + len(_KEYS_END):], mode=0o644)


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
                   domain: dict[str, Any],
                   passthrough: list[str] | None = None) -> list[str]:
    """汇总所有一致性检查问题（引擎名 + 字段值双层）。

    只比引擎名是不够的：名字都在、值被手改（firecrawl.qps 1→99）同样会让
    派生件与来源脱钩，而限流/配额恰恰是靠这些值生效的。第一版校验只比名字，
    把 qps 手改成 99 试一次，它直接漏报。
    """
    issues = []
    config_names = set(engines.keys())
    quota_names = set(k for k in quota if not k.startswith("_"))
    reg_entries = {e["name"]: e for e in registry.get("engines", [])}
    reg_names = set(reg_entries)

    if missing := sorted(config_names - quota_names):
        issues.append(f"quota_profiles 缺失引擎: {missing}")
    if stale := sorted(quota_names - config_names):
        issues.append(f"quota_profiles 含 config 已不存在的引擎: {stale}")
    if missing := sorted(config_names - reg_names):
        issues.append(f"engine_registry 缺失引擎: {missing}")
    if stale := sorted(reg_names - config_names):
        issues.append(f"engine_registry 含 config 已不存在的引擎: {stale}")

    # quota 值级一致性：声明的运营元数据必须逐字段落到派生件上
    expected_quota = derive_quota_profiles(engines)
    for name in sorted(config_names & quota_names):
        exp = expected_quota.get(name, {})
        cur = quota.get(name, {})
        if not isinstance(cur, dict):
            issues.append(f"quota_profiles {name} 不是对象: {type(cur).__name__}")
            continue
        for key, want in exp.items():
            if cur.get(key) != want:
                issues.append(
                    f"quota_profiles {name}.{key} 不一致: 真源={want!r} 派生件={cur.get(key)!r}")

    # registry 值级一致性（跳过生成时间戳：它不是声明派生的）
    expected_reg = {e["name"]: e for e in derive_registry(engines)["engines"]}
    for name in sorted(config_names & reg_names):
        exp = expected_reg.get(name, {})
        cur = reg_entries.get(name, {})
        for key in ("tier", "type", "coverage", "latency_ms", "cost",
                    "status", "recommended", "explicit_only", "desc"):
            if exp.get(key) != cur.get(key):
                issues.append(
                    f"engine_registry {name}.{key} 不一致: 真源={exp.get(key)!r} "
                    f"派生件={cur.get(key)!r}")

    issues.extend(check_domain_profiles(engines, domain))

    # passthrough 是可选参数：调用方不传就读磁盘现状，保证测试与 --check 同口径
    cur_pt = read_passthrough() if passthrough is None else passthrough
    want_pt = derive_passthrough(engines)
    if cur_pt != want_pt:
        miss = sorted(set(want_pt) - set(cur_pt))
        extra = sorted(set(cur_pt) - set(want_pt))
        issues.append(f"env_passthrough.txt 与声明不一致: 缺 {miss} 多 {extra}")
    if DSH_CLIENT_PATH.exists() and read_dsh_keys_block() != render_dsh_keys(derive_dsh_keys(engines)):
        issues.append("packages/dsh-plugin/dsh/client.js 的 KEYS 标记区与引擎声明不一致")
    return issues


def write_quota(quota: dict[str, Any]) -> None:
    atomic_write_text(QUOTA_PROFILES_PATH, dumps_pretty(quota) + "\n", mode=0o644)


def write_registry(registry: dict[str, Any]) -> None:
    import yaml
    atomic_write_text(
        REGISTRY_PATH,
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False, default_flow_style=False),
        mode=0o644)


def write_domain(domain: dict[str, Any]) -> None:
    atomic_write_text(DOMAIN_PROFILES_PATH, dumps_pretty(domain) + "\n", mode=0o644)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="注册表派生与一致性校验（真源：运行时合并后的引擎声明）")
    parser.add_argument("--check", action="store_true", help="只校验不写，不一致退出码 1")
    parser.add_argument("--list", action="store_true", help="输出引擎清单与统计")
    args = parser.parse_args()

    engines = load_engines()
    if args.list:
        tiers: dict[str, list[str]] = {}
        for n, s in engines.items():
            tiers.setdefault(s.get("cost_tier", "free"), []).append(n)
        # 计算方式收紧（D7）：默认数字只有一个——「声明合并后的引擎数」。
        # enabled 与 disabled 由同一入口的 env 就绪判定派生，不再各自成数。
        declared = len(engines)
        try:
            from config import get_engines
            ready_names = set(get_engines() or {})
        except Exception:
            ready_names = {n for n, s in engines.items() if s.get("enabled", True)}
        enabled_declared = {n for n, s in engines.items() if s.get("enabled", True)}
        print(dumps({
            # 默认计算方式：运行时可见的引擎声明总数
            "total": declared,
            "enabled": len(ready_names),
            "disabled": declared - len(ready_names),
            "enabled_declared": len(enabled_declared),
            "by_cost_tier": {k: len(v) for k, v in tiers.items()},
            "engines": sorted(engines),
        }))
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
    # registry 现状也要读：校验必须拿「磁盘上的派生件」比对，而不是拿本次
    # 派生结果比对——后者是拿结果跟自己比，恒等，校验永远绿。
    registry_cur: dict[str, Any] = {}
    if REGISTRY_PATH.exists():
        registry_cur = _load_yaml(REGISTRY_PATH)

    quota_new = derive_quota_profiles(engines)
    registry_new = derive_registry(engines)
    domain_new = patch_domain_profiles(engines, domain_cur)

    # 校验对象是「磁盘现状 vs 来源」，与本次派生了什么无关——派生结果在这里
    # 只用于写入，不用于自行验证。
    issues = collect_issues(engines, quota_cur, registry_cur, domain_cur)

    if args.check:
        if issues:
            print("❌ 一致性校验失败：", file=sys.stderr)
            for issue in issues:
                print(f"  - {issue}", file=sys.stderr)
            print("  修复：python3 scripts/sync_backends.py", file=sys.stderr)
            return 1
        print(f"✅ 一致性校验通过：{len(engines)} 个引擎，"
              f"三份派生件与运行时声明一致。")
        return 0

    write_quota(quota_new)
    write_registry(registry_new)
    write_domain(domain_new)
    pt_new = derive_passthrough(engines)
    atomic_write_text(PASSTHROUGH_PATH, render_passthrough(pt_new), mode=0o644)
    print(f"已派生 {len(engines)} 个引擎的注册表：")
    print(f"  env_passthrough.txt   {len(pt_new)} 个变量名")
    if DSH_CLIENT_PATH.exists():
        keys_new = derive_dsh_keys(engines)
        write_dsh_keys_block(render_dsh_keys(keys_new))
        print(f"  dsh client.js KEYS    {len(keys_new)} 把 key")
    print(f"  quota_profiles.json   {len(quota_new) - 1} 条")
    print(f"  engine_registry.yaml  {len(registry_new['engines'])} 条")
    print(f"  domain_profiles.json  {len([k for k in domain_new if not k.startswith('_')])} 条（缺失已补空模板）")
    try:
        import gen_engine_catalog
        gen_engine_catalog.DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(gen_engine_catalog.DOC_PATH, gen_engine_catalog.render(), mode=0o644)
        gen_engine_catalog.sync_counts(write=True)
        print("  ENGINE_CATALOG.md / counts.json 已同步")
    except Exception as exc:
        print(f"  ⚠️  gen_engine_catalog 同步跳过: {exc}", file=sys.stderr)
    if issues:
        print(f"  已修复 {len(issues)} 处此前与真源不一致的项：", file=sys.stderr)
        for issue in issues:
            print(f"  ⚠️  {issue}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
