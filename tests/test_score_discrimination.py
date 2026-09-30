#!/usr/bin/env python3
"""引擎分数字段必须可区分——锁死「整批常量分拖垮早停」这一类缺陷。

## 这是一类 bug，不是一处

早停的质量守卫 `query_signals.score_clarity_ok` 把「全体 score 完全相等」
（std=0）判为**无区分度**并拒绝早停——这是刻意的（防「字段齐全、计数达标、
分数无区分度」的单引擎垃圾）。问题在于：多个 builder 曾给整批结果写同一个
**常量分**（上游 API 不返 score 字段时的默认档），于是守卫永远拒绝早停，
调度只能干等最慢的引擎耗满超时才返回。

实测（2026-09-30）：`python asyncio` 走默认路由，octen 669ms 就返回了足够的
3 条结果，墙钟却是 2351ms（另一引擎 anysearch 超时），因为 octen 三条分数
全是常量 0.5 → 守卫拒绝早停。`rank_score()` 已在全仓 56 处调用点使用，是
「base 保持引擎档位、按位次温和衰减」的唯一实现；漏用的 builder 就复现了
这个缺陷类。

## 守护的是不变量，不是具体数值

本文件只断言**可区分性**（唯一值数 > 1）与**保序**（单调不增），不锁具体
权重：调 RANK_DECAY 或改档位分都不该让这些用例变红——那是设计自由。真的
回归（有人又写回常量）才会红。

覆盖三类形态，各自对应一处漏用：
  1. 共用 JSON 条目解析器 `_parse_generic`（wikipedia 等 type:http 引擎）
  2. 自有 builder 的循环（octen 标准搜索、exa）
  3. 逐条 mapper 函数（github 的 repositories/issues/code 三端点）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _scores(results: list[dict]) -> list[float]:
    return [r["score"] for r in results]


def _assert_discriminating(results: list[dict], label: str) -> None:
    """整批结果的 score 必须可区分，且保序（单调不增）。"""
    scores = _scores(results)
    assert len(scores) >= 2, f"{label}: 用例构造错误，需要 ≥2 条结果"
    assert len(set(scores)) > 1, (
        f"{label}: {len(scores)} 条结果拿到同一个常量分 {scores[0]}——"
        f"早停的 score_clarity_ok 会判为无区分度而拒绝早停，"
        f"调度将干等最慢引擎耗满超时。请用 rank_score(base, rank) 叠加位次衰减。")
    assert scores == sorted(scores, reverse=True), (
        f"{label}: 分数字段未保序（应单调不增）：{scores}")


# ── 形态 1：共用 JSON 条目解析器 ──────────────────────────────────────────────

class TestGenericJsonParserDiscriminates:
    """上游不返 score 字段时，_parse_generic 必须给可区分的位次分。"""

    def test_items_without_score_field(self):
        from engines_base import _parse_generic
        payload = {"results": [
            {"title": f"t{i}", "url": f"http://x/{i}", "snippet": "s"}
            for i in range(5)
        ]}
        results = _parse_generic(payload, "wikipedia")
        assert len(results) == 5
        _assert_discriminating(results, "_parse_generic（无 score 字段）")

    def test_upstream_score_is_preserved_in_order(self):
        """上游给了递减分时，位次衰减不得打乱原有顺序。"""
        from engines_base import _parse_generic
        payload = {"results": [
            {"title": "a", "url": "http://x/1", "score": 0.9},
            {"title": "b", "url": "http://x/2", "score": 0.6},
            {"title": "c", "url": "http://x/3", "score": 0.3},
        ]}
        results = _parse_generic(payload, "test")
        _assert_discriminating(results, "_parse_generic（含上游分）")

    def test_relevance_score_also_discriminates(self):
        """relevance_score 别名同路径，不得退化成常量。"""
        from engines_base import _parse_generic
        payload = {"results": [
            {"title": f"t{i}", "url": f"http://x/{i}", "relevance_score": None}
            for i in range(4)
        ]}
        results = _parse_generic(payload, "test")
        _assert_discriminating(results, "_parse_generic（relevance_score=None）")


# ── 形态 2：自有 builder 的循环 ───────────────────────────────────────────────

class TestOwnBuilderLoopsDiscriminate:
    """octen 标准搜索 / exa 的循环必须叠加位次衰减。"""

    def _build_with_payload(self, monkeypatch, module_name: str, builder: str,
                            spec: dict, payload: dict, query: str = "q"):
        """打桩 http_open + get_env，让 builder 走到 REST 分支拿固定 payload。

        两个 builder 都按「有无 key」分流：无 key 时走另一条通道（octen 直接
        返回空、exa 转 MCP 匿名通道），那条路与本用例要锁的循环无关。给一个
        假 key 才能稳定命中被测循环（http_open 已打桩，不发真实请求）。
        """
        import io
        import json as _json
        mod = __import__(module_name)

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_http_open(req, timeout=None, engine=None, **kw):
            return _Resp(_json.dumps(payload).encode("utf-8"))

        monkeypatch.setattr(mod, "http_open", _fake_http_open)
        monkeypatch.setattr(mod, "get_env", lambda names: "test-key", raising=False)
        engine = getattr(mod, builder)(spec)
        return engine(query, 5, 8.0, depth="fast")

    def test_octen_standard_search(self, monkeypatch):
        payload = {"data": {"results": [
            {"title": f"t{i}", "url": f"http://x/{i}", "highlight": "h"}
            for i in range(4)
        ]}}
        results = self._build_with_payload(
            monkeypatch, "engines_builders_data", "_build_octen_engine",
            {"_name": "octen", "timeout": 6}, payload)
        assert results, "octen 标准搜索应返回结果（打桩 payload 有 4 条）"
        _assert_discriminating(results, "octen 标准搜索")

    def test_exa_search(self, monkeypatch):
        payload = {"results": [
            {"title": f"t{i}", "url": f"http://x/{i}", "text": "body"}
            for i in range(4)
        ]}
        results = self._build_with_payload(
            monkeypatch, "engines_builders_tech", "_build_exa_engine",
            {"_name": "exa", "timeout": 15}, payload)
        assert results, "exa 应返回结果（打桩 payload 有 4 条）"
        _assert_discriminating(results, "exa")


# ── 形态 3：逐条 mapper 函数 ─────────────────────────────────────────────────

class TestGithubEndpointDiscriminates:
    """github 三条端点的常量 mapper 必须由调用方统一叠加位次衰减。"""

    @pytest.mark.parametrize("endpoint,items", [
        ("repositories", [{"full_name": f"o/r{i}", "html_url": f"http://x/{i}",
                           "description": "d"} for i in range(4)]),
        ("issues", [{"title": f"i{i}", "html_url": f"http://x/{i}",
                     "state": "open"} for i in range(4)]),
        ("code", [{"name": f"f{i}", "html_url": f"http://x/{i}",
                   "path": "p", "repository": {"full_name": "o/r"}}
                  for i in range(4)]),
    ])
    def test_endpoint_scores_discriminate(self, monkeypatch, endpoint, items):
        import io
        import json as _json
        import engines_builders_tech as mod

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(mod, "http_open",
                            lambda req, timeout=None, engine=None, **kw:
                            _Resp(_json.dumps({"items": items}).encode("utf-8")))
        engine = mod._build_github_engine(
            {"_name": "github", "timeout": 10, "_endpoint": endpoint})
        results = engine("repo:o/r", 5, 8.0, depth="fast")
        assert results, f"github/{endpoint} 应返回结果"
        _assert_discriminating(results, f"github/{endpoint}")


# ── 端到端：修复后的形态真的能让守卫放行 ──────────────────────────────────────

class TestClarityGuardAcceptsDiscriminatingScores:
    """反向锁：可区分分数必须让 score_clarity_ok 放行（fail-open 之外的真放行）。"""

    def test_rank_score_sequence_passes_guard(self):
        from engines_base import rank_score
        from query_signals import score_clarity_ok
        results = [{"score": rank_score(0.5, i)} for i in range(3)]
        assert score_clarity_ok(results), (
            f"rank_score 生成的序列未通过平坦分守卫：{[r['score'] for r in results]}"
            f"——守卫生效但衰减幅度太小，早停仍不会触发")

    def test_constant_scores_are_rejected_by_guard(self):
        """常量分必须被守卫拒绝——这是守卫的设计意图，不要为了早停把它删掉。"""
        from query_signals import score_clarity_ok
        assert not score_clarity_ok([{"score": 0.5}] * 3), (
            "平坦分守卫对常量分失效了——它防的正是「字段齐全、计数达标、"
            "分数无区分度」的单引擎垃圾")
