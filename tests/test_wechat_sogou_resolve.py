#!/usr/bin/env python3
"""tests/test_wechat_sogou_resolve.py — 搜狗中间链 → 微信真实链接解析

吸收外部公众号搜索技能的核心增量（2026-09-28 方案A）：搜狗 /link、/weixin
中间跳转链是 SERP 链（纪律 3：不得作正文来源）且分钟级过期，引擎应就地
解析为 mp.weixin.qq.com 真实链接。本文件覆盖：

  - 302 直落：geturl 即终址 → url 重写 + url_resolved=true
  - 反爬 JS 页：200 + `url += '片段'` 拼接 → 拼回真实链接
  - 失败降级：保留搜狗中间链 + url_resolved=false，不丢结果
  - 熔断：连续失败达阈值 → 本轮剩余直接回落，不再发解析请求；冷却期内同样跳过
  - fast 模式：跳过解析，统一显式标注
  - Cookie 复用：搜索响应的 Set-Cookie 应带入解析请求

全程 mock 网络层（patch builders 模块的 http_open），离线必过。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import engines_builders_tech  # noqa: E402
from engines_builders_tech import _build_wechat_sogou_engine  # noqa: E402

# ── 模拟搜狗微信搜索结果页：3 条中间链 + 1 条非中间链 ─────────────────────────

_SEARCH_HTML = """<html><body>
<li id="sogou_vr_11002601_box_0">
    <h3><a href="/link?url=AAA">文章一</a></h3>
    <p class="txt-info">摘要一</p>
    <span class="all-time-y2">公众号A</span>
</li>
<li id="sogou_vr_11002601_box_1">
    <h3><a href="/weixin?url=BBB">文章二</a></h3>
    <p class="txt-info">摘要二</p>
    <span class="all-time-y2">公众号B</span>
</li>
<li id="sogou_vr_11002601_box_2">
    <h3><a href="/link?url=CCC">文章三</a></h3>
    <p class="txt-info">摘要三</p>
    <span class="all-time-y2">公众号C</span>
</li>
<li id="sogou_vr_11002601_box_3">
    <h3><a href="https://weixin.sogou.com/other">文章四</a></h3>
    <p class="txt-info">摘要四</p>
    <span class="all-time-y2">公众号D</span>
