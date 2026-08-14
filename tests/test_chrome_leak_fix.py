#!/usr/bin/env python3
"""Chrome 泄漏修复回归测试：启动失败 kill / __init__ 兜底 / with 异常路径 stop。"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ── 1. _ChromeProcess.start：CDP ready 失败必须 kill 孤儿进程 ───────────────

def test_chrome_start_failure_kills_proc():
    from chrome_cdp import _ChromeProcess
    fake_proc = MagicMock()
    with patch("chrome_cdp.subprocess.Popen", return_value=fake_proc), \
         patch("chrome_cdp._http_request",
               side_effect=ConnectionError("refused")) as req, \
         patch("chrome_cdp.time.sleep"):
        cp = _ChromeProcess(port=9229)
        try:
            cp.start()
            assert False, "应抛 RuntimeError"
        except RuntimeError:
            pass
        # 孤儿 Chrome 必须被杀掉
        fake_proc.kill.assert_called_once()
        assert cp._proc is None, "失败后 _proc 应清空"


def test_chrome_start_success_no_kill():
    from chrome_cdp import _ChromeProcess
    fake_proc = MagicMock()
    with patch("chrome_cdp.subprocess.Popen", return_value=fake_proc), \
         patch("chrome_cdp._http_request",
               return_value={"status": 200, "body": "{}"}), \
         patch("chrome_cdp.time.sleep"):
        cp = _ChromeProcess(port=9230)
        cp.start()
        fake_proc.kill.assert_not_called()
        assert cp._proc is fake_proc


# ── 2. ChromeCDP.__init__：start 半途失败（target 获取异常）必须 stop ──────

def test_chrome_cdp_init_failure_stops_chrome():
    from chrome_cdp import ChromeCDP
    fake_chrome = MagicMock()
    with patch("chrome_cdp._ChromeProcess", return_value=fake_chrome):
        # _chrome.start() 成功但 /json 获取 targets 失败（无 page target）
        fake_chrome.start.return_value = None
        with patch("chrome_cdp._http_request",
                   return_value={"status": 200, "body": '[]'}):
            try:
                ChromeCDP(auto_start=True)
                assert False, "应抛 RuntimeError (No page target)"
            except RuntimeError:
                pass
        # __init__ 异常路径必须回收已启动的 Chrome
        fake_chrome.stop.assert_called_once()


# ── 3. mcp_handlers argo_screenshot：navigate 抛异常也必须 stop ─────────────

def test_screenshot_exception_still_stops():
    import mcp_handlers as mh
    fake_cdp = MagicMock()
    fake_cdp.navigate.side_effect = RuntimeError("nav failed")
    fake_cls = MagicMock(return_value=fake_cdp)
    # __enter__ 返回实例，__exit__ 调用 stop
    fake_cdp.__enter__ = MagicMock(return_value=fake_cdp)
    fake_cdp.__exit__ = MagicMock(return_value=False)

    def fake_lazy(name):
        mod = MagicMock()
        mod.ChromeCDP = fake_cls
        return mod

    with patch.object(mh, "_lazy_cached", fake_lazy), \
         patch.object(mh, "_dumps", lambda x: str(x)):
        r = mh.execute_tool("argo_screenshot",
                            {"url": "http://x.com", "output_path": "/tmp/x.png"})
        assert r.get("isError") is True, "navigate 抛异常应返回错误"
    # 异常路径也必须走 __exit__ → stop（Chrome 进程回收）
    fake_cdp.__exit__.assert_called_once()
