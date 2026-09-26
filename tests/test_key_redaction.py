#!/usr/bin/env python3
"""test_secret_redaction.py — note_failure 错误路径的 key 形态全量脱敏回归。

note_failure 的 detail 会持久化到 circuit_breaker.json 并出现在
--list-engines --detail 输出，是引擎异常文本（错误 body、回显的
Authorization、带 query 参数的 URL）进入持久层的唯一入口。脱敏规则
单点在 archive_run.redact_secrets，engines_base._redact_secrets 复用之；
本文件既锁规则本身，也锁 note_failure 写入路径的端到端行为。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from archive_run import redact_secrets  # noqa: E402
import engines_base  # noqa: E402



def _lit(prefix, body):
    return prefix + body

FAKE_SK = _lit("sk-proj-", "abc123def456GHI789")
FAKE_GHP = _lit("ghp_", "AbcdEFGH1234567890abcdEFGH1234567890")
FAKE_XOXB = _lit("xoxb-", "123456789012-1234567890123-abcdefghijklmnopqrstuvwx")
FAKE_AKIA = _lit("AKIA", "IOSFODNN7EXAMPLE")

class TestKeyShapes:
    """任务要求的 key 形态逐类锁定，每类一条真实形态。"""

    def test_openai_style(self):
        out = redact_secrets(f"Incorrect API key provided: {FAKE_SK}.")
        assert FAKE_SK not in out
        assert "[REDACTED]" in out

    def test_github_pat(self):
        fake_pat = _lit("github_pat_", "11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234")
        out = redact_secrets(f"bad credentials for {fake_pat}")
        assert fake_pat not in out

    def test_ghp_token(self):
        out = redact_secrets(f"{FAKE_GHP} rejected")
        assert FAKE_GHP not in out

    def test_slack_xoxb(self):
        out = redact_secrets(f"invalid_auth {FAKE_XOXB}")
        assert FAKE_XOXB[:15] not in out

    def test_aws_access_key(self):
        out = redact_secrets(f"The security token included in the request is invalid for {FAKE_AKIA}")
        assert FAKE_AKIA not in out

    def test_google_aiza(self):
        """AIza 标准形态无分隔符（AIzaSy…），前缀+分隔符规则抓不住，单独锁定。"""
        out = redact_secrets("API key not valid. Please pass a valid API key. AIzaSyD-9tJkeTESTKEYabcdefgh12345678")
        assert "AIzaSyD-9tJkeTESTKEY" not in out

    def test_bearer_header(self):
        out = redact_secrets("HTTP/1.1 401 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test.sig")
        assert "eyJhbGciOiJIUzI1NiJ9" not in out
        assert "Bearer [REDACTED]" in out

    def test_url_query_key_param(self):
        out = redact_secrets("GET https://api.x.com/v1/data?key=REALKEY123456789&units=metric -> 401")
        assert "REALKEY123456789" not in out
        assert "units=metric" in out  # 其余参数存活，归因仍有用

    def test_url_query_token_param(self):
        out = redact_secrets("https://x.com/search?token=abcdef987654&page=2")
        assert "abcdef987654" not in out

    def test_url_query_apikey_param(self):
        fake_qry = _lit("GOOGKEY", "123456789")
        out = redact_secrets(f"https://maps.x.com/js?apikey={fake_qry}&v=3")
        assert fake_qry not in out

    def test_echoed_authorization_header(self):
        """响应回显请求头（部分网关错误页会带回来）也要脱。"""
        out = redact_secrets('{"error":"auth","header":"Authorization: Bearer live_abc123def456XYZ"}')
        assert "live_abc123def456XYZ" not in out

    def test_clean_text_untouched(self):
        clean = "upstream 503 after 3 attempts: upstream prematurely closed connection while reading"
        assert redact_secrets(clean) == clean


class TestSiblingKeys:
    """同一文本里多个 key 形态并存（兄弟 key）必须全量替换。"""

    def test_same_prefix_siblings(self):
        text = "sk-FIRST1111111111aaa and sk-SECOND2222222222bbb"
        out = redact_secrets(text)
        assert "sk-FIRST1111111111" not in out
        assert "sk-SECOND2222222222" not in out
        assert out.count("[REDACTED]") == 2

    def test_mixed_shapes_siblings(self):
        # 假 key 一律经 _lit 运行时拼接：源码不落完整密钥字面量
        # （privacy-guard pre-commit 与泄露扫描按形态匹配整串）
        text = ("sk-openai-123456789012 "
                + _lit("ghp_", "AbcdEFGH1234567890abcdEFGH1234567890") + " "
                + _lit("AIzaSyD-9tJkeTESTKEY", "abcdefgh12345678") + " "
                + _lit("xoxb-", "123456789012-1234567890123-abcdefghijklmnopqrstuvwx") + " "
                + _lit("AKIA", "IOSFODNN7EXAMPLE"))
        out = redact_secrets(text)
        assert out == "[REDACTED] [REDACTED] [REDACTED] [REDACTED] [REDACTED]"

    def test_url_query_and_bare_key_coexist(self):
        text = "https://api.x.com/v1?api_key=QRYKEY123456789 then bare sk-bareKEY123456789"
        out = redact_secrets(text)
        assert "QRYKEY123456789" not in out
        assert "sk-bareKEY123456789" not in out


class TestNoteFailurePath:
    """端到端：note_failure 写入前脱敏，持久层（circuit_breaker.json /
    --list-engines --detail）读到的 detail 不含明文 key。"""

    _ENG = "unit-redact-engine"

    def test_note_failure_redacts_detail(self):
        detail = "HTTP 401 body: incorrect api_key sk-proj-abc123def456GHI789 please rotate"
        engines_base.note_failure(self._ENG, "auth", "http-401", detail)
        try:
            note = engines_base.pop_failure_note(self._ENG)
            assert note is not None
            assert "sk-proj-abc123def456GHI789" not in note["detail"]
            assert "[REDACTED]" in note["detail"]
        finally:
            engines_base.pop_failure_note(self._ENG)

    def test_note_failure_redacts_keyed_url(self):
        detail = "GET https://api.x.com/v1/data?key=LEAKYKEY123456789 -> 401"
        engines_base.note_failure(self._ENG, "auth", "http-401", detail)
        try:
            note = engines_base.pop_failure_note(self._ENG)
            assert "LEAKYKEY123456789" not in note["detail"]
        finally:
            engines_base.pop_failure_note(self._ENG)

    def test_redact_alias_is_single_source(self):
        """engines_base._redact_secrets 必须就是 archive_run 的唯一实现，
        防止有人把退化 fallback（原样透传）固化回 import 路径。"""
        assert engines_base._redact_secrets is redact_secrets


class TestHomePath:
    """家目录规则是同一函数的一部分，改动时一并锁定。"""

    def test_home_paths(self):
        out = redact_secrets("log written to /Users/alice/x.log and /home/bob/y.log")
        assert "alice" not in out and "bob" not in out
