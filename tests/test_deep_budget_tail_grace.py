#!/usr/bin/env python3
"""test_deep_budget_tail_grace.py — deep 档尾宽与 biorxiv 短路契约。

## 为什么需要

2026-11-09 实测发现 deep 档（全量并行）是调度三条路径里**唯一没有尾宽约束**
的：wave-1/wave-2 与串行都有 `straggler_grace_s`，注释里写明它存在的理由正是
「垂直域第 3+ 引擎常为慢源/死源」——而 deep 恰好全量并行，最容易撞上。

实测 `transformer attention mechanism paper` depth=deep：
  · 改前：4 个引擎 0.5s 交付，biorxiv 一个超时，tail_wait_ms=9490，
    整批 10s 打满预算，结果条数与 fast 档相同（5 条）——9.5s 全花在等
    一个零贡献的慢源。
  · 改后（补尾宽 + biorxiv 短路）：2.2s，条数不变。

本文件锁两件事，防复发：
1. deep 档的全量并行分支必须带 tail_grace_s（方案 A）；
2. biorxiv 的关键词查询必须短路返回空，不发全量请求（方案 B）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCH = ROOT / "scripts" / "engine_dispatch.py"
SCIENCE = ROOT / "scripts" / "engines_builders_science.py"


def test_deep_parallel_branch_has_tail_grace():
    """全量并行分支必须传 tail_grace_s / tail_grace_fn。

    判据：`elif parallel and to_run:` 分支内的 `_run_engines_bounded` 调用
    必须带这两个参数。缺一个即红——那正是 2026-11-09 之前的形态。
    """
    text = DISPATCH.read_text(encoding="utf-8")
    # 定位全量并行分支
    m = re.search(r"elif parallel and to_run:(.*?)(?=\n    else:)", text, re.S)
    assert m, "找不到全量并行分支（elif parallel and to_run）"
    branch = m.group(1)
    assert "tail_grace_s=straggler_grace_s" in branch, (
        "deep 档全量并行未传 tail_grace_s：慢源会独占墙钟"
        "（实测 tail_wait 9490ms / 整批 10s）"
    )
    assert "tail_grace_fn=straggler_tail_fn" in branch, (
        "deep 档全量并行未传 tail_grace_fn：按引擎分层的尾宽不生效"
    )


def test_biorxiv_keyword_query_short_circuits():
    """biorxiv 关键词查询必须短路，不发全量请求。

    背景：API 无关键词搜索端点，旧实现拉「最近 3 天全量」（65 KB / 30 条 /
    2.7s）再本地过滤，泛词（mechanism、paper）命中 7 条无关文献全是假阳性。
    深档实测该引擎 timeout=12s 拖累整批 9.5s，最终结果与不调用它完全相同。
    """
    text = SCIENCE.read_text(encoding="utf-8")
    assert "_TIMELY_TRIGGERS" in text, "缺 _TIMELY_TRIGGERS 常量"
    # 短路必须在拉取之前：kws 计算后立即 return []
    m = re.search(
        r"kws = \[w for w in q\.lower\(\)\.split\(\) if w not in _TIMELY_TRIGGERS\]\s*\n"
        r"\s*if kws:\s*\n"
        r"\s*return \[\]",
        text,
    )
    assert m, (
        "biorxiv 的关键词短路不在拉取之前（或写法已漂移）："
        "关键词查询必须直接返回空，不发全量请求"
    )
    # 短路之后才允许出现「最近 3 天」的拉取 URL
    short_idx = m.start()
    url_idx = text.find("details/biorxiv/", short_idx)
    assert url_idx > short_idx, "全量拉取 URL 出现在短路之前"


def test_biorxiv_timely_mode_still_works():
    """时效模式（最新/recent）必须仍然可用——短路不能误杀它。"""
    text = SCIENCE.read_text(encoding="utf-8")
    assert "最新" in text and "recent" in text, (
        "_TIMELY_TRIGGERS 必须含「最新」与「recent」，否则时效模式被误杀"
    )


if __name__ == "__main__":
    sys.exit(__import__("subprocess").run(
        [sys.executable, "-m", "pytest", __file__, "-q"]).returncode)
