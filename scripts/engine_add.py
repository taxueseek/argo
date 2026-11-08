#!/usr/bin/env python3
"""engine_add.py — `argo engine add`：新引擎一键接入（自适应 + 快速）。

## 为什么需要它

v2.9.8 已有声明式 spec（`engines/specs/*.yaml`）、`engine_validate`、
`engine_admission`、`engine_requires`、`adaptive`，但接入一个新引擎仍需人工多步：

    写 spec → 设 env → engine_validate → sync_backends
    → 改 config.yaml 的域 combo → 补 engine_families 白名单 → 补 domain_profiles 语料

本命令把这条链编排成一步，并**自动派生**路由所需的元数据（family / coverage /
langs / domain_hints），让新引擎「接入即可路由、自适应找域」。

## 三档用法

    # 1) 已有 spec 文件：安装 + 验证 + 准入
    argo engine add --from-yaml engines/specs/myengine.yaml --admit

    # 2) 只有 URL：生成 L1 模板 → 探测 → 派生 → 验证（交互式补全 headers/output_map）
    argo engine add --from-url https://api.example.com/v1/search --demo "test query"

    # 3) 已声明但没接入路由：只补元数据 + 验证 + 准入
    argo engine add --engine myengine --admit

## 派生规则（保守，宁缺勿假）

    family       ← 探测结果字段 + desc 关键词，映射到既有能力族；判不准留空（默认 web_general）
    coverage     ← desc / 探测结果主题，映射到既有域的 coverage 标签
    langs        ← 探测查询的语言 + 引擎语言参数（setlang/hl）推断
    domain_hints ← TF-IDF 语料最近邻域（top-3），供路由先验与探索 boost

## 幂等与安全

    - spec 已存在时默认跳过写入（--overwrite 覆盖）
    - 探测失败不阻断，如实报告（fail-open）
    - --dry-run 只打印计划
    - 写 spec / backends / admission 前都校验 YAML/JSON 可解析
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from cli_io import dumps  # noqa: E402

SPECS_DIR = ROOT / "engines" / "specs"
CONFIG_PATH = ROOT / "config.yaml"
DOMAIN_PROFILES_PATH = ROOT / "backends" / "domain_profiles.json"

# 探测用的通用 canary（垂类引擎用更贴的词，见 probe_engine）
DEFAULT_CANARY = "latest news"

# desc/探测结果 → 能力族 的关键词映射（保守：只映射高置信词）
_FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web_general": ("网页", "搜索", "web", "search", "资讯", "新闻", "网页搜索"),
    "academic": ("论文", "学术", "paper", "scholar", "期刊", "文献", "arxiv", "引用"),
    "code": ("代码", "包", "库", "code", "github", "npm", "pypi", "api", "文档", "sdk"),
    "finance_market": ("股票", "股价", "行情", "基金", "期货", "外汇", "quote", "market", "stock"),
    "finance_macro": ("gdp", "cpi", "宏观", "经济", "通胀", "利率", "失业率", "macro", "经济数据"),
    "news_flash": ("快讯", "电报", "flash", "实时", "突发", "头条"),
    "social": ("社区", "知乎", "微博", "reddit", "twitter", "小红书", "b站", "论坛", "舆情"),
    "knowledge": ("百科", "知识", "维基", "wikipedia", "实体", "词典", "wikidata"),
    "media_book": ("电影", "图书", "音乐", "影视", "imdb", "itunes", "专辑", "书籍", "movie"),
    "sports": ("体育", "足球", "篮球", "比赛", "sports", "nba", "赛事"),
    "science_chem": ("化学", "分子", "化合物", "chem", "pharma", "药物"),
    "science_bio": ("生物", "蛋白", "基因", "物种", "bio", "protein"),
    "science_geo": ("地理", "地图", "地点", "geo", "地点位置", "poi"),
    "legal": ("法律", "判例", "法规", "legal", "law", "案例"),
    "archive": ("归档", "历史", "archive", "快照", "wayback"),
}

# desc/探测结果 → 域 coverage 标签
_COVERAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "general": ("通用", "general", "网页", "搜索"),
    "tech": ("技术", "tech", "开发", "编程"),
    "finance": ("金融", "finance", "财经"),
    "stock": ("股票", "stock", "股价"),
    "fund": ("基金", "fund"),
    "academic": ("学术", "academic", "论文"),
    "film": ("电影", "film", "影视"),
    "movie": ("电影", "movie"),
    "tv": ("剧集", "tv"),
    "media": ("媒体", "media"),
    "news": ("新闻", "news"),
    "code": ("代码", "code"),
    "sports": ("体育", "sports"),
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ── spec 读写 ────────────────────────────────────────────────────────────────

def load_spec_file(path: Path) -> dict[str, Any]:
    # 统一走 yaml_load（loader 选择单一真源，见 tests/test_yaml_loader_centralized）
    from yaml_load import load as _yaml_load
    data = _yaml_load(path) or {}
    if not isinstance(data, dict):
        raise ValueError(f"spec 不是 mapping: {path}")
    return data


def spec_engine_id(spec: dict[str, Any], fallback: str = "") -> str:
    return str(spec.get("engine_id") or spec.get("id") or fallback).strip()


def write_spec(spec: dict[str, Any], *, overwrite: bool = False) -> tuple[Path, bool]:
    """把 spec 写入 engines/specs/<id>.yaml。返回 (路径, 是否新写)。"""
    import yaml
    eid = spec_engine_id(spec)
    if not eid:
        raise ValueError("spec 缺少 engine_id")
    target = SPECS_DIR / f"{eid}.yaml"
    if target.exists() and not overwrite:
        return target, False
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {eid} — 由 `argo engine add` 接入（{time.strftime('%Y-%m-%d')}）。\n"
        f"# 字段说明见 docs/ADDING_NEW_ENGINE.md；路由元数据（family/coverage/langs/\n"
        f"# domain_hints）由 engine_add 自动派生，可手工微调。\n"
    )
    body = yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
    target.write_text(header + body, encoding="utf-8")
    return target, True


def read_domain_profiles() -> dict[str, Any]:
    if DOMAIN_PROFILES_PATH.exists():
        try:
            return json.loads(DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_domain_profiles(data: dict[str, Any]) -> None:
    DOMAIN_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DOMAIN_PROFILES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DOMAIN_PROFILES_PATH)


# ── 探测 ──────────────────────────────────────────────────────────────────────

def probe_engine(engine_id: str, *, query: str | None = None, n: int = 3,
                 timeout: float = 10.0) -> dict[str, Any]:
    """跑一次真实搜索，拿到结构化结果用于派生元数据。"""
    try:
        from engine_validate import run_health, ENGINE_CANARY_QUERIES
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"engine_validate 不可用: {type(e).__name__}"}
    canary = query or ENGINE_CANARY_QUERIES.get(engine_id) or DEFAULT_CANARY
    try:
        health = run_health(engine_id, query=canary, n=n, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"探测异常: {type(e).__name__}: {e}"}
    return {"ok": bool(health.get("ok")), "status": health.get("status"),
            "query": canary, "health": health}


def _texts_from_health(health: dict[str, Any]) -> list[str]:
    """从 health 结果里抽可派生主题的文本（title/snippet/desc）。"""
    out: list[str] = []
    results = (health or {}).get("results") or (health or {}).get("items") or []
    if isinstance(results, list):
        for r in results[:5]:
            if isinstance(r, dict):
                for k in ("title", "snippet", "summary", "description", "url", "source"):
                    v = r.get(k)
                    if isinstance(v, str) and v:
                        out.append(v)
    return out


# ── 元数据派生 ────────────────────────────────────────────────────────────────

def derive_family(spec: dict[str, Any], health: dict[str, Any]) -> str:
    """从 spec 的 family/type/desc + 探测文本推断能力族。判不准返回 ''（走默认）。"""
    declared = str(spec.get("family") or "").strip()
    if declared:
        return declared
    hay = " ".join([
        str(spec.get("desc") or ""),
        str(spec.get("label") or ""),
        str(spec.get("type") or ""),
        " ".join(_texts_from_health(health.get("health") or {})),
    ]).lower()
    best, best_hits = "", 0
    for fam, kws in _FAMILY_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw.lower() in hay)
        if hits > best_hits:
            best, best_hits = fam, hits
    return best if best_hits >= 1 else ""


def derive_coverage(spec: dict[str, Any], health: dict[str, Any]) -> list[str]:
    declared = spec.get("coverage")
    if isinstance(declared, list) and declared:
        return [str(c) for c in declared]
    hay = " ".join([
        str(spec.get("desc") or ""),
        " ".join(_texts_from_health(health.get("health") or {})),
    ]).lower()
    out: list[str] = []
    for cov, kws in _COVERAGE_KEYWORDS.items():
        if any(kw.lower() in hay for kw in kws) and cov not in out:
            out.append(cov)
    return out[:4]


def derive_langs(spec: dict[str, Any], probe_query: str | None) -> list[str]:
    """从 spec.langs / 语言参数 / 探测查询推断语言能力。"""
    declared = spec.get("langs")
    if isinstance(declared, list) and declared:
        return [str(x) for x in declared]
    out: list[str] = []
    if probe_query:
        try:
            from lang_detect import detect_language
            lg = detect_language(probe_query)
            if lg and lg != "other":
                out.append("zh" if lg == "zh" else ("en" if lg in ("en", "latin") else lg))
        except Exception:
            pass
    # 引擎语言参数（setlang/hl/market）→ 多语言
    url = str(spec.get("url") or "")
    params = {k: v for k, v in (spec.get("extra_params") or {}).items()}
    if any(k in url or k in params for k in ("setlang", "hl", "market", "language", "lang")):
        out.append("*")  # 语言中立/多语言
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen[:3] or ["*"]


def derive_domain_hints(spec: dict[str, Any], health: dict[str, Any], top: int = 3) -> list[str]:
    """找最近邻域：TF-IDF 语料最近邻 + 同能力族域，两路并集（保序）。

    为什么两路：TF-IDF 语料是「引擎文档 vs 查询」的相似度，对新引擎不可靠
    （新引擎自己的语料是刚写入的 desc，查询是探测文本，两边都稀）；
    同能力族域（哪些域的 combo 里有本族引擎）是更稳的族级先验。
    """
    try:
        from config import get_domains
        domains = get_domains()
    except Exception:
        return []
    family = str(spec.get("family") or "").strip()
    hints: list[str] = []

    # 路一：同能力族域（稳定先验）
    if family:
        try:
            from engine_families import family_of
            from config import get_engines
            specs_all = get_engines()
            fam_members: set[str] = set()
            for e in (specs_all or {}):
                if family_of(e, (specs_all or {}).get(e)) == family:
                    fam_members.add(e)
            for d in domains:
                name = d.get("name")
                if not name:
                    continue
                members = set((d.get("engines_combo") or []) + ([d.get("primary")] if d.get("primary") else []))
                if members & fam_members and name not in hints:
                    hints.append(name)
                if len(hints) >= top:
                    return hints[:top]
        except Exception:
            pass

    # 路二：TF-IDF 最近邻（引擎 → 域）
    try:
        from tfidf_router import get_router
        router = get_router()
        router._ensure_loaded()
    except Exception:
        return hints[:top]
    hay = " ".join([
        str(spec.get("desc") or ""),
        str(spec.get("label") or ""),
        " ".join(_texts_from_health(health.get("health") or {})),
    ]).strip()
    if not hay:
        return hints[:top]
    try:
        scores = router.route(hay, top_k=5)
    except Exception:
        return hints[:top]
    eng_to_domain: dict[str, list[str]] = {}
    for d in domains:
        name = d.get("name")
        if not name:
            continue
        for e in (d.get("engines_combo") or []) + ([d.get("primary")] if d.get("primary") else []):
            if e:
                eng_to_domain.setdefault(str(e), []).append(name)
    for eng, score, _ in scores:
        if score <= 0:
            continue
        for dom in eng_to_domain.get(eng, []):
            if dom not in hints:
                hints.append(dom)
        if len(hints) >= top:
            break
    return hints[:top]


def build_manifest(spec: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """把派生结果并入 spec（不覆盖已有声明）。"""
    out = dict(spec)
    fam = derive_family(spec, health)
    if fam and not out.get("family"):
        out["family"] = fam
    cov = derive_coverage(spec, health)
    if cov and not out.get("coverage"):
        out["coverage"] = cov
    langs = derive_langs(spec, (health or {}).get("query"))
    if langs and not out.get("langs"):
        out["langs"] = langs
    hints = derive_domain_hints(spec, health)
    if hints:
        out["domain_hints"] = hints
    return out


# ── 语料 / 派生件 ─────────────────────────────────────────────────────────────

def update_corpus(engine_id: str, spec: dict[str, Any]) -> bool:
    """把新引擎写进 domain_profiles.json（TF-IDF 语料），否则语义路由恒 0。"""
    data = read_domain_profiles()
    if engine_id in data:
        return False
    docs: list[str] = []
    if isinstance(spec.get("corpus"), list):
        docs = [str(x) for x in spec["corpus"] if str(x).strip()]
    elif spec.get("desc"):
        # 用 desc + coverage 拼一条语料（保守：不编造）
        parts = [str(spec["desc"]).strip()]
        if spec.get("label"):
            parts.insert(0, str(spec["label"]).strip())
        docs = [" ".join(parts)]
    if not docs:
        return False
    data[engine_id] = {
        "label": spec.get("label") or engine_id,
        "documents": docs,
        "boost_keywords": {},
        "boost_combos": {},
    }
    write_domain_profiles(data)
    return True


def run_sync_backends() -> dict[str, Any]:
    """调用 sync_backends.py 派生三份 backends 文件。"""
    script = SCRIPT_DIR / "sync_backends.py"
    if not script.exists():
        return {"ok": False, "error": "sync_backends.py 不存在"}
    import subprocess
    try:
        r = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        return {"ok": r.returncode == 0, "stdout": r.stdout[-800:],
                "stderr": r.stderr[-800:]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── 域归属建议 ────────────────────────────────────────────────────────────────

def suggest_domain_assignment(engine_id: str, domain_hints: list[str],
                              family: str, coverage: list[str]) -> dict[str, Any]:
    """给出「把该引擎加进哪个域的 combo」的建议（只建议，不改 config）。"""
    if not domain_hints:
        return {"suggestion": "none", "reason": "无域线索，建议先手工观察再决定"}
    return {
        "suggestion": "append_to_combo",
        "domains": domain_hints,
        "how": (
            "在 config.yaml 的 domains[].engines_combo 末尾追加 "
            f"'{engine_id}'（位次靠后，由 route_policy 的加槽机制保护），"
            "或在 route_policy._VERTICAL_NEW_SOURCE 里登记以自动加槽。"
        ),
        "family": family,
        "coverage": coverage,
    }


# ── 主流程 ────────────────────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> int:
    dry = args.dry_run
    spec: dict[str, Any] = {}
    source = ""

    # 1) 解析来源
    if args.from_yaml:
        p = Path(args.from_yaml)
        if not p.exists():
            _log(f"✗ spec 不存在: {p}")
            return 2
        spec = load_spec_file(p)
        source = str(p)
    elif args.from_url:
        eid = args.engine or _slug_from_url(args.from_url)
        spec = _template_from_url(args.from_url, eid, args)
        source = args.from_url
    elif args.engine:
        # 已声明：从合并配置里取 spec
        try:
            from config import load_config, get_engines
            eng = get_engines(load_config())
            if args.engine not in eng:
                _log(f"✗ 引擎未声明: {args.engine}")
                return 2
            spec = dict(eng[args.engine])
            spec["engine_id"] = args.engine
            source = f"config.yaml engines.{args.engine}"
        except Exception as e:
            _log(f"✗ 读取配置失败: {type(e).__name__}: {e}")
            return 2
    else:
        _log("✗ 需要 --from-yaml / --from-url / --engine 之一")
        return 2

    eid = spec_engine_id(spec, args.engine or "")
    if not eid:
        _log("✗ 无法确定 engine_id")
        return 2

    plan = {
        "engine_id": eid,
        "source": source,
        "steps": ["spec", "probe", "derive", "corpus", "validate", "admit", "sync"],
    }
    if dry:
        print(dumps(plan))
        _log("[dry-run] 未做任何写入")
        return 0

    # 2) spec 落盘（from-yaml/from-url 时）
    if args.from_yaml or args.from_url:
        try:
            target, wrote = write_spec(spec, overwrite=args.overwrite)
            _log(f"{'✓ 写入' if wrote else '· 已存在（跳过）'} spec: {target}")
        except Exception as e:
            _log(f"✗ 写 spec 失败: {type(e).__name__}: {e}")
            return 1
    else:
        _log(f"· 使用既有声明: {source}")

    # 3) 探测（缺 key 时 skipped，不阻断）
    _log(f"· 探测引擎 [{eid}] …")
    probe = probe_engine(eid, query=args.demo, timeout=args.timeout)
    if not probe.get("ok"):
        _log(f"· 探测未通过/跳过: {probe.get('status') or probe.get('error')}（继续派生，不阻断）")
    else:
        _log(f"✓ 探测通过: {probe.get('query')}")

    # 4) 派生元数据（并入 spec 并回写）
    manifest = build_manifest(spec, probe)
    if args.from_yaml or args.from_url:
        try:
            target, wrote = write_spec(manifest, overwrite=True)
            if wrote:
                _log(f"✓ 派生元数据已写入: {target}")
        except Exception as e:
            _log(f"· 回写派生失败（不阻断）: {type(e).__name__}")
    else:
        _log(f"· 派生元数据（建议手工并入 config.yaml）: "
             f"family={manifest.get('family') or '-'} "
             f"coverage={manifest.get('coverage') or '-'} "
             f"langs={manifest.get('langs') or '-'} "
             f"domain_hints={manifest.get('domain_hints') or '-'}")

    # 5) 语料（TF-IDF）
    if update_corpus(eid, manifest):
        _log(f"✓ 语料已写入: backends/domain_profiles.json [{eid}]")
    else:
        _log(f"· 语料已存在或无可写内容: backends/domain_profiles.json [{eid}]")

    # 6) 验证
    if args.stage != "none":
        _log(f"· 验证引擎 [{eid}] stage={args.stage} …")
        try:
            from engine_validate import validate_engine
            report = validate_engine(eid, stage=args.stage, admit=args.admit,
                                     timeout=args.timeout)
            ok = bool(report.get("ok"))
            _log(f"{'✓' if ok else '✗'} 验证结果: ok={ok} "
                 f"stages={list((report.get('stages') or {}).keys())}")
            if not ok and not args.admit:
                _log("  （未 --admit，不写准入；可加 --admit 强制准入）")
        except Exception as e:
            _log(f"✗ 验证异常: {type(e).__name__}: {e}")
            return 1

    # 7) 派生件同步
    if not args.no_sync:
        _log("· 同步 backends …")
        sync = run_sync_backends()
        _log(f"{'✓' if sync.get('ok') else '✗'} sync_backends: "
             f"{sync.get('ok') or sync.get('error')}")

    # 8) 域归属建议
    sugg = suggest_domain_assignment(
        eid, manifest.get("domain_hints") or [],
        manifest.get("family") or "", manifest.get("coverage") or [])
    _log("· 域归属建议:")
    print(dumps(sugg))

    _log(f"\n完成。新引擎 [{eid}] 已接入；按上方建议把它加进域 combo 即可参与路由。")
    return 0


def _slug_from_url(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    host = (m.group(1) if m else url).replace("www.", "")
    slug = re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_")
    return slug or "engine"


def _template_from_url(url: str, eid: str, args: argparse.Namespace) -> dict[str, Any]:
    """从 URL 生成 L1 模板（headers/output_map 留待用户补全）。"""
    return {
        "engine_id": eid,
        "type": "http",
        "enabled": True,
        "cost_tier": args.cost_tier,
        "method": "GET",
        "url": url,
        "timeout": args.timeout,
        "query_param": args.query_param or "q",
        "canary_query": args.demo or DEFAULT_CANARY,
        "desc": args.desc or f"{eid} 搜索（由 argo engine add 生成模板，请补全 output_map）",
        "required_env": ([args.env_key] if args.env_key else []),
        "headers": {"Accept": "application/json"},
        "output_map": {
            "items": args.items_path or "results",
            "item_title": "title",
            "item_url": "url",
            "item_summary": "snippet",
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="argo engine add",
        description="新引擎一键接入：spec → 探测 → 派生 → 语料 → 验证 → 准入 → 同步")
    p.add_argument("--from-yaml", help="从现有 spec 文件接入")
    p.add_argument("--from-url", help="从 API URL 生成 L1 模板接入")
    p.add_argument("--engine", help="已声明的引擎 id（只补元数据/验证/准入）")
    p.add_argument("--demo", help="探测用的查询词（垂类引擎给更贴的词）")
    p.add_argument("--desc", help="--from-url 时的引擎描述")
    p.add_argument("--query-param", help="查询参数名（默认 q）")
    p.add_argument("--items-path", help="结果数组路径（默认 results）")
    p.add_argument("--env-key", help="所需环境变量名（如 MYENGINE_API_KEY）")
    p.add_argument("--cost-tier", default="free",
                   choices=["free", "low", "api", "paid"])
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--stage", default="all",
                   choices=["health", "quality", "all", "none"])
    p.add_argument("--admit", action="store_true", help="验证通过后写准入")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的 spec")
    p.add_argument("--no-sync", action="store_true", help="不同步 backends")
    p.add_argument("--dry-run", action="store_true", help="只打印计划")
    return p


def cmd_revive(engine: str) -> int:
    """`argo engine revive <name>`：人工复活被 auth/破位停用的引擎。

    复用既有机制不另造：熔断器 revive（清 auth 禁用/基线破位/opens 计数，
    保留 ever_nonempty 基线）+ quota clear_remote_exhausted（auth/quota 类
    可能同时被配额状态机标记）。两处清干净才算复活。
    """
    if not engine:
        print("用法: argo engine revive <engine_id>", file=sys.stderr)
        return 2
    from circuit_breaker import get_breaker
    cleared = get_breaker().revive(engine)
    quota_cleared = False
    try:
        from quota import get_quota_manager
        qm = get_quota_manager()
        quota_cleared = qm.clear_remote_exhausted(engine)
        if ":" in engine:
            quota_cleared = qm.clear_remote_exhausted(engine.split(":", 1)[-1]) or quota_cleared
    except Exception:
        pass
    print(f"revived {engine}: breaker(had_state={cleared.get('had_state')}, "
          f"auth_disabled={cleared.get('auth_disabled')}, "
          f"suspected_stale={cleared.get('suspected_stale')}) "
          f"quota_exhausted_cleared={quota_cleared}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `argo engine add ...` 与 `argo engine ...` 等价：剥掉首个 "add"
    if argv and argv[0] == "add":
        argv = argv[1:]
    # revive 是独立的动词（不是 add 的 flag）：argo engine revive <engine_id>
    if argv and argv[0] == "revive":
        return cmd_revive(argv[1] if len(argv) > 1 else "")
    parser = _build_parser()
    args = parser.parse_args(argv)
    return cmd_add(args)


if __name__ == "__main__":
    sys.exit(main())
