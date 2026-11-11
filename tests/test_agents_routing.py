#!/usr/bin/env python3
"""test_agents_routing.py — AGENTS.md 路由表的机械一致性门禁。

## 为什么要这道门

AGENTS.md 是 agent 每次启动读的路由表。路由错了比没有路由更坏：agent 会按
一条不存在的路径去找文档，或者更糟——按旧路径改代码，而真源早已搬家。

argo 有过这类事故：`config.yaml` 的引擎声明迁到 `engines/specs/*.yaml` 时，
文档里的旧路径没同步更新；`docs/engines/` 60 份文档删除时，引用它们的地方
留下死链。路由表的意义就在于「动手前一定读到」，所以它自己必须先是对的。

本文件锁三件事：
  1. AGENTS.md 引用的文件路径都真实存在（死链即红）
  2. 提到的默认关开关都在 `references/usage.md` 的开关总表里（口径不漂移）
  3. 「不可回退的决定」清单覆盖 usage.md 里全部默认关开关（漏登即红）

回退测试：`test_agents_md_exists_and_is_loaded` 在**删掉 AGENTS.md 的旧状态**上
必须挂——文件不存在，断言直接失败。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENTS_MD = ROOT / "AGENTS.md"
USAGE_MD = ROOT / "references" / "usage.md"

# 这些前缀的不是文件路径，是命令或变量名。
_NON_PATH_PREFIXES = ("http", "ARGO_", "pytest ", "bash ", "python3 ",
                      "scripts/sync", "scripts/link", "scripts/replay",
                      "scripts/matrix", "scripts/check")


def _mentioned_paths(text: str) -> set[str]:
    """从文本里抽「像文件路径」的反引号片段。"""
    out = set()
    for raw in re.findall(r"`([^`]+)`", text):
        token = raw.split("(")[0].strip()
        if not re.search(r"\.(md|py|yaml|yml|json|toml|sh)$", token):
            continue
        if token.startswith(_NON_PATH_PREFIXES) or "*" in token:
            continue
        out.add(token)
    return out


# 「默认关」的两种含义要分开：
#   1. 布尔能力开关（默认关 = 需要显式打开）——这些是拍过板的产品决定
#   2. 数值参数的默认值（默认 0.5 / 默认 0.85）——这些是调参，不是产品决定
# 第 2 类不该进「不可回退」清单：把阈值调一下不等于改变用户可见行为的方向。
# 判据：描述里写「默认关」的算能力开关；写「默认 <数字>」的算参数。
_PARAM_DEFAULT_RE = re.compile(r"默认\s*\d")


def _default_off_switches(usage_text: str) -> set[str]:
    """从 usage.md 开关总表抽「默认关」的**能力开关**（排除数值参数与反向项）。

    三种情况要分开：
      1. 「默认关；=1 开启」        → 能力开关，**进**不可回退清单
      2. 「默认开；测试环境默认关」  → 反向项（默认开，只是测试时关），**不进**
      3. 「默认 0.5 / 默认 0.2s」   → 数值参数，**不进**

    把第 2、3 类算进来会让清单变成「所有非默认值」的大杂烩，反而看不出哪些
    是拍过板的产品决定。
    """
    out = set()
    for name, desc in re.findall(r"^\| `([A-Z_]+)` \|[^|]+\|([^|]+)\|$",
                                 usage_text, re.M):
        if "默认关" in desc and "默认开" not in desc:
            out.add(name)
        elif re.search(r"默认\s*0[；;，,（(]", desc) and "默认开" not in desc:
            # 「默认 0（阻止…）」形态：0 是开关的关态，不是数值
            out.add(name)
    return out


def test_agents_md_exists_and_is_loaded():
    """回退测试：AGENTS.md 不存在时这条挂——路由表没了，agent 无从路由。"""
    assert AGENTS_MD.exists(), "AGENTS.md 缺失：agent 启动时读不到路由表"
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert len(text) > 500, "AGENTS.md 过短，像是占位文件"


def test_agents_md_paths_resolve():
    """AGENTS.md 引用的文件路径必须真实存在——死链即红。"""
    missing = sorted(p for p in _mentioned_paths(
        AGENTS_MD.read_text(encoding="utf-8"))
        if not (ROOT / p).exists())
    assert not missing, f"AGENTS.md 引用了不存在的路径: {missing}"


def test_agents_md_covers_core_docs():
    """路由表必须覆盖核心文档，漏一个方向 agent 就会走错。"""
    text = AGENTS_MD.read_text(encoding="utf-8")
    for doc in ("docs/COMMIT_STANDARD.md", "docs/ADDING_NEW_ENGINE.md",
                "references/operations.md", "references/engines.md",
                "references/architecture.md", "docs/RELEASING.md",
                "scripts/engine_states.py", "scripts/badcase_ledger.py"):
        assert doc in text, f"AGENTS.md 缺路由: {doc}"


def test_agents_md_switches_are_real():
    """AGENTS.md 提到的开关必须在 usage.md 开关总表里——口径不漂移。"""
    ag = AGENTS_MD.read_text(encoding="utf-8")
    usage = USAGE_MD.read_text(encoding="utf-8")
    switches = set(re.findall(r"`(ARGO_[A-Z_]+)`", ag))
    assert switches, "该从 AGENTS.md 抽到开关名"
    missing = sorted(s for s in switches if s not in usage)
    assert not missing, f"AGENTS.md 提到 usage.md 里没有的开关: {missing}"


def test_all_default_off_switches_are_listed():
    """usage.md 里每个默认关开关都该在 AGENTS.md 的不可回退清单里。

    回退测试：把 AGENTS.md 里的开关清单删空，这条会挂。漏登的后果是 agent
    以为某个默认关的开关可以随便改，而它其实是拍过板的产品决定。
    """
    ag = AGENTS_MD.read_text(encoding="utf-8")
    off = _default_off_switches(USAGE_MD.read_text(encoding="utf-8"))
    assert off, "该从 usage.md 抽到默认关开关"
    missing = sorted(s for s in off if s not in ag)
    assert not missing, (
        f"这些默认关开关未登记进 AGENTS.md 的不可回退清单: {missing}")


def test_agents_md_points_to_commit_standard_not_inline_rules():
    """AGENTS.md 只写路由不写规则：提交规范必须指向 COMMIT_STANDARD.md。

    判据：AGENTS.md 里出现「跑了 / 在 commit / 未跑」三段式说明时，同一份文本
    里必须出现 COMMIT_STANDARD.md 的引用——否则就是把规则抄进了路由表，
    两份真源会各自漂移。
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    mentions_segments = all(s in text for s in ("跑了", "在 commit", "未跑"))
    if mentions_segments:
        assert "docs/COMMIT_STANDARD.md" in text, (
            "AGENTS.md 写了提交规范三段式，必须同时引用 docs/COMMIT_STANDARD.md，"
            "不要把规则抄进路由表"
        )


