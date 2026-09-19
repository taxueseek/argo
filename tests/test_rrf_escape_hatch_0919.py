#!/usr/bin/env python3
"""test_rrf_escape_hatch_0919.py — RRF 加权版的逃生开关回归锁。

## 守的是什么

`rrf_merge` 的签名此前写死 `weighted: bool = True`，调用方想走经典 RRF
只能显式传 False，而 `ARGO_RRF_WEIGHTED` 这类**环境开关无处生效**——CLI
与 MCP 两种宿主都改不了融合层的行为，只能改代码。这在一处具体场景上是
硬伤：结果排序反常时（如权威源条目压过跨引擎共识条目），无法把融合层
单独摘出去定位，只能连引擎一起怀疑。

加权版与经典版的差异是**可辩驳的排序哲学**，不是对错：
  · 加权版（WG-RRF）：按引擎来源加权，权威源提权、社交源降权
  · 经典版（原论文）：各引擎同位次等权，共识本身即信号

实测同一组三引擎结果，两者给出不同次序（加权版把权威源第 1 条置首，
经典版把「被两引擎共同命中」的条目提到第 2）。故开关的价值在于可选与
可回归，而非更优。

## 三条不变式

  1. **默认不变**：不带开关时与旧实现逐位一致（权重的默认仍是加权）。
  2. **显式优先**：传 `weighted=True/False` 时，环境变量不得覆盖它
     ——否则测试与消融脚本会被宿主环境意外改写。
  3. **开关生效**：`ARGO_RRF_WEIGHTED=0` 时排序必须等于经典 RRF 参考值。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 固定夹具：权威源两条 + 社交源两条，其中一条被两引擎共同命中（共识）。
# 用 example 域名，避免与任何真实源纠缠。
_LISTS = [
    [
        {"url": "https://a.example/1", "title": "权威", "_engine": "wikipedia"},
        {"url": "https://a.example/2", "title": "权威2", "_engine": "wikipedia"},
    ],
    [
        {"url": "https://b.example/1", "title": "社交", "_engine": "twitter"},
        {"url": "https://a.example/1", "title": "权威", "_engine": "twitter"},
    ],
]

# 经典 RRF 的期望值（实测锚点，k=60）：1/61 + 1/61 等分，不含引擎权重
_CLASSIC_EXPECT = {"权威": 0.03252, "社交": 0.01639, "权威2": 0.01613}


def _merge(weighted=None):
    from search import rrf_merge

    kw = {} if weighted is None else {"weighted": weighted}
    out = rrf_merge([list(x) for x in _LISTS], **kw)
    return {r["title"]: round(r["_rrf_score"], 5) for r in out}


class TestRrfEscapeHatch:
    def test_default_matches_weighted_behaviour(self):
        """不变式 1：不传参数时，行为与旧实现（默认加权）逐位一致。"""
        got = _merge()          # 走 _rrf_weighted_default()
        explicit = _merge(True)  # 旧签名的默认值
        assert got == explicit, (
            f"默认路径与显式 weighted=True 不一致：{got} != {explicit}。"
            "把默认值改成 None 后，必须仍解析为加权，否则是静默行为变更"
        )

    def test_env_toggle_falls_back_to_classic(self, monkeypatch):
        """不变式 3：开关置 0 时排序等于经典 RRF 参考值。"""
        monkeypatch.setenv("ARGO_RRF_WEIGHTED", "0")
        got = _merge()
        assert got == _CLASSIC_EXPECT, (
            f"ARGO_RRF_WEIGHTED=0 未退回经典 RRF：{got} != {_CLASSIC_EXPECT}"
        )

    def test_env_off_spellings(self, monkeypatch):
        """开关接受多种「关」的写法（与仓库其它 ARGO_* 开关一致）。"""
        for v in ("0", "off", "no", "false", "OFF", "False", "disabled"):
            monkeypatch.setenv("ARGO_RRF_WEIGHTED", v)
            assert _merge() == _CLASSIC_EXPECT, f"写法 {v!r} 未被识别为关闭"

    def test_env_on_spellings_keep_weighted(self, monkeypatch):
        """非「关」的值一律保持加权（含空串与任意真值写法）。"""
        for v in ("1", "on", "yes", "true", ""):
            monkeypatch.setenv("ARGO_RRF_WEIGHTED", v)
            assert _merge() == _merge(True), f"写法 {v!r} 被误判为关闭"

    def test_explicit_arg_beats_env(self, monkeypatch):
        """不变式 2：显式传参优先于环境变量。

        测试与消融脚本必须能钉死自己要的那一版；若 env 能盖掉显式参数，
        宿主环境里残留的 ARGO_RRF_WEIGHTED 会让回归断言静默换靶。
        """
        monkeypatch.setenv("ARGO_RRF_WEIGHTED", "0")
        assert _merge(True) == _merge(True), "夹具自检"
        weighted_now = _merge(True)
        assert weighted_now != _CLASSIC_EXPECT, (
            "显式 weighted=True 被环境变量覆盖成了经典 RRF"
        )
        monkeypatch.setenv("ARGO_RRF_WEIGHTED", "1")
        assert _merge(False) == _CLASSIC_EXPECT, (
            "显式 weighted=False 被环境变量覆盖成了加权"
        )

    def test_classic_differs_from_weighted(self):
        """夹具有效性自检：两版本必须真的产生不同次序，否则本文件测了个寂寞。"""
        w = _merge(True)
        c = _merge(False)
        assert w != c, (
            "加权与经典版在夹具上给出相同分数——夹具失去区分度，"
            "后续断言无法证明开关真的改变了行为"
        )
