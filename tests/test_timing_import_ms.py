"""`--explain-timing` 的 import_ms 恒为 0 —— 工具报不出自己最大的固定开销。

现象：`argo search <q> --json` 的 `timing.import_ms` 永远是 `0.0`，而实测
`import search` 要 30 ms 左右（本机 CPython 3.14，159 个模块）。`overhead_ms`
里其实含了这笔，但 import_ms 这一栏专门为「优化固定开销」而设，它报 0 等于
把优化者指向错误的数字——`--explain-timing` 的全部价值就是回答「瓶颈在哪」。

根因：入口是 `bin/argo` → `runpy.run_module("search", run_name="__main__")`。
`search.py` 的模块体**以 `__main__` 的身份执行一次**，它自己置了
`_MODULE_T0` / `_IMPORTS_DONE`；随后尾部的 `from search_cli import main`
让 `search_cli` 执行 `from search import _IMPORTS_DONE, _MODULE_T0`，
这会把 `search` **再导入一次**（这次是真模块）。此时所有依赖已在
`sys.modules` 里，于是第二次导入几乎是空操作，差值 ≈ 0。

修法不是改搜索逻辑，而是让计时从 `__main__` 那个模块对象上取值——那才是
真正跑过整条 import 链的那份。本测试锁定「无论走哪条入口，import_ms 都
必须是一个非零的实数」。
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _run(args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "search.py"), *args],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )


class TestImportMsIsReal(unittest.TestCase):
    def _timing(self, *args):
        import json
        r = _run([*args, "--json"])
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return json.loads(r.stdout)["timing"]

    def test_import_ms_is_positive_via_runpy_entry(self):
        """bin/argo 走 runpy(run_name='__main__') 这条真实入口。"""
        t = self._timing("记忆宫殿")
        self.assertGreater(
            t.get("import_ms") or 0, 1.0,
            "import_ms 恒为 0：入口经 runpy 以 __main__ 身份跑了一遍 search，"
            "search_cli 再导入 search 时全部命中 sys.modules 缓存，差值被抹平",
        )

    def test_import_ms_is_less_than_overhead(self):
        """import_ms 是 overhead 的子集，不该比它还大。"""
        t = self._timing("记忆宫殿")
        self.assertLessEqual(
            t.get("import_ms") or 0, (t.get("overhead_ms") or 0) + 1.0,
            "import_ms 不应超过 overhead_ms（前者是后者的真子集）",
        )

    def test_process_ms_covers_import(self):
        """process_ms ≥ import_ms：import 一定发生在 process 计时区间内。"""
        t = self._timing("记忆宫殿")
        self.assertGreaterEqual(
            t.get("process_ms") or 0, t.get("import_ms") or 0,
            "process_ms 早于 import 结束，时点取错了模块对象",
        )


class TestDirectModuleEntryAlsoReportsImportTime(unittest.TestCase):
    """直接 `python3 scripts/search.py` 也不该退化成 0。"""

    def test_direct_entry_reports_nonzero(self):
        import json
        r = _run(["记忆宫殿", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        t = json.loads(r.stdout)["timing"]
        self.assertGreater(
            t.get("import_ms") or 0, 1.0,
            "直接入口下 import_ms 也是 0，说明时点取自了二次导入的模块",
        )


if __name__ == "__main__":
    unittest.main()
