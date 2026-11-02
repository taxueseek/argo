#!/usr/bin/env python3
"""test_route_executability — primary 可执行性与域否决（2026-11-02）。

守两件事：

  1. **路由准入与执行准入同源**：`is_engine_routable` 此前不查外部依赖，
     执行层的 `engine_status` 另算 dep_ready。后果是 primary 可以是一个
     「路由认为可用、执行必然失败」的引擎，结果由 fallback 悄悄接管，
     reason 却写着「→ 该引擎」。
  2. **域规则否决不得被 TF-IDF 复活**：域用 unless 否决过某意图后，
     词面高分的 TF-IDF 候选不得再把它注入首位。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class TestDepReadinessInRoutability(unittest.TestCase):
    def test_engine_with_missing_cli_is_not_routable(self):
        from config import get_engines
        from route_combo import is_engine_routable, _dep_ready

        specs = get_engines()
        # xiaohongshu 声明 requires: [{bin: xhs}]；本机未装 xhs。
        # 用「声明了 requires 且本机确实缺」的引擎做判据，避免依赖某台机器的
        # 具体安装状态：找不到这样的引擎时本用例自然跳过。
        target = None
        for name, spec in specs.items():
            if spec.get("requires") and not _dep_ready(name, specs):
                target = name
                break
        if target is None:
            self.skipTest("本机所有声明 requires 的引擎依赖都就绪")
        self.assertFalse(is_engine_routable(target, specs=specs,
                                            check_breaker=False,
                                            check_hard_down=False))

    def test_dep_check_can_be_disabled(self):
        from config import get_engines
        from route_combo import _dep_ready, is_engine_routable

        specs = get_engines()
        target = next((n for n, s in specs.items()
                       if s.get("requires") and not _dep_ready(n, specs)), None)
        if target is None:
            self.skipTest("本机没有缺依赖的引擎")
        self.assertTrue(is_engine_routable(target, specs=specs, check_deps=False,
                                           check_breaker=False, check_hard_down=False))


class TestPrimaryPromotion(unittest.TestCase):
    def test_unexecutable_primary_is_demoted(self):
        from route_primary import promote_executable_primary

        # 造一个「缺依赖」的 spec：requires 指向必然不存在的 bin。
        specs = {"broken_engine": {"requires": [{"bin": "argo-no-such-bin-xyz"}]},
                 "ok_engine": {}}
        combo, demoted = promote_executable_primary(
            ["broken_engine", "ok_engine"], specs=specs,
            enabled={"broken_engine", "ok_engine"})
        self.assertEqual(demoted, "broken_engine")
        self.assertEqual(combo[0], "ok_engine")
        self.assertEqual(sorted(combo), ["broken_engine", "ok_engine"],
                         "顺位不得丢成员")

    def test_all_unexecutable_keeps_order_fail_open(self):
        from route_primary import promote_executable_primary

        specs = {"a": {"requires": [{"bin": "argo-no-such-bin-a"}]},
                 "b": {"requires": [{"bin": "argo-no-such-bin-b"}]}}
        combo, demoted = promote_executable_primary(
            ["a", "b"], specs=specs, enabled={"a", "b"})
        self.assertIsNone(demoted)
        self.assertEqual(combo, ["a", "b"],
                         "全组不可执行时保持原序，交由执行层报错")

    def test_executable_primary_untouched(self):
        from route_primary import promote_executable_primary

        combo, demoted = promote_executable_primary(
            ["arxiv", "bocha"], specs={}, enabled={"arxiv", "bocha"})
        self.assertIsNone(demoted)
        self.assertEqual(combo, ["arxiv", "bocha"])


class TestDomainVeto(unittest.TestCase):
    def test_vetoed_engines_reported(self):
        from route_primary import domain_vetoed_engines

        vetoed = domain_vetoed_engines("怎么写论文 查重 降重")
        self.assertIn("cnii", vetoed,
                      "academic 域被 unless 否决时，其声明引擎应进否决集")

    def test_no_veto_when_domain_hits(self):
        from route_primary import vetoes_apply

        self.assertFalse(vetoes_apply("怎么写论文 查重 降重", hard_domain=True),
                         "已有实域命中时不得启用否决（该域自行选源）")

    def test_select_tfidf_best_respects_veto(self):
        from route_combo import select_tfidf_best

        def fake_route(query, top_k=3):
            return [("cnii", 0.9, "sim=0.900"), ("bocha", 0.2, "sim=0.200")]

        best, score, _ = select_tfidf_best(
            "怎么写论文 查重 降重", {"primary_lang": "zh"}, {}, fake_route,
            enabled={"cnii", "bocha"}, vetoed={"cnii"})
        self.assertEqual(best, "bocha",
                         "被域规则否决的候选不得因词面高分当选")


if __name__ == "__main__":
    unittest.main()
