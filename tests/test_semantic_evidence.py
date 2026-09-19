#!/usr/bin/env python3
"""语义证据层（classifier.dev）的回归测试（全 mock，不联网）。

本层默认关闭，所以主要回归面有两类：
  1. **关闭时零影响**：verify 输出不得出现 semantic 键、结果上不得挂
     semantic_support——「开关没开」必须与「没接入」逐位一致；
  2. **开启时 fail-open**：外部服务非 200 / 抛异常 / 返回条数不匹配，
     核验链路必须照常返回，只是没有语义字段。

标签集纪律也在这里锁住：服务永远返回给定标签之一，标签集必须显式含
「none of these」，否则无关正文会被硬塞进某个语义标签（D 报告实测）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import semantic_evidence as sem  # noqa: E402
from evidence_loop import verify_results  # noqa: E402


def _fake_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    return {"success": True, "url": url, "title": f"Title {url}",
            "content": f"body of {url} " * 30, "fetch_method": "http"}


def _results():
    return [{"url": "https://a.com/x", "title": "A", "snippet": "s"},
            {"url": "https://b.com/y", "title": "B", "snippet": "s"}]


class TestGate(unittest.TestCase):
    def test_default_off(self):
        """未设置 env、config 缺省 → 关闭。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARGO_SEMANTIC_EVIDENCE", None)
            with patch.object(sem, "_config", return_value={}):
                self.assertFalse(sem.enabled())

    def test_env_on(self):
        with patch.dict(os.environ, {"ARGO_SEMANTIC_EVIDENCE": "1"}):
            self.assertTrue(sem.enabled())

    def test_env_off_beats_config_on(self):
        """env 显式关时，即使 config 开着也必须关（env 优先）。"""
        with patch.dict(os.environ, {"ARGO_SEMANTIC_EVIDENCE": "off"}):
            with patch.object(sem, "_config", return_value={"enabled": True}):
                self.assertFalse(sem.enabled())

    def test_labels_include_none(self):
        for labels in (sem.SUPPORT_LABELS, sem.SOURCE_TYPE_LABELS):
            self.assertIn("none of these", labels)


class TestClassifyFailOpen(unittest.TestCase):
    def test_non_200_returns_none(self):
        with patch("http_client.HttpClient.post",
                   return_value={"status": 429, "text": "", "headers": {}}):
            self.assertIsNone(sem.classify(["x"], sem.SUPPORT_LABELS))

    def test_exception_returns_none(self):
        with patch("http_client.HttpClient.post", side_effect=RuntimeError("boom")):
            self.assertIsNone(sem.classify(["x"], sem.SUPPORT_LABELS))

    def test_count_mismatch_returns_none(self):
        with patch("http_client.HttpClient.post",
                   return_value={"status": 200, "headers": {},
                                 "text": '{"results": [{"label": "neutral"}]}'}):
            self.assertIsNone(sem.classify(["x", "y"], sem.SUPPORT_LABELS))

    def test_empty_texts_returns_none(self):
        self.assertIsNone(sem.classify(["  ", ""], sem.SUPPORT_LABELS))

    def test_threshold_gate(self):
        """contradicts/supports 只在 conf ≥ 0.7 时为 True，原始标签仍保留。"""
        items = [{"url": "https://a.com/x", "title": "A", "text": "t"},
                 {"url": "https://b.com/y", "title": "B", "text": "t"}]
        payload = {"status": 200, "headers": {}, "text": (
            '{"results": ['
            '{"label": "contradicts the query", "confidence": 0.95},'
            '{"label": "contradicts the query", "confidence": 0.43}]}')}
        with patch("http_client.HttpClient.post", return_value=payload):
            out = sem.assess_support("q", items)
        self.assertTrue(out["https://a.com/x"]["contradicts"])
        self.assertFalse(out["https://b.com/y"]["contradicts"])
        self.assertEqual(out["https://b.com/y"]["label"], "contradicts the query")


class TestVerifyIntegration(unittest.TestCase):
    def test_disabled_is_bit_identical(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARGO_SEMANTIC_EVIDENCE", None)
            with patch.object(sem, "_config", return_value={}):
                out = verify_results(_results(), "q", fetch_fn=_fake_fetch, top_k=2)
        self.assertNotIn("semantic", out)
        self.assertNotIn("semantic_support", out["verified"][0])

    def test_enabled_attaches_support(self):
        fake = {"https://a.com/x": {"label": "supports the query", "confidence": 0.9,
                                    "supports": True, "contradicts": False},
                "https://b.com/y": {"label": "contradicts the query", "confidence": 0.8,
                                    "supports": False, "contradicts": True}}
        with patch.object(sem, "enabled", return_value=True), \
                patch.object(sem, "assess_support", return_value=fake):
            out = verify_results(_results(), "q", fetch_fn=_fake_fetch, top_k=2)
        self.assertEqual(out["semantic"]["scored"], 2)
        self.assertEqual(out["semantic"]["contradicts"], 1)
        self.assertTrue(out["verified"][0]["semantic_support"]["supports"])

    def test_enabled_but_service_down_still_verifies(self):
        with patch.object(sem, "enabled", return_value=True), \
                patch.object(sem, "assess_support", return_value=None):
            out = verify_results(_results(), "q", fetch_fn=_fake_fetch, top_k=2)
        self.assertNotIn("semantic", out)
        self.assertEqual(out["revision_summary"]["n"], 2)


if __name__ == "__main__":
    unittest.main()
