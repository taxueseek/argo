#!/usr/bin/env python3
"""test_scenario_thresholds.py — 场景阈值契约(2026-09-17 新增)。

## 守的是什么缺陷

GLM 推理基建文章的机制:「场景与验收阈值先于优化」——先定义「什么算不达预期」,
才有可测差异。argo 此前的反馈几乎全是绝对值或「是/否」:138 项矩阵检查答
「路由可达吗」,replay_eval --check 答「条数/体积低于下限吗」,但没有任何一层
回答「**这次查询的形态对不对**」。

2026-09-17 的真实反例:「OpenAI MCP specification」被劫持进 patent_search、
google_patents 单引擎——138 项矩阵全绿、2460 项测试全绿、重放工具正常出报告,
没有任何一层会红,直到人去翻。本文件把这类错形态变成离线可拦的契约。

## 三层断言(每场景)

  1. 域归属:正向 expect_domain / 负向 domain_not_in(不许落到明显错误的域,
     兜底落点允许演进,故锚点场景只锁负向);
  2. 引擎数下限:路由策略后至少 2 个引擎(与预算契约同源:fast=2、auto=3);
  3. 重放 kept 下限:用 pipeline_golden 录制数据离线重放,kept 低于实测基线
     即 FAIL——「0 结果静默」在两层(路由形态、管线输出)都会被抓住。

场景数据在 tests/golden/scenario_thresholds.json;重放数据唯一真源是
pipeline_golden.json,本文件不复制查询或结果。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

GOLDEN = ROOT / "tests" / "golden" / "scenario_thresholds.json"
REPLAY = ROOT / "tests" / "golden" / "pipeline_golden.json"

SCENARIOS = json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"]


@pytest.fixture(scope="module", autouse=True)
def cold_routing():
    """路由断言跑在冷状态:准入无记录、熔断无历史,形态只由 config 决定。

    真实状态目录带着本机的配额用尽/熔断历史,会让断言随机器漂移;场景契约
    锁的是「配置决定的形态」,不是「这台机器此刻的状态」。
    """
    import circuit_breaker
    import engine_admission
    from circuit_breaker import CircuitBreaker

    tmp = Path(__import__("tempfile").mkdtemp(prefix="argo-scenario-"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine_admission, "DEFAULT_ADMISSION_DIR", tmp / "admission")
        mp.setattr(circuit_breaker, "get_breaker",
                   lambda: CircuitBreaker(state_path=str(tmp / "breaker.json")))
        # 自备密钥（2026-09-29）：场景契约按「密钥已配置」标定（如
        # S_react_rsc 的 octen），此前默默消费开发者 ~/.config/argo/env
        # 的真密钥——本机绿、CI/干净机器红（conftest 已隔离 env 文件）
        mp.setenv("ARGO_OCTEN_API_KEY", "test-key")
        yield


# ── 0. 引用完整性:场景文件自身不许漂移 ──────────────────────────────────────

def test_replay_cases_exist_in_golden():
    """场景引用的重放查询必须真实存在于 pipeline_golden,防拼写漂移后静默空跑。"""
    replayed = {c["query"] for c in
                json.loads(REPLAY.read_text(encoding="utf-8"))["cases"]}
    missing = [s["replay_case"] for s in SCENARIOS
               if s["replay_case"] not in replayed]
    assert not missing, f"场景引用的重放查询不存在: {missing}"


def test_declared_domains_are_real():
    """expect_domain / domain_not_in 里的域必须在 config 里存在,防改名后锚点失真。"""
    from config import load_config, get_domains

    known = {d["name"] for d in get_domains(load_config())}
    unknown = []
    for s in SCENARIOS:
        for key in ("expect_domain", "domain_not_in"):
            for dom in ([s[key]] if isinstance(s.get(key), str) else s.get(key) or []):
                if dom not in known:
                    unknown.append(f"{s['id']}/{key}: {dom}")
    assert not unknown, f"场景声明的域不存在: {unknown}"


def test_scenarios_are_unique():
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "场景 id 重复"
    queries = [s["replay_case"] for s in SCENARIOS]
    assert len(queries) == len(set(queries)), "同一查询被两个场景重复声明"


# ── 1 + 2. 路由层:域归属 + 引擎数下限 ────────────────────────────────────────

@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_routing_form(scenario):
    from route import route_query

    r = route_query(scenario["replay_case"], mode="auto")
    domain, engines = r.get("domain"), r.get("engines") or []

    if "domain_not_in" in scenario:
        assert domain not in scenario["domain_not_in"], (
            f"{scenario['id']}: 查询被劫持进错域 {domain!r}(引擎 {engines})")
    if "expect_domain" in scenario:
        assert domain == scenario["expect_domain"], (
            f"{scenario['id']}: 域落点 {domain!r} ≠ 契约 {scenario['expect_domain']!r}"
            f"(引擎 {engines})")

    assert len(engines) >= scenario["min_engines"], (
        f"{scenario['id']}: 策略后引擎数 {len(engines)} < 下限 "
        f"{scenario['min_engines']}({engines})——路由形态退化")


# ── 3. 管线层:重放 kept 下限 ────────────────────────────────────────────────

def test_replay_kept_above_threshold():
    """录制数据离线重放,kept 低于实测基线即 FAIL。

    一次 evaluate_all 跑全部案例(纯内存、不联网),只对声明了 min_replay_kept
    的场景断言——含 local_* 引擎的场景例外,本地引擎不走 engine_search 替换点、
    重放时真跑,kept 会随真跑成败漂移(2026-09-17 实录:同一份录制,天气场景
    kept 在 1↔5 间跳、errors=2),锁不住「处理逻辑」这一层,详见场景文件
    _meta.caveat。「0 结果即 FAIL」由其余纯远程场景承担。
    """
    import replay_eval

    report = replay_eval.evaluate_all(REPLAY)
    by_case = {m["id"]: m for m in report["cases"]}
    by_query = {}
    golden = json.loads(REPLAY.read_text(encoding="utf-8"))["cases"]
    for c in golden:
        by_query[c["query"]] = c["id"]

    failures = []
    for s in SCENARIOS:
        if "min_replay_kept" not in s:
            continue
        m = by_case.get(by_query[s["replay_case"]])
        assert m is not None, f"{s['id']}: 重放报告缺 {s['replay_case']!r}"
        if m["kept"] < s["min_replay_kept"]:
            failures.append(
                f"{s['id']}: kept={m['kept']} < 下限 {s['min_replay_kept']}, "
                f"engines={m.get('engines')}, funnel={m.get('funnel')}, "
                f"not_recorded={m.get('not_recorded')}, errors={m.get('errors')}")
    assert not failures, (
        "重放结果跌破场景基线(先确认是处理逻辑退化还是录制数据过期,后者"
        "按实测更新下限并在 _meta 记一笔):\n  " + "\n  ".join(failures))


def test_kept_thresholds_only_for_pure_replay_cases():
    """声明 min_replay_kept 的场景,combo 必须全是远程引擎——否则重放不确定。

    这是 kept 断言适用面的自动守卫:以后录制进来的新场景若混入 local_* 引擎,
    要么别声明 kept 下限,要么被这里拦下并解释为什么它仍是确定性的。
    """
    offenders = []
    for s in SCENARIOS:
        if "min_replay_kept" not in s:
            continue
        golden = json.loads(REPLAY.read_text(encoding="utf-8"))["cases"]
        case = next(c for c in golden if c["query"] == s["replay_case"])
        local = [e for e in (case.get("engines") or {}) if e.startswith("local_")]
        if local:
            offenders.append(f"{s['id']}: 混入本地引擎 {local}")
    assert not offenders, "\n  ".join(offenders)
