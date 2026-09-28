#!/usr/bin/env python3
"""circuit_breaker.allow() 的跨进程陈旧快照回归（2026-09-28）。

017b01a 修了「构造期读、锁外闭包写」的丢更新，但只修到 _mutate_failure；
allow() 里三个状态转换（half_open_reenable / auto_disable / half_open_probe）
沿用同一错误形态：闭包捕获 allow() 入口读到的 st，_mutate_locked 在文件锁内
_reload_engines() 整体替换 self._engines 之后，mutator 又把旧 st 写回——
另一进程刚落盘的 failures/opens/last_attribution 被静默回滚。

复现（单进程内模拟两进程时序）：
  A 构造 breaker（磁盘 v1）→「B」改写磁盘（v2：更多记忆）→
  A 走 allow() 的 half-open 转换 → 落盘必须 = v2 的记忆 + A 的 state 变更，
  而不是 v1 整体覆盖。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_circuit_breaker_stale.py -q
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import circuit_breaker as cb  # noqa: E402

ENG = "testeng"


def _seed(path: Path, opens: int, failures: int, attribution: str) -> None:
    path.write_text(json.dumps({
        "engines": {ENG: {
            "state": "open",
            # 远超任何 OPEN_SECONDS：冷却必已过，allow() 走转换分支
            "opened_at": time.time() - 10**6,
            "opens": opens, "failures": failures,
            "last_attribution": attribution, "disabled_at": 0,
        }},
        "updated": time.time(),
    }), encoding="utf-8")


class TestAllowUsesFreshState(unittest.TestCase):
    def test_half_open_probe_preserves_fresh_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cb.json"
            _seed(p, opens=1, failures=42, attribution="v1")
            b = cb.CircuitBreaker(state_path=str(p))
            # 「进程 B」在 A 构造之后落盘的新状态
            _seed(p, opens=99, failures=777, attribution="v2")
            ok, reason = b.allow(ENG)
            self.assertTrue(ok)
            self.assertEqual(reason, "half_open_probe")
            on_disk = json.loads(p.read_text())["engines"][ENG]
            self.assertEqual(on_disk["state"], "half_open")  # A 的转换生效
            self.assertEqual(on_disk["failures"], 777)       # B 的记忆未被回滚
            self.assertEqual(on_disk["last_attribution"], "v2")
            self.assertEqual(on_disk["opens"], 99)


if __name__ == "__main__":
    unittest.main()
