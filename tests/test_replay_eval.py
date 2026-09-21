#!/usr/bin/env python3
"""test_replay_eval.py — 离线重跑与对比（2026-09-17 新增）。

## 防的是什么问题

原来这套工具能回答「这次改动有没有弄坏东西」，回答不了「这次改动是不是变好了」：
matrix_search_eval 只判断路由对不对（是或否）；ranking_golden 能算排序质量分，
但它用的引擎返回是手写的假数据；relevance_golden 存的是真实返回，却只核对相关性
判断、不跑后面的流程。于是去重、合并、排序、输出这几处的改动，效果只能等一周一次
的联网检查，或者靠人读代码。

scripts/replay_eval.py 补上这一段：把真实返回录下来，以后离线重跑整条流程，
并给出两次运行的差异。

## 这里检查什么

六件事，每一件都对应一种会让对比工具自己变成「误报来源」的毛病：

  1. 真的不联网：把网络出口整个掐断，重跑仍然要能跑通；
  2. 每次结果一样：跑两遍必须完全相同，否则算出来的差异里混的是噪声；
  3. 检查要管用：录制的数据里没有某个引擎时必须报出来（而且不受这条有没有设
     下限的影响）；低于下限也必须报。这两条都故意造一次错误来确认检查会亮红；
  4. 对比能看出变化：结果新进来、掉出去、位次变化都要体现在差异里；
  5. 录制文件不被改写：重跑跑完，文件内容一个字节都不能变；
  6. 换掉的函数要还原：跑完 search.engine_search 必须变回原样。
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

DATA = ROOT / "tests" / "golden" / "pipeline_golden.json"

import replay_eval  # noqa: E402


# ── 1. 真的不联网 ─────────────────────────────────────────────────────────────

def test_runs_with_network_blocked(monkeypatch):
    """把所有 HTTP 出口掐断，重跑仍然要能跑通整条流程。

    这条是整个做法成立的前提：它一旦不成立，「秒级、不花配额」就只是说法。
    """
    def boom(*_a, **_k):
        raise AssertionError("重跑过程中发生了真实网络调用")

    import net_proxy
    import urllib.request
    monkeypatch.setattr(net_proxy, "open_url", boom, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    report = replay_eval.evaluate_all()
    assert report["n_cases"] >= 6
    assert all(c["status"] != "error" for c in report["cases"]), \
        [c for c in report["cases"] if c["status"] == "error"]


# ── 2. 每次结果一样 ───────────────────────────────────────────────────────────

def test_two_runs_are_identical():
    from cli_io import dumps
    a = dumps(replay_eval.evaluate_all())
    b = dumps(replay_eval.evaluate_all())
    assert a == b, "两次重跑结果不一样：算出来的差异里会混进噪声，而不是代码改动的效果"


# ── 3. 检查要管用 ─────────────────────────────────────────────────────────────

def test_reports_missing_recording_even_without_limit():
    """录制的数据里没有某个引擎时必须报，**而且不受这条有没有设下限的影响**。

    故意造一个错误：给一条**没有设下限**的数据注入「没录到某引擎」。
    第一版实现把这条判断写在「有没有设下限」的里面，没设下限的数据整条被跳过，
    检查于是形同虚设。这条测试就是防它退回去。
    """
    doc = {"cases": [{"id": "no-limit", "engines": {}}]}
    report = {"cases": [{"id": "no-limit", "kept": 3, "bytes": 100,
                         "not_recorded": ["octen"]}]}
    bad = replay_eval.check_limits(report, doc)
    assert any("octen" in b for b in bad), f"没录到引擎这件事没有被报出来：{bad}"


def test_reports_too_few_results_and_too_large_output():
    doc = {"cases": [{"id": "c1", "engines": {},
                      "limit": {"min_kept": 5, "max_bytes": 200}}]}
    report = {"cases": [{"id": "c1", "kept": 2, "bytes": 900}]}
    bad = replay_eval.check_limits(report, doc)
    assert any("少于下限" in b for b in bad), bad
    assert any("超过上限" in b for b in bad), bad


def test_passes_when_everything_is_within_limits():
    doc = {"cases": [{"id": "c1", "engines": {},
                      "limit": {"min_kept": 2, "max_bytes": 200}}]}
    report = {"cases": [{"id": "c1", "kept": 5, "bytes": 100}]}
    assert replay_eval.check_limits(report, doc) == []


# ── 4. 对比能看出变化 ─────────────────────────────────────────────────────────

def test_diff_shows_results_coming_and_going_and_score_change():
    base = {"cases": [{"id": "c1", "kept": 3, "bytes": 100,
                       "urls": ["u1", "u2", "u3"], "ndcg": 0.8}]}
    new = {"cases": [{"id": "c1", "kept": 3, "bytes": 150,
                      "urls": ["u2", "u1", "u4"], "ndcg": 0.6}]}
    d = replay_eval.diff_reports(base, new)
    row = d["cases"][0]
    assert row["entered"] == ["u4"], row
    assert row["left"] == ["u3"], row
    assert row["d_bytes"] == 50
    assert row["d_ndcg"] == pytest.approx(-0.2, abs=1e-6)
    assert {m["url"] for m in row["moved"]} == {"u1", "u2"}
    assert d["n_changed"] == 1


def test_diff_marks_new_items_instead_of_crashing():
    """基准里没有的条目（新录的）要标注出来，不能让整个对比崩掉。"""
    base = {"cases": []}
    new = {"cases": [{"id": "c9", "kept": 1, "bytes": 10, "urls": ["u"]}]}
    d = replay_eval.diff_reports(base, new)
    assert d["cases"][0]["note"].startswith("基准里没有")


# ── 5. 录制文件不被改写 ───────────────────────────────────────────────────────

def test_recording_file_is_not_modified_by_a_run():
    """跑流程时会就地在结果里补 _engine、score、rerank_dims 这些字段。

    不在换掉的那个函数里做深拷贝，这些残留就会被写回录制文件，下一次重跑就带着
    上一次的痕迹——这正是「看着一切都好、其实已经不对」的典型来源。
    """
    before = DATA.read_bytes()
    replay_eval.evaluate_all()
    assert DATA.read_bytes() == before, "重跑把录制文件改动了"


# ── 6. 换掉的函数要还原 ───────────────────────────────────────────────────────

def test_engine_function_is_restored_after_a_run():
    import search
    before = search.engine_search
    replay_eval.evaluate_all()
    assert search.engine_search is before, "跑完没有把网络出口还原回去"


# ── 录制数据的健康状况 ───────────────────────────────────────────────────────

def test_recording_data_is_healthy():
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    cases = doc.get("cases") or []
    assert len(cases) >= 6, f"录制的数据太少：{len(cases)} 条"
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "有条目的 id 重复了"
    for c in cases:
        assert c.get("query"), f"{c['id']} 缺查询词"
        assert c.get("engines"), f"{c['id']} 缺引擎返回"
        assert c.get("decision"), (
            f"{c['id']} 缺路由结果——不存下来的话，重跑会被状态差异带偏"
            f"（见 replay_eval._route_decision）")
        assert c.get("limit"), (
            f"{c['id']} 没设下限（没设的话，检查对这条就是空的）")
    meta = doc.get("_meta") or {}
    assert meta.get("recorded_at"), "缺录制日期"
    assert "不是" in (meta.get("usage") or ""), (
        "缺使用说明：必须写明重跑保证的是「处理逻辑没有退步」，"
        "不是「现在网上就是这个样子」")


# ── 5. 字段级灵敏度（拆加工层重构时的锁）─────────────────────────────────────

def test_metrics_carry_full_result_fields():
    """指标必须带上每条结果的全量字段。

    只存 urls 的话，「位次没动但分数/来源/信号变了」看不见——而拆
    execute_search 这类加工层重构时，那正是最容易漏掉的漂移。
    """
    payload = {"count": 1, "status": "completed",
               "results": [{"url": "https://a", "title": "t", "source": "octen",
                            "score": 0.61, "signals": {"freshness": 1}}]}
    case = {"id": "c1", "query": "q", "max_results": 1}
    m = replay_eval._collect_metrics(case, payload, set(), set())
    assert m["results_full"] == payload["results"], m


def test_diff_detects_field_level_change():
    """同一 URL 上的分数变化必须被报出来（位次完全没动）。"""
    base = {"cases": [{"id": "c1", "kept": 1, "bytes": 10, "urls": ["u1"],
                       "results_full": [{"url": "u1", "score": 0.61,
                                         "source": "octen"}]}]}
    new = {"cases": [{"id": "c1", "kept": 1, "bytes": 10, "urls": ["u1"],
                      "results_full": [{"url": "u1", "score": 0.58,
                                        "source": "octen"}]}]}
    d = replay_eval.diff_reports(base, new)
    row = d["cases"][0]
    assert d["n_changed"] == 1, d
    assert row["field_changes"] == [
        {"url": "u1", "fields": {"score": {"from": 0.61, "to": 0.58}}}], row


def test_field_change_ignores_urls_that_came_or_went():
    """进出的 URL 由 entered/left 表达，字段对比只管两边都有的（不重复报）。"""
    base = {"cases": [{"id": "c1", "kept": 1, "bytes": 10, "urls": ["u1"],
                       "results_full": [{"url": "u1", "score": 0.6}]}]}
    new = {"cases": [{"id": "c1", "kept": 1, "bytes": 10, "urls": ["u2"],
                      "results_full": [{"url": "u2", "score": 0.9}]}]}
    row = replay_eval.diff_reports(base, new)["cases"][0]
    assert "field_changes" not in row, row
    assert row["entered"] == ["u2"] and row["left"] == ["u1"]
