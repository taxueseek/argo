#!/usr/bin/env python3
"""tests/test_ego_search_mode.py — ego-search 专业搜索模式开关（默认关闭）"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ego_search


def _use_state(tmp_path, enabled=None):
    """把模块状态文件指向临时文件；enabled 为 None 时不预写（模拟首次使用）。"""
    p = tmp_path / "pro-mode.json"
    if enabled is not None:
        p.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    ego_search.STATE_PATH = str(p)
    return p


def test_default_off_without_state(tmp_path):
    _use_state(tmp_path)
    assert ego_search.mode_enabled() is False


def test_corrupt_state_treated_as_off(tmp_path):
    p = _use_state(tmp_path)
    p.write_text("{not json", encoding="utf-8")
    assert ego_search.mode_enabled() is False


def test_gate_blocks_search_modes_when_off(tmp_path):
    _use_state(tmp_path)
    for mode in ("search", "fetch", "act", "api"):
        assert ego_search.mode_gate_blocked(mode), f"{mode} 应被闸门拦截"
    # 开关类命令不受闸门限制
    assert ego_search.mode_gate_blocked("enable") is None
    assert ego_search.mode_gate_blocked("disable") is None
    assert ego_search.mode_gate_blocked("status") is None


def test_enable_persists_and_opens_gate(tmp_path, capsys):
    _use_state(tmp_path)
    ego_search.cmd_enable(None)
    state = json.loads((tmp_path / "pro-mode.json").read_text(encoding="utf-8"))
    assert state["enabled"] is True
    assert "enabled_at" in state
    assert ego_search.mode_enabled() is True
    assert ego_search.mode_gate_blocked("search") is None
    assert "已开启" in capsys.readouterr().out


def test_disable_closes_gate(tmp_path, capsys):
    _use_state(tmp_path, enabled=True)
    ego_search.cmd_disable(None)
    assert ego_search.mode_enabled() is False
    assert ego_search.mode_gate_blocked("fetch") is not None
    assert "已关闭" in capsys.readouterr().out


def test_status_reports_mode(tmp_path, capsys):
    _use_state(tmp_path, enabled=True)
    ego_search.cmd_status(None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["pro_mode"] is True
    assert payload["state_file"] == str(tmp_path / "pro-mode.json")
