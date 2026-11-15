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
    # 守卫不得越界拦截其他流
    assert usage_log.emit("route", {"engine": "e0", "engines": ["e0", "slow_backup"]})
    assert len(_read_lines(tele_dir, "route")) == 1


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
