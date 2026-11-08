#!/usr/bin/env python3
"""mcp_launch.sh 密钥透传名单检查。

背景：透传名单原先手写在 mcp_launch.sh 里，只和 engine_env.KNOWN_ENV_ALIASES
对账（issue #12 同类，2026-09-14 起），走 required_env 或占位符声明的引擎
（tinyfish、deepseek_search、saucenao 等 12 个）从未进名单，pixiv 入别名表
后也要单独补一笔提交（5758cfa）。2026-11-08 起名单由 sync_backends.py 从
引擎声明派生到 backends/env_passthrough.txt，启动器只读该文件。

这里锁三件事：
1. 启动器不再手写名单，而是读派生文件；
2. 派生文件覆盖每个引擎声明里能读到的密钥名（含 ARGO_ 与裸名两套）；
3. 启动器真跑时能从派生文件拿到名单（POSIX sh 下解析不出错）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import engine_env as E  # noqa: E402
import sync_backends as sb  # noqa: E402

LAUNCH = ROOT / "scripts" / "mcp_launch.sh"


def test_launcher_reads_derived_file_not_inline_list():
    s = LAUNCH.read_text(encoding="utf-8")
    assert "backends/env_passthrough.txt" in s, "mcp_launch.sh 没有读派生名单"
    assert not re.search(r'_ARGO_PASSTHROUGH_KEYS="[A-Z]', s), \
        "mcp_launch.sh 又出现手写名单，改引擎声明后跑 sync_backends.py 即可"


def test_derived_file_covers_every_declared_key():
    pt = set(sb.read_passthrough())
    missing: dict[str, list[str]] = {}
    for name, spec in sb.load_engines().items():
        for req in E.required_env_for(name, spec) or []:
            chain = set(E.PLACEHOLDER_ALIASES.get(req) or E._name_variants(req))
            if not chain & pt:
                missing.setdefault(req, []).append(name)
    for aliases in E.KNOWN_ENV_ALIASES.values():
        for a in aliases:
            if a not in pt:
                missing.setdefault(a, []).append("KNOWN_ENV_ALIASES")
    for extra in sb.EXTRA_PASSTHROUGH:
        if extra not in pt:
            missing.setdefault(extra, []).append("EXTRA_PASSTHROUGH")
    assert not missing, ("env_passthrough.txt 缺这些密钥名（跑 python3 "
                         f"scripts/sync_backends.py 重新派生）: {missing}")


def test_launcher_parses_file_under_posix_sh():
    # 只执行名单读取那一段，避免真的拉起 MCP server
    s = LAUNCH.read_text(encoding="utf-8")
    m = re.search(r'(_ARGO_PT_FILE=.*?\nfi\n)', s, re.S)
    assert m, "mcp_launch.sh 找不到名单读取段"
    snippet = m.group(1) + 'printf "%s" "$_ARGO_PASSTHROUGH_KEYS"\n'
    out = subprocess.run(["sh", "-c", snippet, str(LAUNCH)],
                         capture_output=True, text=True, check=True).stdout
    got = set(out.split())
    assert got == set(sb.read_passthrough()), "sh 读出的名单与派生文件不一致"
