#!/usr/bin/env python3
"""test_state_integrity.py — 状态层完整性回归（2026-09-12 缺陷批次）。

背景：一次量化诊断发现 argo 的状态层（配额 / 熔断）在多进程并发下
系统性损坏，且错误率计算方式分子分母不同窗口。四类缺陷各自独立，
但共同特征是「失败静默」——写坏了没人知道，只表现为计数偏低或
统计值反常（errors > used）。

本文件把当时的复现固化成回归，防止再退化：

  1. 原子写唯一性：并发写不崩溃、不丢增量（旧实现固定 tmp 名，
     实测 6 进程 × 60 次写 = 崩溃 235 次 / 状态丢失 68%）
  2. 错误率窗口一致性：分子分母同窗口，恒 ≤ 1.0
     （旧实现 1 次成功 + 历史 3 次错 → 3.0 = 300%）
  3. errors 永不超窗口 calls（旧实现真实状态里 github used=1/errors=78）
  4. 批量记账只写入文件一次（N 引擎 → 1 次写）
  5. bin/argo 在 Python 3.9 可执行（entrypoint 必须 3.8+ 语法）
"""

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
BIN_ARGO = Path(__file__).resolve().parent.parent / "bin" / "argo"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _state_dir() -> str:
    return tempfile.mkdtemp(prefix="argo-state-integrity-")


@pytest.fixture(autouse=True)
def _restore_state_dir():
    """快照并还原 ARGO_STATE_DIR，并重载状态模块复位模块级路径常量。

    本文件 4 处直接写 `os.environ["ARGO_STATE_DIR"]` 而不还原，且
    `importlib.reload(quota)` 会把 `quota.QUOTA_STATE_PATH` **永久**指向
    测试临时目录——同进程内排在后面的用例因此对执行顺序敏感（单独跑绿、
    与其它模块组合跑红）。测试不许改坏邻居的环境，这里环境与模块态一并复位。
    """
    import importlib

    prev = os.environ.get("ARGO_STATE_DIR")
    yield
    if prev is None:
        os.environ.pop("ARGO_STATE_DIR", None)
    else:
        os.environ["ARGO_STATE_DIR"] = prev
    mod = sys.modules.get("quota")
    if mod is not None:
        importlib.reload(mod)


# ── 1. 原子写：唯一 tmp 名 ────────────────────────────────────────────────────

class TestAtomicWrite:
    def test_concurrent_writes_do_not_crash_or_lose(self):
        """多进程并发写同一状态文件：零崩溃 + 零丢失。

        用 spawn 起真实子进程——线程/同进程复现不出这个 bug（旧实现
        的 threading.Lock 在同进程内是有效的，跨进程才互相删 tmp）。
        """
        d = _state_dir()
        n_proc, n_each = 6, 40
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(n_proc) as pool:
            results = pool.map(_concurrent_writer, [(d, n_each)] * n_proc)

        crashed = sum(r[1] for r in results)
        assert crashed == 0, f"并发写崩溃 {crashed} 次"

        state = json.loads((Path(d) / "quota.json").read_text())
        got = state.get("race_engine", {}).get("used", 0)
        assert got == n_proc * n_each, f"状态丢失：期望 {n_proc * n_each} 实际 {got}"

    def test_helper_survives_unlocked_concurrent_writers(self):
        """不带文件锁的并发写：atomic_write_json 自身必须零崩溃。

        这一条测的是 `atomic_write_json` 这个函数本身，而不是它的调用方。
        `quota.record_many` 有 file_lock 串行化，会把「固定 tmp 名」的 bug
        掩盖掉——但熔断器、语言偏好、v2ex 缓存都直接用它，外面没有锁。
        所以要在这里单独证明「每次写入都换一个临时文件名」确实生效。

        用真实子进程（spawn）：同进程线程复现不出这个 bug。
        旧实现（`str(path) + ".tmp"`）实测 8 进程 × 200 次 = 崩溃 1032 次。
        """
        d = _state_dir()
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(8) as pool:
            crashed = pool.map(_hammer_atomic_write, [(d, i, 150) for i in range(8)])
        assert sum(crashed) == 0, f"并发原子写崩溃 {sum(crashed)} 次"

        # 最终文件必须是完整可解析的 JSON（不能是 torn/半截写）
        final = json.loads((Path(d) / "shared.json").read_text())
        assert "proc" in final and "n" in final
        # 不留下任何残余 tmp
        assert [p.name for p in Path(d).glob("*.tmp")] == []

    def test_no_leftover_tmp_files(self):
        d = _state_dir()
        os.environ["ARGO_STATE_DIR"] = d
        import argo_paths
        target = Path(d) / "sample.json"
        argo_paths.atomic_write_json(target, {"a": 1})
        argo_paths.atomic_write_json(target, {"a": 2})
        assert json.loads(target.read_text()) == {"a": 2}
        leftovers = [p.name for p in Path(d).glob("*.tmp")]
        assert leftovers == [], f"残余临时文件: {leftovers}"

    def test_failure_cleans_only_own_tmp(self):
        """序列化失败时清理自己的 tmp，且不破坏既有文件。"""
        d = _state_dir()
        import argo_paths
        target = Path(d) / "keep.json"
        argo_paths.atomic_write_json(target, {"ok": True})

        class Unserializable:
            pass

        with pytest.raises(TypeError):
            argo_paths.atomic_write_json(target, {"bad": Unserializable()})
        # 既有内容未被损坏
        assert json.loads(target.read_text()) == {"ok": True}
        assert [p.name for p in Path(d).glob("*.tmp")] == []


