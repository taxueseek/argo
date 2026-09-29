#!/usr/bin/env python3
"""Step 4 —— recovery 接线 family_candidates 回归门。

守契约：recovery L3 步骤 2 的同族候选，除域声明 engines_fallback 外，还用
family_candidates 按已试能力族从全注册表激活启用源——把「单跑可用但域 combo
选不中」的孤儿源接进空结果恢复路径（首次给 family_candidates 这个「单一取源口」
一个真实调用点）。

不变式：仍走 _engine_family 同族门 + _recovery_engine_allowed 安全门；优先级不变
（通用源仍在步骤 1 优先）；失败安全（family_candidates 异常退回 engines_fallback）。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo-rec-fam-"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from recovery import _engine_family, pick_alternative_engines  # noqa: E402


def test_recovery_activates_same_family_via_family_candidates():
    # enabled 限定为 academic 族（排除通用源，隔离步骤 1 的通用优先）→ 此时只能靠
    # family_candidates 按族激活同族源（含域 combo 选不中的孤儿源）。
    from engine_families import family_candidates
    academic = set(family_candidates("academic", "*"))
    assert academic, "config 应有 academic 族源"
    tried = ["crossref"]  # 只试了一个 academic 源
    picks = pick_alternative_engines(tried, engines_fallback=[], enabled=academic, max_n=2)
    assert picks, "应从 family_candidates 激活同族（academic）源"
    for p in picks:
        assert p not in tried, f"不应重复已试源: {p}"
        assert _engine_family(p) == "academic", (p, _engine_family(p))


def test_recovery_step2_still_respects_general_priority():
    # 通用源未试过时，步骤 1 仍优先（family_candidates 不抢先）——优先级不变。
    picks = pick_alternative_engines(["crossref"], engines_fallback=[],
                                     enabled=None, max_n=2)
    # 前两个应是通用免费源（anysearch/local_bing/wikipedia/octen 之一）
    from engine_policy import GENERAL_FREE_FALLBACK
    assert picks and picks[0] in GENERAL_FREE_FALLBACK, picks


def test_recovery_step2_failsafe_without_family_candidates(monkeypatch):
    # family_candidates 抛错 → 步骤 2 退回 engines_fallback，绝不炸；步骤 1 的通用
    # 源仍正常（family_candidates 的故障不连坐恢复链的其它步骤）。
    import engine_families
    from engine_policy import GENERAL_FREE_FALLBACK
    monkeypatch.setattr(engine_families, "family_candidates",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    picks = pick_alternative_engines(["crossref"], engines_fallback=[],
                                     enabled=None, max_n=2)
    assert picks and picks[0] in GENERAL_FREE_FALLBACK, picks  # 不炸，通用源仍在
