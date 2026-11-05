#!/usr/bin/env python3
"""test_route_capability_coverage — 域能力覆盖门（防长尾域零用例）。

## 这道门在治什么

`test_route_matrix_coverage` 守的是「每个域至少有一条路由用例」，但它的语料
来源是 `ROUTE_MATRIX` + `LEGITIMATE_ROUTES`（手工维护）。本门守的是
`tests/golden/route_labeled.jsonl` 的域覆盖：**config 里声明的每个域，都要有
一条 `evidence=capability` 的用例**，且这条用例必须让浏览者看到该域的真实问法。

## 2026-11-06：从 58/99 补到 99/99，覆盖门从 0.55 提到 1.0

上一版（2026-11-05）语料改为全构造用例后，覆盖掉到 58/99，门下限被迫降到
0.55，41 个域登记在 `_KNOWN_GAPS` 里「由快照门/人工集覆盖」。那份登记里
「平台专属语法」「语义过专」的判断经复核不成立：

- `wechat_search` / `zhihu_content` / `skill_search` / `dsh_plugin_search` 的
  触发词是普通中文说法（「公众号 文章」「知乎 如何评价」「agent skills 技能
  市场」），不是平台操作符；
- `modal_card` 的「今日油价 / 火车票 余次 / 手机参数 对比」、`medical` 的
  「临床试验 适应症 不良反应」都是日常问法。

真正卡住的是**判据不一致**：旧能力扩展脚本判「目标域在 `match_domains`
前 5 名」，而回归门判「`route_query(q).domain` 精确相等」——同族域互相抢主位
时前者过、后者红。`extend_route_labels_full_coverage.py` 改用精确命中判据
重建，31 个域一次补齐（31 条候选全命中，无失配），覆盖回到 99/99。

因此 `_KNOWN_GAPS` 与 0.55 的下限一并删除：**缺口登记是过渡态，不是常态**
——登记本身会让人忘记它只是「暂时没有用例」，而不是「这个域不该有用例」。

## 判据

- 覆盖率必须 **1.0**（99/99）：任何域没有用例即红，逼开发者补用例；
- 能力用例条数下限 60（当前 89），防「删语料把门刷绿」。

## 运行

    python3 -m pytest tests/test_route_capability_coverage.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

LABELS = ROOT / "tests" / "golden" / "route_labeled.jsonl"

# 覆盖下限：1.0。语料是全构造用例，每个域都能构造出自然问法（见模块 docstring），
# 「构造不出来」不再是可接受的缺口——真构造不出来，说明域规则本身有问题。
COVERAGE_FLOOR = 1.0

# 能力用例条数下限。当前 89 条；定 60 是「明显删过语料」的下限，不是目标值。
MIN_CAPABILITY_CASES = 60


def _load_labels() -> list[dict]:
    if not LABELS.is_file():
        return []
    return [json.loads(l) for l in
            LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestRouteCapabilityCoverage(unittest.TestCase):
    def test_labels_cover_every_domain(self):
        from config import get_domains

        labels = _load_labels()
        all_doms = {d["name"] for d in get_domains()}
        covered = {e["expect_domain"] for e in labels
                   if e.get("expect_domain") in all_doms}
        ratio = len(covered) / len(all_doms) if all_doms else 0.0
        uncovered = sorted(all_doms - covered)
        self.assertGreaterEqual(
            ratio, COVERAGE_FLOOR,
            f"域覆盖 {len(covered)}/{len(all_doms)} = {ratio:.1%} < "
            f"{COVERAGE_FLOOR:.0%}，未覆盖：{uncovered}。跑 "
            f"python3 scripts/extend_route_labels_full_coverage.py 补用例")

    def test_capability_cases_exist(self):
        """必须有能力用例——只有真实查询的语料覆盖不出长尾域。"""
        labels = _load_labels()
        cap = [e for e in labels if e.get("evidence") == "capability"]
        self.assertGreaterEqual(
            len(cap), MIN_CAPABILITY_CASES,
            f"能力用例只有 {len(cap)} 条（下限 {MIN_CAPABILITY_CASES}），"
            f"长尾域覆盖不足")

    def test_every_domain_has_a_case(self):
        """逐域点名：每个域至少有一条用例（覆盖面之外还要见得着）。

        不要求证据档必须是 `capability`——10 个域（academic/chinese_general/
        redskill_search/stock_query 等）的用例来自意图撞车（feedback）与多语言
        （language）扩展，那同样是有效的域级回归锚。缺的是「一条都没有」。
        """
        from config import get_domains

        labels = _load_labels()
        all_doms = {d["name"] for d in get_domains()}
        case_doms = {e["expect_domain"] for e in labels
                     if e.get("expect_domain") in all_doms}
        self.assertEqual(
            sorted(all_doms - case_doms), [],
            f"这些域一条用例都没有：{sorted(all_doms - case_doms)}")

    def test_corpus_reproducible_from_build_chain(self):
        """语料必须能由生成脚本链复现——防「只改 jsonl 忘了改生成脚本」。

        `extend_route_labels_full_coverage.py` 的用例表是语料的真源，
        jsonl 只是它的投影。有人绕过脚本直接编辑 jsonl 时，本门红。
        """
        import subprocess
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_route_labels.py"), "--reproduce"],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0,
                         f"语料与生成脚本不同源：\n{r.stdout}{r.stderr}")


if __name__ == "__main__":
    unittest.main()
