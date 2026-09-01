#!/usr/bin/env python3
"""test_fetch_deadline.py — fetch_v3 全局 deadline 回归（2026-08-31 审查修复）。

覆盖：
  - ARGO_FETCH_DEADLINE_S 预算耗尽 → 停链 + deadline_exhausted 标记
  - ARGO_FETCH_DEADLINE_S=0 → 关闭约束（旧行为）
  - tinyfish 短内容成功响应不再短路 Chrome（内容质量降级链闭环）
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo_test_dl_"))

import fetch_v3  # noqa: E402

_URL = "https://example.com/page"


@pytest.fixture
def chain_env(monkeypatch):
    """通用隔离：robots 放行 + 禁用 md 变体 + 无缓存。"""
    import robots_guard
    monkeypatch.setattr(robots_guard, "robots_blocked",
                        lambda u, timeout=5.0: False)
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")


def _fail(method, secs=0.0):
    def fn(url, max_chars=8000, timeout=8.0):
        if secs:
            time.sleep(secs)
        return {"url": url, "content": "", "html": "", "title": "", "length": 0,
                "success": False, "error": method, "fetch_method": method}
    return fn


def _shell(url, max_chars=8000, timeout=8.0):
    return {"url": url, "content": "", "html": "<html><body>x</body></html>",
            "title": "", "length": 0, "success": False, "error": None,
            "fetch_method": "http"}


def _result(method, content):
    return {"url": _URL, "content": content, "html": "", "title": "t",
            "length": len(content), "success": True, "error": None,
            "fetch_method": method}


def test_deadline_halts_chain_and_marks(chain_env, monkeypatch):
    """1s 预算 + 每级 0.3s 耗时：链在预算内截断并打标记。"""
    monkeypatch.setenv("ARGO_FETCH_DEADLINE_S", "1.0")
    monkeypatch.setenv("ARGO_FETCH_IMPERSONATE", "1")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "1")
    monkeypatch.setenv("TINYFISH_API_KEY", "sk-test")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _fail("http", 0.3))
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch", _fail("mobile", 0.3))
    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", _fail("tls", 0.3))
    monkeypatch.setattr(fetch_v3, "_wayback_fetch", _fail("wayback", 0.3))
    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch", _fail("tinyfish", 0.3))
    monkeypatch.setattr(fetch_v3, "_browser_fetch", _fail("browser", 0.3))

    t0 = time.time()
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=True)
    elapsed = time.time() - t0
    assert out.get("deadline_exhausted") is True
    assert elapsed < 2.0, f"deadline 未生效：耗时 {elapsed:.2f}s"


def test_deadline_zero_disables(chain_env, monkeypatch):
    """ARGO_FETCH_DEADLINE_S=0：约束关闭，全链跑完（旧行为保留）。"""
    monkeypatch.setenv("ARGO_FETCH_DEADLINE_S", "0")
    monkeypatch.setenv("ARGO_FETCH_IMPERSONATE", "1")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "1")
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.setattr(fetch_v3, "_http_fetch", _fail("http", 0.05))
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch", _fail("mobile", 0.05))
    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", _fail("tls", 0.05))
    monkeypatch.setattr(fetch_v3, "_wayback_fetch", _fail("wayback", 0.05))
    monkeypatch.setattr(fetch_v3, "_browser_fetch",
                        lambda url, mc=8000, timeout=15.0, actions=None:
                        _result("chrome_cdp", "full content" * 50))

    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=True)
    assert out.get("deadline_exhausted") is None
    assert out["fetch_method"] == "chrome_cdp"


def test_tinyfish_short_content_falls_through_to_browser(chain_env, monkeypatch):
    """tinyfish 返回 success 但内容 <100 字：不得短路 Chrome（降级链质量闭环）。"""
    monkeypatch.setenv("ARGO_FETCH_TINYFISH", "1")
    monkeypatch.setenv("ARGO_FETCH_IMPERSONATE", "0")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "0")
    monkeypatch.setenv("ARGO_FETCH_DEADLINE_S", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _shell)
    monkeypatch.setattr(fetch_v3, "_wayback_fetch", _fail("wayback"))
    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch",
                        lambda url, mc=8000, timeout=8.0:
                        _result("tinyfish", "ok"))
    browser_calls = []

    def _browser(url, mc=8000, timeout=15.0, actions=None):
        browser_calls.append(1)
        return _result("chrome_cdp", "full content" * 50)

    monkeypatch.setattr(fetch_v3, "_browser_fetch", _browser)
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=True)
    assert browser_calls, "短内容 tinyfish 成功也应继续降级 Chrome"
    assert out["fetch_method"] == "chrome_cdp"


def test_tinyfish_good_content_still_short_circuits(chain_env, monkeypatch):
    """tinyfish 内容充分（≥100 字）：保持短路 Chrome 的优化路径。"""
    monkeypatch.setenv("ARGO_FETCH_TINYFISH", "1")
    monkeypatch.setenv("ARGO_FETCH_IMPERSONATE", "0")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "0")
    monkeypatch.setenv("ARGO_FETCH_DEADLINE_S", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _shell)
    monkeypatch.setattr(fetch_v3, "_wayback_fetch", _fail("wayback"))
    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch",
                        lambda url, mc=8000, timeout=8.0:
                        _result("tinyfish", "真实正文内容。" * 40))

    def _no_browser(*a, **k):
        raise AssertionError("内容充分的 tinyfish 成功不应再起本地浏览器")

    monkeypatch.setattr(fetch_v3, "_browser_fetch", _no_browser)
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=True)
    assert out["fetch_method"] == "tinyfish"
