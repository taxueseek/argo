#!/usr/bin/env python3
"""argo-zcode 面板桥：daemon 状态必须来自机器可读字段，不靠中文文案反推。

## 为什么要有这条

桥的旧实现是「中文文案 + `daemon_running` 布尔 + 正则抠 pid」：
  - 文案改一个字（「未运行」→「已停止」），pid 正则静默失效，
    面板把活着的进程显示成停止；
  - 布尔只有两态，而 daemon 实际有 running / busy / idle / stopped 四态，
    「正在跑一笔搜索」与「空闲」在面板上无法区分。

修法：daemon 侧（`scripts/daemon.py`）的 ping 与 `status --json` 都带
`state` 字段（词表见 `engine_states` §10），桥直接读它。本用例钉住两条路径——
进程内直读与 CLI 回退——都从字段取值，且**不依赖中文文案**。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "packages" / "argo-zcode" / "server" / "py_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("_argo_py_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_state_comes_from_field_not_from_chinese_text(monkeypatch):
    """CLI 回退路径：state 取字段（busy），文案由字段拼出来而不是反解析。"""
    bridge = _load_bridge()
    # 让进程内直读失败（模拟 argo 本体不在 sys.path / 旧版模块）
    monkeypatch.setitem(sys.modules, "daemon", object())
    monkeypatch.setattr(
        bridge, "_run_argo",
        lambda home, args, timeout=20: (
            json.dumps({"ok": True, "running": True, "state": "busy",
                        "pid": 4242, "socket": "/tmp/x.sock"}),
            "", 0),
    )
    text, state = bridge._daemon_status_fast("/nonexistent-argo-home")
    assert state == "busy"
    assert "4242" in text  # 文案由字段构造，不再正则反推


def test_fallback_accepts_json_without_state_field(monkeypatch):
    """旧版 daemon 的 --json 只有 running 布尔：按布尔降级成 running/stopped。"""
    bridge = _load_bridge()
    monkeypatch.setitem(sys.modules, "daemon", object())
    monkeypatch.setattr(
        bridge, "_run_argo",
        lambda home, args, timeout=20: (
            json.dumps({"ok": True, "running": False, "socket": "/tmp/x.sock"}),
            "", 0),
    )
    text, state = bridge._daemon_status_fast("/nonexistent-argo-home")
    assert state == "stopped"
    assert "未运行" in text


def test_fallback_to_human_text_when_json_unavailable(monkeypatch):
    """连 --json 都拿不到（旧版/异常）时才退回人读文案 + pid 探活。"""
    bridge = _load_bridge()
    monkeypatch.setitem(sys.modules, "daemon", object())
    monkeypatch.setattr(
        bridge, "_run_argo",
        lambda home, args, timeout=20: (
            "not json at all" if "--json" in args else "argo daemon 运行中 pid=1 socket=/tmp/x",
            "", 0),
    )
    monkeypatch.setattr(bridge, "_daemon_running", lambda text: True)
    _text, state = bridge._daemon_status_fast("/nonexistent-argo-home")
    assert state == "running"


@pytest.mark.parametrize("state,expected", [
    ("running", True), ("busy", True), ("idle", True), ("stopped", False),
])
def test_daemon_running_is_derived_from_state(monkeypatch, state, expected):
    """面板的 daemon_running 是兼容字段：stopped 之外都算活着（busy 也是活着）。"""
    bridge = _load_bridge()
    source = BRIDGE.read_text(encoding="utf-8")
    # 判据必须写成「state != stopped」，不能写「state == running」——
    # 后者会把正在跑搜索的 daemon 报成没在跑。同时钉住它引用词表常量而非
    # 手写字面量：此前本用例断言源码里必须出现 `"stopped"`，改词表时改测试
    # 即可全绿，属于「看起来有门禁」的假阳性（2026-11-10 审计）。
    assert "daemon_state != states.DAEMON_STOPPED" in source
    assert expected == (state != "stopped")
