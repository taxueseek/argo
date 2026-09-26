#!/usr/bin/env python3
"""test_citations.py — argo cite（DOI → 引用条目）库层与 CLI 测试。

覆盖（全部离线，mock citations.http_open，打桩点 = 网络读取处）：
  1. Crossref fixture → 四种风格 golden 字符串（含 HTML 实体解码）
  2. OpenAlex fixture（arXiv 预印本）→ [EB/OL] / @misc 变体 + 姓氏虚词切分
  3. 路由：arXiv DOI（10.48550/ 前缀）跳过 Crossref 直取 OpenAlex；
     Crossref 失败落 OpenAlex；两家都失败给出诚实错误
  4. BibTeX 键：非法字符剥离 + 批内去重（a/b/c 递增）
  5. CLI：多 DOI 逐条独立（单条失败不拖垮其余）、--json、bin/argo 注册

运行：
  python3 -m pytest tests/test_citations.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import cite_cli  # noqa: E402
from citations import (  # noqa: E402
    CiteError,
    decode_entities,
    fetch_metadata,
    format_citation,
    from_openalex_work,
    split_display_name,
)

ROOT = Path(__file__).resolve().parent.parent


class _FakeResp:
    """HTTP 响应替身（context manager 协议，供 urlopen 调用方使用）。"""

    def __init__(self, payload):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_urlopen(payload):
    """固定返回同一 payload 的 urlopen 替身（不区分 URL）。"""
    def _fn(req, timeout=None, **kwargs):
        return _FakeResp(payload)
    return _fn


def _router_urlopen(seen: list, crossref_result, openalex_result):
    """按 URL 域名分发的 urlopen 替身：result 为 payload 或待抛异常。"""
    def _fn(req, timeout=None, **kwargs):
        url = getattr(req, "full_url", str(req))
        seen.append(url)
        result = crossref_result if "api.crossref.org" in url else openalex_result
        if isinstance(result, Exception):
            raise result
        return _FakeResp(result)
    return _fn


# ---------------------------------------------------------------------------
# fixtures（JSON 形状，与两家 API 实际响应同构）
# ---------------------------------------------------------------------------

CROSSREF_PAYLOAD = {
    "status": "ok",
    "message": {
        "DOI": "10.1000/example.2020",
        "type": "journal-article",
        "title": ["Attention &amp; memory: a study"],
        "container-title": ["Journal of Examples"],
        "author": [
            {"family": "Vaswani", "given": "Ashish"},
            {"family": "Shazeer", "given": "Noam"},
            {"family": "Parmar", "given": "Niki"},
            {"family": "Uszkoreit", "given": "Jakob"},
        ],
        "published": {"date-parts": [[2020, 6, 1]]},
        "volume": "12",
        "issue": "3",
        "page": "100-110",
        "publisher": "Example Press",
        "URL": "https://doi.org/10.1000/example.2020",
    },
}

# arXiv 预印本：OpenAlex 把 arXiv 名为 "arXiv (Cornell University)"，
# 作者只有 display_name（含姓氏虚词），doi 是完整 URL。
OPENALEX_ARXIV_WORK = {
    "id": "https://openalex.org/W100",
    "doi": "https://doi.org/10.48550/arXiv.1706.03762",
    "title": "Attention Is All You Need",
    "authorships": [
        {"author": {"display_name": "Ashish Vaswani"}},
        {"author": {"display_name": "Diego de Las Casas"}},
        {"author": {"display_name": "Ludwig van Beethoven"}},
    ],
    "publication_year": 2017,
    "primary_location": {"source": {"display_name": "arXiv (Cornell University)"}},
    "biblio": {"volume": "", "issue": "", "first_page": None, "last_page": None},
}

# OpenAlex 期刊论文：走 venue 正路（非预印本）的对照面。
OPENALEX_JOURNAL_WORK = {
    "id": "https://openalex.org/W200",
    "doi": "https://doi.org/10.1000/openalex.2021",
    "title": "A plain OpenAlex work",
    "authorships": [{"author": {"display_name": "Jane Doe"}}],
    "publication_year": 2021,
    "primary_location": {"source": {"display_name": "Journal of Examples"}},
    "biblio": {"volume": "5", "issue": "2", "first_page": "10", "last_page": "20"},
}


def _crossref_meta() -> dict:
    with patch("citations.http_open", _fake_urlopen(CROSSREF_PAYLOAD)):
        return fetch_metadata("10.1000/example.2020")


def _arxiv_meta() -> dict:
    with patch("citations.http_open", _fake_urlopen(OPENALEX_ARXIV_WORK)):
        return fetch_metadata("10.48550/arXiv.1706.03762")


class TestEntityDecode(unittest.TestCase):
    def test_single_pass_no_double_decoding(self):
        """单趟替换：&amp;lt; 解到 &lt; 为止，不二次解码成 <。"""
        self.assertEqual(decode_entities("A &amp; B"), "A & B")
        self.assertEqual(decode_entities("&amp;lt;"), "&lt;")
        self.assertEqual(decode_entities("&#39;x&quot;"), "'x\"")
        self.assertEqual(decode_entities("plain"), "plain")


class TestSplitDisplayName(unittest.TestCase):
    def test_particle_runs_move_to_family(self):
        cases = {
            "Diego de Las Casas": ("de Las Casas", "Diego"),   # de + Las 连续虚词
            "Manuel de Falla": ("de Falla", "Manuel"),
            "Ludwig van Beethoven": ("van Beethoven", "Ludwig"),
        }
        for name, (family, given) in cases.items():
            r = split_display_name(name)
            self.assertEqual((r["family"], r["given"]), (family, given), name)

    def test_non_particle_middle_stays_given(self):
        r = split_display_name("John Paul Smith")
        self.assertEqual((r["family"], r["given"]), ("Smith", "John Paul"))

    def test_single_word_is_family(self):
        r = split_display_name("Madonna")
        self.assertEqual((r["family"], r["given"]), ("Madonna", ""))

    def test_empty_name(self):
        self.assertEqual(split_display_name(""), {"family": "", "given": ""})


class TestGoldenStyles(unittest.TestCase):
    """Crossref / OpenAlex 两路 fixture × 四风格 golden。"""

    def test_crossref_gbt7714(self):
        self.assertEqual(
            format_citation(_crossref_meta(), "gbt7714"),
            "Vaswani A, Shazeer N, Parmar N, et al. "
            "Attention & memory: a study[J]. Journal of Examples, 2020, 12(3).")

    def test_crossref_gbt7714n(self):
        self.assertEqual(
            format_citation(_crossref_meta(), "gbt7714n"),
            "[1] Vaswani A, Shazeer N, Parmar N, et al. "
            "Attention & memory: a study[J]. Journal of Examples, 2020, 12(3).")

    def test_crossref_apa(self):
        self.assertEqual(
            format_citation(_crossref_meta(), "apa"),
            "Vaswani, A., Shazeer, N., Parmar, N., et al. "
            "(2020). Attention & memory: a study. Journal of Examples. "
            "https://doi.org/10.1000/example.2020")

    def test_crossref_bibtex(self):
        self.assertEqual(
            format_citation(_crossref_meta(), "bibtex"),
            "@article{vaswani2020,\n"
            "  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki "
            "and Uszkoreit, Jakob},\n"
            "  title = {Attention & memory: a study},\n"
            "  journal = {Journal of Examples},\n"
            "  year = {2020},\n"
            "  volume = {12},\n"
            "  number = {3},\n"
            "  doi = {10.1000/example.2020}\n}")

    def test_openalex_arxiv_gbt7714_preprint_form(self):
        """预印本走 [EB/OL] 电子资源形态，venue 是 arXiv 时不进 tail。"""
        self.assertEqual(
            format_citation(_arxiv_meta(), "gbt7714"),
            "Vaswani A, de Las Casas D, van Beethoven L. "
            "Attention Is All You Need[EB/OL]. (2017). "
            "https://doi.org/10.48550/arXiv.1706.03762.")

    def test_openalex_arxiv_apa(self):
        self.assertEqual(
            format_citation(_arxiv_meta(), "apa"),
            "Vaswani, A., de Las Casas, D., van Beethoven, L. "
            "(2017). Attention Is All You Need. "
            "https://doi.org/10.48550/arXiv.1706.03762")

    def test_openalex_arxiv_bibtex_misc_preprint(self):
        self.assertEqual(
            format_citation(_arxiv_meta(), "bibtex"),
            "@misc{vaswani2017,\n"
            "  author = {Vaswani, Ashish and de Las Casas, Diego "
            "and van Beethoven, Ludwig},\n"
            "  title = {Attention Is All You Need},\n"
            "  year = {2017},\n"
            "  howpublished = {arXiv preprint},\n"
            "  doi = {10.48550/arXiv.1706.03762}\n}")

    def test_openalex_journal_uses_venue(self):
        meta = from_openalex_work(OPENALEX_JOURNAL_WORK)
        self.assertEqual(meta["type"], "journal-article")
        self.assertEqual(meta["container"], "Journal of Examples")
        self.assertEqual(meta["pages"], "10-20")
        self.assertIsNone(meta["preprint_server"])
        self.assertEqual(
            format_citation(meta, "gbt7714"),
            "Doe J. A plain OpenAlex work[J]. Journal of Examples, 2021, 5(2).")

    def test_unknown_style_raises(self):
        with self.assertRaises(CiteError):
            format_citation(_crossref_meta(), "mla")


class TestFetchRouting(unittest.TestCase):
    """打桩打在 urllib.request.urlopen（网络读取处），按 URL 域名路由。"""

    def test_arxiv_doi_skips_crossref(self):
        """10.48550/ 前缀：Crossref 不收，请求不该发向它。"""
        seen = []
        with patch("citations.http_open",
                   _router_urlopen(seen, None, OPENALEX_ARXIV_WORK)):
            meta = fetch_metadata("10.48550/arXiv.1706.03762")
        self.assertEqual(len(seen), 1)
        self.assertIn("api.openalex.org", seen[0])
        self.assertNotIn("api.crossref.org", seen[0])
        self.assertEqual(meta["type"], "posted-content")
        self.assertEqual(meta["preprint_server"], "arXiv")
        self.assertEqual(meta["doi"], "10.48550/arXiv.1706.03762")

    def test_crossref_success_no_openalex_call(self):
        seen = []
        with patch("citations.http_open",
                   _router_urlopen(seen, CROSSREF_PAYLOAD, None)):
            meta = fetch_metadata("10.1000/example.2020")
        self.assertEqual([u for u in seen if "api.crossref.org" in u], seen)
        self.assertEqual(meta["title"], "Attention & memory: a study")
        self.assertEqual(meta["year"], "2020")

    def test_crossref_failure_falls_back_to_openalex(self):
        boom = urllib.error.HTTPError("https://api.crossref.org/works/x",
                                      404, "Not Found", {}, io.BytesIO(b""))
        seen = []
        with patch("citations.http_open",
                   _router_urlopen(seen, boom, OPENALEX_JOURNAL_WORK)):
            meta = fetch_metadata("10.1000/openalex.2021")
        self.assertEqual(len(seen), 2)
        self.assertIn("api.crossref.org", seen[0])
        self.assertIn("api.openalex.org", seen[1])
        self.assertEqual(meta["container"], "Journal of Examples")

    def test_both_sources_fail_honest_error(self):
        boom404 = urllib.error.HTTPError("https://api.crossref.org/works/x",
                                         404, "Not Found", {}, io.BytesIO(b""))
        boom500 = urllib.error.HTTPError("https://api.openalex.org/works/x",
                                         500, "Server Error", {}, io.BytesIO(b""))
        with patch("citations.http_open",
                   _router_urlopen([], boom404, boom500)):
            with self.assertRaises(CiteError) as ctx:
                fetch_metadata("10.1000/nowhere.2020")
        self.assertIn("10.1000/nowhere.2020", str(ctx.exception))
        self.assertIn("OpenAlex", str(ctx.exception))

    def test_crossref_http_error_message_carries_status(self):
        """HTTPError 的错误体进错误消息：归因不靠猜。"""
        boom = urllib.error.HTTPError("https://api.crossref.org/works/x",
                                      403, "Forbidden", {}, io.BytesIO(b"no quota"))
        with patch("citations.http_open",
                   _router_urlopen([], boom, boom)):
            with self.assertRaises(CiteError) as ctx:
                fetch_metadata("10.1000/nowhere.2020")
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_invalid_doi_rejected_before_network(self):
        for bad in ("", "not-a-doi", "11.1234/x", "10.abc/x"):
            with self.assertRaises(CiteError, msg=bad):
                fetch_metadata(bad)


class TestBibtexKey(unittest.TestCase):
    def _meta(self, family, year):
        return {"doi": "10.1000/x", "authors": [{"family": family, "given": "A"}],
                "title": "T", "container": "", "year": year, "volume": "",
                "issue": "", "pages": "", "publisher": "",
                "type": "journal-article", "url": "", "preprint_server": None}

    def test_dedup_in_batch(self):
        used = set()
        self.assertEqual(format_citation(self._meta("Vaswani", 2020), "bibtex",
                                         used_keys=used).split("{")[1].split(",")[0],
                         "vaswani2020")
        second = format_citation(self._meta("Vaswani", 2020), "bibtex", used_keys=used)
        self.assertIn("{vaswani2020a,", second)

    def test_illegal_chars_stripped(self):
        used = set()
        key = format_citation(self._meta("O'Brien", 2020), "bibtex",
                              used_keys=used).split("{")[1].split(",")[0]
        self.assertEqual(key, "obrien2020")

    def test_particle_family_kept_as_word_run(self):
        used = set()
        key = format_citation(self._meta("de Las Casas", 2017), "bibtex",
                              used_keys=used).split("{")[1].split(",")[0]
        self.assertEqual(key, "delascasas2017")

    def test_no_family_falls_back_to_unknown(self):
        used = set()
        meta = self._meta("", 2020)
        meta["authors"] = []
        key = format_citation(meta, "bibtex", used_keys=used).split("{")[1].split(",")[0]
        self.assertEqual(key, "unknown2020")


class TestCiteCli(unittest.TestCase):
    GOOD_DOI = "10.1000/example.2020"

    def _main(self, argv, fetch):
        out, err = io.StringIO(), io.StringIO()
        with patch("cite_cli.fetch_metadata", fetch), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cite_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _good_fetch(self, doi):
        return _crossref_meta()

    def test_single_doi_success_exit_zero(self):
        code, out, err = self._main([self.GOOD_DOI], self._good_fetch)
        self.assertEqual(code, 0)
        self.assertIn("[J]. Journal of Examples, 2020", out)
        self.assertEqual(err, "")

    def test_one_failure_does_not_kill_the_rest(self):
        def fetch(doi):
            if "bad" in doi:
                raise CiteError("Crossref 与 OpenAlex 均未查到 " + doi)
            return _crossref_meta()

        code, out, err = self._main([self.GOOD_DOI, "10.1000/bad", "--style", "apa"], fetch)
        self.assertEqual(code, 1)
        # 好的那条照常输出（先于坏条的报错到达 stdout）
        self.assertIn("https://doi.org/10.1000/example.2020", out)
        self.assertIn("10.1000/bad", err)
        self.assertIn("Crossref 与 OpenAlex", err)

    def test_json_output_marks_errors_inline(self):
        def fetch(doi):
            if "bad" in doi:
                raise CiteError("Crossref 与 OpenAlex 均未查到 " + doi)
            return _crossref_meta()

        code, out, _ = self._main(
            [self.GOOD_DOI, "10.1000/bad", "--style", "bibtex", "--json"], fetch)
        self.assertEqual(code, 1)
        results = json.loads(out)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["doi"], self.GOOD_DOI)
        self.assertTrue(results[0]["citation"].startswith("@article{"))
        self.assertEqual(results[1]["doi"], "10.1000/bad")
        self.assertIn("error", results[1])

    def test_default_style_is_gbt7714(self):
        code, out, _ = self._main([self.GOOD_DOI], self._good_fetch)
        self.assertIn("[J]", out)
        code, out, _ = self._main([self.GOOD_DOI, "--style", "gbt7714n"], self._good_fetch)
        self.assertTrue(out.startswith("[1] "))

    def test_bin_argo_registers_cite(self):
        """bin/argo 的子命令表里有 cite，且入口能到达 argparse。"""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "argo"), "cite", "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--style", proc.stdout)
        self.assertIn("--json", proc.stdout)


if __name__ == "__main__":
    unittest.main()
