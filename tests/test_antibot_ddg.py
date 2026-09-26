#!/usr/bin/env python3
"""test_antibot_ddg.py — DDG 202 challenge 页的反爬检测回归。

实测口径（2026-09-26，curl -A "Mozilla/5.0"）：lite.duckduckgo.com 的
challenge 以 HTTP 202 返回，页面文案首个 challenge 字样在 2600+ 字符处，
_head 区通用标记全部落空；urllib 回退路径会把这种 2xx body 当正常页送进
解析。本文件锁定：全文级高特异性标记能拦住它、通用标记保持 head-only
（正文合法语义不误杀）、完整 html 引擎链路上拦截页归因为 blocked。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_base  # noqa: E402

# 实测 challenge 页结构复刻：head 区是 preload/meta 壳（无任何通用标记），
# 文案在 2600+ 字符处，整体 > 500 字符可过长度闸
_CHALLENGE_TAIL = (
    '    <link rel="preload" href="/font/ProximaNova-Reg-webfont.woff2" as="font">\n'
    '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
)
_CHALLENGE_PAGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><title>DuckDuckGo</title>"
    + _CHALLENGE_TAIL * 30
    + "</head><body><p>Unfortunately, bots use DuckDuckGo too. Please complete "
      "the following challenge to confirm this search was made by a human. "
      "Select all squares containing a duck: Submit</p></body></html>"
)


class TestHeadOnlyGenericMarkers:
    """通用标记保持 head-only：challenge 一词出现在正文属合法语义。"""

    def test_generic_marker_in_body_not_flagged(self):
        page = ("<html><head><title>Results</title></head><body>"
                + "lorem ipsum dolor sit amet " * 200
                + "<p>this article explains the challenge of urban mobility</p></body></html>")
        assert engines_base._detect_anti_bot(page) is False

    def test_generic_marker_in_head_flagged(self):
        page = "<html><head><title>Checking your browser before accessing</title></head><body>" + "x" * 600
        assert engines_base._detect_anti_bot(page) is True


class TestDDGChallengeMarkers:
    """全文级高特异性标记：文案不在 head 区也必须拦。"""

    def test_ddg_challenge_page_flagged(self):
        assert engines_base._detect_anti_bot(_CHALLENGE_PAGE) is True

    def test_anomaly_word_in_body_not_flagged(self):
        """anomaly 会在「异常检测」主题的结果页正文合法出现，不得进全文表。"""
        page = ("<html><head><title>Results</title></head><body>"
                + "lorem ipsum dolor sit amet " * 200
                + "<p>anomaly detection in time series data</p></body></html>")
        assert engines_base._detect_anti_bot(page) is False


class TestExistingSemantics:
    """既有语义不回退。"""

    def test_empty_html(self):
        assert engines_base._detect_anti_bot("") is True

    def test_short_page(self):
        assert engines_base._detect_anti_bot("<html>hi</html>") is True

    def test_normal_long_page(self):
        page = "<html><head><title>Results</title></head><body>" + "<p>content paragraph</p>" * 300
        assert engines_base._detect_anti_bot(page) is False


class TestFullEngineChain:
    """urllib 回退路径（conftest 默认 ARGO_ENGINE_HTTP_CLIENT=0）下，202 的
    challenge body 会进 _detect_anti_bot：拦截 + 归因 blocked + 诚实空。"""

    def test_challenge_page_honest_empty_and_attributed(self, monkeypatch):
        eng = engines_base._build_html_engine({
            "_name": "local_duckduckgo",
            "url": "https://lite.duckduckgo.com/lite/",
            "query_param": "q",
        })
        monkeypatch.setattr(engines_base, "_http_get_raw",
                            lambda url, headers, timeout, engine="?" , **kw: _CHALLENGE_PAGE)
        out = eng("quantum computing tutorial")
        assert out == []
        note = engines_base.pop_failure_note("local_duckduckgo")
        assert note is not None
        assert note["category"] == "blocked"
        assert note["reason"] == "anti-bot-page"
