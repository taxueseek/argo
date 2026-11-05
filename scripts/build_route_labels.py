#!/usr/bin/env python3
"""build_route_labels.py — 从已归因坏例构建路由回归语料。

## 为什么需要它

路由准确性的评测集此前只有三处手工维护的语料（ROUTE_MATRIX 135 +
LEGITIMATE_ROUTES 43 + SUBSTRING_ACCIDENTS 26 = 快照 201 条）加 13 条人工
标注集。13 条无法区分概念路由的净收益（实测 2 好 2 差），201 条快照只判
「domain+kind 不漂移」，不判「意图对不对」。

`badcase.db` 里的坏例是**已归因的失败**，本脚本把它们转成可入库的回归语料。

## 隐私边界（2026-11-05 修正）

**本脚本不读任何本机运行数据。** 初版从 `usage_log/query.jsonl` 取真实查询做
「分布视图」，又从 `badcase.db` 取坏例——两者都是**用户真实使用 argo 的
记录**，含具体企业名、统一社会信用代码、域名、人名（实测 25 条高风险条目），
合起来还能反推用户的行为画像（在调查哪些公司、用什么工具、关注什么领域）。
这些是别人的查询历史，不该进公开仓。

`AGENTS.md` 的规则对同类数据早有约束：

> 本地数据（会话 JSONL、`.session-digger/`）不入库

`usage_log` 与 `badcase.db` 属同类——本机运行数据，不是可分发的评测资产。

**因此本脚本产出的语料只含构造用例**，来源有三：

1. 按域声明触发词生成的用例（`extend_route_labels_by_capability.py`）；
2. 按语言能力生成的用例（`extend_route_labels_by_language.py`）；
3. 外部反馈的撞车场景（`extend_route_labels.py`，来自 issue 与发布说明的
   转述，不含本机数据）。

`badcase.db` 的坏例仍可在**本机**用于回归验证（`badcase_ledger.py` 的
`check` 子命令），但不入公开仓的语料文件。

## 关键定位：回归门，不是准确率门

**弱标注的 `expect_domain` 是历史路由的输出，不是真值。** 2026-11-04 实测：
31 条「路由输出 != 弱标注期望」的失配里，人工复核 20 条，**19 条是弱标注错、
路由对**（例：`Transformer 注意力机制 原理` 弱标注 academic，路由
chinese_tech_deep，而 13 条人工集判路由是对的）。

因此本脚本产出的语料用于**回归门**（判路由决策**不漂移**），**不用于准确率**。
准确率由 `test_route_accuracy_labeled`（13 条人工集）和后续扩充的人工集承担。

## 证据分档

| 档 | 来源 | 判据 | 用途 |
|---|---|---|---|
| A | 按域能力生成的用例 | 域声明的触发词 | 长尾域覆盖 |
| B | 按语言能力生成的用例 | 各语言特征词 | 多语言路由 |
| C | 外部反馈撞车场景 | issue / 发布说明转述 | 意图撞车回归 |

## 输出

`tests/golden/route_labeled.jsonl`，每行：

    {"query": ..., "expect_domain": ..., "expect_family": ...,
     "evidence": "badcase|usage_error|usage_ok",
     "quality": {...}, "source": "badcase.db|usage_log"}

**只入库查询与期望，不含本机路径、状态目录、时间戳**——语料是可公开的
评测资产，本机状态不是。

## 用法

    python3 scripts/build_route_labels.py            # 重建（清空后依次跑四个扩展脚本）
    python3 scripts/build_route_labels.py --dry-run  # 只统计不写
    python3 scripts/build_route_labels.py --check    # 与已提交文件对账

`--check` 走 `build()`（空基底）与已提交文件对账，因此**只在语料为空时可过**；
非空语料的对账走 `--reproduce`——它按同一条链重建到临时文件再与已提交文件
逐条比，是 CI 与人工复核都用的入口（防止「只改了 jsonl 忘了改生成脚本」）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

OUTPUT = ROOT / "tests" / "golden" / "route_labeled.jsonl"


def build() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """构建标注集，返回 (条目列表, 统计)。

    **只产空基底**——语料内容全部由三个扩展脚本生成（见模块 docstring 的
    「隐私边界」）。本函数保留为空实现，让 `build_route_labels.py` 成为
    「重建语料」的入口：先清空，再依次跑三个 extend 脚本。
    """
    out: list[dict[str, Any]] = []
    stats = {
        "total": 0,
        "by_evidence": {},
        "by_source": {},
        "domains": 0,
        "badcases": 0,
    }
    return out, stats


def _write(entries: list[dict[str, Any]]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _check() -> int:
    if not OUTPUT.is_file():
        print(f"[build_route_labels] 标注集不存在：{OUTPUT}", file=sys.stderr)
        return 1
    recorded = {}
    for line in OUTPUT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            recorded[d["query"]] = d
    entries, stats = build()
    current = {e["query"]: e for e in entries}
    problems = []
    for q in sorted(set(current) - set(recorded)):
        problems.append(f"  新增: {q!r}")
    for q in sorted(set(recorded) - set(current)):
        problems.append(f"  消失: {q!r}（本机数据已清理？）")
    for q in sorted(set(current) & set(recorded)):
        if current[q]["expect_domain"] != recorded[q]["expect_domain"]:
            problems.append(
                f"  期望域漂移: {q!r} {recorded[q]['expect_domain']!r}"
                f" -> {current[q]['expect_domain']!r}")
    if problems:
        print("标注集漂移：\n  " + "\n  ".join(problems[:40]), file=sys.stderr)
        return 1
    print(f"[build_route_labels] 一致（{len(recorded)} 条）")
    return 0


# 四道扩展的这一条链就是语料的**唯一构造过程**（顺序即语义：能力 → 语言 →
# 意图标签 → 未覆盖域补齐）。`--reproduce` 按这条链重放，是「jsonl 与生成脚本
# 同源」的验证入口。
_EXTEND_CHAIN = (
    "extend_route_labels_by_capability",
    "extend_route_labels_by_language",
    "extend_route_labels",
    "extend_route_labels_full_coverage",
)


def _reproduce() -> int:
    """按扩展链重建语料，与已提交文件逐条对账（jsonl 与生成脚本同源验证）。

    扩展脚本写的是同一个 `OUTPUT`（模块常量），所以先把已提交内容留在内存里，
    无论成功失败都还原——`--reproduce` 是**只读**的对账入口，不该改工作区文件。
    """
    original = OUTPUT.read_text(encoding="utf-8")
    recorded = {}
    for line in original.splitlines():
        if line.strip():
            d = json.loads(line)
            recorded[d["query"]] = d

    try:
        _write([])  # 从空基底重放整条链
        for name in _EXTEND_CHAIN:
            mod = __import__(name)
            saved = sys.argv
            sys.argv = [name]
            try:
                rc = mod.main()
            finally:
                sys.argv = saved
            if rc:
                print(f"[build_route_labels] 扩展 {name} 失败（rc={rc}）",
                      file=sys.stderr)
                return rc

        rebuilt = {}
        for line in OUTPUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                rebuilt[d["query"]] = d
    finally:
        OUTPUT.write_text(original, encoding="utf-8")

    problems = []
    for q in sorted(set(rebuilt) - set(recorded)):
        problems.append(f"  未提交: {q!r}（生成脚本产出了语料里没有的条目）")
    for q in sorted(set(recorded) - set(rebuilt)):
        problems.append(f"  未复现: {q!r}（语料里有，生成脚本产不出来）")
    for q in sorted(set(rebuilt) & set(recorded)):
        for k in ("expect_domain", "evidence"):
            if rebuilt[q].get(k) != recorded[q].get(k):
                problems.append(
                    f"  {k} 漂移: {q!r} {recorded[q].get(k)!r} -> {rebuilt[q].get(k)!r}")
    if problems:
        print(f"语料与生成脚本不同源（{len(problems)} 处）：\n  "
              + "\n  ".join(problems[:40]), file=sys.stderr)
        return 1
    print(f"[build_route_labels] 复现一致（{len(rebuilt)} 条，链："
          f"{' → '.join(_EXTEND_CHAIN)}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从已归因坏例构建路由标注集")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写")
    ap.add_argument("--check", action="store_true", help="与已提交文件对账")
    ap.add_argument("--reproduce", action="store_true",
                    help="按扩展链重放并逐条对账（jsonl 与生成脚本同源验证）")
    args = ap.parse_args()

    if args.reproduce:
        return _reproduce()

    entries, stats = build()
    print(f"[build_route_labels] badcase {stats['badcases']} 条 → "
          f"标注集 {stats['total']} 条，覆盖 {stats['domains']} 个域")
    print(f"[build_route_labels] 证据分档: {stats['by_evidence']}")
    print(f"[build_route_labels] 来源: {stats['by_source']}")

    if args.check:
        return _check()
    if args.dry_run:
        return 0
    _write(entries)
    print(f"[build_route_labels] 已写入 {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
