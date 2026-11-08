#!/usr/bin/env python3
"""test_full_text_sources — 源内全文直出（full_text_url）契约与消费（mock 网络）。

## 守的是什么

有些源同时给出两个 URL：给人看的页面，和**可确定性取到正文**的端点。这两者
不等价，实测（2026-09-13）：

  - gutenberg：`/ebooks/{id}` 是下载门户页（去标签首段全是 noprint 脚本），
    而 gutendex 在 `formats` 里本来就给了纯文本 URL ——此前只在缺 id 时当保底，
    正常路径直接丢掉；
  - egov_law：`/law/{id}` 是 JS 空壳页（实测去标签后只剩「e-Gov 法令検索」），
    拿不到条文，而 `/api/1/lawdata/{id}` 返回该法令官方全文 XML。

只把正文 URL 带出来还不够——没有人读就是装饰。故本文件同时锁住**消费端**：
`evidence_loop.gate_results` 的取数建议必须优先用 `full_text_url`，否则这层
等于没接。反向也锁：没有该字段的结果行为不得变化（避免误伤绝大多数源）。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_batch9 as b9  # noqa: E402
import engines_builders_archive as bdata  # noqa: E402


class _FakeResp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, mod, payload: str, fn="http_open"):
    monkeypatch.setattr(mod, fn, lambda *a, **k: _FakeResp(payload.encode()),
                        raising=False)


# ── gutenberg ─────────────────────────────────────────────────────────────

_GUTENDEX = {
    "results": [
        {"id": 1342, "title": "Pride and Prejudice",
         "authors": [{"name": "Austen, Jane"}], "languages": ["en"],
         "formats": {
             "text/plain; charset=us-ascii": "https://www.gutenberg.org/files/1342/1342-0.txt",
             "text/html": "https://www.gutenberg.org/files/1342/1342-h/1342-h.htm",
         }},
        {"id": 84, "title": "Frankenstein", "authors": [], "languages": ["en"],
         "formats": {}},  # 无纯文本格式的条目
    ]
}


class TestGutenbergFullText:
    def test_full_text_url_is_plain_text_endpoint(self, monkeypatch):
        _patch(monkeypatch, bdata, json.dumps(_GUTENDEX))
        out = bdata._build_gutenberg_engine({"_name": "gutenberg", "timeout": 5})("austen", 5)
        assert out[0]["url"] == "https://www.gutenberg.org/ebooks/1342"
        assert out[0]["full_text_url"] == "https://www.gutenberg.org/files/1342/1342-0.txt"

    def test_no_formats_means_no_full_text_field(self, monkeypatch):
        """上游没给纯文本格式时不得凭空造字段。"""
        _patch(monkeypatch, bdata, json.dumps(_GUTENDEX))
        out = bdata._build_gutenberg_engine({"_name": "gutenberg", "timeout": 5})("austen", 5)
        assert "full_text_url" not in out[1]


# ── egov_law ──────────────────────────────────────────────────────────────

_EGOV_XML = ("<LawNameListAll><LawNameListInfo><LawId>321CONSTITUTION</LawId>"
             "<LawName>日本国憲法</LawName><LawNo>昭和二十一年憲法</LawNo>"
             "</LawNameListInfo></LawNameListAll>")


class TestEgovLawFullText:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        b9._EGOV_ENTRIES = None
        b9._EGOV_FETCHED_AT = 0.0
        yield
        b9._EGOV_ENTRIES = None
        b9._EGOV_FETCHED_AT = 0.0

    def test_full_text_url_points_at_lawdata_api(self, monkeypatch):
        _patch(monkeypatch, b9, _EGOV_XML)
        out = b9._build_egov_law_engine({"_name": "egov_law", "timeout": 5})("憲法", 5)
        assert out[0]["url"] == "https://laws.e-gov.go.jp/law/321CONSTITUTION"
        assert out[0]["full_text_url"] == (
            "https://laws.e-gov.go.jp/api/1/lawdata/321CONSTITUTION")

    def test_no_law_id_means_no_full_text_field(self, monkeypatch):
        xml = ("<LawNameListAll><LawNameListInfo><LawName>民法</LawName>"
               "<LawNo>明治二十九年法律第八十九号</LawNo></LawNameListInfo>"
               "</LawNameListAll>")
        _patch(monkeypatch, b9, xml)
        out = b9._build_egov_law_engine({"_name": "egov_law", "timeout": 5})("民法", 5)
        assert out and "full_text_url" not in out[0]


# ── 消费端：取数建议必须优先用正文端点 ────────────────────────────────────

class TestGatePrefersFullTextUrl:
    def test_suggested_uses_full_text_url(self):
        from evidence_loop import gate_results
        results = [
            {"title": "a", "url": "https://www.gutenberg.org/ebooks/1342",
             "full_text_url": "https://www.gutenberg.org/files/1342/1342-0.txt"},
            {"title": "b", "url": "https://v.com/plain"},
        ]
        gate = gate_results(results, domain="literature")
        assert gate["suggested"] == [
            "https://www.gutenberg.org/files/1342/1342-0.txt",
            "https://v.com/plain",
        ]

    def test_behavior_unchanged_without_the_field(self):
        """绝大多数源没有 full_text_url——建议必须与改造前逐字一致。"""
        from evidence_loop import gate_results
        results = [{"title": "a", "url": "https://v.com/a"},
                   {"title": "b", "url": "https://v.com/b"}]
        gate = gate_results(results, domain="general")
        assert gate["suggested"] == ["https://v.com/a", "https://v.com/b"]

    def test_empty_full_text_url_falls_back_to_url(self):
        """空串/None 不得把建议槽位吃掉（falsy 回退）。"""
        from evidence_loop import gate_results
        results = [{"title": "a", "url": "https://v.com/a", "full_text_url": ""},
                   {"title": "b", "url": "https://v.com/b", "full_text_url": None}]
        gate = gate_results(results, domain="general")
        assert gate["suggested"] == ["https://v.com/a", "https://v.com/b"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
