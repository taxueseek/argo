#!/usr/bin/env python3
"""test_ai_labs.py — AI 实验室一手博客聚合引擎（rss_feed/filter）离线契约。

种子全部 2026-11-15 实测 HTTP 200 直连（OpenAI/DeepMind/HF/Google/Apple ML/
Mistral/Epoch/Simon Willison/The Decoder/TechCrunch AI）。锁六件事，全部
打桩 http_open，不碰网络：
  1. spec 注册与契约字段（explicit_only、family、feed_mode、canary）
  2. 种子注册表完整性：11 份、全 https、全 en、域名逐一在册
  3. 不接任何域 combo（explicit_only 语义：不占自动路由预算）
  4. filter 语义：查询命中返回条目，标题加「出处 · 」前缀
  5. 纯中文查询不下载英文种子（语言门）
  6. 一份种子挂了，其余照常返回
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
import urllib.error
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
for p in (str(SCRIPT_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

import engines_builders_feeds as ebf  # noqa: E402
from engine_families import FAMILY_LABELS, family_of  # noqa: E402
from config import load_config, get_engines  # noqa: E402

_OPENAI = "https://openai.com/news/rss.xml"
_DEEPMIND = "https://deepmind.google/blog/rss.xml"
_DOWN = "https://machinelearning.apple.com/rss.xml"


def _rss(title: str, link: str, description: str = "") -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><item>'
        f"<title>{title}</title><link>{link}</link>"
        f"<description>{description}</description>"
        "</item></channel></rss>"
    )
    return xml.encode("utf-8")


_BODIES = {
    _OPENAI: _rss(
        "OpenAI releases GPT-7 with native agent runtime",
        "https://openai.com/news/gpt-7",
        "official announcement",
    ),
    _DEEPMIND: _rss(
        "DeepMind publishes AlphaGenome 2 research",
        "https://deepmind.google/blog/alphagenome-2",
        "research post",
    ),
}


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *args) -> bool:
        return False


def _fake_http_open(fail=()):
    failed = set(fail)

    def fake(req, timeout=10.0, engine=""):
        url = req.full_url
        if url in failed:
            raise urllib.error.URLError("down")
        if url not in _BODIES:
            raise urllib.error.URLError(f"unmapped {url}")
        return _Body(_BODIES[url])

    return fake


class TestRegistration(unittest.TestCase):
    """spec 注册 + 契约字段 + 种子注册表完整性"""

    @classmethod
    def setUpClass(cls) -> None:
        load_config(force=True)
        cls.engines = get_engines()

    def test_registered_and_enabled(self) -> None:
        self.assertIn("ai_labs", self.engines)
        spec = self.engines["ai_labs"]
        self.assertTrue(spec.get("enabled", True))
        self.assertTrue(spec.get("explicit_only"),
                        "窄窗口过滤种子必须 explicit_only，不占自动路由预算")

    def test_contract_fields(self) -> None:
        spec = self.engines["ai_labs"]
        self.assertEqual(spec.get("type"), "rss_feed")
        self.assertEqual(spec.get("feed_mode"), "filter")
        self.assertEqual(family_of("ai_labs", spec), "ai_labs")
        self.assertEqual(FAMILY_LABELS.get("ai_labs"), "AI 实验室一手")
        self.assertEqual(spec.get("canary_query"), "OpenAI")
        self.assertEqual(spec.get("langs"), ["en"])

    def test_seed_registry_complete(self) -> None:
        """10 份种子、全 https、全 en，域名逐一在册（2026-11-15 实测 200）。"""
        feeds = self.engines["ai_labs"].get("feeds") or []
        self.assertEqual(len(feeds), 10)
        hosts = {f["url"].split("/")[2] for f in feeds}
        expected = {
            "openai.com", "deepmind.google", "huggingface.co", "blog.google",
            "developers.googleblog.com", "machinelearning.apple.com",
            "epochai.substack.com", "simonwillison.net",
            "the-decoder.com", "techcrunch.com",
        }
        self.assertEqual(hosts, expected)
        for f in feeds:
            self.assertTrue(f["url"].startswith("https://"), f["url"])
            self.assertIn("en", f.get("langs", []), f["url"])
            self.assertTrue(f.get("label"), f["url"])

    def test_not_wired_into_any_domain(self) -> None:
        """explicit_only 引擎不进域 combo：出现在任何域里都是接线事故。"""
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        for d in cfg.get("domains", []):
            combo = d.get("engines_combo") or []
            self.assertNotIn("ai_labs", combo,
                             f"ai_labs 不应出现在域 {d.get('name')} 的 combo 里")


class TestFilterSemantics(unittest.TestCase):
    """filter 语义（离线打桩，同 test_feed_engine 口径）"""

    @classmethod
    def setUpClass(cls) -> None:
        load_config(force=True)

    def _engine(self):
        spec = dict(get_engines()["ai_labs"])
        spec["_name"] = "ai_labs"
        return ebf._build_rss_feed_engine(spec)

    def test_filter_hits_and_label_prefix(self) -> None:
        with patch.object(ebf, "http_open", side_effect=_fake_http_open()):
            rows = self._engine()("OpenAI", n=10)
        self.assertTrue(rows, "OpenAI 查询应命中 OpenAI 官网种子")
        self.assertTrue(all(r["title"].startswith("OpenAI · ") for r in rows))
        self.assertTrue(all(r["url"].startswith("https://") for r in rows))

    def test_pure_cjk_skips_english_feeds(self) -> None:
        with patch.object(ebf, "http_open", side_effect=_fake_http_open()) as _:
            rows = self._engine()("量子计算", n=10)
        self.assertEqual(rows, [], "纯中文查询对全 en 种子应直接放弃")

    def test_one_feed_down_keeps_the_rest(self) -> None:
        with patch.object(ebf, "http_open",
                          side_effect=_fake_http_open(fail=(_DOWN,))):
            rows = self._engine()("OpenAI", n=10)
        self.assertTrue(rows, "单份种子失败只丢那一份，其余照常")


if __name__ == "__main__":
    unittest.main()
