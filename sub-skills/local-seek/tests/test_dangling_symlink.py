#!/usr/bin/env python3
"""test_dangling_symlink.py — 悬空软链容错与中英词表契约（2026-10-31）。

守的行为（2026-10-31 实测 ~/.agents/skills/web-access 悬空软链暴露）：

  1. rg 主路径：个别路径出错（os error 2）时 rg 跳过该路径继续搜，stdout
     里其余文件的匹配照常有效——returncode 2 不得让整个查询空手而归
     （与 _structural_search 的既有口径对齐，此前只有它做对了）
  2. fd 路径（--filename）：同口径，退出码异常但 stdout 有结果照常解析
  3. _ZH_EN_ALIASES 词表：通用工程高频词在场（渲染/视频/字幕等 13 条，
     原表偏 argo 仓工程词，「视频字幕」查 remotion 系列全 miss）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import seek as s  # noqa: E402
import seek_rank as r  # noqa: E402


@pytest.fixture
def corpus_with_dangling_link(tmp_path: Path) -> Path:
    """两个正常文件 + 一个指向不存在目标的目录软链。"""
    (tmp_path / "a.py").write_text("def render_page():\n    return 'page'\n")
    (tmp_path / "b.py").write_text("def render_view():\n    return 'view'\n")
    (tmp_path / "render_util.py").write_text("x = 1\n")  # 供 fd 按文件名命中
    os.symlink(tmp_path / "nowhere-exists-not", tmp_path / "broken-link")
    return tmp_path


def test_rg_survives_dangling_symlink(corpus_with_dangling_link: Path):
    """含悬空软链的目录：查询必须返回正常文件的结果，rc=0。"""
    text, rc = s.run_query(["render", "--path", str(corpus_with_dangling_link),
                            "--dot", "--json"])
    assert rc == 0, f"查询被判失败: rc={rc}, out={text[:200]}"
    data = json.loads(text)
    files = {r0.get("path", "") for r0 in data["results"]}
    assert any("a.py" in f for f in files), f"a.py 未命中: {files}"
    assert any("b.py" in f for f in files), f"b.py 未命中: {files}"


@pytest.mark.skipif(shutil.which("fd") is None,
                    reason="fd 未安装：--filename 路径不可用（CI 只装 ripgrep，"
                           "内容搜索的断链容错由上一条用 rg 覆盖）")
def test_fd_survives_dangling_symlink(corpus_with_dangling_link: Path):
    """--filename 走 fd：同口径不因断链空手而归（fd 按文件名匹配）。"""
    text, rc = s.run_query(["render_util", "--path", str(corpus_with_dangling_link),
                            "--dot", "--filename", "--json"])
    assert rc == 0, f"查询被判失败: rc={rc}, out={text[:200]}"
    assert "render_util.py" in text


def test_zh_en_aliases_cover_common_engineering_terms():
    """通用工程高频词必须在词表内（key 为 unicode 转义形态的同一字符串）。"""
    for zh, ens in (
        ("渲染", ("render",)),
        ("视频", ("video",)),
        ("字幕", ("subtitle", "caption")),
        ("内存", ("memory",)),
        ("数据库", ("database", "db")),
    ):
        assert r._ZH_EN_ALIASES.get(zh) == ens, f"词表缺/错: {zh}"


def test_aliases_fallback_reaches_english_corpus(corpus_with_dangling_link: Path):
    """中英对齐回落：原 patterns 空手时才带对齐词元重搜。

    只进打分层时纯英文语料零候选；无条件注入检索层又用对齐噪音挤掉既有
    命中（eval_rank 0.8258→0.7857 实测回退）——回落语义两头都保住。
    """
    # build_patterns 本体不含对齐词元（检索面默认不变）
    pats, _ = s.build_patterns("渲染视频")
    assert "render" not in pats, pats
    assert "渲染" in pats, pats
    # 干净英文语料 + 中文查询：首轮零命中触发回落，命中 render 文件
    text, rc = s.run_query(["渲染", "--path", str(corpus_with_dangling_link),
                            "--json"])
    assert rc == 0, f"回落查询被判失败: rc={rc}, out={text[:200]}"
    assert "render_page" in text, text[:300]


def test_no_fallback_when_first_pass_hits(corpus_with_dangling_link: Path):
    """首轮有命中时不回落：对齐词元不得挤掉既有命中（eval_rank 回退红线）。"""
    # 语料同时有中文命中与英文文件：首轮「渲染」字面命中中文注释文件即止，
    # render_util.py（仅文件名含 render、正文无该词）不应经回落混入前排
    text, rc = s.run_query(["渲染", "--path", str(corpus_with_dangling_link),
                            "--json"])
    assert rc == 0
    # 中文注释在 a.py 首行命中 → 首轮非空 → 无回落，顺序可预期
    assert "a.py" in text
