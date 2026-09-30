#!/usr/bin/env python3
"""config.yaml 完整性门禁——防「整文件重写」抹掉决策档案。

## 为什么要这道门（真实事故，2026-09-30）

一个 agent 为了给 6 个引擎补 `qps`、给 2 个补 `timeout`，用
`yaml.dump(cfg, ...)` 把 config.yaml **整体重序列化**——diff 从「改 9 行」
变成「改 747 行」，并造成两处不可逆损失：

  1. **373 行注释归零**（PyYAML 的 dump 不保留注释）。这些注释不是装饰，
     是决策档案：每条引擎的实测数据、停用原因、重开条件、档位取值的由来。
     例如 `disabled_reason` 记录着「为什么关着、什么条件下能重开」——
     仓库 2026-09-29 刚为它加了专门门禁（见 test_engine_catalog
     ::test_disabled_engines_declare_reason），隔天就被整体重写抹掉一次。
  2. **改动不可评审**：747 行 diff 里看不出哪 9 行是真实意图。

## 判据为什么是「注释行数」

唯一能稳定区分「精确编辑」与「整文件重写」的信号。实测：

    原文件           行=4975  注释=373  空行=21
    yaml.dump 重写   行=4627  注释=  0  空行= 0

行数会随正常增删浮动（且 dump 后仍有 4627 行，用行数判会漏），注释数则是
**断崖式归零**——任何保留注释的编辑方式（手改、精确文本替换、编辑器的
YAML-aware 修改）都不会让它掉。

## 上限怎么维护（与 test_module_size_gate 同形的 ratchet）

- 正常新增带注释的配置 → 注释数上升，本门不拦（不需要改数字）；
- 确实要删注释（过时/错误的信息）→ **在同一提交里把下限一起下调**，
  并在 reason 里写清删了什么、为什么。一次可见、可评审的动作，
  而不是无声的漂移，也不是一次 747 行的重写。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

# 注释行数下限（ratchet：只升不降，降必须在本文件里显式改并说明理由）
#
# 排序即待办线索：这些是**当前**的档案规模，不是目标值。
COMMENT_FLOOR = 373

# 必须存在的顶层段。整文件重写若丢段（例如 network 被合并/删掉），这里红。
REQUIRED_TOP_LEVEL = (
    "cache",
    "domains",
    "engines",
    "execution",
    "network",
    "output",
    "semantic_evidence",
)


def _comment_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("#"))


def test_config_keeps_its_comment_archive():
    """config.yaml 的注释行数不得低于已登记的下限。

    注释是决策档案（实测数据 / 停用原因 / 重开条件 / 取值由来）。整文件
    重序列化（`yaml.dump` 等）会把它们全部抹掉且不可逆。
    """
    text = CONFIG.read_text(encoding="utf-8")
    n = _comment_lines(text)
    assert n >= COMMENT_FLOOR, (
        f"config.yaml 注释行数 {n} < 登记下限 {COMMENT_FLOOR}。\n"
        f"注释是决策档案（实测数据 / 停用原因 / 重开条件 / 行为依据），"
        f"整体重写 YAML 会把它们抹掉且不可逆（PyYAML 的 dump 不保留注释）。\n"
        f"正解：用**精确文本编辑**改动需要的行，不要 dump 整个配置对象。\n"
        f"确需删除过时注释：在同一提交里把本文件的 COMMENT_FLOOR 下调到 {n}，"
        f"并在注释里写清删了什么、为什么。"
    )


def test_config_comment_floor_is_not_stale():
    """下限不得高于实际注释数——否则门禁形同虚设（同 module_size_gate 的「上限不得虚高」）。"""
    text = CONFIG.read_text(encoding="utf-8")
    n = _comment_lines(text)
    assert COMMENT_FLOOR <= n, (
        f"登记下限 {COMMENT_FLOOR} > 实际注释数 {n}：下限虚高，门禁失效。"
        f"请把 COMMENT_FLOOR 下调到 {n}。"
    )


def test_config_top_level_sections_survive():
    """顶层段必须齐全：整文件重写若丢段，这里红（比注释数更硬的契约）。"""
    import yaml

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict), "config.yaml 解析结果不是映射"
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in cfg]
    assert not missing, (
        f"config.yaml 缺少顶层段：{missing}。"
        f"整文件重写容易把段合并/丢掉——请用精确编辑。"
    )
    assert cfg.get("engines"), "engines 段为空或缺失"
    assert cfg.get("domains"), "domains 段为空或缺失"


def test_engine_entries_keep_anchor_comments():
    """抽查若干「有档案价值」的键仍在：停用原因与网络出口调度说明。

    这两处是本仓库里最容易被整体重写顺手删掉的信息：
      - disabled_reason：为什么关着 + 什么条件下重开；
      - network.proxy 段的注释：出口调度的优先级与三档典型环境。
    """
    text = CONFIG.read_text(encoding="utf-8")
    assert "disabled_reason" in text, (
        "config.yaml 里找不到 disabled_reason——停用原因档案被抹掉了"
        "（该字段另有门禁 test_engine_catalog::test_disabled_engines_declare_reason）")
    assert re.search(r"^network:\s*$", text, re.M), "network 段不见了"
    assert "解析优先级" in text or "rules" in text, (
        "network.proxy 的出口调度说明（优先级/典型环境）不见了")
