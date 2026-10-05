#!/usr/bin/env python3
"""健康状态原子写回归——非原子写 bug 类第三例的守门测试。

背景：`_save_health` 曾用 `Path.write_text` 直写（截断即写），帧内崩溃会让
整个健康门状态丢失（"全体失忆"）。同族事故已在
sub-skills/local-search/engine_registry 与 scripts/local_seek 修过两例，
argo_engine_registry 是漏网的第三例（2026-10-05 收编 atomic_write_json）。

本测试锁两件事：
  1. 写路径必须委托 argo_paths.atomic_write_json（防回退成手写直写）；
  2. 写失败（os.replace 抛错）时旧状态文件必须完好、无 tmp 残留——这是
     原子写给用户的真实保证，直写做不到。

运行：
  python3 -m pytest tests/test_health_state_atomic_write.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import argo_engine_registry  # noqa: E402
import argo_paths  # noqa: E402


def _registry(monkeypatch, health_file: Path) -> "argo_engine_registry.EngineRegistry":
    """把模块级 HEALTH_STATE_PATH 指到临时文件后再建 registry。

    函数体内读的是模块全局（调用时查找），monkeypatch.setattr 即生效；
    会话结束时自动还原，不污染其他用例。
    """
    monkeypatch.setattr(argo_engine_registry, "HEALTH_STATE_PATH", health_file)
    return argo_engine_registry.EngineRegistry()


def test_save_health_delegates_to_atomic_write_json(monkeypatch, tmp_path):
    """写路径必须走 atomic_write_json——防止回退成 write_text 直写。"""
    calls: list[tuple] = []
    monkeypatch.setattr(
        argo_paths, "atomic_write_json",
        lambda path, payload, **kw: calls.append((path, payload, kw)))

    reg = _registry(monkeypatch, tmp_path / "argo_engine_health.json")
    reg.update_health("probe_engine", True, detail="unit-test")

    assert len(calls) == 1, "update_health 未委托 atomic_write_json（非原子写回归）"
    path, payload, kw = calls[0]
    assert path == argo_engine_registry.HEALTH_STATE_PATH
    assert payload["probe_engine"]["available"] is True
    assert payload["probe_engine"]["consecutive_failures"] == 0


def test_write_failure_preserves_old_state_and_leaves_no_tmp(monkeypatch, tmp_path):
    """崩溃窗口：os.replace 抛错时旧文件必须完好，且不留 tmp 残骸。

    直写（write_text）在本场景下会留下新内容/半截文件；原子写的保证是
    「读者只见旧或新」——replace 未发生就还是旧的。
    """
    health_file = tmp_path / "argo_engine_health.json"
    old_payload = {"old_engine": {"available": True, "consecutive_failures": 0}}
    old_text = json.dumps(old_payload, ensure_ascii=False, separators=(",", ":"))
    health_file.write_text(old_text, encoding="utf-8")

    reg = _registry(monkeypatch, health_file)

    def _boom(src, dst):
        raise OSError("simulated crash between tmp write and replace")

    monkeypatch.setattr(os, "replace", _boom)
    reg.update_health("new_engine", True)  # 内部按设计 fail-open 吞掉

    # 旧状态完好（replace 没发生）
    assert health_file.read_text(encoding="utf-8") == old_text
    # 无 tmp 残骸（atomic_write_text 的失败自清理）
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != health_file.name]
    assert not leftovers, f"原子写失败残留了临时文件: {leftovers}"
