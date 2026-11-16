#!/usr/bin/env python3
"""tests/test_funnel.py — 阶段漏斗账与执行归因（2026-09-17）

背景：端到端指标（「0 结果」）只能说明变差，说明不了**在哪一层**变差。同一个
「0 结果」背后至少有三种病：引擎没抓到（`returned` 归零）、抓到了但被当重复削掉
（`deduped` 归零）、被过滤压没（`kept` 归零）。三者的处置完全不同，混成一句
「没有结果」就没法据此行动。

全程离线：假引擎顶替唯一网络出口（同 tests/test_search_benchmark.py 的做法），
不需要 API key，也不碰真实缓存。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import candidate_envelope  # noqa: E402
import search  # noqa: E402


# ── 假引擎后端（离线） ────────────────────────────────────────────────────────

class _Backend:
    """按引擎名发放预置条目；默认空列表。"""

    def __init__(self):
        self.docs: dict[str, list[dict]] = {}
        self.calls: list[str] = []

    def search(self, query, engine, n=5, timeout=None, depth="fast",
               mode="auto", **_):
        self.calls.append(engine)
        return [dict(d) for d in self.docs.get(engine, [])]


def _doc(engine: str, slug: str) -> dict:
    return {
        "title": f"distinct document {slug}",
        "url": f"https://example.invalid/{engine}/{slug}",
        "snippet": f"unique corpus {slug}",
        "source": engine,
    }


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    monkeypatch.setattr(search, "engine_search", b.search)
    monkeypatch.setattr(search, "_missing_env_for", lambda _eng: [])
    monkeypatch.setattr(search, "get_engines", lambda: {})
    monkeypatch.setattr(search, "get_execution_config",
                        lambda: {"retry_count": 0, "per_engine_budget_s": 10.0})
    monkeypatch.setattr(search, "get_cost_factor", lambda _eng: 1.0)
    return b


def _decision(engines: list[str]) -> dict:
    return {
        "engine": engines[0], "engines": engines, "engines_combo": engines,
        "reason": "测试固定决策", "domain": "general", "parallel": False,
        "tfidf_scores": [], "mode": "auto", "depth": "fast",
        "features": {}, "login_hint": {"needs_login": False, "reason": ""},
    }


def _run(backend, engines, n=5, query=None):
    """跑一次真实 execute_search（假引擎）。

    查询词**每次唯一**：负缓存（按 query+engine 记）落在会话状态目录里、跨用例
    留存，复用同一个查询词会让「上一个用例把引擎标记成无结果」污染本用例
    （实测：本文件的「引擎返回空」用例跑过之后，后续复用同查询词的用例直接
    跳过那两个引擎，returned 变 0）。test_full.py 用时间戳做唯一词是同一个原因。
    """
    from cache import SearchCache
    query = query or f"漏斗测试-{time.time_ns()}"
    return search.execute_search(query, _decision(engines), n, 20, "fast",
                                 SearchCache(), True)


# ── 纯函数 ────────────────────────────────────────────────────────────────────

def test_build_funnel_shape_and_order():
    f = search.build_funnel(2, 1, 10, 9, 9, 5, serp_dropped=1)
    # 存活六格按管线序，serp_dropped（损耗计数）插在 deduped 与 filtered 之间
    assert list(f) == ["routed", "called", "returned", "deduped",
                       "serp_dropped", "filtered", "kept"]
    assert f == {"routed": 2, "called": 1, "returned": 10, "deduped": 9,
                 "serp_dropped": 1, "filtered": 9, "kept": 5}


def test_funnel_collapse_names_first_zero():
    assert search.funnel_collapse(
        {"routed": 2, "called": 2, "returned": 0, "deduped": 0,
         "filtered": 0, "kept": 0}) == "returned"
    assert search.funnel_collapse(
        {"routed": 2, "called": 2, "returned": 10, "deduped": 0,
         "filtered": 0, "kept": 0}) == "deduped"
    assert search.funnel_collapse(
        {"routed": 2, "called": 2, "returned": 10, "deduped": 10,
         "filtered": 10, "kept": 0}) == "kept"
    # 有结果就不是塌陷
    assert search.funnel_collapse(
        {"routed": 2, "called": 2, "returned": 10, "deduped": 10,
         "filtered": 10, "kept": 5}) is None
    # serp_dropped 是损耗计数：0 = 没丢条目（正常），不参与塌陷归因——
    # 否则每条没有 SERP 丢弃的查询都会被误报成「serp_dropped 塌了」
    assert search.funnel_collapse(
        {"routed": 2, "called": 2, "returned": 10, "deduped": 10,
         "serp_dropped": 0, "filtered": 10, "kept": 5}) is None
    # 无漏斗（引入该字段之前写入的缓存条目）
    assert search.funnel_collapse(None) is None
    assert search.funnel_collapse({}) is None


def test_describe_funnel_is_compact_line():
    line = search.describe_funnel(
        {"routed": 2, "called": 1, "returned": 10, "deduped": 9,
         "filtered": 9, "kept": 5})
    assert line == "routed 2→called 1→returned 10→deduped 9→filtered 9→kept 5"
    # 带损耗格的漏斗：serp_dropped 按管线序插在 deduped 与 filtered 之间
    line = search.describe_funnel(
        {"routed": 1, "called": 1, "returned": 10, "deduped": 10,
         "serp_dropped": 10, "filtered": 0, "kept": 0})
    assert line == ("routed 1→called 1→returned 10→deduped 10→"
                    "serp_dropped 10→filtered 0→kept 0")
    assert search.describe_funnel(None) == ""


# ── 与真实执行管线对账 ────────────────────────────────────────────────────────

def test_funnel_counts_every_stage(backend):
    backend.docs["engine_a"] = [_doc("engine_a", "a1"), _doc("engine_a", "a2")]
    backend.docs["engine_b"] = [_doc("engine_b", "b1"), _doc("engine_b", "b2")]
    out = _run(backend, ["engine_a", "engine_b"])
    assert out["funnel"] == {"routed": 2, "called": 2, "returned": 4,
                            "deduped": 4, "serp_dropped": 0,
                            "filtered": 4, "kept": 4}
    assert out["count"] == 4


def test_funnel_accounts_serp_drops(backend):
    """SERP 守卫丢弃要单独落账：不再只体现为 deduped→filtered 的无名差值。

    外部实测（2026-11-16）：wechat_sogou fast 档搜到 10 条、全被闸掉，
    漏斗报 deduped 10 → filtered 0，六格里没有一格指明真凶。现在这批丢弃
    必须出现在 serp_dropped，且 count/funnel_collapse 不受影响。
    """
    serp_row = {"title": "百度搜索结果页", "url": "https://www.baidu.com/link?url=abc",
                "snippet": "jump page", "source": "engine_a"}
    backend.docs["engine_a"] = [_doc("engine_a", "a1"), serp_row]
    out = _run(backend, ["engine_a"])
    assert out["funnel"]["returned"] == 2
    assert out["funnel"]["deduped"] == 2
    assert out["funnel"]["serp_dropped"] == 1
    assert out["funnel"]["kept"] == 1
    assert out["count"] == 1


def test_funnel_localizes_empty_engines(backend):
    """引擎一条都没返回 → 塌在 returned，不是「去重削掉了」。"""
    backend.docs["engine_a"] = []
    backend.docs["engine_b"] = []
    out = _run(backend, ["engine_a", "engine_b"])
    assert out["count"] == 0
    assert out["funnel"]["returned"] == 0
    assert search.funnel_collapse(out["funnel"]) == "returned"


def test_funnel_separates_returned_from_deduped(backend):
    """两个引擎返回同一批 URL：catch 到「返回 4 条、去重后 2 条」的差别。

    这正是漏斗要能区分的那类归因——只报 2 条会让人以为引擎没抓到。
    """
    same = [{"title": "same doc", "url": "https://example.invalid/same",
             "snippet": "same corpus", "source": "engine_a"}]
    backend.docs["engine_a"] = same
    backend.docs["engine_b"] = [dict(same[0], source="engine_b")]
    out = _run(backend, ["engine_a", "engine_b"])
    assert out["funnel"]["returned"] == 2
    assert out["funnel"]["deduped"] <= 2
    assert out["funnel"]["deduped"] < out["funnel"]["returned"]


def test_funnel_kept_reflects_max_results(backend):
    backend.docs["engine_a"] = [_doc("engine_a", f"a{i}") for i in range(3)]
    backend.docs["engine_b"] = [_doc("engine_b", f"b{i}") for i in range(3)]
    out = _run(backend, ["engine_a", "engine_b"], n=2)
    assert out["funnel"]["returned"] == 6
    assert out["funnel"]["kept"] == 2
    assert out["count"] == 2


def test_funnel_routed_counts_combo_not_calls(backend):
    """routed 是路由选出的引擎数；某引擎空手而归不改变 routed。"""
    backend.docs["engine_a"] = [_doc("engine_a", "a1")]
    out = _run(backend, ["engine_a", "engine_b", "engine_c"])
    assert out["funnel"]["routed"] == 3
    assert set(backend.calls) == {"engine_a", "engine_b", "engine_c"}


# ── 归因进入局限声明 ──────────────────────────────────────────────────────────

def test_zero_result_limitation_names_collapse_stage(backend):
    """0 结果时局限声明要写明塌在哪一层，而不是只说「没结果」。"""
    backend.docs.clear()
    r = search.super_search("独家词汇 zzqqxxtt", n=3, depth="fast", mode="fast")
    if r.get("count") != 0:
        pytest.skip("离线环境下该查询拿到了结果，无法构造 0 结果场景")
    lims = r.get("limitations") or []
    assert any("pipeline emptied at" in x for x in lims), lims
    assert any("routed" in x and "kept" in x for x in lims), lims


def test_early_stop_limitation_appears_once_with_numbers(backend):
    """早停说明必须只有一条，且带 called/routed 数字。

    此前 generic 版（candidate_envelope）与带数字版（search）会同时出现——
    同一件事两条表述会互相削弱（读者该信哪条？）。
    """
    result = {
        "early_stopped": True,
        "count": 5,
        "funnel": {"routed": 3, "called": 1, "returned": 5, "deduped": 5,
                   "filtered": 5, "kept": 5},
    }
    lims = candidate_envelope.build_limitations(
        result, ["early_stopped: only 1 of 3 routed engines were queried; "
                 "coverage may be narrower than the route implies"])
    early = [x for x in lims if "early_stopped" in x]
    assert len(early) == 1, early
    assert "1 of 3" in early[0]


def test_generic_early_stop_kept_when_no_funnel():
    """没有漏斗（旧缓存条目）时仍要有泛化表述——不能让这层信息凭空消失。"""
    lims = candidate_envelope.build_limitations(
        {"early_stopped": True, "count": 3})
    assert any("early_stopped" in x for x in lims)


# ── 视图与缓存 ────────────────────────────────────────────────────────────────

def test_agent_view_keeps_funnel(backend):
    """agent 档必须保留漏斗：它是唯一能回答「0 结果卡在哪」的东西。"""
    backend.docs["engine_a"] = [_doc("engine_a", "a1")]
    out = _run(backend, ["engine_a"])
    slim = search._strip_for_agent(out)
    assert "funnel" in slim
    assert slim["funnel"] == out["funnel"]


class _OldEntryCache:
    """只模拟一种状态：命中一条「写入于引入漏斗之前」的缓存。

    不碰缓存内部结构（L1/L2/锁），因为这里要验的是**命中路径怎么组装输出**，
    不是缓存本身。
    """

    def __init__(self, results):
        self._hit = {"results": results, "_cache_level": "L2",
                     "engines_used": ["engine_a"], "engine_outcomes": []}

    def get(self, *_a, **_k):
        return dict(self._hit)

    def set(self, *_a, **_k):
        return None


def test_old_cache_entry_omits_funnel_not_null(backend):
    """存档里没有漏斗时，输出里该键**缺席**，不能写成 null。

    null 会被读成「漏斗算出来是空」，缺席才是「这次没有这个数据」。默认档与
    agent 档必须同一形态，否则同一件事有两种表述。
    """
    out = search.execute_search(
        "老缓存条目", _decision(["engine_a"]), 5, 20, "fast",
        _OldEntryCache([{"title": "t", "url": "https://example.invalid/x",
                         "snippet": "s", "source": "engine_a"}]),
        False)
    assert out.get("cached") is True
    assert "funnel" not in out


def test_funnel_survives_cache_roundtrip(backend, tmp_path, monkeypatch):
    """缓存命中时漏斗随存档一起回来——缺席比错误数字更容易被误读成「没数据」。"""
    from cache import SearchCache
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    backend.docs["engine_a"] = [_doc("engine_a", "a1"), _doc("engine_a", "a2")]
    cache = SearchCache()
    q = "漏斗缓存往返"
    first = search.execute_search(q, _decision(["engine_a"]), 5, 20, "fast",
                                  cache, False)
    second = search.execute_search(q, _decision(["engine_a"]), 5, 20, "fast",
                                   cache, False)
    assert second.get("cached") is True
    assert second.get("funnel") == first.get("funnel") == {
        "routed": 1, "called": 1, "returned": 2, "deduped": 2,
        "serp_dropped": 0, "filtered": 2, "kept": 2}


def test_funnel_called_counts_recovery_engines(backend):
    """`called` = 真正发起过调用的引擎数，救援链换上去的引擎一个都不许漏。

    改造前这里只数 `engine_outcomes ∪ raw_results`，而零结果救援链
    （`recovery.run_recovery` → `_recovery_executor`）走的**不是** dispatch：
    实测「python 怎么读csv」报出 `routed 2 → called 2 → returned 5`
    ——两个引擎交出了 5 条结果，真正打这一枪的救援引擎在漏斗里不存在。

    漏斗的全部用途就是定位塌陷点；一个会把真凶漏掉的漏斗比没有更危险，
    因为它给出的是一个**看似自洽**的错误结论。

    用例必须走到 L3（换引擎）才有意义：L1/L2 是**拿原引擎换查询词重试**，
    引擎名不变，漏掉它也看不出来。所以这里给一个非 general 的域 + 定向保底
    名单，让恢复链放行 L3 并真的换上新引擎。
    """
    from cache import SearchCache
    from engines import available_engines

    real = sorted(available_engines())[:2]
    assert real, "本机没有可用引擎，本用例无法构造 L3 场景"

    backend.docs = {}                     # 全部空 → 逼出恢复链
    dec = _decision(["empty_a", "empty_b"])
    dec["domain"] = "macro_data"          # 非 general → 恢复链放行 L3
    dec["engines_fallback"] = real
    out = search.execute_search(f"漏斗恢复测试-{time.time_ns()}", dec, 5, 20,
                                "fast", SearchCache(), True)

    invoked = set(backend.calls)
    assert len(invoked) > 2, (
        f"恢复链没有换引擎（只调用了 {sorted(invoked)}），本用例测不到 L3 路径")
    assert out["funnel"]["called"] == len(invoked), (
        f"called={out['funnel']['called']} 与实际发起调用的引擎数 "
        f"{len(invoked)} 不一致（invoked={sorted(invoked)}）")
