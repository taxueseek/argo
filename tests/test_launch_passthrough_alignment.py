#!/usr/bin/env python3
"""mcp_launch.sh 密钥透传保持一致检查。

背景（issue #12 同类，2026-09-14）：launchctl 回退透传名单是手写硬编码，
与 engine_env.KNOWN_ENV_ALIASES 长期漂移（33 个别名缺 25 个）——两份清单
各说各话。规则：别名表里的全部变量名 + 代理变量必须都在透传名单里。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from engine_env import KNOWN_ENV_ALIASES  # noqa: E402

REQUIRED_EXTRA = {"ARGO_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "NO_PROXY"}


def _launch_names() -> set[str]:
    """从 mcp_launch.sh 提取透传名单。

    2026-10-02 起名单收敛到单一变量 `_ARGO_PASSTHROUGH_KEYS`（launchctl
    print 批量路径与 getenv 回退路径共用同一份，防两处漂移）。
    """
    s = (ROOT / "scripts" / "mcp_launch.sh").read_text(encoding="utf-8")
    m = re.search(r'_ARGO_PASSTHROUGH_KEYS="(.*?)"', s, re.S)
    assert m, "mcp_launch.sh 未找到 _ARGO_PASSTHROUGH_KEYS 透传名单"
    return set(re.findall(r"[A-Z][A-Z0-9_]*", m.group(1)))


class TestLaunchPassthroughAlignment(unittest.TestCase):
    def test_all_alias_names_passthrough(self):
        declared = _launch_names()
        missing = sorted(({n for ch in KNOWN_ENV_ALIASES.values() for n in ch}
                          | REQUIRED_EXTRA) - declared)
        self.assertEqual(missing, [],
                         "mcp_launch.sh 透传名单与别名表漂移，缺失: "
                         + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