# ── pre-commit 文件数的机械对拍 ──────────────────────────────────────────────
#
# 数字是纯手写的，改了 pre-commit 清单而忘了改路由表不会有任何信号。

_HOOK_PYTEST_BLOCK = re.compile(r"-m pytest -q \\\n(.*?)(?=\n\n|\n#)", re.S)
_HOOK_PYTEST_FILE = re.compile(r"(tests/test_[a-z_0-9]+\.py)")
_AGENTS_COUNT = re.compile(r"`bash scripts/pre-commit`（(\d+) 个 pytest 文件")


def _listed_pytest_files(hook_text: str) -> set[str]:
    """pre-commit 里 `-m pytest -q` 之后续行声明的 pytest 文件（去重）。

    口径只认 pytest：node 测试与 `scripts/sync_backends.py --check` 不算。
    """
    m = _HOOK_PYTEST_BLOCK.search(hook_text)
    assert m, "pre-commit 里找不到 `-m pytest -q` 清单块"
    return set(_HOOK_PYTEST_FILE.findall(m.group(1)))


def _declared_pytest_count(agents_text: str) -> int:
    """AGENTS.md 路由表里声称的 pytest 文件数。"""
    counts = [int(c) for c in _AGENTS_COUNT.findall(agents_text)]
    assert counts, "AGENTS.md 未写 pre-commit 的 pytest 文件数"
    assert len(set(counts)) == 1, f"AGENTS.md 里出现多个不同的文件数: {counts}"
    return counts[0]


def test_agents_md_precommit_file_count_matches():
    """AGENTS.md 声称的 pytest 文件数必须等于 pre-commit 的实际清单数。

    为什么要有这道门：这个数字是**纯手写**的，改了 pre-commit 清单而忘了改
    路由表时不会有任何信号——而它正是 agent 判断「提交前该跑什么、要多久」的
    依据，数字虚高或虚低都会让 agent 对门禁的规模判断失真。

    回退验证：把 AGENTS.md 的 34 改回 35，本条挂（实测 35 != 34）。
    """
    listed = _listed_pytest_files((ROOT / "scripts" / "pre-commit").read_text(encoding="utf-8"))
    assert listed, "pre-commit 清单解析为空——正则或清单形态漂移了"
    declared = _declared_pytest_count(AGENTS_MD.read_text(encoding="utf-8"))
    assert declared == len(listed), (
        f"AGENTS.md 写 {declared} 个 pytest 文件，pre-commit 实际清单是 "
        f"{len(listed)} 个——改清单时同步这个数字，或把数字从文案里去掉")


def test_precommit_count_gate_has_teeth():
    """自检：判据必须能区分「一致」与「不一致」，否则它恒真、等于没检查。"""
    sample_hook = (
        "$PY -m pytest -q \\\n"
        "    tests/test_a.py \\\n"
        "    tests/test_b.py \\\n"
        "    tests/test_a.py\n\n"
        "echo done\n"
    )
    assert _listed_pytest_files(sample_hook) == {"tests/test_a.py", "tests/test_b.py"}, \
        "清单解析把重复项或块边界算错了"
    # 去掉非 pytest 行后仍只认 pytest 文件
    assert "echo" not in " ".join(_listed_pytest_files(sample_hook))

    assert _declared_pytest_count("`bash scripts/pre-commit`（2 个 pytest 文件）") == 2
    # 数字不一致时必须不相等——这是上一条判据的全部意义
    assert _declared_pytest_count("`bash scripts/pre-commit`（3 个 pytest 文件）") \
        != len(_listed_pytest_files(sample_hook))
