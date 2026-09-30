#!/usr/bin/env python3
"""tests/test_audit_20260930_fixes.py — 2026-09-30 审查轮修复的红绿回归门。

每条对应审查报告的一个编号缺陷；先在未修复代码上红，修复后绿。
  1  set_engine 空结果负缓存覆盖同键好条目（与 combo 层 2026-09-19 修复同族）
  2  _adaptive_ttl 突破域帽 / 压缩日末延长（docstring 承诺「上限为域 TTL」）
  3  route_cache 只有条数帽没有体积帽（b23a053 提交声明与实现不符）
  4  phrase_filter 全角引号失明 + 连字符/下划线变体误伤
  5  net_proxy ${VAR} 只支持整值插值，内嵌/部分插值被当字面量
  6  变体召回波无波级死线（2 变体 × 2 引擎 × 6s 最坏 +24s）
  7  变体 gate 未感知 early_stop_min_results（答案型域每次白付变体调用）
  8  变体调用漏传 engine_domain/sub_domain（用户约束被变体旁路）
  9  recovery L3 未透传 mode，fast 档恢复链可选中付费引擎
 11  local_seek 落盘缓存缺 schema 版本 + 命中返回共享对象
 12  local-search 健康存盘非原子 + 30 天陈旧条目全量写回复活
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import cache  # noqa: E402
from cache import SearchCache  # noqa: E402


class _Req:
    """variant_recall 消费的 duck-typed 请求对象。"""

    def __init__(self, query: str):
        self.retrieval_query = query
        self.mode = "auto"
        self.depth = "fast"
        self.engines = ["e1", "e2"]
        self.max_results = 5
        self.eff_timeout = 6
        self.since_iso = None
        self.until_iso = None
        self.decision = None
        self.engine_domain = None
        self.engine_sub_domain = None


# ── 1. set_engine 空结果不得覆盖仍有寿命的好条目 ─────────────────────────────

def test_set_engine_empty_preserves_live_entry():
    sc = SearchCache(db_path=":memory:")
    good = [{"url": "https://a.example/x", "title": "good", "snippet": "s"}]
    sc.set_engine("q1", "eng1", 10, list(good), domain="general", ttl=3600)
    assert sc.get_engine("q1", "eng1", 10), "前置：好条目应可命中"
    sc.set_engine("q1", "eng1", 10, [], domain="general")  # 网络抖动返回空
    assert sc.get_engine("q1", "eng1", 10), "空结果负缓存覆盖了仍有寿命的好条目"


# ── 2. 自适应 TTL：不超域帽，也不压缩日末延长 ────────────────────────────────

def _seed_combo(sc: SearchCache, q: str, e: str, d: str, results: list) -> None:
    sc._write(sc._key(q, e, 0, d, "auto", "fast", kind="combo"),
              q, e, 0, {"results": results}, d, 300)


def test_adaptive_ttl_respects_domain_cap(monkeypatch):
    monkeypatch.setattr(cache, "is_freshness_sensitive_query", lambda q: False)
    sc = SearchCache(db_path=":memory:")
    q, e, d = "市盈率 计算方法", "sina", "stock_query"
    results = [{"url": "https://x/1", "title": "t1"}]
    _seed_combo(sc, q, e, d, results)
    base = 300
    out = sc._adaptive_ttl(q, e, d, base, results, mode="auto", depth="fast")
    assert out <= base, f"稳定内容把 TTL 从 {base} 延到 {out}，突破域帽"


def test_adaptive_ttl_preserves_day_end_extension(monkeypatch):
    monkeypatch.setattr(cache, "is_freshness_sensitive_query", lambda q: False)
    sc = SearchCache(db_path=":memory:")
    q, e, d = "机器学习 入门 教程", "wiki", "chinese_tech_deep"
    results = [{"url": "https://x/1", "title": "t1"}]
    _seed_combo(sc, q, e, d, results)
    monkeypatch.setattr(sc, "resolve_ttl", lambda domain, query=None: 7200)
    base = 84900  # 日末延长后的 base
    out = sc._adaptive_ttl(q, e, d, base, results, mode="auto", depth="fast")
    assert out >= base, f"日末延长的 base({base}) 被压缩到 {out}"


# ── 3. route_cache 体积帽 ────────────────────────────────────────────────────

def test_route_cache_prune_byte_cap():
    import route_cache as rc
    cap = getattr(rc, "_ROUTE_CACHE_MAX_BYTES", None)
    assert cap, "route_cache 缺体积帽常量（b23a053 声称与实现不符）"
    big = "x" * 40_000
    entries = {f"k{i}": {"ts": time.time() - i,
                         "decision": {"engines_fallback": [big] * 12}}
               for i in range(30)}
    pruned = rc._route_cache_prune(entries)
    size = len(json.dumps(pruned, ensure_ascii=False).encode("utf-8"))
    assert size <= cap, f"prune 后仍 {size} 字节，超过体积帽 {cap}"


# ── 4. phrase_filter：全角引号 + 连字符/下划线折叠 ───────────────────────────

def test_phrase_filter_fullwidth_quotes():
    from phrase_filter import extract_phrases
    assert extract_phrases("“GPT 5” 价格") == ["GPT 5"], "全角引号短语失明"


def test_phrase_filter_hyphen_variant_not_dropped():
    from phrase_filter import apply_phrase_filter, extract_phrases
    rs = [{"title": "How to use GPT-5", "snippet": "guide"},
          {"title": "unrelated", "snippet": "nothing here"}]
    kept, dropped = apply_phrase_filter(rs, extract_phrases('"GPT 5" 价格'))
    assert any("GPT-5" in r["title"] for r in kept), "连字符变体被误剔除"
    assert dropped == 1


# ── 5. net_proxy ${VAR} 内嵌插值 ─────────────────────────────────────────────

def test_interp_env_embedded_var(monkeypatch):
    import net_proxy
    monkeypatch.setattr(net_proxy, "_argo_env",
                        lambda name: "proxyhost" if name == "P_HOST" else "")
    assert net_proxy._interp_env("http://${P_HOST}:7890") == "http://proxyhost:7890"
    assert net_proxy._interp_env("${P_HOST}") == "proxyhost"
    assert net_proxy._interp_env("http://${P_MISSING}:7890") is None
    assert net_proxy._interp_env("direct") is None


# ── 6/7/8. 变体召回波：死线 / early_stop 门 / domain 透传 ────────────────────

def test_variant_wave_budget(monkeypatch):
    import variant_recall as vr
    import query_enhance
    monkeypatch.setattr(query_enhance, "retrieval_variants",
                        lambda base, max_n=3: [base, "alpha variant one", "alpha variant two"])
    monkeypatch.setattr(vr, "_WAVE_BUDGET_S", 0.32)
    calls = []

    def fake_search(q, eng, **kw):
        calls.append((q, eng))
        time.sleep(0.15)
        return [{"url": "u", "title": "t"}]

    extra = vr.variant_recall_wave(_Req("alpha beta"), fake_search)
    assert extra, "波应至少补回一轮结果"
    assert len(calls) < 4, f"波级死线缺失：4 次调用全部跑完（{calls}）"


def test_variant_gate_respects_early_stop_min():
    import variant_recall as vr
    req = _Req("q")
    req.decision = {"early_stop_min_results": 1}
    clean = [[{"url": "u", "title": "t"}]]  # 1 条已满足答案型域的完整判据
    assert not vr.should_variant_recall(req, clean, max_results=10), \
        "early_stop_min_results=1 时 1 条结果仍触发变体波"


def test_variant_passes_domain_kwargs(monkeypatch):
    import variant_recall as vr
    import query_enhance
    monkeypatch.setattr(query_enhance, "retrieval_variants",
                        lambda base, max_n=3: [base, "alpha variant one"])
    captured = {}

    def fake_search(q, eng, **kw):
        captured.update(kw)
        return [{"url": "u", "title": "t"}]

    req = _Req("alpha beta")
    req.engine_domain = "finance"
    req.engine_sub_domain = "cn"
    vr.variant_recall_wave(req, fake_search)
    assert captured.get("domain") == "finance", "用户 --domain 约束被变体旁路"
    assert captured.get("sub_domain") == "cn"


# ── 9. recovery L3 透传 mode ─────────────────────────────────────────────────

def test_recovery_l3_mode_passthrough(monkeypatch):
    import recovery
    import engine_families
    captured = {}

    def fake_fc(family, lang="*", **kw):
        captured.setdefault("modes", []).append(kw.get("mode"))
        return []

    monkeypatch.setattr(engine_families, "family_candidates", fake_fc)
    monkeypatch.setattr(recovery, "_GENERAL_FREE_COMBO", ())
    recovery.pick_alternative_engines(["anysearch"], [], enabled=None, mode="fast")
    assert captured.get("modes"), "family_candidates 未被调用"
    assert all(m == "fast" for m in captured["modes"]), \
        f"L3 未透传 mode，fast 档可选中付费引擎：{captured['modes']}"


# ── 10. _parse_generic：显式 0 分不得被 or 链膨胀成 0.5 ──────────────────────

def test_parse_generic_zero_score_not_inflated():
    import engines_base as eb
    out = eb._parse_generic(
        {"results": [{"title": "t", "url": "https://u/1", "score": 0}]}, "eng")
    assert out, "前置：_parse_generic 应产出结果"
    assert out[0]["score"] < 0.25, f"显式 0 分被膨胀成 {out[0]['score']}"


# ── 11. local_seek 落盘缓存：schema 版本 + 命中返回拷贝 ──────────────────────

def test_local_seek_disk_cache_copy_and_schema(tmp_path, monkeypatch):
    import local_seek as ls
    monkeypatch.setattr(ls, "_seek_disk_cache_path", lambda: tmp_path / "lsc.json")
    ls._seek_disk_cache_put("k1", [{"url": "file:///a", "title": "A"}])
    got1 = ls._seek_disk_cache_get("k1")
    assert got1 and got1[0]["title"] == "A"
    got1[0]["title"] = "MUTATED"
    got2 = ls._seek_disk_cache_get("k1")
    assert got2[0]["title"] == "A", "命中返回共享对象，调用方就地改写污染缓存"
    data = json.loads((tmp_path / "lsc.json").read_text(encoding="utf-8"))
    assert data["k1"].get("v") == 1, "落盘缓存条目缺 schema 版本字段"


# ── 12. local-search 健康存盘：陈旧条目过滤（原子性由实现保证） ──────────────

def test_health_save_drops_stale_entries(tmp_path):
    lr = Path(__file__).resolve().parent.parent / "sub-skills" / "local-search"
    sys.path.insert(0, str(lr))
    import engine_registry as er
    reg = er.EngineRegistry(health_state_path=tmp_path / "health.json")
    reg._health = {
        "fresh": {"last_checked": time.time(), "available": True},
        "stale": {"last_checked": time.time() - 40 * 86400, "available": False},
    }
    reg._save_health()
    data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert "stale" not in data, "30 天陈旧条目被原样写回（复活）"
    assert "fresh" in data
