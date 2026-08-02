#!/usr/bin/env python3
"""已废弃：多副本 rsync 同步违反「单一真源」。

请改用：
  python3 scripts/link_source.py --to <消费者入口>
  # 或 installs.local.yaml / ARGO_LINK_TARGETS（见 link_source.py 文档）

注册表派生仍使用：
  python3 scripts/sync_backends.py
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "sync_installs.py 已废弃（禁止硬编码路径 + rsync 多副本）。\n"
        "请使用:\n"
        "  python3 scripts/link_source.py --to <path>\n"
        "  python3 scripts/sync_backends.py   # 仅派生 backends\n",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
