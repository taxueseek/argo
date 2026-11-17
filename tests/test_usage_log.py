#!/usr/bin/env python3
"""tests/test_usage_log.py — P2-6 本地使用日志字段格式 + 采集点落数"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
EGO_SCRIPTS = SKILL_DIR / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(EGO_SCRIPTS))

import usage_log  # noqa: E402


@pytest.fixture
def tele_dir(tmp_path, monkeypatch):
    """把本地使用日志目录注入 tmp，隔离真实 ~/.cache。"""
    d = tmp_path / "usage_log"
    monkeypatch.setenv("ARGO_USAGE_LOG_DIR", str(d))
    monkeypatch.setenv("ARGO_USAGE_LOG", "1")
    return d


def _read_lines(d: Path, stream: str) -> list[dict]:
    p = d / f"{stream}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


# ── 格式单测 ──────────────────────────────────────────────────────────────────

def test_emit_writes_append_only_jsonl(tele_dir):
    assert usage_log.emit("recovery", {"query": "q1", "triggered": True})
    assert usage_log.emit("recovery", {"query": "q2", "recovered": True})
    assert usage_log.emit("merge", {"merged_count": 3})

    rows = _read_lines(tele_dir, "recovery")
    assert len(rows) == 2
    assert rows[0]["query"] == "q1"          # 顺序保持
    assert rows[1]["recovered"] is True
    for r in rows:
        assert {"ts", "stream", "version"} <= set(r)  # 必填外壳字段
        assert r["stream"] == "recovery"
        assert r["version"] == 1
        assert r["ts"]                          # 非空时间戳
    assert _read_lines(tele_dir, "merge")[0]["merged_count"] == 3


def test_emit_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_USAGE_LOG_DIR", str(tmp_path / "usage_log"))
    monkeypatch.setenv("ARGO_USAGE_LOG", "0")
    assert usage_log.emit("recovery", {"query": "x"}) is False
    assert not (tmp_path / "usage_log" / "recovery.jsonl").exists()


def test_emit_failure_silent(tmp_path, monkeypatch):
    blocker = tmp_path / "usage_log"
    blocker.write_text("not a dir")  # 用同名文件堵住 mkdir
    monkeypatch.setenv("ARGO_USAGE_LOG_DIR", str(blocker))
    monkeypatch.setenv("ARGO_USAGE_LOG", "1")
    assert usage_log.emit("recovery", {"query": "x"}) is False  # 不抛异常


def test_tail_reads_recent(tele_dir):
    for i in range(15):
        usage_log.emit("route", {"engine": f"e{i}"})
    rows = usage_log.tail("route", n=10)
    assert len(rows) == 10
    assert rows[0]["engine"] == "e5"
    assert rows[-1]["engine"] == "e14"
    assert usage_log.tail("no_such_stream") == []


# ── 采集点：route 采样 ────────────────────────────────────────────────────────

def test_route_sample_emits(tele_dir, monkeypatch):
    from route_log import sample_route as _sample_route
    monkeypatch.setattr("route_log._ROUTE_SAMPLE_RATE", 1)
    monkeypatch.setattr("route_log._route_sample_counter", 0)

    _sample_route({}, {
        "domain": "academic",
        "engine": "arxiv",
        "engines": ["arxiv", "anysearch"],
        "confidence": 0.9,
        "mode": "auto",
        "features": {
            "lang_override": "en",
            "primary_lang": "zh",
            "script": "cjk",
            "has_compare": False,
            "has_technical": True,
            "chinese_ratio": 0.6,
            "intents": ["academic"],
        },
    })
    rows = _read_lines(tele_dir, "route")
    assert len(rows) == 1
    r = rows[0]
    assert r["domain"] == "academic"
    assert r["engine"] == "arxiv"
    assert r["lang_override"] == "en"
    assert r["primary_lang"] == "zh"
    assert r["chinese_ratio"] == 0.6


def test_route_sample_skips_without_features(tele_dir, monkeypatch):
    from route_log import sample_route as _sample_route
    monkeypatch.setattr("route_log._ROUTE_SAMPLE_RATE", 1)
    monkeypatch.setattr("route_log._route_sample_counter", 0)

    _sample_route({}, {"engine": "custom"})  # engine_override 直通无 features
    assert not (tele_dir / "route.jsonl").exists()


# ── 采集点：merge 融合 ────────────────────────────────────────────────────────

def test_merge_payloads_emits(tele_dir):
    import merge as merge_mod  # noqa: E402

    public = {
        "query": "q", "engine": "local_bing", "source": "local_bing",
        "results": [
            {"title": "Public T", "url": "https://example.com/a?utm_source=x", "snippet": "p"},
        ],
    }
    login = {
        "query": "q", "engine": "ego_browser_bing", "source": "ego-browser",
        "runtime": "ego", "login_state_used": True, "cache_eligible": False,
        "results": [
            {"title": "Login T", "url": "https://example.com/a", "snippet": "l"},
        ],
    }
    out = merge_mod.merge_payloads(public, login, query="q")

    rows = _read_lines(tele_dir, "merge")
    assert len(rows) == 1
    r = rows[0]
    assert r["public_count"] == out["public_count"] == 1
    assert r["login_count"] == out["login_count"] == 1
    assert r["merged_count"] == out["merged_count"]
    assert r["dual_sourced_count"] == out["dual_sourced_count"] >= 1
    assert r["conflicts"] == len(out["conflicts"])
    assert r["public_engine"] == "local_bing"
    assert r["login_engine"] == "ego_browser_bing"


# ── 夹具守卫（2026-10-25）：query 流注册表白名单 ──────────────────────────────
# 黑名单（is_synthetic_run）追不全新夹具名，生产 query.jsonl 曾三次漏进表外
# 引擎（最近 2026-10-24）；守卫在唯一落盘口反向判定。真值每次现取
# （get_registry 自带 mtime 指纹缓存），无进程级缓存需要用例间复位。

def test_guard_drops_fixture_engine_query(tele_dir):
    assert usage_log.emit("query", {"query": "q 25", "count": 1,
                                    "engines_used": ["quick_primary", "slow_backup"]}) is False
    assert usage_log.emit("query", {"query": "e2e abort probe 35", "count": 1,
                                    "engines_used": ["slow"]}) is False
    assert _read_lines(tele_dir, "query") == []


def test_guard_keeps_registry_engine_query(tele_dir):
    assert usage_log.emit("query", {"query": "真实查询", "count": 5,
                                    "engines_used": ["yuanbao", "octen"]})
    rows = _read_lines(tele_dir, "query")
    assert len(rows) == 1 and rows[0]["engines_used"] == ["yuanbao", "octen"]


def test_guard_alias_and_missing_field_pass(tele_dir):
    # 改名前历史别名豁免（与 usage_log_clean._ALIAS_KEEP 同账）
    assert usage_log.emit("query", {"query": "旧条目", "engines_used": ["openlibrary"]})
    # engines_used 缺失 / 空列表不是夹具信号，照常记录
    assert usage_log.emit("query", {"query": "无引擎字段"})
    assert usage_log.emit("query", {"query": "空列表", "engines_used": []})
    assert len(_read_lines(tele_dir, "query")) == 3


def test_guard_scoped_to_query_stream(tele_dir):
    # route 流的格式测试本身用假名（e0..e14，见 test_tail_reads_recent），
    # 守卫不得越界拦截 route。cache_hit 与 query 同形态，另有用例锁它被拦。
    assert usage_log.emit("route", {"engine": "e0", "engines": ["e0", "slow_backup"]})
    assert len(_read_lines(tele_dir, "route")) == 1
    assert usage_log.emit("route", {"engines_used": ["engine_a", "slow_backup"]})
    assert len(_read_lines(tele_dir, "route")) == 2


def test_guard_drops_fixture_engine_cache_hit(tele_dir):
    """cache_hit 与 query 同一道注册表守卫：表外名整条拒写。"""
    import engines  # 守卫只在 engines 已加载时判定；本用例要的就是已加载
    assert "yuanbao" in engines.available_engines()
    assert usage_log.emit("cache_hit", {
        "query": "夹具", "elapsed_ms": 1, "cached": True,
        "engines_used": ["engine_a"],
    }) is False
    assert usage_log.emit("cache_hit", {
        "query": "夹具", "engines_used": ["slow_backup", "yuanbao"],
    }) is False
    assert _read_lines(tele_dir, "cache_hit") == []


def test_guard_keeps_registry_engine_cache_hit(tele_dir):
    assert usage_log.emit("cache_hit", {
        "query": "真实查询", "elapsed_ms": 3, "count": 2, "cached": True,
        "engines_used": ["yuanbao"], "cache_level": "L1",
        "domain": "general", "mode": "auto", "depth": "fast",
    })
    rows = _read_lines(tele_dir, "cache_hit")
    assert len(rows) == 1
    assert rows[0]["stream"] == "cache_hit"
    assert rows[0]["elapsed_ms"] == 3
    assert rows[0]["cached"] is True
    assert rows[0]["engines_used"] == ["yuanbao"]


def _hit_cache(engines_used, **extra):
    class _Cache:
        def get(self, *_a, **_k):
            hit = {
                "results": [{"title": "t", "url": "https://example.invalid/x",
                             "snippet": "s"}],
                "engines_used": list(engines_used),
                "engines_combo": list(engines_used[:1] or ["anysearch"]),
                "_cache_level": "L1",
                "engine_outcomes": [],
            }
            hit.update(extra)
            return hit

        def set(self, *_a, **_k):
            return None

    return _Cache()


def _fixed_decision(engines):
    return {
        "engine": engines[0], "engines": engines, "engines_combo": engines,
        "reason": "测试固定决策", "domain": "general", "parallel": False,
        "tfidf_scores": [], "mode": "auto", "depth": "fast",
        "features": {}, "login_hint": {"needs_login": False, "reason": ""},
    }


_HIT_KEYS = {
    "query", "engine", "engines", "engines_combo", "cached", "cache_level",
    "domain", "elapsed_ms", "tfidf_scores", "route_reason", "login_hint",
    "results", "count", "engines_used", "mode", "depth", "reranker",
    "engine_outcomes", "errors", "time_filtered",
}


def test_execute_search_cache_hit_logs_without_new_response_keys(tele_dir):
    """命中写 cache_hit.jsonl，响应键集合与加日志前相同，且不写 query 流。"""
    import search
    q = "缓存命中记账" + ("长" * 80)
    out = search.execute_search(
        q, _fixed_decision(["yuanbao"]), 5, 20, "fast",
        _hit_cache(["yuanbao"]), False)
    assert out.get("cached") is True
    assert out["query"] == q
    assert set(out) == _HIT_KEYS
    assert "semantic_hit" not in out
    rows = _read_lines(tele_dir, "cache_hit")
    assert len(rows) == 1
    row = rows[0]
    assert row["stream"] == "cache_hit"
    assert row["cached"] is True
    assert row["query"] == q[:60]
    assert isinstance(row["elapsed_ms"], int)
    assert row["count"] == 1
    assert row["engines_used"] == ["yuanbao"]
    assert row["cache_level"] == "L1"
    assert "semantic_hit" not in row
    assert _read_lines(tele_dir, "query") == []


def test_execute_search_cache_hit_drops_fixture_engine(tele_dir, monkeypatch):
    """命中路径必须尝试记账，表外引擎被守卫丢掉。删掉 emit 或撤掉守卫都会红。"""
    import engines  # noqa: F401
    import search
    import search_entry
    calls = []
    real = search_entry._emit_usage

    def _wrap(stream, record):
        calls.append((stream, dict(record)))
        return real(stream, record)

    monkeypatch.setattr(search_entry, "_emit_usage", _wrap)
    out = search.execute_search(
        "夹具命中", _fixed_decision(["engine_a"]), 5, 20, "fast",
        _hit_cache(["engine_a"]), False)
    assert out.get("cached") is True
    assert set(out) == _HIT_KEYS
    assert calls and calls[0][0] == "cache_hit"
    assert calls[0][1]["engines_used"] == ["engine_a"]
    assert _read_lines(tele_dir, "cache_hit") == []
    assert _read_lines(tele_dir, "query") == []


def test_execute_search_semantic_hit_logs_flag_only(tele_dir):
    import search
    out = search.execute_search(
        "语义命中", _fixed_decision(["yuanbao"]), 5, 20, "fast",
        _hit_cache(["yuanbao"], _semantic_hit=True,
                   _semantic_query="另一条", _semantic_similarity=0.9),
        False)
    assert out.get("semantic_hit") is True
    assert out.get("semantic_query") == "另一条"
    rows = _read_lines(tele_dir, "cache_hit")
    assert len(rows) == 1
    assert rows[0]["semantic_hit"] is True
    assert "semantic_query" not in rows[0]


def test_cache_hit_silent_when_usage_log_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_USAGE_LOG", "0")
    monkeypatch.setenv("ARGO_USAGE_LOG_DIR", str(tmp_path))
    import search
    out = search.execute_search(
        "关闭日志", _fixed_decision(["yuanbao"]), 5, 20, "fast",
        _hit_cache(["yuanbao"]), False)
    assert out.get("cached") is True
    assert set(out) == _HIT_KEYS
    assert not (tmp_path / "cache_hit.jsonl").exists()


def test_cache_hit_log_does_not_import_engines(tmp_path):
    """命中账打开时仍不得为守卫 import engines。"""
    import os
    import subprocess
    script = r'''
import json, os, sys
os.environ["ARGO_USAGE_LOG"] = "1"
os.environ["ARGO_USAGE_LOG_DIR"] = os.environ["LOGDIR"]
os.environ["ARGO_STATE_DIR"] = os.environ["STATEDIR"]
os.environ["ARGO_NO_DAEMON"] = "1"
sys.path.insert(0, os.environ["SCRIPTS"])
import search

class Cache:
    def get(self, *a, **k):
        return {
            "results": [{"title": "t", "url": "https://example.invalid/x",
                         "snippet": "s"}],
            "engines_used": ["anysearch"],
            "engines_combo": ["anysearch"],
            "_cache_level": "L1",
            "engine_outcomes": [],
        }
    def set(self, *a, **k):
        return None

decision = {
    "engine": "anysearch", "engines": ["anysearch"],
    "engines_combo": ["anysearch"], "reason": "fixed",
    "domain": "general", "parallel": False, "tfidf_scores": [],
    "mode": "auto", "depth": "fast", "features": {},
    "login_hint": {"needs_login": False, "reason": ""},
}
out = search.execute_search("GitHub", decision, 5, 20, "fast", Cache(), False)
assert out.get("cached") is True, out.get("cached")
assert "engines" not in sys.modules
assert "stream" not in out and "semantic_hit" not in out
path = os.path.join(os.environ["LOGDIR"], "cache_hit.jsonl")
rows = [json.loads(x) for x in open(path, encoding="utf-8")]
assert len(rows) == 1 and rows[0]["stream"] == "cache_hit"
assert rows[0]["cached"] is True and rows[0]["engines_used"] == ["anysearch"]
print("ok")
'''
    env = os.environ.copy()
    env["LOGDIR"] = str(tmp_path / "logs")
    env["STATEDIR"] = str(tmp_path / "state")
    env["SCRIPTS"] = str(SCRIPT_DIR)
    env["ARGO_USAGE_LOG"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_guard_fail_open_when_registry_broken(tele_dir, monkeypatch):
    import engines as engines_mod

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(engines_mod, "available_engines", _boom)
    # 注册表不可用：守卫失效放行——不能因守卫丢真日志（fail-open）
    assert usage_log.emit("query", {"query": "x", "engines_used": ["whatever"]})


def test_route_sample_stateless_cli_and_dynamic_env(tele_dir, monkeypatch):
    """单次 CLI 调用（counter=0->1）按无状态采样仍可落盘，且 ARGO_ROUTE_SAMPLE_RATE 运行期生效。"""
    import route_log
    monkeypatch.setattr("route_log._ROUTE_SAMPLE_RATE", route_log._DEFAULT_ROUTE_SAMPLE_RATE)
    monkeypatch.setattr("route_log._route_sample_counter", 0)
    monkeypatch.setenv("ARGO_ROUTE_SAMPLE_RATE", "1")
    sample = {"domain": "academic", "engine": "arxiv", "engines": ["arxiv"],
              "confidence": 0.9, "mode": "auto", "features": {"intents": ["academic"]}}
    route_log.sample_route({}, sample)
    assert len(_read_lines(tele_dir, "route")) == 1

    # 未改 env，单次进程首调（counter 重置为 0）在 rate=20 时依赖 randrange==0 采样而非 1%20 永不命中
    monkeypatch.delenv("ARGO_ROUTE_SAMPLE_RATE", raising=False)
    monkeypatch.setattr("route_log._route_sample_counter", 0)
    monkeypatch.setattr("random.randrange", lambda _n: 0)
    route_log.sample_route({}, sample)
    assert len(_read_lines(tele_dir, "route")) == 2


def test_route_sample_env_change_takes_effect_without_module_cache(tele_dir, monkeypatch):
    """采样率每次调用现读 env：长驻进程改 env 后必须立即生效。

    此前实现把 env 解析结果缓存进模块级 `_ROUTE_SAMPLE_RATE`，于是
    「先设 1、后改 5」的进程在第 2 次调用起静默沿用 1（实测）；删掉 env
    也会退回被缓存的旧值而非默认 20。修后：显式覆盖槽（测试注入）优先，
    否则每次现读。"""
    import route_log
    monkeypatch.setattr("route_log._ROUTE_SAMPLE_RATE", route_log._DEFAULT_ROUTE_SAMPLE_RATE)
    monkeypatch.setattr("route_log._route_sample_counter", 0)
    sample = {"domain": "academic", "engine": "arxiv", "engines": ["arxiv"],
              "confidence": 0.9, "mode": "auto", "features": {"intents": ["academic"]}}

    monkeypatch.setenv("ARGO_ROUTE_SAMPLE_RATE", "1")
    route_log.sample_route({}, sample)
    monkeypatch.setenv("ARGO_ROUTE_SAMPLE_RATE", "2")
    route_log.sample_route({}, sample)
    route_log.sample_route({}, sample)
    # rate=2 时第 2、3 次调用（counter=2、3）只有 counter%2==0 那次落盘
    assert route_log._resolve_rate() == 2
    assert len(_read_lines(tele_dir, "route")) == 2


def test_emit_scrubs_credentials_and_url_params(tele_dir):
    """落盘前即使未开掩码也必须剔除查询中的密钥与敏感 URL 参数。"""
    fake_sk = "sk-" + "unitestkey" * 3
    fake_bearer = "Bearer " + "bearerval" * 3
    usage_log.emit("query", {
        "query": f"debug {fake_sk} https://x.test?api_key=secret999",
        "final_query": fake_bearer,
        "engines_used": ["anysearch"],
    })
    rows = _read_lines(tele_dir, "query")
    assert len(rows) == 1
    assert fake_sk not in rows[0]["query"]
    assert "secret999" not in rows[0]["query"]
    assert fake_bearer.split()[-1] not in rows[0]["final_query"]


def test_emit_mask_replaces_query_with_hash_when_enabled(tele_dir, monkeypatch):
    """开启 ARGO_USAGE_LOG_MASK=1 时，query/final_query 落盘只留长度与 SHA-256 摘要。"""
    monkeypatch.setenv("ARGO_USAGE_LOG_MASK", "1")
    usage_log.emit("query", {
        "query": "confidential project alpha",
        "final_query": "confidential project alpha",
        "count": 3,
        "engines_used": ["anysearch"],
    })
    rows = _read_lines(tele_dir, "query")
    assert len(rows) == 1
    assert rows[0]["query"].startswith("[masked:26:")
    assert "confidential" not in rows[0]["query"]
    assert rows[0]["final_query"] == rows[0]["query"]
    assert rows[0]["count"] == 3


def test_route_sample_records_effective_lang(tele_dir, monkeypatch):
    import route_log
    monkeypatch.setattr("route_log._ROUTE_SAMPLE_RATE", route_log._DEFAULT_ROUTE_SAMPLE_RATE)
    monkeypatch.setattr("route_log._route_sample_counter", 0)
    monkeypatch.setenv("ARGO_ROUTE_SAMPLE_RATE", "1")
    route_log.sample_route({}, {
        "domain": "general_search", "engine": "anysearch", "engines": ["anysearch"],
        "confidence": 0.7, "mode": "auto",
        "features": {"primary_lang": "en", "effective_lang": "es", "intents": ["news"]},
    })
    rows = _read_lines(tele_dir, "route")
    assert len(rows) == 1
    assert rows[0]["effective_lang"] == "es"


def test_export_anonymized_telemetry_strips_queries_and_paths(tmp_path):
    """遥测导出仅输出聚合统计与指纹，绝不泄露原始 query 与本机绝对路径。"""
    import sqlite3
    import route_telemetry_export

    state = tmp_path / "argo_state"
    log_dir = state / "usage_log"
    log_dir.mkdir(parents=True)
    secret_query = "private-token-query-xyz-98765"
    (log_dir / "route.jsonl").write_text(
        json.dumps({"domain": "general_search", "engine": "anysearch",
                    "effective_lang": "es", "intents": ["news"]}) + "\n"
        + json.dumps({"domain": "academic", "engine": "arxiv",
                      "effective_lang": "en", "intents": []}) + "\n",
        encoding="utf-8",
    )
    (log_dir / "query.jsonl").write_text(
        json.dumps({"query": secret_query, "final_query": secret_query, "count": 0}) + "\n",
        encoding="utf-8",
    )
    db_path = state / "adaptive.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE engine_perf (engine TEXT, domain TEXT, lang TEXT, empty INTEGER)")
    for _ in range(5):
        conn.execute("INSERT INTO engine_perf VALUES ('broken_eng', 'general', '', 1)")
    conn.commit()
    conn.close()

    report = route_telemetry_export.export_anonymized_telemetry(state, min_samples=5)
    dumped = json.dumps(report, ensure_ascii=False)
    assert secret_query not in dumped
    assert str(tmp_path) not in dumped
    assert report["summary"]["route_events"] == 2
    assert report["summary"]["query_events"] == 1
    assert report["summary"]["query_zero_result_rate"] == 1.0
    assert report["summary"]["route_fallback_rate"] == 0.5
    assert report["summary"]["adaptive"]["empty_lang_rows"] == 5
    assert len(report["summary"]["adaptive"]["high_empty_buckets"]) == 1
    assert len(report["anonymized_fingerprints"]) == 2


def test_cache_hit_guard_fail_open_before_engines_loaded():
    """cache_hit 守卫在 engines 未加载时 fail-open：表外名也放行。

    判据是「engines 不在 sys.modules」。改名或重构会让守卫静默失效
    （fail-open 方向安全，但守卫等于没有），这里把行为钉住。
    """
    import sys as _sys
    saved = _sys.modules.get("engines")
    _sys.modules.pop("engines", None)
    try:
        assert usage_log._canonical_engines("cache_hit") == frozenset()
        # 表外夹具名在 fail-open 下不被判为夹具 → 整条记录照常落盘
        assert usage_log._fixture_engines(
            {"engines_used": ["definitely_not_a_real_engine"]}, "cache_hit") == []
    finally:
        if saved is not None:
            _sys.modules["engines"] = saved
