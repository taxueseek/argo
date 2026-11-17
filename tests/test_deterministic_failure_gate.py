#!/usr/bin/env python3
"""确定性失败与元数据不得冒充「有效调用 / 有效证据」（三处同源口径）。

2026-11-17 活网基线审计（commit 0c768d9，`tmp/plan2_baseline_live_0c768d9.json`）
收敛出三条同源缺陷，本文件一次锁死：

1. **引擎内扇出**：thesportsdb 公网 403 后，builder 的
   `except Exception: continue` 仍把「变体 × 端点」全部打完——实测
   `Cristiano Ronaldo club` 连续 6 次 `HTTP 403 thesportsdb`、6195ms。
   确定性失败（配额/鉴权/封锁）重试只是多烧调用并推迟切备选。
2. **恢复链候选**：`recovery_runner` 的 `enabled` 取
   `available_engines()` 全量（含熔断/禁用/缺密钥），死源照样进 L3 候选。
3. **活网评测口径**：`_blob_from_results` 把 `engines_used` / `engine`
   拼进金标 blob，引擎名可冒充答案证据（gold 含 pubchem、nasa 这类源名
   时尤其危险），「关键词出现」被当成「答案正确」。

三处的判据来源唯一：失败类别走 `retry_policy`，可路由判定走
`engines.available_engines(routable_only=True)`。
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import engines_builders_media as media  # noqa: E402
import recovery  # noqa: E402
from recovery_runner import run_pipeline_recovery  # noqa: E402


class _FakeQuota:
    """quota_batch 最小替身：只被 .add(engine, bool) 调用。"""

    def add(self, *args, **kwargs):
        pass


def _fake_run_recovery_factory(captured):
    def _fake(*args, **kwargs):
        captured["enabled"] = kwargs.get("enabled")
        captured["args"] = args
        return [], recovery.RecoveryResult()

    return _fake


# ──────────────────────────────────────────────────────────────────────────────
# 1. 引擎内扇出：确定性失败一次就停
# ──────────────────────────────────────────────────────────────────────────────

def test_thesportsdb_aborts_variant_endpoint_fanout_on_403(monkeypatch):
    """403 后只允许发一次请求：变体与端点只是同一堵墙的不同叫法。

    旧实现 `except Exception: continue` 会把 3 个变体 × 3 个端点打满（9 次），
    实测线上是 6 次 403、6195ms。
    """
    calls: list[str] = []

    def fake_open(req, timeout=10.0, engine=""):
        calls.append(str(req))
        raise urllib.error.HTTPError(
            str(req), 403, "Forbidden", {}, None)

    monkeypatch.setattr(media, "http_open", fake_open)
    engine = media._build_thesportsdb_engine(
        {"_name": "thesportsdb", "timeout": 5})
    out = engine("Cristiano Ronaldo", n=5)
    assert out == []
    assert len(calls) == 1, f"403 后仍扇出 {len(calls)} 次: {calls}"


def test_thesportsdb_keeps_fanout_on_transient_failure(monkeypatch):
    """网络抖动不是确定性失败：仍按原语义继续试变体（不得一刀切中止）。"""
    calls: list[str] = []

    def fake_open(req, timeout=10.0, engine=""):
        calls.append(str(req))
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(media, "http_open", fake_open)
    engine = media._build_thesportsdb_engine(
        {"_name": "thesportsdb", "timeout": 5})
    assert engine("Cristiano Ronaldo", n=5) == []
    assert len(calls) > 1, "瞬时网络错误应继续扇出，不应被误判为确定性失败"


class _FakeResp:
    """http_open 的上下文管理器替身：只提供 .read()。"""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_thesportsdb_entity_query_stops_after_strong_hit(monkeypatch):
    """实体问拿到一条强命中就停：继续扇出只是同一答案的重复请求。

    旧实现把「变体 × 端点」打满（最多 15 次串行请求），实测
    `Cristiano Ronaldo club` 34.45s，直接吃掉 10s 引擎超时 → 0 结果。
    新口径：player/team 单条 rel ≥ 0.9 即停。
    """
    calls: list[str] = []
    payload = {"player": [{
        "idPlayer": "34145985", "strPlayer": "Cristiano Ronaldo",
        "strTeam": "Al-Nassr", "strSport": "Soccer",
        "strPosition": "Centre-Forward", "strNationality": "Portugal",
    }]}

    def fake_open(req, timeout=10.0, engine=""):
        calls.append(str(req))
        return _FakeResp(payload)

    monkeypatch.setattr(media, "http_open", fake_open)
    engine = media._build_thesportsdb_engine(
        {"_name": "thesportsdb", "timeout": 5})
    out = engine("Cristiano Ronaldo club", n=5)
    assert out, "强命中应产出结果"
    assert len(calls) == 1, f"强命中后仍扇出 {len(calls)} 次: {calls}"


def test_thesportsdb_retries_transient_failure_once(monkeypatch):
    """瞬时错误重试一次：早停后只剩一次请求，一次抖动不该让整台引擎归零。

    旧实现无重试，首个请求 SSL EOF 后只能靠后续变体/端点碰运气。
    """
    calls: list[str] = []
    payload = {"player": [{
        "idPlayer": "34145985", "strPlayer": "Cristiano Ronaldo",
        "strTeam": "Al-Nassr", "strSport": "Soccer",
        "strPosition": "Centre-Forward", "strNationality": "Portugal",
    }]}

    def fake_open(req, timeout=10.0, engine=""):
        calls.append(str(req))
        if len(calls) == 1:
            raise urllib.error.URLError("SSL EOF")
        return _FakeResp(payload)

    monkeypatch.setattr(media, "http_open", fake_open)
    engine = media._build_thesportsdb_engine(
        {"_name": "thesportsdb", "timeout": 5})
    out = engine("Cristiano Ronaldo club", n=5)
    assert out, "重试后应拿到结果"
    assert len(calls) == 2, f"重试次数不对: {len(calls)}"


def test_thesportsdb_event_query_keeps_fanout_after_hit(monkeypatch):
    """赛事问不套实体早停线：一条命中不代表「问齐了」，多样性优先。"""
    calls: list[str] = []
    payload = {"event": [{
        "idEvent": "1", "strEvent": "World Cup", "strSport": "Soccer",
        "strLeague": "FIFA", "dateEvent": "2022-11-20",
    }]}

    def fake_open(req, timeout=10.0, engine=""):
        calls.append(str(req))
        return _FakeResp(payload)

    monkeypatch.setattr(media, "http_open", fake_open)
    engine = media._build_thesportsdb_engine(
        {"_name": "thesportsdb", "timeout": 5})
    out = engine("World Cup 2022 final", n=5)
    assert out, "赛事结果应产出"
    assert len(calls) > 1, "赛事问被误套实体早停线，扇出被提前截断"


# ──────────────────────────────────────────────────────────────────────────────
# 2. 恢复链候选：只要「当前可路由」
# ──────────────────────────────────────────────────────────────────────────────

def test_recovery_runner_requests_routable_engines(monkeypatch):
    """`enabled` 必须是 routable 集合：熔断/禁用/缺密钥的源不得进 L3 候选。

    旧实现调 `available_engines_fn()`（全量），死源（实测 thesportsdb）照样
    被 family_candidates 选为恢复候选，白跑一次注定失败的调用。
    """
    seen: dict = {}

    def fake_available(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        if kwargs.get("routable_only"):
            return ["anysearch", "wikipedia"]
        # 旧签名拿到的全量：含已熔断的死源
        return ["anysearch", "wikipedia", "thesportsdb"]

    captured: dict = {}
    monkeypatch.setattr(recovery, "run_recovery",
                        _fake_run_recovery_factory(captured))

    run_pipeline_recovery(
        query="库里 球队", merged=[], raw_results={}, decision={},
        engines=[], available_engines_fn=fake_available, max_results=5,
        max_rec_level="L0", echo_triggered=False, primary_grace_s=0.0,
        timeout=1.0, depth="fast", mode="fast", since_iso=None, until_iso=None,
        skip_cache=True, quota_batch=_FakeQuota(),
        engine_search_fn=lambda *a, **k: [],
    )

    assert seen["kwargs"].get("routable_only") is True, (
        f"恢复链未按可路由取源: args={seen['args']} kwargs={seen['kwargs']}")
    assert "thesportsdb" not in (captured.get("enabled") or set()), (
        f"死源进了恢复候选: {captured.get('enabled')}")


def test_recovery_runner_tolerates_old_available_engines_signature(monkeypatch):
    """旧调用方（`available_engines_fn()` 不收参数）回退全量，不因收紧而炸。"""
    def old_available():
        return ["anysearch"]

    captured: dict = {}
    monkeypatch.setattr(recovery, "run_recovery",
                        _fake_run_recovery_factory(captured))

    run_pipeline_recovery(
        query="x", merged=[], raw_results={}, decision={},
        engines=[], available_engines_fn=old_available, max_results=5,
        max_rec_level="L0", echo_triggered=False, primary_grace_s=0.0,
        timeout=1.0, depth="fast", mode="fast", since_iso=None, until_iso=None,
        skip_cache=True, quota_batch=_FakeQuota(),
        engine_search_fn=lambda *a, **k: [],
    )

    assert captured.get("enabled") == {"anysearch"}


# ──────────────────────────────────────────────────────────────────────────────
# 3. 活网评测：引擎名不是答案证据
# ──────────────────────────────────────────────────────────────────────────────

def test_live_blob_excludes_query_level_engine_identifiers():
    """`engines_used` / `engine` 是「谁答的」元数据，不得进金标 blob。

    否则 gold 里的源名（pubchem、nasa 同类）会命中在引擎标识上，
    一条正文毫不相关的结果也能判 hit。
    """
    from matrix_search_eval import _blob_from_results

    results = [{"title": "无关结果", "snippet": "无关摘要", "source": "example.com"}]
    blob = _blob_from_results(results)
    assert "pubchem" not in blob
    assert "thesportsdb" not in blob
