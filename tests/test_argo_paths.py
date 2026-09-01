#!/usr/bin/env python3
"""
test_argo_paths.py — 本地状态目录单一真源回归测试

背景：此前 11 个模块各自拼 ~/.cache/unified-search，构造方式 4 种分裂，
config.yaml 的 cache.db_path 管不住 quota.json / health.db 等文件，
测试也无法整体隔离。现统一由 argo_paths 派生。

覆盖：
  ARGO_STATE_DIR 硬开关优先级（含能盖掉磁盘 config.yaml 的 db_path）
  未设置时回落到历史默认目录（存量缓存不失效）
  各模块路径确实落在同一根目录
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import argo_paths  # noqa: E402


# ─── 根目录派生 ───────────────────────────────────────────────────────────────

def test_state_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    assert argo_paths.state_root() == tmp_path


def test_state_root_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, os.path.join("~", ".cache", "x"))
    root = argo_paths.state_root()
    assert str(root).startswith(os.path.expanduser("~"))
    assert not str(root).startswith("~")


def test_state_root_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv(argo_paths.ENV_STATE_DIR, raising=False)
    root = argo_paths.state_root()
    # 无 env 时不崩、且必须落在真实 home 下（历史默认目录）
    assert root == argo_paths.legacy_root()
    assert str(root).endswith("unified-search")


def test_env_beats_config_yaml_db_path(monkeypatch, tmp_path):
    """ARGO_STATE_DIR 是硬开关：磁盘 config.yaml 的 db_path 不能盖掉它。

    否则「设了 env 却仍写进 ~/.cache」会让测试隔离和只读环境形同虚设。
    """
    from cache import SearchCache
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    assert SearchCache()._db_path == str(tmp_path / "cache.db")


# ─── 各模块统一落在同一根目录 ─────────────────────────────────────────────────

_EXPECT = {
    "argo_engine_registry": ("HEALTH_STATE_PATH", "argo_engine_health.json"),
    "circuit_breaker": ("STATE_PATH", "circuit_breaker.json"),
    "telemetry": ("_telemetry_dir", "telemetry"),
    "engine_admission": ("DEFAULT_ADMISSION_DIR", "admission"),
    "adaptive": ("DB_PATH", "adaptive.db"),
    "lang_pref": ("STATE_PATH", "lang_habit.json"),
    "quota": ("QUOTA_STATE_DIR", "."),
    "health_probe": ("DB_PATH", "health.db"),
}


@pytest.mark.parametrize("mod_name,attr_name",
                         [(m, a) for m, (a, _) in _EXPECT.items()])
def test_module_paths_under_state_root(monkeypatch, tmp_path, mod_name, attr_name):
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    sys.modules.pop(mod_name, None)
    mod = __import__(mod_name)
    got = getattr(mod, attr_name)
    if callable(got):
        # telemetry._telemetry_dir 是惰性函数（每次调用重读 env）
        got = got()
    expect_name = _EXPECT[mod_name][1]
    expect = tmp_path if expect_name == "." else tmp_path / expect_name
    assert str(got) == str(expect)


def test_cache_and_config_default_under_state_root(monkeypatch, tmp_path):
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    for mod_name in ("config", "cache"):
        sys.modules.pop(mod_name, None)
    import cache
    import config
    expect = str(tmp_path / "cache.db")
    assert cache.DEFAULT_DB_PATH == expect
    assert config.DEFAULT_CONFIG["cache"]["db_path"] == expect


# ─── 目录创建与容错 ───────────────────────────────────────────────────────────

def test_ensure_state_dir_creates(monkeypatch, tmp_path):
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    d = argo_paths.ensure_state_dir("a", "b")
    assert d.is_dir()


def test_ensure_state_dir_failopen_on_unwritable(monkeypatch, tmp_path):
    """不可创建时 fail-open 返回路径，不在 import 期就崩。"""
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    d = argo_paths.ensure_state_dir("x")
    os.chmod(d, 0o500)
    try:
        got = argo_paths.ensure_state_dir("x", "child")
        assert str(got).endswith("child")
    finally:
        os.chmod(d, 0o700)


def test_state_path_is_pure_join(monkeypatch, tmp_path):
    """state_path 只读拼接，不产生副作用（不建目录）。"""
    monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
    p = argo_paths.state_path("nope", "deep.json")
    assert p == tmp_path / "nope" / "deep.json"
    assert not p.exists()
