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


def test_dsh_runner_keyrefs_block_matches_declarations():
    """DSH 插件 runner.js 的 keyRefs 默认名单也由派生块供给（issue #41 收口）。

    DEFAULTS.keyRefs 此前硬编码 ['DEEPSEEK_API_KEY']，宿主凭证库里其余引擎
    的 key 不进面板手动加就永远到不了 argo 子进程。派生块让「新装即全量
    转发」，与 env_passthrough.txt、client.js 的 KEYS 块同一真源
    （derive_dsh_keys）。
    """
    runner = ROOT / "packages" / "dsh-plugin" / "dsh" / "runner.js"
    assert runner.exists(), "dsh runner.js 不存在"
    want = sb.render_keyrefs(sb.derive_dsh_keys(sb.load_engines()))
    got = sb.read_keyrefs_block()
    assert got == want, ("runner.js keyRefs 标记区与引擎声明不一致（跑 python3 "
                         "scripts/sync_backends.py 重新派生）")
    refs = re.findall(r"'([A-Z0-9_]+)'", got)
    assert "DEEPSEEK_API_KEY" in refs, "派生名单丢了历史默认项"
    assert len(refs) > 1, "keyRefs 默认名单退回硬编码单条（issue #41 复发）"
