#!/usr/bin/env python3
"""test_xhs_url_verifiability.py — 小红书链接可核验性回归测试。

背景：小红书强制 xsec_token 机制。裸 note_id 拼出的
`/explore/<id>` 会被 302 拦截，**打开即失败**（实测确认）。
原实现只用 `item['id']` 重新拼 URL，丢弃了上游可能返回的原始 URL
与 xsec_token，于是稳定产出不可核验的链接 —— 与 V2EX 旧实现同类问题：
argo 的「结果可核验」承诺被破坏。

本文件锁定契约：
  1. 优先用上游原始 URL（含 token）
  2. 有 token 时拼带 token 的 URL
  3. 只有裸 ID 时仍产出，但显式标注 url_verifiable=False
  4. 无 ID 无 URL 时不产出（不可核验的东西不进结果集）
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOCIAL_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts", "social_engines")
if SOCIAL_DIR not in sys.path:
    sys.path.insert(0, SOCIAL_DIR)

import xiaohongshu_engine as xe  # noqa: E402


def _parse(payload: dict) -> list[dict]:
    return xe._parse_xhs_output(json.dumps(payload), 5)


class TestUrlPreference:
    """URL 来源优先级：原始 URL > 带 token 拼接 > 裸 ID。"""

    def test_prefers_raw_url_from_upstream(self):
        rs = _parse({"items": [{"id": "n1", "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=ABC",
                                "note_card": {"display_title": "t"}}]})
        assert rs[0]["url"].endswith("xsec_token=ABC")
        assert rs[0]["social_meta"]["url_verifiable"] is True

    def test_builds_url_with_token_when_absent(self):
        rs = _parse({"items": [{"id": "n2", "xsec_token": "TOK",
                                "note_card": {"display_title": "t"}}]})
        assert "xsec_token=TOK" in rs[0]["url"]
        assert rs[0]["social_meta"]["url_verifiable"] is True
        assert rs[0]["social_meta"]["has_xsec_token"] is True

    def test_bare_id_marked_unverifiable(self):
        """核心契约：裸 ID 链接仍产出，但必须标注不可核验。"""
        rs = _parse({"items": [{"id": "n3", "note_card": {"display_title": "t"}}]})
        assert len(rs) == 1
        m = rs[0]["social_meta"]
        assert m["url_verifiable"] is False
        assert m["has_xsec_token"] is False
        assert "xsec_token" not in rs[0]["url"]


class TestNoUnverifiableOutput:
    """无 ID 无 URL 的条目不得进结果集。"""

    def test_dropped_when_no_id_and_no_url(self):
        rs = _parse({"items": [{"note_card": {"display_title": "无标识"}}]})
        assert rs == []

    def test_mixed_items_only_verifiable_kept(self):
        rs = _parse({"items": [
            {"id": "a", "xsec_token": "T", "note_card": {"display_title": "有token"}},
            {"note_card": {"display_title": "无标识"}},
            {"id": "c", "note_card": {"display_title": "裸id"}},
        ]})
        titles = [r["title"] for r in rs]
        assert "无标识" not in titles
        assert len(rs) == 2


class TestExistingFieldsIntact:
    """改动不得破坏既有字段。"""

    def test_core_fields_present(self):
        rs = _parse({"items": [{"id": "n9", "xsec_token": "T", "note_card": {
            "display_title": "标题", "desc": "正文",
            "user": {"nickname": "作者"},
            "interact_info": {"liked_count": 5, "comment_count": 2, "collected_count": 1},
            "type": "video"}}]})
        r = rs[0]
        assert r["title"] == "标题" and r["snippet"] == "正文"
        assert r["source"] == "xiaohongshu"
        m = r["social_meta"]
        assert m["author"] == "作者" and m["likes"] == 5
        assert m["comments"] == 2 and m["collects"] == 1 and m["type"] == "video"

    def test_note_id_recorded(self):
        rs = _parse({"items": [{"id": "abc123", "xsec_token": "T",
                                "note_card": {"display_title": "t"}}]})
        assert rs[0]["social_meta"]["note_id"] == "abc123"


class TestSubprocessCliInvocation:
    """CLI 调用契约：强制 --json、剥离 NO_PROXY 中的 [::1]、未登录返回 error 占位。"""

    def test_passes_json_flag_and_strips_ipv6_loopback_no_proxy(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = json.dumps({
                "ok": True,
                "data": {"items": [{"id": "n1", "xsec_token": "TOK", "note_card": {"display_title": "ok"}}]},
            })
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
        monkeypatch.setattr(xe.subprocess, "run", fake_run)
        rs = xe.search("哲学", n=2)
        assert captured["cmd"] == ["xhs", "search", "--json", "哲学"]
        assert "[::1]" not in captured["env"].get("NO_PROXY", "")
        assert len(rs) == 1 and rs[0]["social_meta"]["url_verifiable"] is True

    def test_not_authenticated_returns_error_placeholder(self, monkeypatch):
        class FakeProc:
            returncode = 1
            stdout = '{"ok": false, "error": {"code": "not_authenticated", "message": "No cookies"}}'
            stderr = ""

        monkeypatch.setattr(xe.subprocess, "run", lambda *a, **kw: FakeProc())
        rs = xe.search("哲学", n=2)
        assert len(rs) == 1
        assert "not_authenticated" in rs[0].get("error", "")
        assert rs[0].get("source") == "xiaohongshu"

