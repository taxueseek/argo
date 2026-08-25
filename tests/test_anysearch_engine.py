#!/usr/bin/env python3
"""anysearch 进程内 builder：解析与配额判定边界测试（mock HttpClient，无网络）。

覆盖 2026-08 修复：
  - 结果块顶格开头（无前导 \\n）时首块不丢失（re.split 行首锚定）
  - 配额信号判定只在「无任何结果块」时触发（正文含 quota 词不误判）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engines_builders_tech import _build_anysearch_engine  # noqa: E402

# 响应体按 anysearch JSON-RPC content 文本块组织
_SAMPLE_HAS_LEADING_NL = (
    "\n### 1. OpenAI 发布 GPT-5\n"
    "简介文本\n"
    "- **URL**: https://openai.com/gpt-5\n"
    "\n"
    "### 2. GPT-5 评测汇总\n"
    "第二篇\n"
    "- **URL**: https://example.com/review\n"
)

# 顶格开头：首个结果块前无换行（旧 re.split(r"\n### ...") 会整块丢失）
_SAMPLE_TOPPED = (
    "### 1. OpenAI 发布 GPT-5\n"
    "简介文本\n"
    "- **URL**: https://openai.com/gpt-5\n"
    "\n"
    "### 2. GPT-5 评测汇总\n"
    "第二篇\n"
    "- **URL**: https://example.com/review\n"
)


def _engine_with(resp_text: str, status: int = 200):
    """构造 builder 并 mock HttpClient.post 返回固定响应（JSON-RPC 结构）。"""
    import json

    eng = _build_anysearch_engine({"timeout": 5})
    payload = {
        "status": status,
        "headers": {"content-type": "application/json"},
        "text": json.dumps({"result": {"content": [{"text": resp_text}]}}),
        "url": "https://api.anysearch.com/mcp",
        "elapsed_ms": 10,
    }
    with patch("http_client.HttpClient") as MockClient:
        MockClient.return_value.post.return_value = payload
        return eng("测试查询", n=5)


class TestAnysearchBuilder(unittest.TestCase):
    def test_parses_block_with_leading_newline(self):
        results = _engine_with(_SAMPLE_HAS_LEADING_NL)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "OpenAI 发布 GPT-5")
        self.assertEqual(results[0]["url"], "https://openai.com/gpt-5")

    def test_parses_topped_first_block(self):
        """顶格首块（无前导 \\n）不丢失。"""
        results = _engine_with(_SAMPLE_TOPPED)
        self.assertEqual(len(results), 2, results)
        self.assertEqual(results[0]["title"], "OpenAI 发布 GPT-5")
        self.assertEqual(results[0]["url"], "https://openai.com/gpt-5")
        self.assertEqual(results[1]["title"], "GPT-5 评测汇总")

    def test_quota_words_in_real_content_not_hit(self):
        """结果块存在时正文出现 'quota/rate limit' 不算配额耗尽。"""
        resp = (
            "\n### 1. API rate limit 处理指南\n"
            "关于 429 quota 的说明文字\n"
            "- **URL**: https://example.com/429\n"
        )
        results = _engine_with(resp)
        self.assertEqual(len(results), 1)

    def test_quota_only_response_returns_empty(self):
        resp = "quota exhausted, daily_free_quota reached, please recharge"
        results = _engine_with(resp)
        self.assertEqual(results, [])

    def test_http_error_returns_empty(self):
        results = _engine_with("anything", status=429)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
