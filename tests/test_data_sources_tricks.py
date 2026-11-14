#!/usr/bin/env python3
"""取信技巧回归门（2026-09-14）：fetch AI 变体扩展 + stackexchange/doi 引擎。

对应取信技巧调研（2026-09-14，已归档）的落地项：
  1. fetch 第零级：{url}.md 之外新增站点根 /llms.txt 探测
  2. fetch 第一级C：r.jina.ai 阅读器（keyless，仅公网 URL，第三方代理边界）
  3. stackexchange 引擎（search/advanced，link 自带，匿名 300/天/IP）
  4. doi 引擎（内容协商 CSL JSON，单对象根走 _CUSTOM_JSON_PARSERS）

全部离线（mock HttpClient，不触网）。

运行：
  python3 -m pytest tests/test_data_sources_tricks.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_v3  # noqa: E402
from engines_base import _parse_doi, _parse_http_payload  # noqa: E402


def _fake_client_response(status: int = 200, text: str = "",
                          content_type: str = "text/plain"):
    client = MagicMock()
    client.get.return_value = {
        "status": status, "text": text,
        "headers": {"Content-Type": content_type},
    }
    return MagicMock(return_value=client)


# ── 1. fetch 第零级：AI 变体候选 ──────────────────────────────────────────────

class TestAiVariantCandidates(unittest.TestCase):

    def test_doc_page_probes_md_only(self):
        cands = fetch_v3._ai_variant_candidates(
            "https://developers.openai.com/codex/build-skills")
        self.assertEqual(cands,
                         [("https://developers.openai.com/codex/build-skills.md",
                           "md_variant")])

    def test_site_root_probes_llms_txt(self):
        cands = fetch_v3._ai_variant_candidates("https://example.com/")
        kinds = [k for _, k in cands]
        self.assertIn("llms_txt", kinds)
        self.assertIn(("https://example.com/llms.txt", "llms_txt"), cands)

    def test_page_with_extension_probes_nothing(self):
        self.assertEqual(
            fetch_v3._ai_variant_candidates(
                "https://example.com/docs/page.html"), [])
        self.assertEqual(
            fetch_v3._ai_variant_candidates(
                "https://example.com/docs/page?ref=x"), [])

    def test_md_variant_hit_on_root_returns_llms_kind(self):
        # 站点根 + /llms.txt 返回 markdown → 命中且 kind=llms_txt，url 保持原始
        md_text = "# Example Site\n\n" + "正文" * 80
        with patch("http_client.HttpClient",
                   _fake_client_response(200, md_text, "text/markdown")):
            r = fetch_v3._md_variant_fetch("https://example.com/")
        self.assertIsNotNone(r)
        self.assertEqual(r["fetch_method"], "llms_txt")
        self.assertEqual(r["url"], "https://example.com/")
        self.assertEqual(r["title"], "Example Site")


# ── 2. fetch 第一级C：r.jina.ai 阅读器 ────────────────────────────────────────

class TestJinaReader(unittest.TestCase):

    def test_public_host_gate(self):
        self.assertTrue(fetch_v3._is_public_host("developers.openai.com"))
        self.assertFalse(fetch_v3._is_public_host("localhost"))
        self.assertFalse(fetch_v3._is_public_host("192.168.1.3"))
        self.assertFalse(fetch_v3._is_public_host("10.0.0.2"))
        self.assertFalse(fetch_v3._is_public_host("127.0.0.1"))
        self.assertFalse(fetch_v3._is_public_host("nas.local"))

    def test_disabled_via_env(self):
        with patch.dict("os.environ", {"ARGO_FETCH_JINA": "0"}):
            self.assertFalse(fetch_v3._jina_enabled())

    def test_success_parses_title_and_strips_preamble(self):
        raw = ("Title: Testing Agent Skills\n"
               "URL Source: https://example.com/a\n\n"
               "Markdown Content:\n"
               "# Testing Agent Skills\n\n" + "正文段落。" * 60)
        with patch("http_client.HttpClient", _fake_client_response(200, raw)):
            r = fetch_v3._jina_reader_fetch("https://example.com/a")
        self.assertIsNotNone(r)
        self.assertTrue(r["success"])
        self.assertEqual(r["fetch_method"], "jina_reader")
        self.assertEqual(r["title"], "Testing Agent Skills")
        self.assertTrue(r["content"].startswith("# Testing Agent Skills"))
        self.assertNotIn("URL Source:", r["content"])

    def test_rate_limited_returns_none(self):
        with patch("http_client.HttpClient", _fake_client_response(429, "")):
            self.assertIsNone(
                fetch_v3._jina_reader_fetch("https://example.com/a"))


# ── 3. stackexchange 引擎声明与解析 ───────────────────────────────────────────

_SE_PAYLOAD = json.dumps({"items": [
    {"title": "How to limit concurrency with asyncio?",
     "link": "https://stackoverflow.com/q/48483348", "score": 12,
     "is_answered": True},
    {"title": "Python asyncio sleep forever",
     "link": "https://stackoverflow.com/q/75844912", "score": 3},
]}, ensure_ascii=False)


class TestStackExchangeEngine(unittest.TestCase):

    def test_spec_wiring(self):
        from engines import get_registry
        reg = get_registry()
        self.assertIn("stackexchange", reg)
        from config import load_config
        spec = (load_config().get("engines") or {}).get("stackexchange") or {}
        self.assertEqual(spec.get("tier"), "daily_support")
        self.assertEqual((spec.get("extra_params") or {}).get("site"),
                         "stackoverflow")
        self.assertEqual(spec.get("url"),
                         "https://api.stackexchange.com/2.3/search/advanced")

    def test_output_map_parses_advanced_payload(self):
        from config import load_config
        spec = (load_config().get("engines") or {}).get("stackexchange") or {}
        out = _parse_http_payload(_SE_PAYLOAD, "json", "stackexchange", 5,
                                  spec.get("output_map") or {}, spec)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["url"],
                         "https://stackoverflow.com/q/48483348")
        self.assertTrue(out[0]["title"])


# ── 4. doi 引擎：CSL JSON 单对象解析 ─────────────────────────────────────────

_CSL_SAMPLE = {
    "DOI": "10.1145/3442188.3445922",
    "URL": "http://dx.doi.org/10.1145/3442188.3445922",
    "title": ["On the Dangers of Stochastic Parrots"],
    "container-title": ["Proceedings of the 2021 ACM Conference on "
                        "Fairness, Accountability"],
    "publisher": "ACM",
    "issued": {"date-parts": [[2021, 3]]},
    "author": [
        {"given": "Emily", "family": "Bender"},
        {"given": "Timnit", "family": "Gebru"},
    ],
}


class TestDoiEngine(unittest.TestCase):

    def test_csl_single_object_flattened(self):
        out = _parse_doi(_CSL_SAMPLE)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r["title"], "On the Dangers of Stochastic Parrots")
        self.assertEqual(r["url"],
                         "http://dx.doi.org/10.1145/3442188.3445922")
        self.assertIn("ACM", r["snippet"])
        self.assertEqual(r["published_at"], "2021-3")
        self.assertIn("Bender", r["authors"])

    def test_registered_for_doi_engine(self):
        from engines_base import _CUSTOM_JSON_PARSERS
        self.assertIs(_CUSTOM_JSON_PARSERS.get("doi"), _parse_doi)

    def test_spec_is_explicit_only(self):
        from config import load_config
        spec = (load_config().get("engines") or {}).get("doi") or {}
        self.assertTrue(spec.get("explicit_only"))
        self.assertEqual(spec.get("query_param"), "")


if __name__ == "__main__":
    unittest.main()
