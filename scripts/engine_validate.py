#!/usr/bin/env python3
"""engine_validate.py — 引擎标准化验证（准入门禁）

用法：
  python3 scripts/engine_validate.py --engine hackernews --stage health
  python3 scripts/engine_validate.py --engine hackernews --stage quality
  python3 scripts/engine_validate.py --engine hackernews --stage all --admit
  python3 scripts/engine_validate.py --all-free --stage health
  python3 scripts/engine_validate.py --engine my_new --stage health --admit

Stage：
  health  — 连通性 + 结果 schema + 延迟
  quality — 固定 query 集（空结果率 / 字段完整率 / 延迟）
  all     — health + quality

通过 health 且 --admit 时写入 admission（blocked=false）。
缺 Key：status=skipped，不算失败，不写 block（除非 --block-on-skip）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config  # noqa: E402
from engine_env import missing_env_for, env_ready  # noqa: E402
from engine_admission import record_validation, load_admission  # noqa: E402
from engine_status import engine_detail  # noqa: E402

REQUIRED_KEYS = ("title", "url", "source")

# 垂类引擎 health canary（通用词会误杀快讯/行情类）
ENGINE_CANARY_QUERIES: dict[str, str] = {
    "fxtwitter": "OpenAI",
    "twitter": "OpenAI",
    "jin10": "美联储",
    "cls_telegraph": "股市",
    "em_global_news": "股市",
    "ths_hot": "热点",
    "eastmoney": "贵州茅台",
    "qweather": "北京 天气",
    "finviz": "AAPL",
    "seeking_alpha": "AAPL",
    "arxiv": "transformer",
    "semantic_scholar": "attention mechanism",
    "google_scholar": "machine learning",
    "hackernews": "Python",
    "stackoverflow": "python asyncio",
    "v2ex": "Python",
    "wechat_sogou": "人工智能",
}

# 质量基准：少而稳；默认偏通用/技术，避免垂类引擎被无关 query 误杀
# 金融/中文垂类可在调用时传入自定义 queries
QUALITY_QUERIES: list[dict[str, str]] = [
    {"id": "tech_en", "query": "Python asyncio", "category": "tech"},
    {"id": "tech_lib", "query": "open source LLM", "category": "tech"},
    {"id": "news_en", "query": "OpenAI research", "category": "news"},
    {"id": "general", "query": "machine learning", "category": "general"},
    {"id": "dev", "query": "kubernetes networking", "category": "tech"},
]

# 中文/金融场景补充集（--profile cn 时使用）
QUALITY_QUERIES_CN: list[dict[str, str]] = [
    {"id": "tech_zh", "query": "Python 异步编程", "category": "tech_zh"},
    {"id": "finance_zh", "query": "沪深300 指数", "category": "finance"},
    {"id": "general_zh", "query": "人工智能 应用", "category": "general"},
    {"id": "news_zh", "query": "美联储 利率", "category": "news"},
    {"id": "market_zh", "query": "ETF 增强策略", "category": "finance"},
]


def _get_spec(engine_id: str) -> dict[str, Any]:
    cfg = load_config(force=True)
    return dict((cfg.get("engines") or {}).get(engine_id) or {})


def _schema_ok(results: list[dict[str, Any]]) -> tuple[bool, str, float]:
    """返回 (ok, message, field_complete_rate)。"""
    if not isinstance(results, list):
        return False, "results 非 list", 0.0
    if not results:
        return False, "空结果", 0.0
    complete = 0
    total = 0
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            return False, f"results[{i}] 非 dict", 0.0
        if "error" in r and not r.get("title"):
            continue
        total += 1
        missing = [k for k in REQUIRED_KEYS if not r.get(k)]
        # title 或 url 至少一个；source 应用 _engine 回填，可缺
        if r.get("title") or r.get("url"):
            if not missing or missing == ["source"]:
                complete += 1
            elif "title" not in missing or "url" not in missing:
                # 有 title 或 url 算半完整
                complete += 0.5
        else:
            return False, f"results[{i}] 缺 title/url", complete / max(total, 1)
    if total == 0:
        return False, "无有效条目", 0.0
    rate = complete / total
    ok = rate >= 0.5
    return ok, f"字段完整率 {rate:.0%}", rate


def run_health(engine_id: str, *, query: str | None = None, n: int = 3,
               timeout: float = 10.0) -> dict[str, Any]:
    """连通性 + schema 健康检查。"""
    from engines import search as engine_search, get_registry

    spec = _get_spec(engine_id)
    if not spec:
        return {
            "ok": False,
            "status": "error",
            "engine": engine_id,
            "error": "engine_not_in_config",
        }
    if not spec.get("enabled", True):
        return {
            "ok": False,
            "status": "skipped",
            "engine": engine_id,
            "error": "disabled_in_config",
        }
    if not env_ready(engine_id, spec):
        return {
            "ok": False,
            "status": "skipped",
            "engine": engine_id,
            "error": "missing_env",
            "missing_env": missing_env_for(engine_id, spec),
        }

    # 确保已加载
    reg = get_registry()
    if engine_id not in reg:
        return {
            "ok": False,
            "status": "error",
            "engine": engine_id,
            "error": "no_builder_or_not_registered",
            "type": spec.get("type"),
        }

    # 垂类 canary：spec.canary_query > 内置表 > 默认 Python
    if not query:
        query = (
            spec.get("canary_query")
            or ENGINE_CANARY_QUERIES.get(engine_id)
            or "Python"
        )

    t0 = time.time()
    try:
        results = engine_search(query, engine_id, n=n, timeout=timeout)
    except Exception as e:
        return {
            "ok": False,
            "status": "error",
            "engine": engine_id,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": round((time.time() - t0) * 1000, 1),
        }
    latency = round((time.time() - t0) * 1000, 1)
    results = results if isinstance(results, list) else []
    schema_ok, schema_msg, field_rate = _schema_ok(results)

    # 回填 source 再评一次（部分引擎只靠 _engine）
    if not schema_ok and results:
        patched = []
        for r in results:
            if isinstance(r, dict):
                rr = dict(r)
                rr.setdefault("source", engine_id)
                patched.append(rr)
        schema_ok, schema_msg, field_rate = _schema_ok(patched)

    ok = schema_ok and latency <= timeout * 1000 * 1.2
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "engine": engine_id,
        "query": query,
        "count": len(results),
        "latency_ms": latency,
        "schema_ok": schema_ok,
        "schema_msg": schema_msg,
        "field_complete_rate": round(field_rate, 3),
        "sample_title": (results[0].get("title") if results and isinstance(results[0], dict) else None),
        "error": None if ok else (schema_msg if not schema_ok else "timeout_or_fail"),
    }


def run_quality(engine_id: str, *, queries: list[dict[str, str]] | None = None,
                n: int = 3, timeout: float = 10.0,
                max_queries: int = 5,
                profile: str = "default") -> dict[str, Any]:
    """质量基准：多 query 空结果率、平均延迟、字段完整率。"""
    spec = _get_spec(engine_id)
    if not spec or not spec.get("enabled", True):
        return {"ok": False, "status": "skipped", "engine": engine_id, "error": "disabled_or_missing"}
    if not env_ready(engine_id, spec):
        return {
            "ok": False,
            "status": "skipped",
            "engine": engine_id,
            "error": "missing_env",
            "missing_env": missing_env_for(engine_id, spec),
        }

    if queries is not None:
        base_qs = queries
    elif profile == "cn":
        base_qs = QUALITY_QUERIES_CN
    else:
        base_qs = QUALITY_QUERIES
    qs = base_qs[:max_queries]
    runs: list[dict[str, Any]] = []
    for item in qs:
        h = run_health(engine_id, query=item["query"], n=n, timeout=timeout)
        runs.append({
            "id": item.get("id"),
            "query": item["query"],
            "category": item.get("category"),
            "ok": h.get("ok"),
            "count": h.get("count", 0),
            "latency_ms": h.get("latency_ms"),
            "field_complete_rate": h.get("field_complete_rate", 0),
            "error": h.get("error"),
        })

    total = len(runs) or 1
    empty = sum(1 for r in runs if not r.get("count"))
    empty_rate = empty / total
    latencies = [r["latency_ms"] for r in runs if isinstance(r.get("latency_ms"), (int, float))]
    avg_lat = sum(latencies) / len(latencies) if latencies else None
    field_rates = [r.get("field_complete_rate") or 0 for r in runs]
    avg_field = sum(field_rates) / len(field_rates) if field_rates else 0.0
    pass_n = sum(1 for r in runs if r.get("ok"))
    pass_rate = pass_n / total

    # 质量分：通过率 0.5 + (1-空结果率)*0.3 + 字段完整 0.2
    quality_score = round(pass_rate * 0.5 + (1 - empty_rate) * 0.3 + avg_field * 0.2, 3)
    ok = pass_rate >= 0.4 and empty_rate <= 0.6

    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "engine": engine_id,
        "quality_score": quality_score,
        "pass_rate": round(pass_rate, 3),
        "empty_rate": round(empty_rate, 3),
        "avg_latency_ms": round(avg_lat, 1) if avg_lat is not None else None,
        "avg_field_complete_rate": round(avg_field, 3),
        "runs": runs,
    }


def validate_engine(
    engine_id: str,
    *,
    stage: str = "health",
    admit: bool = False,
    timeout: float = 10.0,
    write_doc: bool = False,
    profile: str = "default",
) -> dict[str, Any]:
    """执行验证并可选写入 admission。"""
    stages = []
    if stage == "all":
        stages = ["health", "quality"]
    elif stage in ("health", "quality"):
        stages = [stage]
    else:
        return {"ok": False, "error": f"unknown stage: {stage}"}

    report: dict[str, Any] = {
        "engine": engine_id,
        "stage_requested": stage,
        "stages": {},
        "ok": True,
        "status": "pass",
        "profile": profile,
    }
    passed: list[str] = []
    health_res = None
    quality_res = None

    if "health" in stages:
        health_res = run_health(engine_id, timeout=timeout)
        report["stages"]["health"] = health_res
        if health_res.get("status") == "skipped":
            report["ok"] = False
            report["status"] = "skipped"
            report["skip_reason"] = health_res.get("error")
        elif health_res.get("ok"):
            passed.append("health")
        else:
            report["ok"] = False
            report["status"] = "fail"

    if "quality" in stages and report.get("status") != "skipped":
        quality_res = run_quality(engine_id, timeout=timeout, profile=profile)
        report["stages"]["quality"] = quality_res
        if quality_res.get("status") == "skipped":
            report["ok"] = False
            report["status"] = "skipped"
            report["skip_reason"] = quality_res.get("error")
        elif quality_res.get("ok"):
            passed.append("quality")
        else:
            report["ok"] = False
            if report["status"] != "skipped":
                report["status"] = "fail"

    # 写 admission
    if report.get("status") != "skipped":
        blocked = not ("health" in passed)
        reason = ""
        if blocked:
            reason = (health_res or {}).get("error") or "health_failed"
        elif admit and "health" in passed:
            reason = "validation_passed"
        avg_lat = None
        qscore = None
        if quality_res:
            avg_lat = quality_res.get("avg_latency_ms")
            qscore = quality_res.get("quality_score")
        elif health_res:
            avg_lat = health_res.get("latency_ms")
        adm = record_validation(
            engine_id,
            stages_passed=passed,
            quality_score=qscore,
            avg_latency_ms=avg_lat,
            blocked=blocked if ("health" in stages) else None,
            reason=reason,
            health=health_res,
            quality=quality_res,
            admit=admit and ("health" in passed),
        )
        report["admission"] = adm
    else:
        report["admission"] = load_admission(engine_id)

    if write_doc and report.get("status") == "pass":
        path = _write_engine_doc(engine_id, report)
        report["doc_path"] = str(path)

    report["detail"] = engine_detail(engine_id)
    return report


def _write_engine_doc(engine_id: str, report: dict[str, Any]) -> Path:
    """生成 docs/engines/<id>.md 运行手册。"""
    docs = ROOT / "docs" / "engines"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / f"{engine_id}.md"
    detail = report.get("detail") or engine_detail(engine_id)
    adm = report.get("admission") or {}
    health = (report.get("stages") or {}).get("health") or {}
    quality = (report.get("stages") or {}).get("quality") or {}
    lines = [
        f"# 引擎：`{engine_id}`",
        "",
        f"- 准入时间: {adm.get('admitted_at') or '（未标记 admit）'}",
        f"- 状态: {detail.get('status')}",
        f"- cost_tier: {detail.get('cost_tier')}",
        f"- type: {detail.get('type')}",
        f"- quality_score: {adm.get('quality_score')}",
        f"- avg_latency_ms: {adm.get('avg_latency_ms')}",
        f"- blocked: {adm.get('blocked')}",
        "",
        "## 环境变量",
        "",
        "| 变量 | 必填 |",
        "|------|------|",
    ]
    req = detail.get("required_env") or []
    if not req:
        lines.append("| （无） | 否 |")
    else:
        for v in req:
            lines.append(f"| `{v}` | 是 |")
    lines += [
        "",
        "## 最近验证",
        "",
        f"- health: {health.get('status')} · latency={health.get('latency_ms')}ms · count={health.get('count')}",
        f"- quality: {quality.get('status')} · score={quality.get('quality_score')} · empty_rate={quality.get('empty_rate')}",
        "",
        "## 调用",
        "",
        "```bash",
        f'python3 scripts/search.py "查询词" --engine {engine_id}',
        f"python3 scripts/engine_validate.py --engine {engine_id} --stage health",
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _free_engine_ids() -> list[str]:
    from config import get_cost_tiers
    cfg = load_config()
    tiers = get_cost_tiers(cfg)
    free = set(tiers.get("free") or [])
    out = []
    for name, spec in (cfg.get("engines") or {}).items():
        if not isinstance(spec, dict) or not spec.get("enabled", True):
            continue
        if name in free or (name not in (tiers.get("paid") or []) and name not in (tiers.get("low") or []) and name not in (tiers.get("api") or [])):
            # 跳过需要 key 的
            if env_ready(name, spec):
                out.append(name)
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Argo 引擎标准化验证")
    parser.add_argument("--engine", "-e", help="引擎 ID")
    parser.add_argument("--stage", default="health", choices=["health", "quality", "all"])
    parser.add_argument("--admit", action="store_true", help="通过后写入准入状态")
    parser.add_argument("--write-doc", action="store_true", help="通过后生成 docs/engines/<id>.md")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--profile", default="default", choices=["default", "cn"],
                        help="quality 查询集：default=通用/技术，cn=中文/金融")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all-free", action="store_true", help="对所有 free 且 env 就绪的引擎跑 health")
    parser.add_argument("--engines", help="逗号分隔多个引擎")
    args = parser.parse_args()

    targets: list[str] = []
    if args.all_free:
        targets = _free_engine_ids()
    elif args.engines:
        targets = [x.strip() for x in args.engines.split(",") if x.strip()]
    elif args.engine:
        targets = [args.engine]
    else:
        parser.error("请指定 --engine / --engines / --all-free")

    reports = []
    exit_ok = True
    for eng in targets:
        # all-free 默认只跑 health 且不强制 admit 全部
        stage = args.stage
        admit = args.admit
        if args.all_free and args.stage == "all" and len(targets) > 3:
            stage = "health"
        rep = validate_engine(
            eng, stage=stage, admit=admit, timeout=args.timeout,
            write_doc=args.write_doc, profile=args.profile,
        )
        reports.append(rep)
        if rep.get("status") == "fail":
            exit_ok = False
        if not args.json:
            st = rep.get("status")
            lat = ""
            h = (rep.get("stages") or {}).get("health") or {}
            if h.get("latency_ms") is not None:
                lat = f" {h['latency_ms']}ms"
            q = (rep.get("stages") or {}).get("quality") or {}
            qnote = f" q={q.get('quality_score')}" if q else ""
            print(f"[{st}] {eng}{lat}{qnote} stages_passed={ (rep.get('admission') or {}).get('stages_passed')}")
            if rep.get("skip_reason"):
                print(f"       skip: {rep['skip_reason']}")
            if st == "fail":
                err = h.get("error") or q.get("error") or rep.get("error")
                print(f"       error: {err}")

    if args.json:
        payload = reports if len(reports) > 1 else reports[0]
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0 if exit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