</li>
</body></html>"""

_REAL_1 = "https://mp.weixin.qq.com/s?__biz=AAA"
_REAL_2 = "https://mp.weixin.qq.com/s?__biz=CCC"

# 反爬 JS 页：目标地址被拆成 url += 片段
_ANTIBOT_JS = """<html><body><script>
var url = 'https://mp.weixin.qq.com/s?';
url += '__biz=CCC';
url += '&mid=22&idx=1';
window.location.replace(url);
</script></body></html>"""


class _Headers:
    def __init__(self, set_cookies: list[str]):
        self._sc = set_cookies

    def get_all(self, name: str) -> list[str] | None:
        return list(self._sc) if self._sc else None


class _Resp:
    """http_open 返回的响应替身：上下文管理器 + read(size) + geturl + headers。"""

    def __init__(self, body: bytes = b"", final_url: str = "",
                 set_cookies: list[str] | None = None):
        self._body = body
        self._final_url = final_url
        self.headers = _Headers(set_cookies or [])

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def geturl(self) -> str:
        return self._final_url


class _FakeHttp:
    """第 1 次调用返回搜索结果页，其后按序消费 resolve 剧本；记录全部请求。"""

    def __init__(self, search_html: str = _SEARCH_HTML,
                 resolve_script: list[_Resp] | None = None):
        self.search_html = search_html
        self.script = list(resolve_script or [])
        self.calls: list[str] = []
        self.req_headers: list[dict] = []

    def __call__(self, req, timeout: float = 10.0, engine: str = ""):
        self.calls.append(req.full_url)
        self.req_headers.append(dict(req.header_items()))
        if len(self.calls) == 1:
            return _Resp(body=self.search_html.encode("utf-8"),
                         set_cookies=["SNUID=abc123; path=/", "SUV=xyz; path=/"])
        if self.script:
            return self.script.pop(0)
        # 剧本耗尽：默认按「跳转后仍停在搜狗域（解析失败）」处理
        return _Resp(body=b"", final_url=req.full_url)


def _run(fake: _FakeHttp, **call_kwargs):
    engine = _build_wechat_sogou_engine({})
    with patch.object(engines_builders_tech, "http_open", fake):
        return engine("测试", n=5, **call_kwargs)


class TestSogouLinkResolution(unittest.TestCase):
    """方案A：中间链解析、降级、熔断与 fast 语义"""

    def test_302_redirect_resolved(self) -> None:
        fake = _FakeHttp(resolve_script=[_Resp(final_url=_REAL_1)])
        results = _run(fake)
        self.assertEqual(results[0]["url"], _REAL_1)
        self.assertTrue(results[0]["url_resolved"])
        # 第 2 条剧本耗尽 → 默认解析失败：中间链保留 + 显式 false
        self.assertTrue(results[1]["url"].startswith("https://weixin.sogou.com/weixin?url="))
        self.assertFalse(results[1]["url_resolved"])
        # 非中间链不标注、不被解析
        self.assertNotIn("url_resolved", results[3])
        # 请求次数 = 1 次搜索 + 2 次解析（第 3 条中间链也在剧本耗尽后失败 = 3 次解析）
        self.assertEqual(len(fake.calls), 4, f"应有 1 搜索 + 3 解析: {fake.calls}")

    def test_antibot_js_page_resolved(self) -> None:
        fake = _FakeHttp(resolve_script=[_Resp(body=_ANTIBOT_JS.encode("utf-8"))])
        results = _run(fake)
        self.assertEqual(
            results[0]["url"],
            "https://mp.weixin.qq.com/s?__biz=CCC&mid=22&idx=1",
            "反爬 JS 页应按 url += 片段拼回真实链接",
        )
        self.assertTrue(results[0]["url_resolved"])

    def test_failure_fallback_keeps_serp_url(self) -> None:
        fake = _FakeHttp(resolve_script=[_Resp(body=b"<html>antispider</html>")])
        results = _run(fake)
        self.assertTrue(results[0]["url"].startswith("https://weixin.sogou.com/link?url=AAA"))
        self.assertFalse(results[0]["url_resolved"], "解析失败应显式标 false")

    def test_breaker_stops_after_fail_limit(self) -> None:
        fail = _Resp(body=b"<html>antispider</html>")
        fake = _FakeHttp(resolve_script=[fail, fail, fail])
        results = _run(fake)
        resolved_calls = len(fake.calls) - 1
        self.assertEqual(
            resolved_calls, 3,
            f"连续失败达阈值 {engines_builders_tech._SOGOU_RESOLVE_FAIL_LIMIT} 条后应熔断: {fake.calls}",
        )
        # 第 3 条中间链是触发熔断的第 3 次失败；其后（若有）不再发请求。
        # 本用例 3 条中间链全部失败 → 全部 false，引擎不崩、结果不丢。
        self.assertTrue(all(not r["url_resolved"] for r in results[:3]))

    def test_cooldown_skips_resolution(self) -> None:
        fail = _Resp(body=b"<html>antispider</html>")
        fake = _FakeHttp(resolve_script=[fail, fail, fail])
        _run(fake)  # 第一轮：3 连败 → 进入冷却
        calls_after_round1 = len(fake.calls)
        results2 = _run(fake)  # 第二轮：冷却期内
        self.assertEqual(len(fake.calls), calls_after_round1 + 1,
                         "冷却期内只应发生搜索请求，不再发解析请求")
        self.assertTrue(all(not r["url_resolved"] for r in results2[:3]))

    def test_fast_mode_skips_resolution(self) -> None:
        fake = _FakeHttp()
        results = _run(fake, mode="fast")
        self.assertEqual(len(fake.calls), 1, "fast 模式不应发任何解析请求")
        self.assertTrue(all(not r["url_resolved"] for r in results[:3]))
        self.assertNotIn("url_resolved", results[3])

    def test_search_cookie_forwarded_to_resolve(self) -> None:
        fake = _FakeHttp(resolve_script=[_Resp(final_url=_REAL_1), _Resp(final_url=_REAL_2)])
        _run(fake)
        self.assertGreaterEqual(len(fake.req_headers), 2)
        resolve_hdrs = fake.req_headers[1]
        cookie_vals = [v for k, v in resolve_hdrs.items() if k.lower() == "cookie"]
        self.assertTrue(cookie_vals, "解析请求应携带 Cookie 头")
        joined = "; ".join(cookie_vals)
        self.assertIn("SNUID=abc123", joined)
        self.assertIn("SUV=xyz", joined)

    def test_intermediate_urls_only(self) -> None:
        from engines_builders_tech import _sogou_is_intermediate
        self.assertTrue(_sogou_is_intermediate("https://weixin.sogou.com/link?url=AAA"))
        self.assertTrue(_sogou_is_intermediate("https://weixin.sogou.com/weixin?url=BBB"))
        self.assertFalse(_sogou_is_intermediate("https://weixin.sogou.com/other"))
        self.assertFalse(_sogou_is_intermediate("https://mp.weixin.qq.com/s?__biz=AAA"))

    def test_space_in_link_url_encoded(self) -> None:
        """实网发现：搜狗把原始查询词原样塞进 /link 的 query，空格未编码，
        urllib 会拒收（InvalidURL）。解析前必须把空格编码为 %20。"""
        from engines_builders_tech import _resolve_sogou_link
        fake2 = _FakeHttp(resolve_script=[_Resp(final_url=_REAL_1)])
        fake2.calls.append("<search-already-done>")  # 直调解析：跳过 fake 的搜索页阶段
        fake2.req_headers.append({})
        with patch.object(engines_builders_tech, "http_open", fake2):
            with patch.object(engines_builders_tech.time, "sleep", lambda *_: None):
                real = _resolve_sogou_link(
                    "https://weixin.sogou.com/link?url=AAA&type=2&query=AI agent 工作流",
                    3.0, "")
        self.assertEqual(real, _REAL_1, "带空格的中间链应正常解析")
        req_url = fake2.calls[-1]
        self.assertIn("%20", req_url, f"请求 URL 应把空格编码为 %20: {req_url}")
        self.assertNotIn(" ", req_url)


if __name__ == "__main__":
    unittest.main()
