#!/usr/bin/env python3
"""tests/test_cn_ai_news.py — 中文 AI 垂直资讯引擎原生集成

吸收外部 AI 资讯聚合技能的能力，以 argo 声明式 http 引擎（cn_ai_news）
原生接入，不引用任何外部技能名称：
  - engines/specs/cn_ai_news.yaml：免 Key REST API（aihot.news /api/v1）
  - v1 迁移（issue #49，站长通知）：旧站 virxact /api/public/* 已于
    2026-10-31 停服；take→limit，url→links.original、source→source.name
  - 输出含 publishedAt 时间戳与上游来源（preserve_source 保留真实发布方）
  - config.yaml chinese_tech_deep 域 combo 接入

覆盖：spec 注册、family/dedupe 存活、域路由接线、字段提取（嵌套
output_map 点路径）、来源保留语义、URL 构造、全链路 registry 调用。
全程 mock 网络层，离线必过（ARGO_LIVE=1 时追加真实 API 冒烟）。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
for p in (str(SCRIPT_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

from engines_base import _build_http_engine, _ensure_engine_source  # noqa: E402
from engine_families import family_of, dedupe_by_family  # noqa: E402
from config import load_config, get_engines  # noqa: E402

LIVE = os.environ.get("ARGO_LIVE", "").strip() in {"1", "true", "yes"}

# ── 真实 API 响应结构样例（取自在线抓包，字段名逐一保持一致） ──────────────────

SAMPLE_ITEMS = {
    "schemaVersion": 1,
    "query": {
        "mode": "selected", "category": None, "window": "7d",
        "q": "Claude", "by": "timeline", "ordering": "timelineDesc",
    },
    "items": [
        {
            "id": "e9o5h2c1o5f0xuh2bgqtc2w7k",
            "title": "Cursor 披露安全事件调查结果，恶意 Claude Code 扩展波及最多 5000 用户",
            "originalTitle": "Cursor 披露安全事件调查结果，恶意 Claude Code 扩展波及最多 5000 用户",
            "summary": "Cursor 发布安全事件完整调查：攻击者利用 Fireworks 暴露的访问密钥读取用于模型训练的 coding agent trace，少于 0.01% 客户的 API 凭证暴露。",
            "source": {"name": "Cursor Blog"},
            "links": {
                "aihot": "https://aihot.news/items/e9o5h2c1o5f0xuh2bgqtc2w7k",
                "original": "https://cursor.com/blog/security-incident-update",
            },
            "publishedAt": "2026-11-13T12:00:00.000Z",
            "discoveredAt": "2026-11-13T12:20:00.000Z",
            "category": "industry",
            "score": 88,
            "selected": True,
            "reason": "原文给出事件完整时间线与影响面收敛结论，开发者可据此评估扩展供应链风险。",
            "attribution": {
                "name": "AIHOT",
                "url": "https://aihot.news/items/e9o5h2c1o5f0xuh2bgqtc2w7k",
            },
        },
        {
            "id": "ypeap2t8w4fqqfuvt7mw3wg79",
            "title": "Anthropic 详解常驻智能体的五个核心架构原语",
            "summary": "Anthropic 发布博客，拆解常驻智能体（always-on agents）背后的架构。",
            "source": {"name": "Claude：Blog（网页）"},
            "links": {
                "aihot": "https://aihot.news/items/ypeap2t8w4fqqfuvt7mw3wg79",
                "original": "https://claude.com/blog/building-always-on-agents",
            },
            "publishedAt": "2026-11-13T00:00:00.000Z",
            "discoveredAt": "2026-11-13T00:05:00.000Z",
            "category": "tip",
            "score": 81,
            "selected": True,
        },
    ],
    "page": {"count": 2, "hasMore": True, "nextCursor": "it3.eyJhIjox"},
}


class _FakeResp:
    """上下文管理器风格的 urlopen 响应桩。"""

    def __init__(self, body: bytes, url: str = "") -> None:
        self._body = body
        self.url = url

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _run_http(query: str, n: int = 5, body: bytes | None = None,
              spec_overrides: dict | None = None) -> tuple[list[dict], str]:
    """用 mock urlopen 跑一次 http 引擎调用，返回 (results, 实际请求 URL)。"""
    spec = dict(get_engines()["cn_ai_news"])
    spec["_name"] = "cn_ai_news"
    if spec_overrides:
        spec.update(spec_overrides)
    engine = _build_http_engine(spec)
    body = body if body is not None else json.dumps(SAMPLE_ITEMS).encode("utf-8")
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp(body)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        results = engine(query, n=n)
    return results, captured.get("url", "")


class TestRegistration(unittest.TestCase):
    """spec 注册 + 字段完整性"""

    def test_registered_and_enabled(self) -> None:
        load_config(force=True)
        engines = get_engines()
        self.assertIn("cn_ai_news", engines, "cn_ai_news 未注册（检查 engines/specs/cn_ai_news.yaml）")
        self.assertTrue(engines["cn_ai_news"].get("enabled", True))
        self.assertIn("aihot.news", engines["cn_ai_news"].get("url", ""))

    def test_spec_key_fields(self) -> None:
        engines = get_engines()
        spec = engines["cn_ai_news"]
        self.assertEqual(spec.get("type"), "http")
        self.assertEqual(spec.get("family"), "news_flash")
        self.assertEqual(spec.get("query_param"), "q")
        self.assertTrue(spec.get("canary_query"))
        self.assertTrue(spec.get("preserve_source"), "preserve_source 应为 true 保留上游来源")
        om = spec.get("output_map", {})
        self.assertEqual(om.get("items"), "items")
        self.assertEqual(om.get("item_title"), "title")
        self.assertEqual(om.get("item_url"), "links.original",
                         "v1 迁移后原文链接在 links.original（issue #49）")
        self.assertEqual(om.get("item_summary"), "summary")
        self.assertEqual(om.get("item_source"), "source.name",
                         "v1 迁移后来源名在 source.name（issue #49）")
        self.assertEqual(om.get("item_published_at"), "publishedAt")

    def test_spec_file_exists(self) -> None:
        spec_file = SKILL_DIR / "engines" / "specs" / "cn_ai_news.yaml"
        self.assertTrue(spec_file.is_file(), "缺少 engines/specs/cn_ai_news.yaml")

    def test_family_mapping(self) -> None:
        spec = get_engines()["cn_ai_news"]
        self.assertEqual(family_of("cn_ai_news", spec), "news_flash")


class TestRouting(unittest.TestCase):
    """域路由接线 + dedupe 存活"""

    def test_chinese_tech_deep_combo_has_engine(self) -> None:
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        domains = {d["name"]: d for d in cfg["domains"]}
        self.assertIn("chinese_tech_deep", domains)
        self.assertIn("cn_ai_news", domains["chinese_tech_deep"]["engines_combo"],
                      "chinese_tech_deep 域 combo 应包含 cn_ai_news")

    def test_dedupe_keeps_engine_not_dropped_as_third_web_general(self) -> None:
        """byted/octen 已占满 web_general 两个槽位；cn_ai_news 若默认为
        web_general 会被 dedupe 挤掉。family: news_flash 使其存活。"""
        specs = get_engines()
        combo = ["byted", "octen", "juejin", "cn_ai_news"]
        deduped = dedupe_by_family(
            combo, max_per_family=2, spec_lookup=specs,
            limit_families=frozenset({"web_general"}),
        )
        self.assertIn("cn_ai_news", deduped)


class TestParsing(unittest.TestCase):
    """字段提取 + 时间戳 + 来源保留"""

    def test_extracts_fields(self) -> None:
        results, url = _run_http("大模型", n=5)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["title"], "Cursor 披露安全事件调查结果，恶意 Claude Code 扩展波及最多 5000 用户")
        self.assertEqual(first["url"], "https://cursor.com/blog/security-incident-update")
        self.assertIn("少于 0.01% 客户的 API 凭证暴露", first["snippet"])

    def test_published_at_preserved(self) -> None:
        results, _ = _run_http("大模型", n=5)
        self.assertEqual(results[0]["published_at"], "2026-11-13T12:00:00.000Z")
        self.assertEqual(results[1]["published_at"], "2026-11-13T00:00:00.000Z")

    def test_source_preserved_via_preserve_source(self) -> None:
        """preserve_source=true 时保留上游发布方（v1 取 source.name），而非改写为引擎名。"""
        results, _ = _run_http("大模型", n=5)
        self.assertEqual(results[0]["source"], "Cursor Blog")
        self.assertEqual(results[1]["source"], "Claude：Blog（网页）")

    def test_without_preserve_source_corrects_to_engine_name(self) -> None:
        """去掉 preserve_source 时回落默认语义：source 统一为引擎名。"""
        results, _ = _run_http("大模型", n=5,
                               spec_overrides={"preserve_source": False})
        self.assertEqual(results[0]["source"], "cn_ai_news")

    def test_ensure_source_preserve_semantics(self) -> None:
        """_ensure_engine_source preserve 参数：保留真实来源、空来源保底引擎名。"""
        kept = _ensure_engine_source(
            [{"title": "t", "url": "https://u", "source": "字节 Seed"}],
            "cn_ai_news", preserve=True,
        )
        self.assertEqual(kept[0]["source"], "字节 Seed")
        filled = _ensure_engine_source(
            [{"title": "t", "url": "https://u"}], "cn_ai_news", preserve=True,
        )
        self.assertEqual(filled[0]["source"], "cn_ai_news")
        # 默认行为不变：不匹配来源仍纠正为引擎名
        fixed = _ensure_engine_source(
            [{"title": "t", "url": "https://u", "source": "other"}], "cn_ai_news",
        )
        self.assertEqual(fixed[0]["source"], "cn_ai_news")

    def test_url_construction(self) -> None:
        """URL 应带 mode=selected、limit={n}（v1：take→limit）、q=关键词；不得残留可选占位符。"""
        results, url = _run_http("大模型", n=5)
        self.assertIn("mode=selected", url)
        self.assertIn("limit=5", url)
        self.assertNotIn("take=", url, "v1 已停用 take 参数，不得出现")
        self.assertIn("q=%E5%A4%A7%E6%A8%A1%E5%9E%8B", url)
        self.assertNotIn("since=", url, "未透传 since，不得出现该参数")
        self.assertNotIn("{", url, "URL 不得含未解析占位符残留")

    def test_engine_search_integration_via_registry(self) -> None:
        """走真实 registry 构建路径（engines.search），验证端到端接线。"""
        from engines import search as engine_search

        body = json.dumps(SAMPLE_ITEMS).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            results = engine_search("大模型", "cn_ai_news", n=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["_engine"], "cn_ai_news")
        self.assertIn("published_at", results[0])


@unittest.skipUnless(LIVE, "set ARGO_LIVE=1 for live cn_ai_news calls")
class TestLive(unittest.TestCase):
    def test_live_search(self) -> None:
        from engines import search as engine_search

        results = engine_search("大模型", "cn_ai_news", n=3, timeout=12)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0, "live 空结果")
        r = results[0]
        self.assertTrue(r.get("title"))
        self.assertTrue(r.get("url", "").startswith("http"))
        self.assertIn("published_at", r)


if __name__ == "__main__":
    unittest.main()
