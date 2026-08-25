#!/usr/bin/env python3
"""结构化搜索改进：github 按语法切端点 + recovery 结构化语法放宽（离线单测）

覆盖：
  1. _github_endpoint：按 repo:/is: / in:file 选 repositories/issues/code
  2. _github_url：端点映射
  3. _build_github_engine：mock urllib 验 URL 与输出 schema
  4. structured_relax_steps：结果归零时按「排除→…→短语」逐层撤条件

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_structured_search.py -q
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engines_builders_tech import _github_endpoint, _github_url, _build_github_engine  # noqa: E402
from recovery import structured_relax_steps  # noqa: E402


class _FakeResp:
    """假响应对象：支持 context manager + read()，供 mock urlopen 使用。"""
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self._b


class TestGithubEndpointSelect(unittest.TestCase):
    def test_repositories_default(self):
        self.assertEqual(_github_endpoint("memory", has_token=False), "repositories")

    def test_issue_syntax(self):
        self.assertEqual(_github_endpoint("repo:langchain-ai/langchain is:issue memory", False), "issues")
        self.assertEqual(_github_endpoint("repo:x/y is:issue memory", True), "issues")

    def test_code_requires_token(self):
        self.assertEqual(_github_endpoint("in:file memory", True), "code")
        self.assertEqual(_github_endpoint("in:file memory", False), "issues")  # 无 token 尽力退到 issues

    def test_url_mapping(self):
        self.assertIn("/search/repositories", _github_url("repositories", "memory", 5))
        self.assertIn("/search/issues", _github_url("issues", "repo:x/y is:issue memory", 5))
        self.assertIn("/search/code", _github_url("code", "in:file memory", 5))


class TestGithubEngine(unittest.TestCase):
    def _resp(self, payload: dict):
        return _FakeResp(payload)

    def test_repositories(self):
        eng = _build_github_engine({"timeout": 8})
        payload = {"items": [{
            "full_name": "openai/openai-python",
            "html_url": "https://github.com/openai/openai-python",
            "description": "the official library",
            "stargazers_count": 30000, "language": "Python", "updated_at": "2024-01-01",
        }]}
        with mock.patch("urllib.request.urlopen", return_value=self._resp(payload)) as uo:
            res = eng("openai python", n=3)
        self.assertEqual(res[0]["title"], "openai/openai-python")
        self.assertEqual(res[0]["source"], "github")
        self.assertIn("/search/repositories", uo.call_args[0][0].full_url)

    def test_issues(self):
        eng = _build_github_engine({"timeout": 8})
        payload = {"items": [{
            "title": "Memory leaks",
            "html_url": "https://github.com/x/y/issues/1",
            "repository_url": "https://api.github.com/repos/x/y",
            "state": "open", "comments": 3,
            "user": {"login": "alice"}, "created_at": "2024-01-01",
        }]}
        with mock.patch("urllib.request.urlopen", return_value=self._resp(payload)) as uo:
            res = eng("repo:x/y is:issue memory", n=3)
        self.assertEqual(res[0]["metadata"]["repo"], "x/y")
        self.assertEqual(res[0]["snippet"].split("|")[0].strip(), "[x/y] open")
        self.assertIn("/search/issues", uo.call_args[0][0].full_url)


class TestStructuredRelax(unittest.TestCase):
    def test_plain_query_no_steps(self):
        self.assertEqual(structured_relax_steps("openai api"), [])

    def test_removes_classes_in_order(self):
        steps = structured_relax_steps('from:OpenAI "GPT-5" min_faves:100 until:2026-01-01 -replies')
        self.assertTrue(steps)
        self.assertTrue(any("min_faves" not in s and "-replies" not in s for s in steps))
        self.assertEqual(steps[-1], "GPT-5")  # 最终只保留短语核心词

    def test_repo_is_strip(self):
        steps = structured_relax_steps("repo:langchain-ai/langchain is:issue memory")
        self.assertEqual(steps, ["memory"])

    def test_site_removed(self):
        steps = structured_relax_steps("site:platform.openai.com responses api")
        self.assertEqual(steps, ["responses api"])


class TestStripStructured(unittest.TestCase):
    def test_strip_structured(self):
        from recovery import strip_structured
        self.assertEqual(strip_structured('from:OpenAI "GPT-5" min_faves:100 until:2026-01-01'), "GPT-5")
        self.assertEqual(strip_structured("repo:langchain-ai/langchain is:issue memory"), "memory")
        self.assertEqual(strip_structured("site:platform.openai.com responses api"), "responses api")
        self.assertEqual(strip_structured("openai api"), "openai api")   # 无语法原样
        self.assertEqual(strip_structured("python vs java"), "python vs java")
        self.assertEqual(strip_structured("node -v"), "node -v")          # 不误伤连字符词


class TestSemanticRouting(unittest.TestCase):
    def _mk(self, seen):
        def fake(query, n=5, timeout=8, depth="fast", mode="fast", **kw):
            seen["q"] = query
            return []
        return fake

    def test_semantic_engine_strips(self):
        import engines
        seen = {}
        with mock.patch.object(engines, "get_registry", return_value={"byted": self._mk(seen)}):
            engines.search('from:X "gpt" min_faves:5', "byted")
        self.assertEqual(seen["q"], "gpt")  # 语义引擎收到剥字段后的核心词

    def test_passthrough_engine_keeps(self):
        import engines
        seen = {}
        with mock.patch.object(engines, "get_registry", return_value={"local_bing": self._mk(seen)}):
            engines.search('from:X "gpt"', "local_bing")
        self.assertEqual(seen["q"], 'from:X "gpt"')  # 透传引擎保留平台语法


class TestStructuredPlatformDomain(unittest.TestCase):
    """social 域提前：语法判定真源在 config.yaml social patterns（单一真源），
    route 侧只做命中顺序调整（_social_domain_first）。"""

    def test_social_syntax(self):
        from route import _social_domain_first, match_domains, get_domains
        hits = match_domains("from:OpenAI GPT-5", get_domains())
        ordered = _social_domain_first(hits)
        self.assertEqual(ordered[0]["name"], "social")  # 提前为主域
        # 无 social 命中时原样返回
        self.assertEqual(_social_domain_first([]), [])

    def test_non_social_syntax_no_boost(self):
        from route import _social_domain_first, match_domains, get_domains
        hits = match_domains("repo:langchain-ai/langchain is:issue memory", get_domains())
        self.assertNotEqual(hits[0]["name"], "social")  # repo: 归 GitHub/code 语义
        before = [h["name"] for h in hits]
        self.assertEqual([h["name"] for h in _social_domain_first(hits)], before)

    def test_plain_query_unchanged(self):
        from route import _social_domain_first, match_domains, get_domains
        hits = match_domains("openai api", get_domains())
        self.assertEqual(_social_domain_first(hits), hits)


if __name__ == "__main__":
    unittest.main()
