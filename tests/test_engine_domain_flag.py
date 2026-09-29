#!/usr/bin/env python3
"""test_engine_domain_flag — `--domain` / `--sub_domain` 真的到达引擎（2026-09-27）。

守的是一条「接了、写了、传了、没人用」的断链：两个开关被 argparse 接下、
写进 SKILL.md 的参数表和 usage.md 的用法示例，**却没有任何一层读取它们**。
`super_search` 连参数都没有，请求照发、结果照回，只是 `domain` 从未到达
引擎——「限定金融域」静默退化成通用搜索，且退出码 0、无告警。

同类断链在本仓反复出现过（SKILL.md 第 104 行的「不带 --engine 是瘦身全量
清单」也是这么写出来的），所以守的点不止是「参数存在」，而是
**端到端到达 + 不给时不下发空键 + 缓存键隔离**三条。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import search as S  # noqa: E402


def _capture():
    """把 engine_search 换成记录器，返回 (调用记录, 假引擎)。"""
    calls: list[dict] = []

    def fake(query, engine, **kwargs):
        calls.append({"engine": engine, **kwargs})
        return [{"title": "T", "url": "https://example.com/a", "source": engine}]

    return calls, fake


class TestEngineDomainFlag(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = S.engine_search

    def tearDown(self) -> None:
        S.engine_search = self._orig

    def test_domain_and_sub_domain_reach_engine(self) -> None:
        calls, fake = _capture()
        S.engine_search = fake
        S.super_search("AAPL", engine="anysearch", n=3, skip_cache=True,
                       engine_domain="finance",
                       engine_sub_domain="finance.us_stock")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("domain"), "finance")
        self.assertEqual(calls[0].get("sub_domain"), "finance.us_stock")

    def test_no_empty_keys_when_not_requested(self) -> None:
        """不给就不下发：空 `domain=""` 会让部分引擎走「显式空域」分支。"""
        calls, fake = _capture()
        S.engine_search = fake
        S.super_search("AAPL", engine="anysearch", n=3, skip_cache=True)
        self.assertNotIn("domain", calls[0])
        self.assertNotIn("sub_domain", calls[0])

    def test_cache_key_isolates_domain(self) -> None:
        """同 query 不同 domain 不共享缓存：否则会把不限域结果当限定域答案发回。"""
        import tempfile
        import os

        os.environ["ARGO_STATE_DIR"] = tempfile.mkdtemp()
        calls, fake = _capture()
        S.engine_search = fake
        # 第 1 次同 domain：记录请求数。注意：Step 1/2 的 multi-query 变体召回会在
        # 召回不足时追加 engine_search 调用，故首次请求数未必为 1——这里只把它当基线。
        S.super_search("iso-probe-query", engine="anysearch", n=3,
                       engine_domain="finance")
        after_first = len(calls)
        # 同 domain 重复 → 命中 combo 缓存，整个 execute_search 提前返回，
        # 连变体召回都不触发，calls 不应增长。
        S.super_search("iso-probe-query", engine="anysearch", n=3,
                       engine_domain="finance")
        self.assertEqual(
            len(calls), after_first,
            "同 domain 重复查询应命中缓存，不再发请求")

        # 换 domain → 必须重新发请求
        S.super_search("iso-probe-query", engine="anysearch", n=3,
                       engine_domain="tech")
        self.assertGreater(len(calls), after_first,
                           "不同 --domain 不该复用同一份缓存")

    def test_cli_parses_and_forwards(self) -> None:
        """CLI 入口确实把两个开关接到 super_search（防止再断在 argparse 之后）。"""
        import inspect

        src = inspect.getsource(S.super_search)
        self.assertIn("engine_domain=engine_domain", src,
                      "super_search 未把 engine_domain 转发给 execute_search")
        self.assertIn("engine_sub_domain=engine_sub_domain", src)

        import search_cli
        cli_src = inspect.getsource(search_cli)
        self.assertIn("engine_domain=args.domain", cli_src,
                      "search_cli 未把 --domain 转发给 super_search")
        self.assertIn("engine_sub_domain=args.sub_domain", cli_src)


if __name__ == "__main__":
    unittest.main()