def _hammer_atomic_write(args):
    """子进程：反复原子写同一文件，返回崩溃次数。"""
    d, proc_id, n = args
    sys.path.insert(0, str(SCRIPT_DIR))
    import argo_paths
    target = Path(d) / "shared.json"
    crashed = 0
    for i in range(n):
        try:
            argo_paths.atomic_write_json(target, {"proc": proc_id, "n": i})
        except Exception:
            crashed += 1
    return crashed


def _concurrent_writer(args):
    d, n_each = args
    os.environ["ARGO_STATE_DIR"] = d
    sys.path.insert(0, str(SCRIPT_DIR))
    import quota as Q
    mgr = Q.get_quota_manager()
    ok = crashed = 0
    for _ in range(n_each):
        try:
            mgr.record("race_engine", success=True)
            ok += 1
        except Exception:
            crashed += 1
    return ok, crashed


# ── 2/3. 错误率与滑动窗口计算方式 ────────────────────────────────────────────────

class TestErrorRateWindow:
    def setup_method(self):
        os.environ["ARGO_STATE_DIR"] = _state_dir()
        import importlib
        import quota
        importlib.reload(quota)
        self.Q = quota
        self.mgr = quota.get_quota_manager()

    def test_error_rate_within_unit_interval(self):
        """错误率恒在 [0,1]：旧实现可算出 3.0。"""
        for _ in range(10):
            self.mgr.record("e1", success=True)
        for _ in range(3):
            self.mgr.record("e1", success=False)
        rate = self.mgr.get_error_rate("e1")
        assert 0.0 <= rate <= 1.0
        assert rate == pytest.approx(3 / 13, abs=1e-3)

    def test_no_samples_means_zero_not_stale_errors(self):
        """窗口内无调用 = 无观测 → 0.0，而不是拿历史 errors 去除。"""
        now = time.time()
        self.mgr._state["stale"] = {
            "used": 5, "limit": 0, "calls": [now - 7200],
            "errors": 9, "last_reset": now, "total_cost": 0.0,
        }
        assert self.mgr.get_error_rate("stale") == 0.0

    def test_errors_never_exceed_window_calls(self):
        """errors 恒 ≤ 窗口 calls：旧实现真实状态出现 used=1/errors=78。"""
        now = time.time()
        st = {"used": 1, "calls": [now - 10, now - 5], "errors": 78,
              "last_reset": now, "total_cost": 0.0}
        self.Q.QuotaManager._prune_locked(st, now)
        assert st["errors"] <= len(st["calls"])

    def test_prune_clears_window_entirely(self):
        now = time.time()
        st = {"used": 3, "calls": [now - 7200] * 4, "errors": 4,
              "last_reset": now, "total_cost": 0.0}
        self.Q.QuotaManager._prune_locked(st, now)
        assert st["calls"] == []
        assert st["errors"] == 0

    def test_used_is_quota_counter_not_pruned(self):
        """used 是配额计算方式：1h 修剪不得让它回退（否则配额被无限复用）。"""
        for _ in range(3):
            self.mgr.record("q1", success=True)
        used_before = self.mgr._state["q1"]["used"]
        now = time.time()
        self.mgr._state["q1"]["calls"] = [now - 7200] * 3
        self.Q.QuotaManager._prune_locked(self.mgr._state["q1"], now)
        assert self.mgr._state["q1"]["used"] == used_before


