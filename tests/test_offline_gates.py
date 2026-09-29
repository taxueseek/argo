#!/usr/bin/env python3
"""离线门禁接线（2026-09-29）：三个重门禁进 pytest。

此前的状态：replay_eval --check / regression_p0p1 --offline / ab_eval_p0p1
都是「手动跑脚本才红」的 exit-code 门禁——pytest 3490 全绿的同时，其中
两个红着（2026-09-28 实测：regression 66 PASS/18 FAIL、ab_eval 47/3、
replay 天气条 kept=1 < 下限 5），CI 形态下等于不存在。本文件把它们接进
测试套件：

  - replay / regression：纯离线（录制数据 / 路由演算），常规收集；
  - ab_eval：端到端打真网络（冷/热缓存、负缓存），ARGO_LIVE=1 才跑
    （与仓内 live 测试同一约定）。

接线的前提是门禁本身是对的——同批已修掉三处门禁缺陷（陈旧预算预期、
weather 金标下限、skip_cache 读写同源的测试假设），见各脚本注释。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

GOLDEN = ROOT / "tests" / "golden" / "pipeline_golden.json"


class TestOfflineGates(unittest.TestCase):
    """纯离线门禁：不联网、确定性，每次全量跑都收集。"""

    def test_replay_eval_limits_hold(self):
        """录制数据重放：kept 条数与输出体积不得低于契约下限。

        锁的是「处理逻辑没漂」——重放对同一份录制数据必须产出同一批结果。
        """
        import json

        import replay_eval

        doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
        report = replay_eval.evaluate_all(GOLDEN)
        bad = replay_eval.check_limits(report, doc)
        self.assertEqual(
            bad, [],
            "录制数据重放低于契约下限：\n  " + "\n  ".join(bad))

    def test_regression_p0p1_offline(self):
        """路由精度 + 预算契约 + research_only 隔离（离线部分全量）。"""
        import regression_p0p1

        c = regression_p0p1.Checker()
        regression_p0p1.run_offline(c)
        failed = [f"{r['name']} — {r['detail']}"
                  for r in c.rows if r.get("status") != "PASS"]
        self.assertEqual(failed, [],
                         f"regression_p0p1 离线门禁 {len(failed)} 项失败：\n  "
                         + "\n  ".join(failed))


@unittest.skipUnless(os.environ.get("ARGO_LIVE"),
                     "ab_eval 端到端打真网络（冷/热缓存、负缓存）："
                     "ARGO_LIVE=1 才跑")
class TestLiveGates(unittest.TestCase):
    """live 门禁：与仓内 ARGO_LIVE 测试同一约定，默认跳过。"""

    def test_ab_eval_p0p1(self):
        import ab_eval_p0p1

        rc = ab_eval_p0p1.main()
        self.assertEqual(rc, 0, "ab_eval_p0p1 有用例失败（见上方输出）")


if __name__ == "__main__":
    unittest.main()
