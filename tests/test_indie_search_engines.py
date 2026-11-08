#!/usr/bin/env python3
"""独立索引引擎测试 — Marginalia / Wiby。

覆盖：
  1. 解析（离线 mock，必过）：字段映射、大写键、坏数据容错
  2. 注册（离线）：config.yaml 声明 + builder 注册一致性
  3. 真实调用（网络依赖，可 skip）

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_indie_search_engines.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from engines_builders_data import (  # noqa: E402
    _build_marginalia_engine,
    _build_wiby_engine,
)


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestMarginaliaParse(unittest.TestCase):
    """2026-11-08 搬家后契约：old-search 两步 sst 流程 + HTML 解析。

    fixtures 取自 2026-11-08 实测真实响应（等待页 1011B / 结果页 58.5KB、
    88 外链）的骨架，结构与类名（card search-result / title / description /
    additional-results）逐一对齐上游。
    """

    _WAIT_PAGE = (
        b'<html lang="en-US"><head><title>Error</title></head><body>'
        b'<div class="infobox"><h1>Wait For A Moment</h1>'
        b'<p>The search engine is currently barraged by queries from bots</p>'
        b'<a href="/search?query=python&amp;sst=S-abc123def">this link</a>'
        b'</div></body></html>'
    )

    _RESULTS_PAGE = b"""<section id="results" class="sb-left">
<section data-ms-rank="5" class="card search-result" >
<div class="url"><a rel="nofollow external" href="https://a.example/1">https://a.example/1</a></div>
<h2> <a tabindex="-1" class="title" rel="nofollow external" href="https://a.example/1">T1</a> </h2>
<p class="description">desc <b>one</b></p>
<div class="utils">Also from a.example</div>
<ul class="additional-results">
<li><a href="https://a.example/1x">Extra One</a></li>
</ul>
</section>
<section data-ms-rank="6" class="card search-result" >
<div class="url"><a rel="nofollow external" href="https://b.example/2">https://b.example/2</a></div>
<h2> <a tabindex="-1" class="title" rel="nofollow external" href="https://b.example/2">T2</a> </h2>
<p class="description">desc two</p>
</section>
</section>"""

    def setUp(self):
        self.engine = _build_marginalia_engine({"timeout": 8})

    def test_gated_two_step_flow(self):
        """bot 门等待页 → 提取 sst → 睡 2.2s 复访 → 解析结果页。"""
        with patch("urllib.request.urlopen",
                   side_effect=[_FakeResp(self._WAIT_PAGE),
                                _FakeResp(self._RESULTS_PAGE)]), \
             patch("time.sleep"):
            res = self.engine("python", 3)
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["title"], "T1")
        self.assertEqual(res[0]["url"], "https://a.example/1")
        self.assertEqual(res[0]["snippet"], "desc one")  # 内嵌 <b> 被剥掉
        self.assertEqual(res[0]["source"], "marginalia")
        # additional-results 补足：只有标题链接，无 description
        self.assertEqual(res[2]["title"], "Extra One")
        self.assertEqual(res[2]["url"], "https://a.example/1x")
        self.assertEqual(res[2]["snippet"], "")

    def test_direct_results_skip_gate(self):
        """未触发 bot 门时一步直出，不发第二次请求。"""
        with patch("urllib.request.urlopen",
                   side_effect=[_FakeResp(self._RESULTS_PAGE)]) as mu:
            res = self.engine("python", 5)
        self.assertEqual(mu.call_count, 1)
        self.assertEqual(len(res), 3)

    def test_n_respects_limit(self):
        cards = b"".join(
            b'<section class="card search-result" >'
            b'<h2> <a class="title" href="https://x/%d">T%d</a> </h2>'
            b'<p class="description">d%d</p></section>' % (i, i, i)
            for i in range(5))
        with patch("urllib.request.urlopen",
                   side_effect=[_FakeResp(b'<section id="results">' + cards + b'</section>')]):
            res = self.engine("q", 2)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["title"], "T0")

    def test_no_results_returns_empty(self):
        with patch("urllib.request.urlopen",
                   side_effect=[_FakeResp(b'<html>nothing here</html>')]):
            self.assertEqual(self.engine("q", 3), [])

    def test_expired_token_gate_again_returns_empty(self):
        """复用过期令牌会被再次门住（2026-11-08 实测形态）：结果为空不抛错。"""
        with patch("urllib.request.urlopen",
                   side_effect=[_FakeResp(self._WAIT_PAGE),
                                _FakeResp(self._WAIT_PAGE)]), \
             patch("time.sleep"):
            self.assertEqual(self.engine("q", 3), [])

    def test_network_error_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertEqual(self.engine("q", 3), [])


class TestWibyParse(unittest.TestCase):
    def setUp(self):
        self.engine = _build_wiby_engine({"timeout": 8})

    def test_uppercase_keys(self):
        payload = [
            {"URL": "https://a.example/1", "Title": "A Page", "Snippet": "snip", "Description": "desc"},
            {"URL": "https://b.example/2", "Title": "B Page", "Snippet": "snip2"},
        ]
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("q", 3)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["title"], "A Page")
        self.assertEqual(res[0]["url"], "https://a.example/1")
        self.assertEqual(res[0]["snippet"], "snip")

    def test_mixed_and_bad_items(self):
        payload = [
            {"URL": "https://c.example/3", "Title": "C"},
            "not a dict",
            {"URL": "https://d.example/4", "Title": "D"},
        ]
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("q", 5)
        self.assertEqual(len(res), 2)  # 非 dict 元素跳过

    def test_non_list_response(self):
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps({"a": 1}).encode())):
            self.assertEqual(self.engine("q", 3), [])

    def test_network_error_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertEqual(self.engine("q", 3), [])


class TestIndieRegistry(unittest.TestCase):
    def test_registered_in_builders(self):
        from engines_builders import _build_marginalia_engine as bm, _build_wiby_engine as bw  # noqa: F401
        from engines import get_registry
        reg = get_registry()
        self.assertIn("marginalia", reg)
        self.assertIn("wiby", reg)

    def test_config_declared(self):
        from config import load_config
        cfg = load_config()
        for name in ("marginalia", "wiby"):
            spec = cfg.get("engines", {}).get(name)
            self.assertIsNotNone(spec, f"{name} 未在 config.yaml 声明")
            self.assertTrue(spec.get("enabled"))
            self.assertEqual(spec.get("cost_tier"), "free")


if __name__ == "__main__":
    unittest.main()
