"""fetch_v3 微信公众号前置分支测试：分流正确、失败诚实、旁路让路。

分支契约（2026-11-07）：mp.weixin 文章 URL 委托 wechat_article.fetch_wechat_article
（`argo wx` 同一实现）；三通道全败返回结构化失败而非落回通用链——通用链对
mp.weixin 只会拿 JS 骨架冒充成功，那是本分支要修的病灶本身。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

import fetch_v3  # noqa: E402

WX_URL = "https://mp.weixin.qq.com/s?__biz=test&mid=1&idx=1&sn=abc"


@pytest.fixture()
def wx_stub(monkeypatch):
    """受控 wechat_article：工厂闭包持状态（不走类属性陷阱），隔离网络。"""
    state = {"calls": [], "result": {}}

    def _fetch(url):
        state["calls"].append(url)
        return dict(state["result"])

    mod = types.ModuleType("wechat_article")
    mod.fetch_wechat_article = _fetch
    monkeypatch.setitem(sys.modules, "wechat_article", mod)
    monkeypatch.setitem(sys.modules, "robots_guard",
                        types.SimpleNamespace(robots_blocked=lambda *a, **k: False))
    return state


def _ok_result():
    return {"ok": True, "title": "标题", "author": "作者",
            "publish_time": "2026-11-07", "content": "正文" * 100,
            "char_count": 200, "fetch_method": "http_direct",
            "images": [], "image_count": 0}


@pytest.fixture()
def offline_http(monkeypatch):
    """通用链网络出口按真实契约打桩：HttpClient 返回 599 失败响应而非抛异常，
    open_url 返回空响应上下文——让降级链离线走完，而不是被桩子炸穿。"""

    class _DeadClient:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return {"status": 599, "text": "", "headers": {}}

        get_impersonated = get
        post = get

    fake_http = types.ModuleType("http_client")
    fake_http.HttpClient = _DeadClient

    class _EmptyResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    monkeypatch.setitem(sys.modules, "http_client", fake_http)
    monkeypatch.setattr(fetch_v3, "open_url", lambda *a, **k: _EmptyResp())


# ── 域名判定 ────────────────────────────────────────────────────────────────

def test_wechat_url_predicate():
    assert fetch_v3._is_wechat_article_url(
        "https://mp.weixin.qq.com/s?__biz=x")
    assert fetch_v3._is_wechat_article_url(
        "https://mp.weixin.qq.com/s/abc123")
    assert not fetch_v3._is_wechat_article_url("https://example.com/s/page")
    assert not fetch_v3._is_wechat_article_url("https://weixin.qq.com/cgi-bin/x")


# ── 分流行为 ────────────────────────────────────────────────────────────────

def test_wechat_url_delegates_to_wx_chain(wx_stub):
    wx_stub["result"] = _ok_result()
    out = fetch_v3.fetch_v3(WX_URL, skip_cache=True,
                            use_browser_fallback=False)
    assert wx_stub["calls"] == [WX_URL]
    assert out["success"] is True
    assert out["fetch_method"].startswith("wechat:")
    assert out["title"] == "标题"
    assert out["author"] == "作者"
    assert out["cached"] is False


def test_wechat_failure_is_structured_not_generic_fallback(wx_stub):
    wx_stub["result"] = {"ok": False, "error": "所有通道均失败"}
    out = fetch_v3.fetch_v3(WX_URL, skip_cache=True,
                            use_browser_fallback=False)
    assert wx_stub["calls"] == [WX_URL]  # 只调一次专用链，无第二落点
    assert out["success"] is False
    assert out["fetch_method"] == "wechat_failed"
    assert "所有通道均失败" in out["error"]


def test_explicit_html_intent_bypasses_wx_branch(wx_stub, offline_http):
    """need_html 是调用方的显式抓取意图：结构化提取车道必须让路。
    通用链以离线桩走完（599 全败），断言只关心 wx 链未被触发。"""
    out = fetch_v3.fetch_v3(WX_URL, skip_cache=True, need_html=True,
                            use_browser_fallback=False)
    assert wx_stub["calls"] == []
    assert out["success"] is False  # 离线桩下通用链必然失败，不伪装成功


def test_non_wechat_url_never_hits_wx_chain(wx_stub, offline_http):
    """域名分流只在 mp.weixin 文章 URL 生效，其他 URL 不多付一次函数调用。"""
    fetch_v3.fetch_v3("https://example.com/some/page", skip_cache=True,
                      need_html=True, use_browser_fallback=False)
    assert wx_stub["calls"] == []
