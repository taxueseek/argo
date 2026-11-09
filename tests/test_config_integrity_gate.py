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

## 2026-11-09 迁移后的口径

engines 段（165 个引擎 + 170 行决策注释）已迁移到 engines/specs/*.yaml
（每引擎一文件，唯一声明源）。config.yaml 的 engines: 段保留空键仅为兼容
读顶层键的调用方。注释档案现在分布在两处：

  - config.yaml：cache / domains / execution / output / semantic_evidence /
    network 各段的注释
  - engines/specs/*.yaml：每个引擎的决策注释（实测数据 / 停用原因 / 重开
    条件 / 档位取值由来）

本文件校验两处注释总数之和，下限与迁移前一致（373）。

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
SPECS_DIR = ROOT / "engines" / "specs"

# 注释行数下限（ratchet：只升不降，降必须在本文件里显式改并说明理由）
#
# 2026-11-09：engines 段迁移后，config.yaml 与 engines/specs/ 的注释总数
# 与迁移前一致（引擎决策注释随文件一起搬走，没有丢失）。
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


def _total_comment_lines() -> int:
    """config.yaml + engines/specs/*.yaml 的注释总数。"""
    n = _comment_lines(CONFIG.read_text(encoding="utf-8"))
    if SPECS_DIR.is_dir():
        for p in sorted(SPECS_DIR.glob("*.yaml")):
            n += _comment_lines(p.read_text(encoding="utf-8"))
    return n


def test_config_keeps_its_comment_archive():
    """config.yaml + engines/specs/ 的注释行数不得低于已登记的下限。

    注释是决策档案（实测数据 / 停用原因 / 重开条件 / 取值由来）。整文件
    重序列化（`yaml.dump` 等）会把它们全部抹掉且不可逆。
    """
    n = _total_comment_lines()
    assert n >= COMMENT_FLOOR, (
        f"注释行数 {n} < 登记下限 {COMMENT_FLOOR}。\n"
        f"注释是决策档案（实测数据 / 停用原因 / 重开条件 / 行为依据），"
        f"整体重写 YAML 会把它们抹掉且不可逆（PyYAML 的 dump 不保留注释）。\n"
        f"正解：用**精确文本编辑**改动需要的行，不要 dump 整个配置对象。\n"
        f"确需删除过时注释：在同一提交里把本文件的 COMMENT_FLOOR 下调到 {n}，"
        f"并在注释里写清删了什么、为什么。"
    )


def test_config_comment_floor_is_not_stale():
    """下限不得高于实际注释数——否则门禁形同虚设（同 module_size_gate 的「上限不得虚高」）。"""
    n = _total_comment_lines()
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
    # engines 段保留空键（2026-11-09 迁移后引擎声明在 engines/specs/）；
    # 校验 specs/ 目录存在且非空，确保引擎声明没有丢失
    assert "engines" in cfg, "engines 顶层段缺失"
    assert cfg.get("domains"), "domains 段为空或缺失"
    assert SPECS_DIR.is_dir(), f"engines/specs/ 目录不存在"
    specs = list(SPECS_DIR.glob("*.yaml"))
    assert len(specs) >= 290, (
        f"engines/specs/ 只有 {len(specs)} 个 spec 文件，"
        f"预期 >= 290（迁移后 290 个引擎）"
    )


def test_engine_entries_keep_anchor_comments():
    """抽查若干「有档案价值」的键仍在：停用原因与网络出口调度说明。

    这两处是本仓库里最容易被整体重写顺手删掉的信息：
      - disabled_reason：为什么关着 + 什么条件下重开（现在分散在 specs/ 里）；
      - network.proxy 段的注释：出口调度的优先级与三档典型环境。
    """
    # disabled_reason 现在分散在 specs/ 里，抽查总数
    disabled_reasons = 0
    for p in sorted(SPECS_DIR.glob("*.yaml")):
        if "disabled_reason" in p.read_text(encoding="utf-8"):
            disabled_reasons += 1
    assert disabled_reasons > 0, (
        "engines/specs/ 里找不到 disabled_reason——停用原因档案被抹掉了"
        "（该字段另有门禁 test_engine_catalog::test_disabled_engines_declare_reason）")

    text = CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^network:\s*$", text, re.M), "network 段不见了"
    assert "解析优先级" in text or "rules" in text, (
        "network.proxy 的出口调度说明（优先级/典型环境）不见了")
