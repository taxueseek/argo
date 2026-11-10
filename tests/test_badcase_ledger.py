"""badcase_ledger 单元测试：三源入账、幂等去重、门禁判定。"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402

import badcase_causes  # noqa: E402
import badcase_ledger  # noqa: E402

_NOW = datetime.now().astimezone().isoformat(timespec="seconds")


@pytest.fixture()
def ledger_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    logs = state / "usage_log"
    logs.mkdir(parents=True)
    monkeypatch.setenv("ARGO_STATE_DIR", str(state))
    monkeypatch.setenv("ARGO_USAGE_LOG_DIR", str(logs))
    return logs


def _seed_sources(logs: Path):
    (logs / "query.jsonl").write_text("\n".join([
        json.dumps({"ts": _NOW, "stream": "query", "query": "错误查询",
                    "engines_used": ["anysearch"], "count": 0,
                    "errors": ["anysearch: timeout"], "mode": "auto"}),
        json.dumps({"ts": _NOW, "stream": "query", "query": "空结果查询",
                    "engines_used": ["octen", "slow_primary"], "count": 0,
                    "errors": [], "mode": "auto"}),  # 混夹具但真引擎在 → 入账
        json.dumps({"ts": _NOW, "stream": "query", "query": "正常查询",
                    "engines_used": ["anysearch"], "count": 5,
                    "errors": [], "mode": "auto"}),  # 正常 → 不入账
        json.dumps({"ts": _NOW, "stream": "query", "query": "夹具查询",
                    "engines_used": ["slow_primary"], "count": 3,
                    "errors": [], "mode": "auto"}),  # 全表外名 → 不入账
    ]) + "\n", encoding="utf-8")
    (logs / "recovery.jsonl").write_text(json.dumps(
        {"ts": _NOW, "stream": "recovery", "query": "恢复查询",
         "triggered": True, "recovered": True, "level_used": "L3",
         "strategy_used": "switch_engine", "steps_tried": 1}) + "\n",
        encoding="utf-8")
    (logs.parent / "circuit_breaker.json").write_text(json.dumps(
        {"engines": {"wikipedia": {"state": "open", "failures": 5,
                                   "opens": 1},
                     "zhihu": {"state": "closed", "failures": 0,
                               "opens": 0}}}), encoding="utf-8")


def test_ingest_three_sources_and_idempotent(ledger_env):
    _seed_sources(ledger_env)
    first = badcase_ledger.ingest()
    assert first["sources"] == 4  # 错误/空结果/恢复/breaker，正常与夹具排除
    assert first["added"] == 4
    again = badcase_ledger.ingest()
    assert again["added"] == 0 and again["total"] == 4  # 幂等
    kinds = json.loads(json.dumps(badcase_ledger.stats()))["by_kind"]
    assert kinds == {"query_error": 1, "empty_result": 1,
                     "recovery": 1, "breaker": 1}


def test_check_gate_threshold(ledger_env):
    _seed_sources(ledger_env)
    badcase_ledger.ingest()
    ok = badcase_ledger.check("1d", max_new=10)
    assert ok["ok"] is True and ok["new_badcases"] == 4
    strict = badcase_ledger.check("1d", max_new=3)
    assert strict["ok"] is False


def test_list_filter_by_kind(ledger_env):
    _seed_sources(ledger_env)
    badcase_ledger.ingest()
    rows = badcase_ledger._fetch("breaker", None, 10)
    assert len(rows) == 1 and rows[0]["engine"] == "wikipedia"


# ── 归因层（2026-11-10 补）：root_cause + Seen N× ────────────────────────────
# 这一组测试锁住 badcase_ledger 的「归因」与「复发」两件事。
#
# 回退测试说明（照 magpie 的验收标准）：`test_seen_n_promotion_gate` 与
# `test_external_ranking_excluded_from_promotion` 在**没有 Seen N× 判定的旧
# 实现**上必须挂——旧 `check()` 只看条数，返回的 dict 里没有 `recurrence_ok`
# 字段，`report["recurrence_ok"]` 直接 KeyError。这是刻意的：不验证「测试真能
# 抓到退步」的测试，等于没有测试。


def _seed_days(logs: Path, cause_detail: str, days: list[str],
               engine: str = "anysearch"):
    """按给定日期写 query.jsonl，每天一条同根因坏例。

    同一天多条时用不同小时——`ts` 进指纹，同秒同内容会被幂等去重掉。
    """
    lines = []
    for i, d in enumerate(days):
        hh = f"{(i % 23) + 1:02d}"
        lines.append(json.dumps({
            "ts": f"{d}T{hh}:00:00+08:00", "stream": "query",
            "query": "跨天坏例", "engines_used": [engine], "count": 0,
            "errors": [cause_detail], "mode": "auto"}))
    (logs / "query.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_classify_root_cause_by_evidence():
    """根因判定只吃自带证据，判不出返回 unattributed（不硬猜）。"""
    f = badcase_ledger.classify_root_cause
    assert f("query_error", "anysearch: timeout", "", "anysearch") == \
        "network"
    assert f("query_error", "401 unauthorized login required", "", "xhs") == \
        "auth_expired"
    assert f("query_error", "429 rate limit exceeded", "", "exa") == \
        "rate_limited"
    assert f("query_error", "404 not found: endpoint", "", "arxiv") == \
        "upstream_change"
    assert f("query_error", "count=0", "", "octen") == "unattributed"
    # kind 兜底：detail 无线索时按坏例类型给粗分类
    assert f("recovery", "level=L3 recovered=False", "", "") == \
        "recovery_missed"
    assert f("breaker", "state=open failures=5", "", "wikipedia") == \
        "engine_disabled"


def test_auth_not_misclassified_as_blocked():
    """带 auth 语义的 403 不得被判成被封锁——与 engine_failure 裁决顺序一致。"""
    f = badcase_ledger.classify_root_cause
    assert f("query_error", "403 members-only login required", "", "xhs") == \
        "auth_expired"
    assert f("query_error", "403 forbidden by WAF captcha", "", "exa") == \
        "source_blocked"


def test_ingest_records_root_cause(ledger_env):
    """入账的每条坏例都带 root_cause，且恒为词表成员。"""
    import engine_states
    _seed_sources(ledger_env)
    badcase_ledger.ingest()
    rows = badcase_ledger._fetch(None, None, 100)
    assert rows and all(r["root_cause"] for r in rows)
    assert {r["root_cause"] for r in rows} <= badcase_causes.ROOT_CAUSES


def test_seen_days_counts_distinct_days_not_rows(ledger_env):
    """seen_days 数的是不同日期数，不是条数——同一天 10 条只是余波。"""
    logs = ledger_env
    _seed_days(logs, "timeout", ["2026-11-01", "2026-11-01", "2026-11-01",
                                 "2026-11-02", "2026-11-03"])
    badcase_ledger.ingest()
    stat = next(s for s in badcase_ledger.root_cause_stats()
                if s["root_cause"] == "network")
    assert stat["count"] == 5
    assert stat["seen_days"] == 3, "同一天内的重复不该被算成复发"


def test_seen_n_promotion_gate(ledger_env):
    """同一根因跨 3 天出现 → 该挪进硬规则（Seen 3× 转正）。

    回退测试：旧 `check()` 没有 `recurrence_ok` 字段，这条会 KeyError 挂掉。
    """
    _seed_days(ledger_env, "timeout",
               ["2026-11-01", "2026-11-02", "2026-11-03"])
    badcase_ledger.ingest()
    report = badcase_ledger.check("30d", max_new=1000)
    assert report["volume_ok"] is True, "条数没超阈值，量的门该是绿的"
    assert report["recurrence_ok"] is False, "跨 3 天复发，该拦"
    assert report["ok"] is False
    assert [c["root_cause"] for c in report["recurrence"]] == ["network"]
    assert report["recurrence"][0]["seen_days"] == 3


def test_seen_below_threshold_does_not_promote(ledger_env):
    """只有 2 天 → 还在观察期，不拦。"""
    _seed_days(ledger_env, "timeout", ["2026-11-01", "2026-11-02"])
    badcase_ledger.ingest()
    report = badcase_ledger.check("30d", max_new=1000)
    assert report["recurrence_ok"] is True and report["ok"] is True


def test_external_ranking_excluded_from_promotion(ledger_env):
    """活网排名漂移不算 argo 的问题，不参与升级判定。

    回退测试：旧实现没有这个排除，任何根因都会被当成复发候选。
    """
    _seed_days(ledger_env, "ranking drift: results replaced by content farm",
               ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"])
    badcase_ledger.ingest()
    stat = next(s for s in badcase_ledger.root_cause_stats()
                if s["root_cause"] == "external_ranking")
    assert stat["seen_days"] == 4
    report = badcase_ledger.check("30d", max_new=1000)
    assert report["recurrence"] == [], "外部漂移不该进升级候选"
    assert report["recurrence_ok"] is True


def test_promote_days_is_configurable(ledger_env):
    """阈值可调：把阈值抬到 5 天后，3 天的复发不该再拦。"""
    _seed_days(ledger_env, "timeout",
               ["2026-11-01", "2026-11-02", "2026-11-03"])
    badcase_ledger.ingest()
    report = badcase_ledger.check("30d", max_new=1000, promote_days=5)
    assert report["recurrence_ok"] is True


def test_lessons_render_marks_promotion(ledger_env):
    """lessons 导出要标出「该挪进硬规则」的条目。"""
    _seed_days(ledger_env, "timeout",
               ["2026-11-01", "2026-11-02", "2026-11-03"])
    badcase_ledger.ingest()
    text = badcase_ledger.render_lessons()
    assert "Seen 3×" in text
    assert "该挪进硬规则" in text
    assert "network" in text


def test_old_schema_migrates_without_losing_rows(ledger_env, tmp_path):
    """老库（无 root_cause 列）补列而不是重建——历史坏例是证据，不能丢。"""
    db = ledger_env.parent / "badcase.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE badcases (id INTEGER PRIMARY KEY,"
                " kind TEXT NOT NULL, ts TEXT NOT NULL,"
                " query TEXT NOT NULL DEFAULT '',"
                " engine TEXT NOT NULL DEFAULT '',"
                " detail TEXT NOT NULL DEFAULT '',"
                " fingerprint TEXT NOT NULL UNIQUE)")
    con.execute("INSERT INTO badcases(kind, ts, query, engine, detail,"
                " fingerprint) VALUES ('query_error', '2026-11-01T00:00:00',"
                " 'q', 'anysearch', 'timeout', 'fp-old')")
    con.commit()
    con.close()
    # 连接时自动补列
    con2 = badcase_ledger._connect()
    cols = {row[1] for row in con2.execute("PRAGMA table_info(badcases)")}
    n = con2.execute("SELECT COUNT(*) FROM badcases").fetchone()[0]
    con2.close()
    assert "root_cause" in cols, "老库该被补上 root_cause 列"
    assert n == 1, "补列不该丢历史坏例"
