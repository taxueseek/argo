#!/usr/bin/env python3
"""engine_validate.py — 引擎标准化验证（准入检查）

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
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config  # noqa: E402
import engine_states  # noqa: E402
from engine_env import missing_env_for, env_ready  # noqa: E402
from engine_admission import record_validation, load_admission  # noqa: E402
from engine_status import engine_detail  # noqa: E402
from cli_io import dumps

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


# 查询语言 → 该引擎需声明的 coverage 标签；声明 general 视为不限语言。
_RELEVANCE_LANG_TAGS: dict[str, tuple[str, ...]] = {
    "zh": ("chinese", "zh"),
    "en": ("english", "en"),
    "ja": ("japanese", "ja"),
    "ko": ("korean", "ko"),
}


def _relevance_applicable(spec: dict[str, Any], query: str) -> tuple[bool, str]:
    """相关性判据是否适用于本次探针。

    不适用（返回 False）的三种情形，都是为避免误杀而非放水：
      1. 引擎声明 `relevance_check: false`：热榜/快讯/列表型源按设计
         忽略查询词，拿查询词去评判它们等于用错标准。标准不降，只是
         对这类源问错了问题。
      2. 查询为空或取不到词元。
      3. 查询语言不在该引擎声明的 coverage 内：词面比较在跨语言下
         没有意义（中文查询配纯英文源，零字符交集不代表结果不相关）。
    """
    if spec.get("relevance_check") is False:
        return False, "declared_off"
    if not query or not query.strip():
        return False, "empty_query"
    try:
        from tfidf_router import tokenize
        if not set(tokenize(query)):
            return False, "no_tokens"
    except ImportError:
        return False, "tokenizer_unavailable"
    try:
        from lang_detect import detect_language, is_non_latin_lang
        lang = detect_language(query)
    except Exception:
        return True, ""
    cov = set(spec.get("coverage") or [])
    if not cov or "general" in cov:
        return True, ""
    # 跨语言守卫**只对非拉丁查询生效**。中文查询配纯英文源时词面零交集不
    # 代表结果不相关，判据没意义；反过来英文查询几乎所有源都能处理，若
    # 要求 coverage 必须声明 english，会把只声明内容域的源（mdn 的 docs、
    # github 的 code、arxiv 的 academic）全部误跳过，判据就形同虚设了。
    want = _RELEVANCE_LANG_TAGS.get(lang)
    if want and is_non_latin_lang(lang) and not (cov & set(want)):
        return False, f"cross_lang_{lang}_engine_no_{lang}"
    return True, ""


def _relevance_check(spec: dict[str, Any], results: list[dict[str, Any]],
                     query: str) -> dict[str, Any]:
    """结果与查询有无词面交集。

    为什么要这道判据：schema 完整率只看字段齐不齐。检索层被接错语料的引擎
    （2026-09-16 Seltz 实测：中文查询落 news 语料，返回时政与播客条目）字段
    100% 完整、延迟正常，在旧判据下拿满分 1.0 通过准入。结构合法但答非所问，
    只能靠相关性判据挡。

    判负门槛取**零交集**而非「多数命中」。实测依据：mdn 的 canary 是单词
    "Python"，它工作正常却只有 0.2 命中率（MDN 的 Python 章节页标题未必都
    含 python 字样）——用多数规则会误杀好引擎。零交集没有这种歧义，也是
    「答非所问」唯一无争议的形态。命中率照常上报，供人判断灰度。

    返回 ok=True 表示「不适用或通过」。不适用时 hit_rate 为 None，调用方
    不得把它算作失败。
    """
    applicable, skip_reason = _relevance_applicable(spec, query)
    if not applicable:
        return {"applicable": False, "ok": True, "hit_rate": None,
                "relevance_msg": f"not_applicable: {skip_reason}"}
    if not results:
        # 空结果由 schema 判据负责（"空结果"），此处不重复记账，
        # 否则 error 归因会被相关性抢走，看不出真实原因。
        return {"applicable": False, "ok": True, "hit_rate": None,
                "relevance_msg": "not_applicable: no_results"}
    try:
        from tfidf_router import tokenize
        q_set = set(tokenize(query))
        covered = sum(
            1 for r in results
            if q_set & set(tokenize(f"{r.get('title') or ''} {r.get('snippet') or ''}"))
        )
        hit_rate = round(covered / len(results), 3)
    except Exception as e:
        # 判据不可用时不设卡（fail-open），但如实记明原因，避免静默通过
        return {"applicable": False, "ok": True, "hit_rate": None,
                "relevance_msg": f"not_applicable: check_unavailable({type(e).__name__})"}
    return {"applicable": True, "ok": hit_rate > 0.0, "hit_rate": hit_rate,
            "relevance_msg": "" if hit_rate > 0.0 else "结果与查询零词面交集（疑似语料选错）"}


def run_health(engine_id: str, *, query: str | None = None, n: int = 3,
               timeout: float = 10.0) -> dict[str, Any]:
    """连通性 + schema 健康检查。"""
    from engines import search as engine_search, get_registry

    spec = _get_spec(engine_id)
    if not spec:
        return {
            "ok": False,
            "status": engine_states.VALIDATION_STATUS_ERROR,
            "engine": engine_id,
            "error": "engine_not_in_config",
        }
    if not spec.get("enabled", True):
        return {
            "ok": False,
            "status": engine_states.VALIDATION_STATUS_SKIPPED,
            "engine": engine_id,
            "error": "disabled_in_config",
        }
    if not env_ready(engine_id, spec):
        return {
            "ok": False,
            "status": engine_states.VALIDATION_STATUS_SKIPPED,
            "engine": engine_id,
            "error": "missing_env",
            "missing_env": missing_env_for(engine_id, spec),
        }

    # 确保已加载
    reg = get_registry()
    if engine_id not in reg:
        return {
            "ok": False,
            "status": engine_states.VALIDATION_STATUS_ERROR,
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
            "status": engine_states.VALIDATION_STATUS_ERROR,
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
    rel = _relevance_check(spec, results, query)
    if not rel["ok"]:
        ok = False
    return {
        "ok": ok,
        "status": (engine_states.VALIDATION_STATUS_PASS if ok
                  else engine_states.VALIDATION_STATUS_FAIL),
        "engine": engine_id,
        "query": query,
        "count": len(results),
        "latency_ms": latency,
        "schema_ok": schema_ok,
        "schema_msg": schema_msg,
        "field_complete_rate": round(field_rate, 3),
        "sample_title": (results[0].get("title") if results and isinstance(results[0], dict) else None),
        "relevance_ok": rel["ok"],
        "relevance_applicable": rel["applicable"],
        "relevance_hit_rate": rel["hit_rate"],
        "relevance_msg": rel["relevance_msg"],
        # 失败原因按「先归因再报错」排序：schema 问题 > 相关性 > 超时
        "error": None if ok else (
            schema_msg if not schema_ok
            else (rel["relevance_msg"] or "relevance_failed") if not rel["ok"]
            else "timeout_or_fail"
        ),
    }


def run_quality(engine_id: str, *, queries: list[dict[str, str]] | None = None,
                n: int = 3, timeout: float = 10.0,
                max_queries: int = 5,
                profile: str = "default") -> dict[str, Any]:
    """质量基准：多 query 空结果率、平均延迟、字段完整率。"""
    spec = _get_spec(engine_id)
    if not spec or not spec.get("enabled", True):
        return {"ok": False, "status": engine_states.VALIDATION_STATUS_SKIPPED, "engine": engine_id, "error": "disabled_or_missing"}
    if not env_ready(engine_id, spec):
        return {
            "ok": False,
            "status": engine_states.VALIDATION_STATUS_SKIPPED,
            "engine": engine_id,
            "error": "missing_env",
            "missing_env": missing_env_for(engine_id, spec),
        }

    if queries is not None:
        base_qs = queries
    else:
        # 引擎自述的适用查询集优先：候选池型引擎（如 v2ex 只能取官方 API 的
        # 热帖/最新帖再本地过滤）对通用技术查询天然空结果，用通用集评估会把
        # 「检索模式不匹配」误判成「引擎质量差」。让引擎声明自己的适用集，
        # 比放宽阈值诚实——标准不降，只是问对问题。
        declared = spec.get("quality_queries")
        declared_qs: list[dict[str, str]] = []
        if isinstance(declared, list):
            declared_qs = [
                {"id": q.get("id") or f"declared_{i}",
                 "query": q["query"],
                 "category": q.get("category") or "declared"}
                for i, q in enumerate(declared)
                if isinstance(q, dict) and q.get("query")
            ]
        if declared_qs:
            base_qs = declared_qs
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
            "relevance_applicable": h.get("relevance_applicable"),
            "relevance_hit_rate": h.get("relevance_hit_rate"),
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

    # 相关性：只统计判据适用的那些 query（不适用的不算失败，也不算通过）
    rel_runs = [r for r in runs if r.get("relevance_applicable")]
    # hit_rate 为 None 时 None == 0.0 即 False，无需额外判空
    rel_fail = sum(1 for r in rel_runs if r.get("relevance_hit_rate") == 0.0)
    rel_applicable_n = len(rel_runs)
    rel_fail_rate = (rel_fail / rel_applicable_n) if rel_applicable_n else 0.0

    # 质量分：通过率 0.5 + (1-空结果率)*0.3 + 字段完整 0.2
    quality_score = round(pass_rate * 0.5 + (1 - empty_rate) * 0.3 + avg_field * 0.2, 3)
    # 相关性多数不通过时给分封顶：字段齐全、计数正常但答非所问的引擎
    # 不该继续拿高分（Seltz 2026-09-16 就是这么拿到 1.0 的）。
    relevance_capped = False
    if rel_applicable_n and rel_fail_rate > 0.5:
        quality_score = min(quality_score, 0.3)
        relevance_capped = True
    ok = pass_rate >= 0.4 and empty_rate <= 0.6 and rel_fail_rate <= 0.5

    return {
        "ok": ok,
        "status": (engine_states.VALIDATION_STATUS_PASS if ok
                  else engine_states.VALIDATION_STATUS_FAIL),
        "engine": engine_id,
        "quality_score": quality_score,
        "pass_rate": round(pass_rate, 3),
        "empty_rate": round(empty_rate, 3),
        "relevance_applicable": rel_applicable_n,
        "relevance_fail": rel_fail,
        "relevance_fail_rate": round(rel_fail_rate, 3),
        "relevance_capped": relevance_capped,
        "avg_latency_ms": round(avg_lat, 1) if avg_lat is not None else None,
        "avg_field_complete_rate": round(avg_field, 3),
        "runs": runs,
    }


def family_claim_missing(engine_id: str, spec: dict[str, Any]) -> bool:
    """family 判定（单引擎口径，准入与全局测试同消费这一份）。"""
    from engine_families import _ENGINE_FAMILY_OVERRIDES
    return not (spec.get("family") or engine_id in _ENGINE_FAMILY_OVERRIDES)


def check_contracts(engine_id: str) -> list[str]:
    """清单契约检查（准入时点可判定的静态对账）：family 显式声明 + skill_registry。

    单一真源：判定逻辑提炼自 tests/test_engine_families.py 与
    tests/test_skill_registry_contract.py，那两个测试改为对本函数对拍——
    防止「测试一套判定、准入另一套」漂移。休眠台账不同：它要到引擎被排进
    域 combo 之后才可判定，归 sync_backends.check_dormant_ledger（domains
    变更后跑 sync 即发现）。
    """
    issues: list[str] = []
    cfg = load_config()
    engines_map = cfg.get("engines") or {}
    spec = engines_map.get(engine_id)
    if spec is None:
        try:
            from config import get_engines
            spec = get_engines(cfg, routable_only=False).get(engine_id)
        except Exception:
            spec = None
    if spec is None:
        return [f"engine {engine_id} 不在 config.yaml engines 里，清单契约无从检查"]
    # 1) family 必须显式声明（不许静默落到默认 web_general）
    if family_claim_missing(engine_id, spec):
        issues.append(
            "缺 family 显式声明：补 config.yaml 的 family 字段或 "
            "engine_families._ENGINE_FAMILY_OVERRIDES 覆盖表")
    # 2) coverage 声明 skill（config 原始或 specs 文件任一）→ 常量必须同步
    has_skill = "skill" in (spec.get("coverage") or [])
    if not has_skill:
        spec_path = ROOT / "engines" / "specs" / f"{engine_id}.yaml"
        if spec_path.exists():
            from yaml_load import load as _yaml_load
            raw = _yaml_load(str(spec_path)) or {}
            has_skill = "skill" in (raw.get("coverage") or [])
    if has_skill:
        from candidate_envelope import SKILL_REGISTRY_ENGINES
        if engine_id not in SKILL_REGISTRY_ENGINES:
            issues.append(
                "spec coverage 声明了 skill，但 candidate_envelope."
                "SKILL_REGISTRY_ENGINES 未含本引擎——新增技能目录源时两边要一起改")
    return issues


def validate_engine(
    engine_id: str,
    *,
    stage: str = engine_states.VALIDATION_STAGE_HEALTH,
    admit: bool = False,
    timeout: float = 10.0,
    write_doc: bool = False,
    profile: str = "default",
) -> dict[str, Any]:
    """执行验证并可选写入 admission。"""
    stages = []
    if stage == "all":
        stages = [engine_states.VALIDATION_STAGE_CONTRACT,
                  engine_states.VALIDATION_STAGE_HEALTH,
                  engine_states.VALIDATION_STAGE_QUALITY]
    elif stage in engine_states.VALIDATION_STAGES:
        stages = [stage]
    else:
        return {"ok": False, "error": f"unknown stage: {stage}"}

    report: dict[str, Any] = {
        "engine": engine_id,
        "stage_requested": stage,
        "stages": {},
        "ok": True,
        "status": engine_states.VALIDATION_STATUS_PASS,
        "profile": profile,
    }
    passed: list[str] = []
    health_res = None
    quality_res = None

    # contract 先于 health/quality：纯静态毫秒级，失败即短路——「先查声明
    # 再测行为」，清单缺口不必烧网络探针。
    contract_issues: list[str] = []
    if engine_states.VALIDATION_STAGE_CONTRACT in stages:
        contract_issues = check_contracts(engine_id)
        report["stages"]["contract"] = {"ok": not contract_issues,
                                        "issues": contract_issues}
        if contract_issues:
            report["ok"] = False
            report["status"] = engine_states.VALIDATION_STATUS_FAIL
            stages = [s for s in stages
                      if s == engine_states.VALIDATION_STAGE_CONTRACT]
        else:
            passed.append(engine_states.VALIDATION_STAGE_CONTRACT)

    if engine_states.VALIDATION_STAGE_HEALTH in stages:
        health_res = run_health(engine_id, timeout=timeout)
        report["stages"]["health"] = health_res
        if health_res.get("status") == engine_states.VALIDATION_STATUS_SKIPPED:
            report["ok"] = False
            report["status"] = engine_states.VALIDATION_STATUS_SKIPPED
            report["skip_reason"] = health_res.get("error")
        elif health_res.get("ok"):
            passed.append(engine_states.VALIDATION_STAGE_HEALTH)
        else:
            report["ok"] = False
            report["status"] = engine_states.VALIDATION_STATUS_FAIL

    if ("quality" in stages and report.get("status")
            != engine_states.VALIDATION_STATUS_SKIPPED):
        quality_res = run_quality(engine_id, timeout=timeout, profile=profile)
        report["stages"]["quality"] = quality_res
        if quality_res.get("status") == engine_states.VALIDATION_STATUS_SKIPPED:
            report["ok"] = False
            report["status"] = engine_states.VALIDATION_STATUS_SKIPPED
            report["skip_reason"] = quality_res.get("error")
        elif quality_res.get("ok"):
            passed.append(engine_states.VALIDATION_STAGE_QUALITY)
        else:
            report["ok"] = False
            if report["status"] != engine_states.VALIDATION_STATUS_SKIPPED:
                report["status"] = engine_states.VALIDATION_STATUS_FAIL

    # 写 admission
    if report.get("status") != engine_states.VALIDATION_STATUS_SKIPPED:
        blocked = not (engine_states.VALIDATION_STAGE_HEALTH in passed)
        # reason 只在**被 block** 时写：它表达「为什么不能用」。
        # 未 block 时留空——`record_validation` 的不变式是
        # 「blocked=False ⇒ reason 为空」，成功路径上写
        # `validation_passed` 会直接违反该不变式（实测线上 64 条记录
        # blocked=false/reason=validation_passed，与注释宣称的语义矛盾）。
        # 成功与否由 blocked/admitted_at/stages_passed 表达，不需要 reason。
        if blocked:
            reason = (contract_issues[0] if contract_issues
                      else (health_res or {}).get("error")
                      or engine_states.ADMISSION_REASON_HEALTH_FAILED)
        else:
            reason = ""
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
            # contract 失败必须显式 block：此时 passed 为空，record 默认分支
            # 会把「空 passed」当不表态（blocked=False）——清单缺口就漏过去了
            blocked=(True if contract_issues else
                     (blocked if (engine_states.VALIDATION_STAGE_HEALTH in stages)
                      else None)),
            reason=reason,
            health=health_res,
            quality=quality_res,
            admit=admit and not contract_issues
            and (engine_states.VALIDATION_STAGE_HEALTH in passed),
        )
        report["admission"] = adm
    else:
        report["admission"] = load_admission(engine_id)

    if write_doc and report.get("status") == engine_states.VALIDATION_STATUS_PASS:
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
    health = (report.get("stages") or {}).get(engine_states.VALIDATION_STAGE_HEALTH) or {}
    quality = (report.get("stages") or {}).get(engine_states.VALIDATION_STAGE_QUALITY) or {}
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
    parser.add_argument("--stage", default=engine_states.VALIDATION_STAGE_HEALTH,
                   choices=[*sorted(engine_states.VALIDATION_STAGES), "all"])
    parser.add_argument("--admit", action="store_true", help="通过后写入准入状态")
    parser.add_argument("--write-doc", action="store_true", help="通过后生成 docs/engines/<id>.md")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--profile", default="default", choices=["default", "cn"],
                        help="quality 查询集：default=通用/技术，cn=中文/金融")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all-free", action="store_true", help="对所有 free 且 env 就绪的引擎跑 health")
    parser.add_argument("--engines", help="逗号分隔多个引擎")
    parser.add_argument(
        "--strict", action="store_true",
        help="skipped（缺 key/被禁用/源改名）也算失败——夜间交付门禁用。"
             "默认宽松：批量体检时跳过未配置的源不是故障。")
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
            stage = engine_states.VALIDATION_STAGE_HEALTH
        rep = validate_engine(
            eng, stage=stage, admit=admit, timeout=args.timeout,
            write_doc=args.write_doc, profile=args.profile,
        )
        reports.append(rep)
        if rep.get("status") == engine_states.VALIDATION_STATUS_FAIL:
            exit_ok = False
        elif (args.strict and rep.get("status")
                  != engine_states.VALIDATION_STATUS_PASS):
            # skipped 也是「这个源没证明自己能交付」：夜间门禁要的是
            # 11 个关键源全部 pass，缺 key/改名/禁用都不该静默全绿。
            exit_ok = False
        if not args.json:
            st = rep.get("status")
            lat = ""
            h = (rep.get("stages") or {}).get(engine_states.VALIDATION_STAGE_HEALTH) or {}
            if h.get("latency_ms") is not None:
                lat = f" {h['latency_ms']}ms"
            q = (rep.get("stages") or {}).get(engine_states.VALIDATION_STAGE_QUALITY) or {}
            qnote = f" q={q.get('quality_score')}" if q else ""
            print(f"[{st}] {eng}{lat}{qnote} stages_passed={ (rep.get('admission') or {}).get('stages_passed')}")
            if rep.get("skip_reason"):
                print(f"       skip: {rep['skip_reason']}")
            if st == engine_states.VALIDATION_STATUS_FAIL:
                err = h.get("error") or q.get("error") or rep.get("error")
                print(f"       error: {err}")

    if args.json:
        payload = reports if len(reports) > 1 else reports[0]
        print(dumps(payload))

    return 0 if exit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
