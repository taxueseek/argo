#!/usr/bin/env python3
"""Bangs 路由与新引擎注册回归（2026-09-28，补 9044621/46ecd3e 的测试欠账）。

9044621 落地 Bangs 时把解析写进 route_query 内联、修复提交（46ecd3e）又把
逻辑挪了位置并删了重复注册——两代实现都零测试。这里钉住三层：

1. 解析层：命中 / 大小写 / 裸 Bang / 未知 / 非 Bang 五态；
2. 集成层：route_query 对 !gh 查询走「用户指定引擎」分支；!so 必须落到
   stackoverflow 而不是**同日新增、名字同形**的 360 搜索引擎 so——这是
   最容易被后来者「顺手改对」又改错的映射；
3. 注册冒烟：映射表里每个目标引擎、以及本批 4 个新引擎，都必须真实
   存在于 engines 的构建器注册表（防「Bangs 指向不存在的源」与
   「声明了但没通电」两类静默缺陷）。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_route_bangs.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import route  # noqa: E402
import route_bangs  # noqa: E402


class TestResolveBangs(unittest.TestCase):
    """resolve_bangs 的五态。"""

    def test_hit(self):
        self.assertEqual(route_bangs.resolve_bangs("!gh react"), ("github", "react"))

    def test_case_insensitive(self):
        self.assertEqual(route_bangs.resolve_bangs("!GH react"), ("github", "react"))

    def test_bare_bang(self):
        """!w 无剩余查询：引擎点名成立，空查询交由调用方语义处理。"""
        self.assertEqual(route_bangs.resolve_bangs("!w"), ("wikipedia", ""))

    def test_unknown_bang_passthrough(self):
        self.assertIsNone(route_bangs.resolve_bangs("!zz query"))

    def test_no_bang(self):
        self.assertIsNone(route_bangs.resolve_bangs("普通查询 !gh"))


class TestRouteQueryIntegration(unittest.TestCase):
    """route_query 与 Bangs 的对接。"""

    def test_bang_routes_to_engine(self):
        d = route.route_query("!gh fastapi", engine_override="auto")
        self.assertEqual(d["engine"], "github")
        self.assertEqual(d["engines"], ["github"])
        self.assertEqual(d["engine_request"], "github")

    def test_so_is_stackoverflow_not_360(self):
        """!so → stackoverflow；同日新增的 360 引擎名叫 so，别改串。"""
        d = route.route_query("!so rust lifetimes", engine_override="auto")
        self.assertEqual(d["engine"], "stackoverflow")

    def test_unknown_bang_normal_routing(self):
        d = route.route_query("!zz python gil", engine_override="auto")
        self.assertEqual(d.get("engine_request"), "auto")


class TestRegistered(unittest.TestCase):
    """映射目标与本批新引擎的注册冒烟。

    判据用 engines.is_registered（与执行层同一注册表，见 4e04c88）——
    不要用 engines._BUILDERS：那只是构建器表，config 声明式引擎不在里面，
    而「构建器有了、config 声明缺失」的死引擎恰恰是 9044621 的实锤形态。
    """

    def test_bang_targets_registered(self):
        import engines
        for bang, eng in route_bangs._BANGS_MAP.items():
            self.assertTrue(engines.is_registered(eng),
                            f"{bang} → {eng} 未注册（死映射）")

    def test_batch12_engines_registered(self):
        import engines
        for name in ("so", "shenma", "qwant", "ecosia"):
            self.assertTrue(engines.is_registered(name),
                            f"新引擎 {name} 未注册（构建器与 config 声明必须成对）")


if __name__ == "__main__":
    unittest.main()
