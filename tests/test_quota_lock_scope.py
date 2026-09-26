"""C2 回归护栏：quota 的写路径必须跨进程安全。

为什么需要：`_mutate_locked` 的 docstring 记录了原始 bug——进程内
threading.Lock 挡不住 CLI / MCP server / 评测脚本三者并行，各自读到旧状态、
各自 +1、后写者覆盖前写者，计数直接丢失。

首次修复只覆盖了 `record_many`（即 `_mutate_locked` 那一处），而
`mark_remote_exhausted` / `clear_remote_exhausted` /
`_refresh_remote_state_locked` / `remote_exhausted_marks` /
`get_remaining_ratio` 的周期重置 / `is_available` 的周期重置这 6 处
仍是「threading.Lock 下 load-mutate-save」，跨进程同样会丢。

本文件用「写方不取文件锁、读方随后读到旧值」来复现：不模拟真多进程
（那要 fork 整套 import 链，慢且脆），而是直接断言**写路径确实持有了
文件锁**——锁的缺失正是竞态的成因，用 mock 打桩比时序复现更确定。
"""

import inspect
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import argo_paths  # noqa: E402
import quota  # noqa: E402


def _quota():
    """实时取 quota 模块，不缓存模块级引用。

    tests/test_state_integrity.py 会对 quota 调 importlib.reload()，reload 会
    换掉 sys.modules 里的模块对象。于是本文件顶部 `import quota` 拿到的引用
    与被测代码内部 `from quota import get_quota_manager` 解析到的**不是同一个
    模块**——patch 打在旧对象上，断言自然全错，且单跑绿、全跑红。
    """
    import quota as _q
    return _q


def _argo_paths():
    """同 _quota：argo_paths 也可能被 reload 类测试换掉，故不缓存引用。"""
    import argo_paths as _ap
    return _ap


def _lock_held_during(manager, method, *args, **kwargs):
    """调用 method，记录 `argo_paths.file_lock` 的进入次数。"""
    calls = []
    ap = _argo_paths()
    real = ap.file_lock

    def spy(path, *a, **kw):
        calls.append(path)
        return real(path, *a, **kw)

    with mock.patch.object(_argo_paths(), "file_lock", spy):
        method(*args, **kwargs)
    return calls


class TestQuotaWritePathsTakeFileLock(unittest.TestCase):
    """每个会改写状态的方法都必须在文件锁内完成 load-modify-write。"""

    def setUp(self):
        q = _quota()
        self.mgr = q.QuotaManager()
        # 隔离状态文件：测试不得碰真实配额账本
        q = _quota()
        self._orig_state_path = q.QUOTA_STATE_PATH
        self._d = tempfile.mkdtemp()
        q.QUOTA_STATE_PATH = Path(self._d) / "quota.json"
        self.addCleanup(self._restore)

    def _restore(self):
        _quota().QUOTA_STATE_PATH = self._orig_state_path

    def test_record_many_takes_file_lock(self):
        """基准行为：这条路径本来就是对的（本测试是护栏，防回归）。"""
        calls = _lock_held_during(self.mgr, self.mgr.record_many, [("eng_x", True)])
        self.assertTrue(calls, "record_many 必须持文件锁")

    def test_mark_remote_exhausted_takes_file_lock(self):
        calls = _lock_held_during(
            self.mgr, self.mgr.mark_remote_exhausted, "eng_x", "quota gone")
        self.assertTrue(calls, "mark_remote_exhausted 改写状态却未取文件锁 → 跨进程会丢")

    def test_clear_remote_exhausted_takes_file_lock(self):
        self.mgr.mark_remote_exhausted("eng_x", "gone")
        calls = _lock_held_during(self.mgr, self.mgr.clear_remote_exhausted, "eng_x")
        self.assertTrue(calls, "clear_remote_exhausted 改写状态却未取文件锁 → 跨进程会丢")

    def test_period_reset_in_remaining_ratio_takes_file_lock(self):
        """周期重置会写状态（used=0 / last_reset=now），同样需要跨进程安全。"""
        self.mgr.mark_remote_exhausted("eng_y", "gone")
        # 造一个已过期的 last_reset，触发重置分支
        self.mgr._state["eng_y"] = {"used": 5, "limit": 10, "calls": [],
                                    "errors": 0, "last_reset": 0,
                                    "total_cost": 0.0}
        self.mgr._profiles.setdefault("eng_y", {})["period"] = "day"
        calls = _lock_held_during(self.mgr, self.mgr.get_remaining_ratio, "eng_y")
        self.assertTrue(calls, "get_remaining_ratio 的周期重置未取文件锁 → 跨进程会丢")


class TestQuotaBatchFlushDoesNotLoseEntries(unittest.TestCase):
    """C3：_QuotaBatch.flush 先清空缓冲再写盘，写失败则这批记账永久丢失。

    **不复用** `quota.get_quota_manager` 单例：全量跑时它早已被别的测试
    创建并绑定当时的 QUOTA_STATE_PATH，走单例等于把本测试的结果交给
    执行顺序决定。这里自建一个指向临时目录的 QuotaManager，用
    `ARGO_STATE_DIR` 之外的显式路径隔离，单独与单独跑结果一致。
    """

    def setUp(self):
        self._d = Path(tempfile.mkdtemp())
        self._orig = _quota().QUOTA_STATE_PATH
        _quota().QUOTA_STATE_PATH = self._d / "quota.json"
        self.addCleanup(self._restore)

    def _restore(self):
        _quota().QUOTA_STATE_PATH = self._orig

    def test_flush_failure_does_not_swallow_entries(self):
        q = _quota()
        batch = q._QuotaBatch()
        batch.add("eng_a", True)
        batch.add("eng_b", False)
        self.assertEqual(len(batch._entries), 2)

        # 把状态文件挪到一个不可写的父目录下：file_lock 与 atomic_write_json
        # 都会真的失败，且不依赖任何 mock 的执行时序。
        q.QUOTA_STATE_PATH = Path("/nonexistent-ar-go/quota.json")
        batch.flush()

        self.assertEqual(
            len(batch._entries), 2,
            "flush 失败后丢掉了缓冲条目：配额被永久少记，且没有任何告警",
        )

    def test_flush_success_clears_entries(self):
        """配对正例：写盘成功时必须清空（否则重复计入）。"""
        q = _quota()
        batch = q._QuotaBatch()
        batch.add("eng_a", True)
        mgr = mock.MagicMock()
        with mock.patch.object(_quota(), "get_quota_manager", return_value=mgr):
            batch.flush()
        self.assertEqual(len(batch._entries), 0, "成功后应清空缓冲")
        mgr.record_many.assert_called_once()


if __name__ == "__main__":
    import unittest
    unittest.main()
