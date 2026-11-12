#!/usr/bin/env python3
"""test_silent_catch_gate — 宽口径吞异常棘轮门禁（只准减不准增）。

共同根因范式：本仓反复出现的低级 bug（env 绑定 9 处、裸 urlopen、SKILL.md
数字漂移）根因都是「同一语义两处真相、无机械对拍」，修一次配一道门。
静默吞异常是同类：mcp_handlers._ok 的脱敏吞异常是全仓唯一「吞出可利用后果」
的实例（2026-11-12 评审，已改 fail-closed），其余存量裸吞无安全后果但会让
故障静默。

为什么是棘轮不是零祖父：test_http_attribution 管裸 urlopen、
test_env_binding_gate 管导入期读 env 都是先清零再上锁；吞异常存量每处都要
判断正确的异常类型（非机械改动），一次性窄化违反「改动尽量小」。所以走
GRANDFATHERED 棘轮：只准减不准增，窄化一处同步下调 BASELINE。

口径（机械，不含人工判断）：`except Exception`（恰好 Name，非元组/属性）
且 handler 体只有一条 Pass/Continue。窄口径（OSError/ImportError/…）不计
——可选依赖降级、解析守卫属仓库约定（OPT_IMPORT/SHAPE_BENIGN/IO_BENIGN
常量拼装）。注释不豁免：口径看 AST，想豁免请换窄口径或让失败可见。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 棘轮基线（2026-11-12 全量 AST 实测 187）。真源在 scripts/badcase_ledger.py
# （BROAD_SILENT_CATCH_BASELINE，overview 输出同一常量）——同一数字只写
# 一处，窄化一处就下调一格。
from badcase_ledger import BROAD_SILENT_CATCH_BASELINE as BASELINE  # noqa: E402


def _count_broad_silent_catches() -> int:
    n = 0
    for p in sorted(SCRIPTS.glob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.ExceptHandler)
                    and isinstance(node.type, ast.Name)
                    and node.type.id == "Exception"
                    and len(node.body) == 1
                    and isinstance(node.body[0], (ast.Pass, ast.Continue))):
                n += 1
    return n


def test_broad_silent_catches_do_not_grow():
    n = _count_broad_silent_catches()
    assert n <= BASELINE, (
        f"宽口径静默吞异常 {n} 处 > 棘轮基线 {BASELINE}：新增了"
        f"「except Exception + 裸 pass/continue」。修法：窄化异常类型，"
        f"或让失败可见（raise/log）。窄化存量时同步下调本文件 BASELINE。")


def test_gate_has_teeth():
    """判据自检：能识别目标形态、不误伤窄口径，否则恒真等于没检查。"""
    broad = ast.parse(
        "try:\n    pass\nexcept Exception:\n    pass\n")
    hits = [n for n in ast.walk(broad)
            if (isinstance(n, ast.ExceptHandler)
                and isinstance(n.type, ast.Name) and n.type.id == "Exception"
                and len(n.body) == 1 and isinstance(n.body[0], ast.Pass))]
    assert len(hits) == 1, "判据识别不到目标形态"

    narrow = ast.parse("try:\n    pass\nexcept OSError:\n    pass\n")
    hits2 = [n for n in ast.walk(narrow)
             if (isinstance(n, ast.ExceptHandler)
                 and isinstance(n.type, ast.Name) and n.type.id == "Exception")]
    assert not hits2, "窄口径被误计"
