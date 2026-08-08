#!/usr/bin/env python3
"""tests/test_local_360_jisilu.py — P2 原生集成：360 搜索 / 集思录

吸收 multi-search-engine 技能的两个国内增量源（argo 缺失且可抓）：
  - local_360：so.com 中文全网搜索（html，data-mdurl 取真实目标 URL）
  - local_jisilu：集思录投资社区探索（html，meta 行作 snippet）

覆盖：config/parse_maps 注册、族映射、smart_router 中文路由、
解析正确性、无标题容器过滤（360 AI 卡片 / Jisilu 顶部横幅）。
全程 mock 网络层，离线必过。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
LOCAL_SEARCH_DIR = SKILL_DIR / "sub-skills" / "local-search"
for p in (str(SCRIPT_DIR), str(LOCAL_SEARCH_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from engines_base import _build_html_engine  # noqa: E402
from engine_families import family_of  # noqa: E402
from argo_engine_registry import get_registry  # noqa: E402

# ── 简化模拟 HTML ──────────────────────────────────────────────────────────────

_PAD = "<!-- padding: " + "x" * 600 + " -->"

_SO_HTML = """<html><body>
<li class="res-list"><!-- AI 卡片：无 h3.res-title，应被过滤 -->
  <div class="g-mohe">百科卡片</div>
</li>
<li class="res-list">
  <h3 class="res-title"><a href="https://www.so.com/link?m=track1"
     data-mdurl="https://example.com/a">人工智能<em>高亮</em>标题</a></h3>
  <p class="res-desc">这是摘要<span class="gray">日期</span></p>
</li>
<li class="res-list">
  <h3 class="res-title"><a href="https://www.so.com/link?m=track2">无真实URL标题</a></h3>
  <p class="res-desc">摘要二</p>
</li>
</body></html>""" + _PAD

_JSL_HTML = """<html><body>
<div class="aw-item"><!-- 顶部横幅：在 .aw-question-list 外，应被过滤 -->
  <h4><a href="/ads">会员广告</a></h4>
</div>
<div class="aw-question-list">
  <div class="aw-item">
    <h4><a href="https://www.jisilu.cn/question/524200">不出大家所料</a></h4>
    <span class="aw-text-color-999">套利 • 作者 回复 • 2026-08-08 22:56 • 5982 次浏览</span>
  </div>
  <div class="aw-item">
    <h4><a href="https://www.jisilu.cn/question/516891">第二条</a></h4>
    <span class="aw-text-color-999">其他 • 作者2 回复 • 2026-08-08 21:00 • 100 次浏览</span>
  </div>
</div>
</body></html>""" + _PAD


class _FakeResp:
    def __init__(self, html: str) -> None:
        self._html = html

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._html.encode("utf-8")


def _run_html(engine_name: str, spec: dict, html: str, query: str = "q") -> list[dict]:
    full = dict(spec)
    full["_name"] = engine_name
    engine = _build_html_engine(full)
    with patch("urllib.request.urlopen", return_value=_FakeResp(html)):
        return engine(query, n=5)


_SO_SPEC = {"type": "html", "url": "https://www.so.com/s", "query_param": "q",
            "format": "html", "timeout": 8}
_JSL_SPEC = {"type": "html", "url": "https://www.jisilu.cn/explore/",
             "query_param": "keyword", "format": "html", "timeout": 8}


class TestRegistration(unittest.TestCase):
    """config.yaml + parse_maps.yaml 注册"""

    def test_config_has_both_engines_enabled(self) -> None:
        import yaml
        cfg = yaml.safe_load(open(LOCAL_SEARCH_DIR / "config.yaml", encoding="utf-8"))
        engines = cfg["engines"]
        for name in ("local_360", "local_jisilu"):
            self.assertIn(name, engines, f"config 缺少 {name}")
            self.assertEqual(engines[name]["type"], "html")
            self.assertTrue(engines[name].get("enabled", True), f"{name} 应 enabled")

    def test_parse_maps_has_selectors(self) -> None:
        import yaml
        maps = yaml.safe_load(open(LOCAL_SEARCH_DIR / "parse_maps.yaml", encoding="utf-8"))
        html_maps = maps["html"]
        for name in ("local_360", "local_jisilu"):
            self.assertIn(name, html_maps, f"parse_maps 缺少 {name}")
            self.assertTrue(html_maps[name]["container"])
            self.assertTrue(html_maps[name]["title"])

    def test_registry_merged(self) -> None:
        reg = get_registry()
        names = reg.list_local_engines()
        self.assertIn("local_360", names)
        self.assertIn("local_jisilu", names)

    def test_family_mapping(self) -> None:
        self.assertEqual(family_of("local_360"), "web_general")
        self.assertEqual(family_of("local_jisilu"), "social")


class TestSmartRouter(unittest.TestCase):
    """smart_router 中文路由白名单"""

    def test_chinese_priority_has_360(self) -> None:
        from smart_router import CATEGORY_PRIORITY
        self.assertIn("local_360", CATEGORY_PRIORITY["chinese"])

    def test_chinese_query_routes_to_chinese_domain(self) -> None:
        from smart_router import CATEGORY_PRIORITY, route_query
        r = route_query("北京今天天气怎么样")
        self.assertEqual(r["domain"], "chinese")
        self.assertIn("local_360", r["engines"],
                      "中文查询应路由到 local_360（chinese 白名单第 3 位）")
        for e in r["engines"]:
            self.assertIn(e, CATEGORY_PRIORITY["chinese"], f"{e} 不在 chinese 白名单")


class TestParsing(unittest.TestCase):
    """解析正确性与容器过滤"""

    def test_360_ai_card_filtered(self) -> None:
        results = _run_html("local_360", _SO_SPEC, _SO_HTML)
        self.assertEqual(len(results), 2, "AI 卡片应被标题选择器自然过滤")

    def test_360_extracts_real_url_via_datamdurl(self) -> None:
        results = _run_html("local_360", _SO_SPEC, _SO_HTML)
        first = results[0]
        self.assertEqual(first["url"], "https://example.com/a",
                         "应优先取 data-mdurl 真实目标地址而非 so.com 跳转链接")
        self.assertEqual(first["title"], "人工智能高亮标题")
        self.assertIn("这是摘要", first["snippet"])

    def test_360_missing_datamdurl_no_url_but_kept(self) -> None:
        results = _run_html("local_360", _SO_SPEC, _SO_HTML)
        second = results[1]
        self.assertEqual(second["title"], "无真实URL标题")
        self.assertEqual(second["url"], "", "无 data-mdurl 时 URL 为空但结果保留")

    def test_jisilu_banner_filtered(self) -> None:
        results = _run_html("local_jisilu", _JSL_SPEC, _JSL_HTML)
        self.assertEqual(len(results), 2, "顶部会员横幅应被容器选择器排除")

    def test_jisilu_extracts_fields(self) -> None:
        results = _run_html("local_jisilu", _JSL_SPEC, _JSL_HTML)
        first = results[0]
        self.assertEqual(first["title"], "不出大家所料")
        self.assertEqual(first["url"], "https://www.jisilu.cn/question/524200")
        self.assertIn("2026-08-08 22:56", first["snippet"])


if __name__ == "__main__":
    unittest.main()
