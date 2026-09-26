#!/usr/bin/env python3
"""
test_argo_paths.py — 本地状态目录唯一来源回归测试

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
    "usage_log": ("usage_log_dir", "usage_log"),
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
        # usage_log.usage_log_dir 是惰性函数（每次调用重读 env）
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


# ─── 跨进程文件锁（POSIX flock / Windows msvcrt 双实现）───────────────────────

import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402


class TestFileLock:
    """锁的契约：真互斥、能释放、拿不到时 fail-open（绝不阻断搜索主路径）。

    为什么两套实现都要测：Windows 没有 fcntl，此前这里直接 fail-open——于是
    「6 进程 × 60 次 record 状态丢失 77%」那类丢更新在 Windows 上原样复现，而
    调用方看不出任何异常（没有日志，锁看起来"加了"）。msvcrt 分支必须在 macOS
    上也能被覆盖，否则它永远是「没被跑过的一行」。
    """

    def _lock_path(self, tmp_path):
        return tmp_path / "state.json"

    def _fake_msvcrt(self, recorder):
        return types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=recorder)

    def test_posix_mutual_exclusion(self, tmp_path):
        """同一把锁的第二个持有者必须等第一个释放——这是锁存在的全部意义。"""
        path = self._lock_path(tmp_path)
        order: list[str] = []

        def worker(tag: str, hold: float) -> None:
            with argo_paths.file_lock(path, timeout=5.0):
                order.append(f"{tag}-in")
                time.sleep(hold)
                order.append(f"{tag}-out")

        a = threading.Thread(target=worker, args=("A", 0.15))
        b = threading.Thread(target=worker, args=("B", 0.0))
        a.start()
        time.sleep(0.02)          # 让 A 先拿到
        b.start()
        a.join()
        b.join()
        assert order == ["A-in", "A-out", "B-in", "B-out"], f"未互斥：{order}"

    def test_windows_impl_locks_first_byte(self, monkeypatch, tmp_path):
        """Windows 实现：msvcrt.locking 锁首字节（空文件先写占位字节）。"""
        calls: list[tuple[int, int, int]] = []
        monkeypatch.setitem(sys.modules, "msvcrt", self._fake_msvcrt(
            lambda fd, mode, n: calls.append((mode, n, os.fstat(fd).st_size))))

        impl = argo_paths._make_lock_impl(None)          # None = 模拟「没有 fcntl」
        assert impl is not None, "Windows 分支丢了对 None 入参的处理"
        acquire, release = impl

        lock_file = self._lock_path(tmp_path)
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            acquire(fd)
            release(fd)
        finally:
            os.close(fd)
        assert [c[0] for c in calls] == [2, 0], f"未按 LK_NBLCK/LK_UNLCK 加解锁：{calls}"
        assert all(c[1] == 1 for c in calls), "锁区间应为 1 字节"
        assert calls[0][2] >= 1, "空文件应先写占位字节，否则锁区间为空"

    def test_windows_path_runs_body_and_releases(self, monkeypatch, tmp_path):
        """整条 file_lock 在 Windows 实现下：加锁 → 临界区 → 解锁，顺序不能乱。"""
        trace: list[str] = []
        monkeypatch.setitem(sys.modules, "msvcrt", self._fake_msvcrt(
            lambda fd, mode, n: trace.append("lock" if mode == 2 else "unlock")))
        real_impl = argo_paths._make_lock_impl      # 先取原件，否则 patch 后自我递归
        monkeypatch.setattr(argo_paths, "_make_lock_impl", lambda _fcntl: real_impl(None))
        with argo_paths.file_lock(self._lock_path(tmp_path), timeout=1.0):
            trace.append("body")
        assert trace == ["lock", "body", "unlock"], f"Windows 路径顺序不对：{trace}"

    def test_timeout_fails_open(self, monkeypatch, tmp_path):
        """抢不到锁时按超时放行，而不是抛异常——锁是保护层，不该成为单点故障。"""
        def always_busy(fd, mode, n):
            raise OSError("被占用")

        monkeypatch.setitem(sys.modules, "msvcrt", self._fake_msvcrt(always_busy))
        real_impl = argo_paths._make_lock_impl
        monkeypatch.setattr(argo_paths, "_make_lock_impl", lambda _fcntl: real_impl(None))
        entered = False
        t0 = time.monotonic()
        with argo_paths.file_lock(self._lock_path(tmp_path), timeout=0.05):
            entered = True
        assert entered, "拿不到锁时没有放行（会阻断主路径）"
        assert time.monotonic() - t0 >= 0.05, "应在 timeout 之后才放行"

    def test_unknown_platform_fails_open(self, monkeypatch, tmp_path):
        """两套锁都不可用的平台（不认识的系统）同样 fail-open。"""
        monkeypatch.setattr(argo_paths, "_make_lock_impl", lambda _fcntl: None)
        with argo_paths.file_lock(self._lock_path(tmp_path), timeout=0.01) as _:
            pass    # 只要不抛异常即可

    def test_lock_file_is_separate_from_data_file(self, tmp_path):
        """锁落在独立的 .lock 文件上：数据文件靠 os.replace 整体替换，若锁与数据
        同 inode，替换后新进程会锁到另一个 inode 而形同无锁。"""
        data = tmp_path / "quota.json"
        with argo_paths.file_lock(data, timeout=1.0):
            assert (tmp_path / ".quota.json.lock").exists(), "锁文件未落在独立路径"
            assert not data.exists(), "保护一个尚未创建的数据文件不该先建它"