# ── 4. 批量记账 ──────────────────────────────────────────────────────────────

class TestBatchRecording:
    def setup_method(self):
        os.environ["ARGO_STATE_DIR"] = _state_dir()
        import importlib
        import quota
        importlib.reload(quota)
        self.Q = quota
        self.mgr = quota.get_quota_manager()

    def test_record_many_aggregates_all_engines(self):
        self.mgr.record_many([("a", True), ("b", False), ("c", True)])
        assert self.mgr._state["a"]["used"] == 1
        assert self.mgr._state["b"]["errors"] == 1
        assert self.mgr._state["c"]["used"] == 1

    def test_record_many_writes_once(self):
        """N 个引擎只写入文件一次（旧实现 N 次全量写）。"""
        writes = []
        orig = self.Q.argo_paths.atomic_write_json

        def counting(path, payload, **kw):
            writes.append(1)
            return orig(path, payload, **kw)

        self.Q.argo_paths.atomic_write_json = counting
        try:
            self.mgr.record_many([(f"eng{i}", True) for i in range(8)])
        finally:
            self.Q.argo_paths.atomic_write_json = orig
        assert len(writes) == 1, f"期望 1 次落盘，实际 {len(writes)}"

    def test_record_many_empty_is_noop(self):
        writes = []
        orig = self.Q.argo_paths.atomic_write_json

        def counting(path, payload, **kw):
            writes.append(1)
            return orig(path, payload, **kw)

        self.Q.argo_paths.atomic_write_json = counting
        try:
            self.mgr.record_many([])
        finally:
            self.Q.argo_paths.atomic_write_json = orig
        assert writes == []

    def test_record_still_works_as_single(self):
        self.mgr.record("solo", success=False)
        assert self.mgr._state["solo"]["used"] == 1
        assert self.mgr._state["solo"]["errors"] == 1


# ── 5. entrypoint 运行时兼容 ─────────────────────────────────────────────────

