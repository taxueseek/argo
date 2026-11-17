#!/usr/bin/env python3
"""跨平台守卫与密钥读取的离线单测（不依赖真机 OS/Keychain/Chrome）。"""
from __future__ import annotations

import base64
import builtins
import json
import shutil
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_sogou_browser_uses_cross_platform_file_lock(tmp_path):
    """sogou_weixin_browser 不顶层依赖 fcntl，状态写盘走 argo_paths.file_lock。"""
    import sogou_weixin_browser as swb

    locked_paths: list[Path] = []

    @contextmanager
    def fake_lock(path, timeout=30.0):
        locked_paths.append(Path(path))
        yield

    state_file = tmp_path / "state.json"
    fake_out = swb.JSON_MARKER + json.dumps({"ok": True, "space_id": "sp1", "items": []})
    ok_proc = subprocess.CompletedProcess(["ego-browser"], 0, stdout=fake_out, stderr="")
    with patch.object(swb, "file_lock", fake_lock), \
            patch.object(swb, "_state_path", return_value=str(state_file)), \
            patch.object(swb, "_safe_state_path", side_effect=lambda p: p), \
            patch.object(swb.shutil, "which", return_value="/usr/bin/ego-browser"), \
            patch.object(subprocess, "run", return_value=ok_proc):
        rc = swb.main(["hello", "--min-interval", "0"])

    assert rc == 0
    assert locked_paths == [state_file]
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["space_id"] == "sp1"


def test_zhihu_secret_resolution_linux_and_windows():
    """Linux secret-tool 与 Windows keyring 降级路径在跨平台下工作正常。"""
    import engine_env

    orig_cache = engine_env._zhihu_keychain_cache
    try:
        # Linux secret-tool 返回 go-keyring-base64: 编码字符串
        engine_env._zhihu_keychain_cache = None
        b64 = base64.b64encode(b"linux-zhihu-secret").decode("ascii")
        encoded = f"go-keyring-base64:{b64}\n".encode("utf-8")
        with patch.object(engine_env.sys, "platform", "linux"), \
                patch.object(shutil, "which", return_value="/usr/bin/secret-tool"), \
                patch.object(subprocess, "check_output", return_value=encoded):
            assert engine_env._read_zhihu_keychain() == "linux-zhihu-secret"

        # Windows keyring 降级路径
        engine_env._zhihu_keychain_cache = None
        fake_keyring = types.SimpleNamespace(
            get_password=lambda svc, acct: "win-zhihu-secret",
        )
        orig_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "keyring":
                return fake_keyring
            return orig_import(name, globals, locals, fromlist, level)

        with patch.object(engine_env.sys, "platform", "win32"), \
                patch.object(builtins, "__import__", side_effect=fake_import):
            assert engine_env._read_zhihu_keychain() == "win-zhihu-secret"
    finally:
        engine_env._zhihu_keychain_cache = orig_cache


def test_daemon_windows_process_helpers(tmp_path):
    """daemon 在 Windows (os.name == 'nt') 下的识别/终止/后台拉起分支。"""
    import daemon

    # _pid_is_daemon Windows PowerShell CIM 查询
    ok_line = f"python.exe {Path(daemon.SCRIPTS_DIR) / 'daemon.py'} start\n"
    ok_proc = subprocess.CompletedProcess(["powershell"], 0, stdout=ok_line, stderr="")
    with patch.object(daemon.os, "name", "nt"), \
            patch.object(subprocess, "run", return_value=ok_proc):
        assert daemon._pid_is_daemon(4321) is True

    other_proc = subprocess.CompletedProcess(["powershell"], 0, stdout="notepad.exe\n", stderr="")
    with patch.object(daemon.os, "name", "nt"), \
            patch.object(subprocess, "run", return_value=other_proc):
        assert daemon._pid_is_daemon(4321) is False

    # _terminate_pid Windows taskkill
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch.object(daemon.os, "name", "nt"), \
            patch.object(subprocess, "run", side_effect=fake_run):
        daemon._terminate_pid(4321)
    assert calls and calls[0][:3] == ["taskkill", "/PID", "4321"]

    # _start_detached_windows 使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    popen_mock = MagicMock()
    ns = types.SimpleNamespace(idle_exit=300, json=False)
    with patch.object(subprocess, "Popen", popen_mock), \
            patch.object(daemon, "_wait_ready_and_report", return_value=0) as wait_mock:
        rc = daemon._start_detached_windows(ns)
    assert rc == 0
    wait_mock.assert_called_once()
    _, popen_kw = popen_mock.call_args
    expected_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    assert popen_kw["creationflags"] == expected_flags


def test_zhihu_keychain_empty_value_self_heals():
    """空值（Keychain 超时/未录入）只黏 60s 短 TTL，过期重探。

    旧实现把失败空串写进进程级永久缓存：daemon/MCP 首次读失败（如安全弹窗
    未应答）后知乎全族整进程判缺钥且无自愈。回退验证：把缓存改回
    `str | None` 并无条件返回，本测试第二段（TTL 过期重探）挂。
    """
    import engine_env

    orig_cache = engine_env._zhihu_keychain_cache
    calls = []

    def _boom(*_a, **_kw):
        calls.append(1)
        raise subprocess.TimeoutExpired(cmd="security", timeout=1.5)

    try:
        engine_env._zhihu_keychain_cache = None
        with patch.object(engine_env.sys, "platform", "darwin"), \
                patch.object(subprocess, "check_output", side_effect=_boom):
            assert engine_env._read_zhihu_keychain() == ""
            assert engine_env._read_zhihu_keychain() == ""
        assert len(calls) == 1, "TTL 内不应重复探 Keychain"

        # 时间倒拨越过负 TTL：应重探（仍失败，但确实又调了一次）
        ts, _val = engine_env._zhihu_keychain_cache
        engine_env._zhihu_keychain_cache = (ts - 61.0, _val)
        with patch.object(engine_env.sys, "platform", "darwin"), \
                patch.object(subprocess, "check_output", side_effect=_boom):
            assert engine_env._read_zhihu_keychain() == ""
        assert len(calls) == 2, "TTL 过期应重探（自愈对称：envfile 有签名热读，keychain 不能黏死）"
    finally:
        engine_env._zhihu_keychain_cache = orig_cache
