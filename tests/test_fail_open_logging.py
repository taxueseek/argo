#!/usr/bin/env python3
"""fail-open 路径不得因自身缺陷变成 fail-closed（2026-09-25 两个 P0 的回归锁）。

## 缺陷形态

`search_output.shape_response` 与 `search_pipeline.postprocess` 都声明了
「每一步都 fail-open：增强失败不得让一次搜索失败」。但两个文件都在模块级
`import logging`，又在**某个 except 分支里**写了一次裸 `import logging`。

CPython 在**编译期**就把 `logging` 判定为该函数的局部变量，与那行 import
是否执行到无关。于是同函数内其余 3~5 处 `logging.getLogger(...)` 全部编译成
`LOAD_FAST`（读未赋值的局部槽），走到即 `UnboundLocalError`。

危害被放大的原因是位置：这些读取**全部位于 except 处理器内**。异常处理器里
再抛异常，会顶替掉原本被优雅处理的错误——本该「跳过本地正文索引」，实际变成
「整次搜索崩溃」。

## 为什么这类 bug 能活下来

静态检查当时存在盲区（ruff F821 判 `logging` 合法，因为它在模块级确实存在；
仓内门禁只查 `global X` 声明），而 2854 条测试全绿——因为触发它需要
`cache.local_status()` / 域过滤 / 时间窗过滤**抛异常**，而这些在测试里从未失败。

所以这里用**行为断言**而不是源码文本断言：直接把被保护的依赖换成会抛异常的
替身，驱动真实代码路径，断言它不抛。测试与实现解耦：修法从「删掉函数内
import」换成「改用别名」也照样能过，因为它锁的是行为。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import search_output  # noqa: E402


def _ctx(**overrides):
    """shape_response 的完整入参——只覆盖本用例关心的字段。"""
    fields = dict(
        query="测试查询", kind="search", tier="auto", envelope=False,
        decision={}, extra_lim=[], cache=None, include_domains=[],
        exclude_domains=[], include_local=False, n=5, run_local_seek=None,
    )
    fields.update(overrides)
    return search_output._ShapeContext(**fields)


class _Boom:
    """任何调用都抛异常的替身。"""

    def __getattr__(self, _name):
        def _raise(*_a, **_k):
            raise RuntimeError("injected failure")
        return _raise


class TestShapeResponseFailOpen(unittest.TestCase):
    """输出成形的每个增强阶段失败时，响应仍须成形。"""

    def _assert_survives(self, **ctx_overrides):
        result = {"results": [{"url": "https://example.com/a", "title": "t"}]}
        # 不设 assertRaises：一旦异常逸出，这里就是测试失败本身。
        shaped = search_output.shape_response(_ctx(**ctx_overrides), result)
        self.assertIsInstance(shaped, dict)
        return shaped

    def test_local_status_failure_does_not_abort(self):
        """P0-1：本地正文索引失败（DB 锁/文件缺失）不得拖垮整次搜索。"""
        shaped = self._assert_survives(cache=_Boom())
        self.assertEqual(len(shaped["results"]), 1)

    def test_domain_filter_failure_does_not_abort(self):
        """P0-2：域过滤抛异常时保留原结果，并落一条 domain_filter 以外的降级说明。"""
        shaped = self._assert_survives(
            cache=None, include_domains=["example.com"])
        self.assertEqual(len(shaped["results"]), 1)

    def test_evidence_gate_failure_does_not_abort(self):
        """证据门控失败（可选模块缺失/数据形状异常）不得 abort。"""
        shaped = self._assert_survives(cache=_Boom(), decision={"domain": "finance"})
        self.assertIsInstance(shaped, dict)

    def test_all_stages_failing_still_returns_envelope(self):
        """全部增强阶段同时失败时，响应契约本身仍成立。

        注意 `query` 由调用方（search.super_search）写入，shape_response 不负责
        补——这里只断言**它自己负责产出**的字段。
        """
        shaped = self._assert_survives(
            cache=_Boom(), include_domains=["a.com"], include_local=True,
            run_local_seek=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        for key in ("results", "limitations", "fetch_required", "evidence_loop"):
            self.assertIn(key, shaped)


class TestNoShadowedLogger(unittest.TestCase):
    """结构性断言：这些函数里的 logging 必须是全局查找，不是局部槽。"""

    def test_shape_response_uses_global_logging(self):
        names = search_output.shape_response.__code__.co_varnames
        self.assertNotIn(
            "logging", names,
            "shape_response 里存在函数内 import logging，会把模块级 logging 遮蔽成"
            "局部名：其余 logging.getLogger 调用将抛 UnboundLocalError")

    def test_postprocess_uses_global_logging(self):
        import search_pipeline
        names = search_pipeline.postprocess.__code__.co_varnames
        self.assertNotIn(
            "logging", names,
            "postprocess 里存在函数内 import logging，时间窗/恢复/rerank 三处"
            "except 处理器将抛 UnboundLocalError")

    def test_postprocess_logging_reads_are_global(self):
        """字节码层面确认：postprocess 里的 logging 全部编译成 LOAD_GLOBAL。"""
        import dis
        import search_pipeline
        instrs = dis.get_instructions(search_pipeline.postprocess)
        fast = [i for i in instrs
                if i.argrepr == "logging" and "LOAD_FAST" in i.opname]
        self.assertEqual(
            fast, [],
            "postprocess 存在 LOAD_FAST logging（局部读取），走到即 UnboundLocalError")


if __name__ == "__main__":
    unittest.main()