class TestEntrypointRuntime:
    def test_bin_argo_imports_under_python39(self):
        """bin/argo 必须能被 3.9 解释器执行到 usage 输出。

        旧实现 `def _engine_count() -> int | None:` 在模块级求值期抛
        TypeError，让「挑更高版本解释器」的 _pick_python() 根本没机会跑。
        """
        py39 = "/usr/bin/python3"
        if not os.path.exists(py39):
            pytest.skip("系统 3.9 解释器不存在")
        r = subprocess.run(
            [py39, str(BIN_ARGO), "--help"],
            capture_output=True, text=True, timeout=60,
        )
        assert "unsupported operand type" not in r.stderr, r.stderr
        assert "usage" in r.stdout.lower() or "Usage" in r.stdout, r.stdout

    def test_bin_argo_unknown_subcommand_is_clean_error(self):
        """未知子命令给可读提示，不是 traceback。"""
        r = subprocess.run(
            [sys.executable, str(BIN_ARGO), "definitely-not-a-cmd"],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 1
        assert "Unknown subcommand" in r.stderr
        assert "Traceback" not in r.stderr

    def test_bin_argo_has_future_annotations(self):
        """entrypoint 必须带 `from __future__ import annotations`。

        它是唯一先被低版本解释器执行、再切高版本的文件，少了这行
        任何 `X | None` 注解都会在 3.9 上炸。
        """
        src = BIN_ARGO.read_text(encoding="utf-8")
        assert "from __future__ import annotations" in src


# ── 6. 跨文件原子写唯一来源 ──────────────────────────────────────────────────

class TestNoHandRolledTmp:
    """禁止再出现手写的固定名 `.tmp` + replace 模式。"""

    HAND_ROLLED = [
        "scripts/quota.py",
        "scripts/circuit_breaker.py",
        "scripts/lang_pref.py",
        "scripts/v2ex_nodes.py",
    ]

    def test_no_fixed_tmp_suffix_pattern(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for rel in self.HAND_ROLLED:
            src = (root / rel).read_text(encoding="utf-8")
            for ln, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if 'with_suffix(".tmp")' in line or '.tmp"' in line and "mkstemp" not in line:
                    offenders.append(f"{rel}:{ln}: {line.strip()}")
        assert offenders == [], (
            "发现手写固定名 .tmp（应用 argo_paths.atomic_write_json）:\n"
            + "\n".join(offenders)
        )


class TestQuotaDropsForeignEngineNames:
    """配额账本只为「配置里真的存在的引擎」留行（2026-09-27）。

    实测（改前）：本机 quota.json 419 条里 195 条是测试/探针残留
    （`eng_x`、`benchmark_a`、`p_<8hex>`、`nope`…），只增不减——每跑一次测试或
    评测就多几行并永久留存。它不改变配额判断（那些名字不会被路由到），但让
    「引擎用量账本」从根上不可信：近一半的行不指向任何真实来源。

    修法的两条边界同样重要，都在本文件锁住：
      - **不按「现在能否路由」删**：临时缺密钥/熔断禁用的源恢复后要接着用这份账。
      - **不删本次正在写入的名字**：测试替身与评测脚本靠这个口子写非配置名，
        删掉会让「写进了什么」与「读回来什么」不一致（实测三处并发/窗口用例
        因此变红）。
    """

    def test_drops_only_names_absent_from_config(self):
        import quota
        state = {
            "anysearch": {"used": 1, "calls": []},
            "definitely-not-in-config": {"used": 9, "calls": []},
        }
        out = quota._prune_foreign_engines(state, set())
        assert "anysearch" in out, "真实引擎被误删"
        assert "definitely-not-in-config" not in out, "配置外的残留没被丢掉"

    def test_keeps_names_written_in_this_batch(self):
        """本次正在写入的名字永不删——否则测试替身/评测脚本写不下去。"""
        import quota
        state = {"quick_primary": {"used": 1, "calls": []},
                 "benchmark_a": {"used": 2, "calls": []}}
        out = quota._prune_foreign_engines(state, {"quick_primary"})
        assert "quick_primary" in out
        assert "benchmark_a" not in out

    def test_fail_safe_when_config_unavailable(self):
        """拿不到配置就不删（判据不可用时不猜）。"""
        import quota
        state = {"whatever": {"used": 1, "calls": []}}
        orig = quota._config_engine_names
        quota._config_engine_names = lambda: None
        try:
            out = quota._prune_foreign_engines(state, set())
        finally:
            quota._config_engine_names = orig
        assert out == state, "判据不可用时不该丢任何行"

    def test_prune_is_wired_into_the_write_path(self):
        """形态门：自愈必须挂在唯一写入口，否则它只是个没人调的函数。"""
        src = (SCRIPT_DIR / "quota.py").read_text(encoding="utf-8")
        assert "_prune_foreign_engines(" in src, "自愈没有接线"
        # 取**最后一次**出现（调用点在定义之后）：定义那次不带宽进 protect。
        call = src.rsplit("_prune_foreign_engines(", 1)[1][:200]
        assert "self._state" in call, "自愈没有作用在状态上"
        assert "{name for name" in call or "entries" in call, (
            "protect 没传本次写入的名字：自愈会删掉自己刚写的行")
