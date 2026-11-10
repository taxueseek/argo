#!/usr/bin/env python3
"""test_check_commit_report.py — 高风险提交检查清单的判定与门禁。

## 为什么要这道门

argo 的 pre-commit 注释自己记着两起真实事故：`f702ee2` 把 SKILL.md 压到
8049B、`4a71693` 让 deepseek_search 成死源，「两笔都过了 hook 却在全量回归里
红」。跑了的门禁全绿，**没跑的那条门没人知道它该跑**——因为提交信息里没有
「未跑什么」这个位置，省略不写是默认行为。

本文件锁两件事：
  1. 高风险提交的判定口径（与 docs/COMMIT_STANDARD.md 一一对应）
  2. 清单三段的识别（`跑了` / `在 commit` / `未跑`，`未跑: 无` 合法）

回退测试：`test_missing_segments_detects_old_behavior` 在**没有清单判定的旧
实现**上必须挂——旧实现只回 ok=True，`missing` 字段不存在，直接 KeyError。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_commit_report as ccr  # noqa: E402

_HIGH_RISK_TITLES = [
    "feat(engines): 接入 Mwmbl 源",
    "fix(engines): 修 X 源解析器零交付",
    "chore(engines): 停用失效源",
    "feat(route): 新增域路由规则",
    "fix(search): 修早停误判",
    "fix(rank): 修权重漏乘",
    "feat(states): 补登隐式状态机",
    "refactor(states): 收口状态词表",
    "fix(states): 修 L0 落到默认档",
    "perf(dispatch): deep 档补尾宽",
    "fix(release): 修版本号漏改",
    "chore(release): v2.13.0",
    "build(deps): 依赖收口到 uv",
]

_LOW_RISK_TITLES = [
    "docs: 补一份说明",
    "test: 加一条回归用例",
    "refactor: 抽公共函数",
    "style: 统一引号",
    "chore: 清理临时文件",
]


@pytest.mark.parametrize("title", _HIGH_RISK_TITLES)
def test_high_risk_titles_detected(title):
    assert ccr.is_high_risk(title) is True, title


@pytest.mark.parametrize("title", _LOW_RISK_TITLES)
def test_low_risk_titles_skipped(title):
    assert ccr.is_high_risk(title) is False, title


@pytest.mark.parametrize("title", [
    # 子技能改动不进高风险：它们影响面小、跑子技能自己的测试就够
    "feat(local-seek): 加能力",
    "feat(seek): 加能力",
    "feat(parsers): 加解析器",
    "feat(local-search): 优化打分",
    "feat(js-run): 加运行时",
])
def test_subskill_scopes_are_not_high_risk(title):
    """子技能 scope 不算高风险。

    回退测试：把白名单改成 `[a-z_]+` 通配时这条会挂——2026-11-10 就试过
    通配，结果 `feat(local-seek)` 被误判成高风险，清单于是变成所有带 scope
    的提交都要写，反而没人当真。
    """
    assert ccr.is_high_risk(title) is False, title


def test_agents_scope_is_high_risk():
    """`agents` 必须算高风险——门禁管不到自己正是它最该管的地方。

    回退测试：首次落地时白名单漏了 `agents`，本次这笔改动自己的提交信息
    没被门禁检查到。
    """
    assert ccr.is_high_risk("feat(agents): 补全错题本闭环") is True


def test_complete_checklist_passes():
    """三段齐全即通过——`未跑: 无` 是合法写法。"""
    text = ("feat(engines): 接入 Mwmbl 源\n\n"
            "检查：\n"
            "- 跑了：scripts/engine_validate.py --engines mwmbl --strict\n"
            "- 在 commit：abc123\n"
            "- 未跑：无\n")
    r = ccr.check(text)
    assert r["high_risk"] is True
    assert r["ok"] is True and r["missing"] == []


def test_missing_not_run_segment_detected():
    """只写「跑了」不写「未跑」→ 缺段。这正是两起真实事故的形态。"""
    text = ("feat(engines): 接入 Mwmbl 源\n\n"
            "跑了：scripts/engine_validate.py --strict\n")
    r = ccr.check(text)
    assert r["ok"] is False
    assert "未跑" in r["missing"]


def test_missing_all_segments_detected():
    text = "feat(engines): 接入 Mwmbl 源\n\n正文没有清单。"
    r = ccr.check(text)
    assert r["missing"] == ["跑了", "在 commit", "未跑"]


def test_low_risk_without_checklist_passes():
    """非高风险提交不要求清单。"""
    r = ccr.check("docs: 补一份说明\n\n正文。")
    assert r["high_risk"] is False and r["ok"] is True


def test_comments_are_stripped_before_checking():
    """git 注释行（# 开头）不算正文，避免误判。"""
    text = ("feat(engines): 接入 X\n\n"
            "# 这是 git 生成的注释\n"
            "# Please enter the commit message\n")
    r = ccr.check(text)
    assert r["high_risk"] is True and r["missing"] != []


def test_missing_segments_detects_old_behavior(monkeypatch):
    """回退测试：旧实现没有 `missing` 字段，这条必须挂。

    做法：把 check() 换成只回 ok=True 的旧版，确认断言失败。
    """
    def old_check(text: str) -> dict:
        return {"title": text.splitlines()[0], "high_risk": False, "ok": True}

    monkeypatch.setattr(ccr, "check", old_check)
    with pytest.raises(KeyError):
        ccr.check("feat(engines): x")["missing"]


def test_strict_exit_code_on_missing_segments(tmp_path):
    """--strict 下缺段退出 1，非 strict 退出 0（只警告）。"""
    msg = tmp_path / "msg.txt"
    msg.write_text("feat(engines): 接入 X\n", encoding="utf-8")
    strict = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_commit_report.py"),
         "--message-file", str(msg), "--strict"],
        capture_output=True, text=True)
    loose = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_commit_report.py"),
         "--message-file", str(msg)],
        capture_output=True, text=True)
    assert strict.returncode == 1
    assert loose.returncode == 0, "非 strict 只该警告，不该拦住提交"


def test_standard_doc_lists_the_same_high_risk_patterns():
    """docs/COMMIT_STANDARD.md 与本模块的高风险 scope 白名单必须一致。

    两份真源会漂移，而漂移的方向是「门禁管不到某类改动」——2026-11-10 首次
    落地时就漏了 `agents`，本次这笔改动自己的提交信息没被检查。

    口径：从代码的 `_HIGH_RISK_SCOPES` 逐个确认文档提到它。不手写期望列表——
    手写的那份会和代码各自漂移，就失去了对拍的意义。
    """
    doc = (Path(__file__).resolve().parents[1]
           / "docs" / "COMMIT_STANDARD.md").read_text(encoding="utf-8")
    assert ccr._HIGH_RISK_SCOPES, "该从代码抽到 scope 白名单"
    missing = sorted(s for s in ccr._HIGH_RISK_SCOPES if s not in doc)
    assert not missing, f"文档缺高风险 scope: {missing}"
    for seg in ("跑了", "在 commit", "未跑"):
        assert seg in doc, f"文档缺清单段: {seg}"
