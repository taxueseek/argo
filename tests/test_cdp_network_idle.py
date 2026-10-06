#!/usr/bin/env python3
"""tests/test_cdp_network_idle.py — 真 networkidle 判据与事件分发

覆盖：NetworkIdleTracker 计数口径（重定向不加、负数钳零、load/readyState
双信号）、_CDPSession 事件分发（send 路上事件不再丢弃）、持久 profile 的
进程语义（不 rmtree / 有头模式）、fetch_json。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from chrome_cdp import NetworkIdleTracker, _ChromeProcess, _CDPSession  # noqa: E402


# ── NetworkIdleTracker：计数口径 ─────────────────────────────────────────────

def test_tracker_request_finish_balance():
    t = NetworkIdleTracker()
    t.feed("Network.requestWillBeSent", {"request": {"url": "https://a"}})
    assert t.inflight == 1
    t.feed("Network.loadingFinished", {"requestId": "1"})
    assert t.inflight == 0 and t.idle(None) is False  # load 未发、readyState 未知
    t.feed("Page.loadEventFired", {})
    assert t.idle(None) is True


def test_tracker_redirect_does_not_leak():
    """重定向：原请求不再来 loadingFinished，改道请求不许 +1，否则计数只涨不跌。"""
    t = NetworkIdleTracker()
    t.feed("Network.requestWillBeSent", {"request": {"url": "https://a"}})
    t.feed("Network.requestWillBeSent",
           {"request": {"url": "https://b"}, "redirectResponse": {"status": 302}})
    assert t.inflight == 1, "重定向应复用同一次 in-flight，不该累加"
    t.feed("Network.loadingFinished", {"requestId": "b"})
    assert t.inflight == 0


def test_tracker_failed_request_and_negative_clamp():
    t = NetworkIdleTracker()
    t.feed("Network.requestWillBeSent", {"request": {"url": "https://a"}})
    t.feed("Network.loadingFailed", {"errorText": "net::ERR_ABORTED"})
    assert t.inflight == 0
    # attach 晚了只有 finished 到达：钳零不许负
    t.feed("Network.loadingFinished", {"requestId": "x"})
    assert t.inflight == 0


def test_tracker_inflight_blocks_idle_even_after_load():
    t = NetworkIdleTracker()
    t.feed("Page.loadEventFired", {})
    t.feed("Network.requestWillBeSent", {"request": {"url": "https://poll"}})
    assert t.idle("complete") is False, "load 后仍有轮询在途 → 不算静默"
    t.feed("Network.loadingFinished", {"requestId": "poll"})
    assert t.idle("complete") is True


def test_tracker_readystate_fallback():
    t = NetworkIdleTracker()
    assert t.idle("complete") is True
    assert t.idle("loading") is False


# ── _CDPSession：事件分发 ────────────────────────────────────────────────────

def _bare_session() -> _CDPSession:
    s = _CDPSession.__new__(_CDPSession)
    s._sock = MagicMock()
    s._msg_id = 0
    s._pending = {}
    s._event_handlers = []
    s.timeout = 5.0
    return s


def test_session_send_dispatches_event_frames(monkeypatch):
    s = _bare_session()
    seen = []
    s.on_event(lambda m, p: seen.append((m, p)))
    frames = [
        '{"method":"Network.requestWillBeSent","params":{"request":{"url":"https://x"}}}',
        '{"id":1,"result":{}}',  # send 自己的响应
    ]
    monkeypatch.setattr("chrome_cdp._ws_send", lambda sock, payload: None)
    monkeypatch.setattr("chrome_cdp._ws_recv", lambda sock, timeout=5: frames.pop(0) if frames else None)
    r = s.send("Page.navigate", {"url": "https://x"})
    assert r == {}
    assert seen and seen[0][0] == "Network.requestWillBeSent", \
        "send() 等响应路上的事件帧必须分发（真 networkidle 的数据来源）"


def test_session_pump_distributes_and_ignores_stale_responses(monkeypatch):
    s = _bare_session()
    seen = []
    s.on_event(lambda m, p: seen.append(m))
    frames = [
        '{"id":99,"result":{}}',  # 迟到响应：丢弃
        '{"method":"Page.loadEventFired","params":{}}',
    ]
    monkeypatch.setattr("chrome_cdp._ws_recv", lambda sock, timeout=5: frames.pop(0) if frames else None)
    s.pump(0.3)
    assert seen == ["Page.loadEventFired"]


# ── _ChromeProcess：持久 profile / 有头模式 ─────────────────────────────────

def test_persistent_profile_not_wiped(tmp_path):
    prof = tmp_path / "example.com"
    prof.mkdir()
    (prof / "Cookies").write_bytes(b"precious")
    fake_proc = MagicMock()
    with patch("chrome_cdp.subprocess.Popen", return_value=fake_proc) as popen, \
         patch("chrome_cdp._http_request", return_value={"status": 200, "body": "{}"}), \
         patch("chrome_cdp.time.sleep"), \
         patch("shutil.rmtree") as rmtree:
        cp = _ChromeProcess(port=9331, user_data_dir=str(prof), start_url="https://example.com")
        cp.start()
        rmtree.assert_not_called(), "持久 profile 绝不允许被启动逻辑清掉"
        assert (prof / "Cookies").read_bytes() == b"precious"
        cmd = popen.call_args.args[0]
        assert "--headless=new" in cmd and cmd[-1] == "https://example.com"


def test_temp_profile_still_wiped_and_headed_flag(tmp_path):
    fake_proc = MagicMock()
    temp_profile = Path(tempfile.gettempdir()) / "argo_chrome_9332"
    temp_profile.mkdir(parents=True, exist_ok=True)  # 旧残骸：启动前必须被清
    try:
        with patch("chrome_cdp.subprocess.Popen", return_value=fake_proc) as popen, \
             patch("chrome_cdp._http_request", return_value={"status": 200, "body": "{}"}), \
             patch("chrome_cdp.time.sleep"), \
             patch("shutil.rmtree") as rmtree:
            cp = _ChromeProcess(port=9332, headless=False)  # 临时 profile + 有头
            cp.start()
            rmtree.assert_called()  # 临时 profile 保持「启动前清残骸」
            cmd = popen.call_args.args[0]
            assert "--headless=new" not in cmd, "headless=False 不得带 headless 标志"
    finally:
        shutil.rmtree(temp_profile, ignore_errors=True)


# ── fetch_json：页面内带凭证请求 ─────────────────────────────────────────────

def _cdp_with_session():
    from chrome_cdp import ChromeCDP
    c = ChromeCDP.__new__(ChromeCDP)
    c._session = MagicMock()
    return c


def test_fetch_json_returns_status_and_body():
    c = _cdp_with_session()
    c._session.send.return_value = {
        "result": {"type": "object",
                   "value": {"status": 200, "body": '{"ok":1}'}}}
    out = c.fetch_json("https://example.com/api/history")
    assert out == {"status": 200, "body": '{"ok":1}'}
    expr = c._session.send.call_args.args[1]["expression"]
    assert "credentials:include" in expr.replace(" ", "").replace("'", "'") or \
        "credentials:'include'" in expr.replace('\"', '"')
    assert "https://example.com/api/history" in expr
    assert c._session.send.call_args.args[1].get("awaitPromise") is True


def test_fetch_json_failure_paths():
    c = _cdp_with_session()
    c._session.send.return_value = {"error": "timeout"}
    assert c.fetch_json("https://x/api") is None
    c._session.send.return_value = {"result": {"exceptionDetails": {"text": "x"}}}
    assert c.fetch_json("https://x/api") is None
    c._session.send.side_effect = RuntimeError("sock dead")
    assert c.fetch_json("https://x/api") is None
