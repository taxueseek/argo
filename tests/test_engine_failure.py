#!/usr/bin/env python3
"""test_engine_failure.py — 引擎失败归因与依赖声明回归测试。

覆盖 v2.4.2 新增的两项能力：
  1. engine_requires：声明式后端依赖检查 → 状态机 missing_dep
     （修复「脚本存在即 ready、but 后端 CLI 缺失导致静默返回空」）
  2. engine_failure：失败归因四分类（dependency/auth/upstream/network）
     （修复「熔断只说要不要用、不说为什么坏」）

判定优先级必须有测试锁住：声明先于运行时猜测，强信号先于弱信号，
否则归因会在真实混合信号下漂移。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engine_failure as ef  # noqa: E402
import engine_requires as er  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    er.clear_cache()
    yield
    er.clear_cache()


class TestRequiresParsing:
    """requires 声明的容错解析。"""

    def test_dict_form(self):
        spec = {"requires": [{"bin": "xhs", "fix": "npm i -g xiaohongshu-cli"}]}
        reqs = er.requirements_of(spec)
        assert len(reqs) == 1 and reqs[0]["bin"] == "xhs"

    def test_string_shorthand(self):
        assert [r["bin"] for r in er.requirements_of({"requires": ["yt-dlp"]})] == ["yt-dlp"]

    def test_no_requires(self):
        assert er.requirements_of({}) == []
        assert er.requirements_of(None) == []

    def test_malformed_ignored(self):
        spec = {"requires": [{"no_bin": 1}, "", None, 42]}
        assert er.requirements_of(spec) == []


class TestRequiresStatus:
    """依赖检查语义：必需缺失才算 missing_dep，可选缺失只降级。"""

    def test_missing_required_bin(self):
        st = er.requires_status({"requires": [
            {"bin": "definitely-not-a-real-bin-xyz", "fix": "install it"}]})
        assert st["dep_ready"] is False
        assert st["missing_deps"][0]["bin"] == "definitely-not-a-real-bin-xyz"
        assert st["dep_fixes"] == ["install it"]

    def test_present_bin_is_ready(self):
        # python3 必然存在（argo 自身依赖它）
        st = er.requires_status({"requires": [{"bin": "python3"}]})
        assert st["dep_ready"] is True and st["missing_deps"] == []

    def test_optional_missing_does_not_block(self):
        """optional 缺失不算故障：支撑 reddit 的 rdt 降级链语义。"""
        st = er.requires_status({"requires": [
            {"bin": "definitely-not-a-real-bin-xyz", "optional": True}]})
        assert st["dep_ready"] is True
        assert st["missing_deps"] == []
        # 但仍记录检查明细，便于 detail 展示「可选增强未装」
        assert st["requires"][0]["ok"] is False

    def test_no_requires_is_ready(self):
        st = er.requires_status({})
        assert st["dep_ready"] is True and st["requires"] == []

    def test_generic_fix_when_unspecified(self):
        st = er.requires_status({"requires": [{"bin": "definitely-not-a-real-bin-xyz"}]})
        assert st["dep_fixes"] and "definitely-not-a-real-bin-xyz" in st["dep_fixes"][0]


class TestVersionGate:
    """版本门槛比较（抽不出数字时不误报）。"""

    def test_ok_and_too_old(self):
        assert er._version_ok("2.5.0", "2.4.0") is True
        assert er._version_ok("2.3.0", "2.4.0") is False
        assert er._version_ok("2024.10.07", "2024.01.01") is True

    def test_no_minimum_passes(self):
        assert er._version_ok(None, None) is True
        assert er._version_ok("anything", None) is True

    def test_unparsable_does_not_false_positive(self):
        """版本串解析不出数字时视为通过——不把探测失败当成缺失。"""
        assert er._version_ok("unknown-build", "2.0.0") is True
        assert er._version_ok(None, "2.0.0") is True


class TestFailureClassification:
    """四分类：dependency / auth / upstream / network。"""

    def test_declared_missing_deps_wins(self):
        """声明事实优先于运行时猜测。"""
        r = ef.classify(missing_deps=[{"bin": "xhs"}],
                        output="Read timed out")  # 同时有网络信号
        assert r["category"] == ef.DEPENDENCY
        assert r["confidence"] == "high"

    def test_missing_env_is_auth(self):
        r = ef.classify(missing_env=["ARGO_XHS_COOKIE"])
        assert r["category"] == ef.AUTH and r["confidence"] == "high"

    def test_file_not_found_is_dependency(self):
        r = ef.classify(error=FileNotFoundError("[Errno 2] No such file: 'xhs'"))
        assert r["category"] == ef.DEPENDENCY

    def test_http_401_403_are_auth(self):
        for code in (401, 403):
            assert ef.classify(status_code=code)["category"] == ef.AUTH

    def test_http_404_410_and_5xx_are_upstream(self):
        for code in (404, 410, 500, 502):
            assert ef.classify(status_code=code)["category"] == ef.UPSTREAM

    def test_http_503_is_transient_not_upstream(self):
        """503 是源站暂时不受理，与 429 同族。

        HTTP 层已把 503 当「合规等待信号」处理（带 Retry-After 则等，
        见 test_stop_signal.py），归因层若判 upstream 会给出「需更新解析
        实现」的错误方向——两者必须同计算方式。
        """
        assert ef.classify(status_code=503)["category"] == ef.RATE_LIMITED

    def test_http_403_with_quota_message_is_not_auth(self):
        """403 里写着「套餐/额度不足」时不该让人去重新登录（登录改不了套餐）。

        实测来源：博查 AI Search 端点对本机 key 返回
        403 {"message":"You do not have enough money or package quota"}，
        结构化模态卡因此长期静默降级成普通网页结果。

        2026-10-18 起这批文案归 quota_exhausted 而不是 rate_limited：
        充值或到周期边界前重试都不会成功，必须由配额状态机停用，
        而不是冷却 60s 后再撞（octen 后备链死引擎事故）。
        """
        for body in ('{"message":"You do not have enough money or package quota"}',
                     "insufficient quota", "套餐额度不足"):
            r = ef.classify(status_code=403, output=body)
            assert r["category"] == ef.QUOTA_EXHAUSTED, (body, r["category"])
        # 反例：纯 403 / 反爬页特征仍按原语义
        assert ef.classify(status_code=403)["category"] == ef.AUTH
        assert ef.classify(status_code=403,
                           output="enable javascript and cookies")["category"] == ef.BLOCKED

    def test_http_408_and_connection_failure_are_network(self):
        # 408 是请求超时，状态码 0 表示连接层就没成（DNS/拒绝/重置）
        assert ef.classify(status_code=408)["category"] == ef.NETWORK
        assert ef.classify(status_code=0)["category"] == ef.NETWORK

    def test_text_auth_patterns(self):
        for txt in ("401 Unauthorized", "login required",
                    "cookie expired", "登录已过期，请重新登录"):
            assert ef.classify(output=txt)["category"] == ef.AUTH, txt

    def test_text_upstream_patterns(self):
        for txt in ("parse error: unexpected html", "Object Not Found",
                    "接口已变更", "api deprecated"):
            assert ef.classify(output=txt)["category"] == ef.UPSTREAM, txt

    def test_text_network_patterns(self):
        for txt in ("Read timed out", "Connection reset by peer",
                    "SSL certificate problem", "连接被重置"):
            assert ef.classify(output=txt)["category"] == ef.NETWORK, txt

    def test_unknown_when_no_signal(self):
        """信息不足时诚实返回 unknown，不硬猜。"""
        r = ef.classify(output="qwertyuiop")
        assert r["category"] == ef.UNKNOWN and r["confidence"] == "low"

    def test_every_category_has_action(self):
        """每类都必须给出可执行动作，否则归因没有意义。"""
        for cat in (ef.DEPENDENCY, ef.AUTH, ef.UPSTREAM, ef.NETWORK, ef.UNKNOWN):
            assert ef._ACTIONS[cat].strip()

    def test_text_priority_auth_over_network(self):
        """混合信号时 auth 优先于 network（更具体、更可操作）。"""
        r = ef.classify(output="403 Forbidden: connection reset")
        assert r["category"] == ef.AUTH


class TestExplainIntegration:
    """explain 自动带入已声明的依赖/密钥事实。"""

    def test_explain_uses_spec_requires(self):
        spec = {"requires": [{"bin": "definitely-not-a-real-bin-xyz"}]}
        r = ef.explain("someengine", spec=spec)
        assert r["category"] == ef.DEPENDENCY
        assert r["engine_id"] == "someengine"

    def test_explain_without_facts(self):
        r = ef.explain("someengine", spec={}, output="Read timed out")
        assert r["category"] == ef.NETWORK


class TestQualityQueriesDeclaration:
    """引擎自述质量查询集：让候选池型引擎用对计算方式，而非放宽阈值。"""

    def test_declared_queries_parsed(self, monkeypatch):
        import engine_validate as ev
        captured = {}

        def fake_health(engine_id, *, query=None, n=3, timeout=10.0, **kw):
            captured.setdefault("queries", []).append(query)
            return {"ok": True, "count": 2, "latency_ms": 100,
                    "field_complete_rate": 1.0, "error": None}

        monkeypatch.setattr(ev, "run_health", fake_health)
        monkeypatch.setattr(ev, "_get_spec", lambda eid: {
            "enabled": True,
            "quality_queries": [{"query": "Apple"}, {"query": "AI"}],
        })
        monkeypatch.setattr(ev, "env_ready", lambda *a, **k: True)

        res = ev.run_quality("someengine")
        assert captured["queries"] == ["Apple", "AI"]
        assert res["pass_rate"] == 1.0

    def test_falls_back_to_default_when_absent(self, monkeypatch):
        import engine_validate as ev
        captured = {}

        def fake_health(engine_id, *, query=None, n=3, timeout=10.0, **kw):
            captured.setdefault("queries", []).append(query)
            return {"ok": True, "count": 1, "latency_ms": 50,
                    "field_complete_rate": 1.0, "error": None}

        monkeypatch.setattr(ev, "run_health", fake_health)
        monkeypatch.setattr(ev, "_get_spec", lambda eid: {"enabled": True})
        monkeypatch.setattr(ev, "env_ready", lambda *a, **k: True)

        ev.run_quality("someengine")
        # 无声明时回落内置集，不静默变成空集
        assert len(captured["queries"]) == 5
        assert "Python asyncio" in captured["queries"]

    def test_thresholds_unchanged(self, monkeypatch):
        """自述集不改变通过阈值——标准不降，只是问对问题。"""
        import engine_validate as ev

        def fake_health(engine_id, *, query=None, n=3, timeout=10.0, **kw):
            # 全空结果
            return {"ok": False, "count": 0, "latency_ms": 100,
                    "field_complete_rate": 0.0, "error": "空结果"}

        monkeypatch.setattr(ev, "run_health", fake_health)
        monkeypatch.setattr(ev, "_get_spec", lambda eid: {
            "enabled": True, "quality_queries": [{"query": "Apple"}]})
        monkeypatch.setattr(ev, "env_ready", lambda *a, **k: True)

        res = ev.run_quality("someengine")
        assert res["ok"] is False and res["empty_rate"] == 1.0


class TestBlockedAndRateLimited:
    """v2.8.7 新增分类：blocked（封锁）/ rate_limited（限流）。

    核心裁决：403 有歧义——带封锁特征判 blocked，纯 403 判 auth；
    把「需要登录」和「被拦截」混为一谈会把用户支去错误的修复方向。
    """

    def test_403_with_block_sign_is_blocked(self):
        d = ef.classify(status_code=403,
                        output="<title>Just a moment...</title>challenge")
        assert d["category"] == ef.BLOCKED
        assert d["confidence"] == "high"

    def test_plain_403_is_auth(self):
        d = ef.classify(status_code=403, output="HTTP 403")
        assert d["category"] == ef.AUTH

    def test_members_only_403_is_auth(self):
        # auth 语义比封锁特征更具体：members-only 内容 403 ≠ 被封锁
        d = ef.classify(status_code=403, output="members-only content forbidden")
        assert d["category"] == ef.AUTH

    def test_429_is_rate_limited(self):
        d = ef.classify(status_code=429)
        assert d["category"] == ef.RATE_LIMITED
        assert "等待" in d["action"]

    def test_anti_bot_hint_wins_over_status(self):
        # 页面级检测显式命中是强证据：拦截页也常披着 200 的皮
        d = ef.classify(status_code=200, output="looks fine", anti_bot=True)
        assert d["category"] == ef.BLOCKED

    def test_text_block_sign(self):
        d = ef.classify(output="ddos-guard is protecting this site")
        assert d["category"] == ef.BLOCKED

    def test_text_rate_limit(self):
        d = ef.classify(output="Error: too many requests, slow down")
        assert d["category"] == ef.RATE_LIMITED

    def test_auth_beats_block_when_both_in_text(self):
        # 文本同时含 auth 与封锁信号：auth 更具体，先判
        d = ef.classify(output="please log in to continue")
        assert d["category"] == ef.AUTH

    def test_block_actions_mention_client_form(self):
        d = ef.classify(status_code=403, output="enable javascript and cookies")
        assert "客户端形态" in d["action"] or "浏览器" in d["action"]

    def test_explain_passes_anti_bot(self):
        d = ef.explain("eng_x", spec={}, anti_bot=True)
        assert d["category"] == ef.BLOCKED
        assert d["engine_id"] == "eng_x"


class TestBlockedKindInBreaker:
    """blocked / rate-limited 在熔断器里不累计 opens（封错人问题）。"""

    def _fresh_breaker(self, tmp_path):
        from circuit_breaker import CircuitBreaker
        return CircuitBreaker(state_path=str(tmp_path / "cb.json"))

    def test_blocked_never_opens(self, tmp_path):
        b = self._fresh_breaker(tmp_path)
        for _ in range(10):
            b.record_failure("e", kind="blocked")
        assert b.status("e")["state"] == "closed"

    def test_rate_limited_never_opens(self, tmp_path):
        b = self._fresh_breaker(tmp_path)
        for _ in range(10):
            b.record_failure("e", kind="rate-limited")
        assert b.status("e")["state"] == "closed"

    def test_error_still_opens(self, tmp_path):
        from circuit_breaker import FAILURE_THRESHOLD, CircuitBreaker
        b = self._fresh_breaker(tmp_path)
        for _ in range(FAILURE_THRESHOLD):
            b.record_failure("e", kind="error")
        assert b.status("e")["state"] == "open"

    def test_blocked_records_failure_and_kind(self, tmp_path):
        b = self._fresh_breaker(tmp_path)
        b.record_failure("e", kind="blocked")
        st = b.status("e")
        assert st["failures"] == 1
        assert st.get("last_kind") == "blocked"

    def test_rate_limited_exponential_cooldown_and_recovery(self, tmp_path, monkeypatch):
        """rate-limited 触发 60s -> 300s -> 900s 指数冷却，但不开闸不禁用；成功后清零。"""
        from circuit_breaker import CircuitBreaker, RATE_LIMIT_COOLDOWNS
        cb = self._fresh_breaker(tmp_path)
        now = 1_000_000.0
        monkeypatch.setattr("time.time", lambda: now)

        for idx, want_cd in enumerate(RATE_LIMIT_COOLDOWNS, 1):
            cb.record_failure("semantic_scholar", kind="rate-limited")
            st = cb.status("semantic_scholar")
            assert st["state"] == "closed"
            assert st["opens"] == 0
            assert st["cooldown_streak"] == idx
            assert abs(st["cooldown_remain"] - want_cd) <= 1
            allowed, reason = cb.allow("semantic_scholar")
            assert not allowed and reason.startswith("rate_limit_cooldown:")
            now += want_cd + 1
            allowed_after, _ = cb.allow("semantic_scholar")
            assert allowed_after

        cb.record_success("semantic_scholar")
        assert cb.status("semantic_scholar")["cooldown_streak"] == 0
        assert cb.status("semantic_scholar")["cooldown_remain"] == 0
