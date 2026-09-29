#!/usr/bin/env python3
"""sub-skills 测试套件接线（2026-09-29）。

sub-skills/{ego-search,local-seek}/tests 的 33 个用例（本地搜索降级、
pcre2 探针、拼音排序、噪声档、查询归一化）不在 `pytest tests/` 的收集
范围——主套件全绿时它们可能已经红透，CI 形态下等于不存在（2026-09-28
覆盖盘点记录在案）。

接线方式选子进程而不是改收集路径：sub-skills 各带自己的 sys.path 与
conftest 语义（本地搜索的技能包），直接并入主收集有 import 次序与状态
隔离的不确定性；子进程跑是「一条绿盖住三包」的最薄方案，代价是每次
全量多 ~1s。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_suite(skill: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest",
         str(ROOT / "sub-skills" / skill / "tests"), "-q"],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
    )


def test_ego_search_suite():
    r = _run_suite("ego-search")
    assert r.returncode == 0, f"ego-search 套件失败：\n{r.stdout[-3000:]}"


def test_local_seek_suite():
    r = _run_suite("local-seek")
    assert r.returncode == 0, f"local-seek 套件失败：\n{r.stdout[-3000:]}"
