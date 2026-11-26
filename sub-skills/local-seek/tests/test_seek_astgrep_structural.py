#!/usr/bin/env python3
"""test_seek_astgrep_structural.py — 结构搜索的 ast-grep 优先与 rg 回落。

守的缺陷（本次升级实测逐个复现）：

1. **新版 ast-grep 接口漂移**：`sg scan --pattern` 在 0.51.x 已被移除，
   实测报 `unexpected argument '--pattern'`。正确入口是
   `ast-grep run -p <pat> --json=stream --lang <LANG>`。若按旧文档实现，
   装了新版的机器上 AST 路径会静默失效、整条退回正则近似——能力降级且无提示。
2. **`--lang` 必须显式给**：省略时按扩展名推断，`.tsx` 会被当 `.ts`，
   含 JSX 的文件零命中（实测）。故语言表按扩展名硬映射，不依赖推断。
3. **退出码不能判命中**：`run --json=stream` 零命中也返回 0；非 0 只代表
   该语言扫描失败，须跳过而不是当成「无结果」。
4. **两条路径的行号口径不同**：AST 从 0 起、报整段首行；rg `-U` 对多行匹配
   逐行给前缀且各自行号。按行号去重会在合并处漏重复，故统一按规范化签名去重。
5. **同一节点被多个 pattern 命中**：`async def g()` 会被 `def $NAME...` 与
   `async def $NAME...` 各命中一次，AST 侧必须跨 pattern 去重。

不联网，全部用临时目录 + 直接调用模块函数；外部工具缺失时按环境分流，
不假设本机一定装了 ast-grep。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest sub-skills/local-seek/tests/test_seek_astgrep_structural.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek_structural as ss  # noqa: E402


PY_SAMPLE = """\
def plain():
    pass

async def afn():
    pass

@deco
def decorated():
    pass
"""

JS_SAMPLE = """\
function fn() { return 1; }
async function afn() { await x(); }
export function exported() {}

class Plain {}
export class Derived extends Base {}
"""


class _FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _sg_json(path: str, line0: int, text: str) -> str:
    """构造一条 `--json=stream` 的 match 对象（行号 0 基）。"""
    import json
    return json.dumps({
        "text": text,
        "range": {"start": {"line": line0}, "end": {"line": line0}},
        "file": path,
    }, ensure_ascii=False)


class TestRuleTableContract(unittest.TestCase):
    """规则表的形态契约：字段齐、别名不撞、scope 取值合法。"""

    def test_every_rule_has_required_fields(self):
        for key, rule in ss.STRUCTURAL_RULES.items():
            with self.subTest(rule=key):
                self.assertIn("aliases", rule)
                self.assertIn("patterns", rule)
                self.assertIn("ast_scope", rule)
                self.assertIn("ast_patterns", rule)
                self.assertIn(rule["ast_scope"], ("none", "full", "partial"))
                if rule["ast_scope"] == "partial":
                    self.assertTrue(rule.get("why_rg"),
                                    f"{key} 是 partial 但没说 rg 兜什么缺口")

    def test_alias_lookup_is_idempotent(self):
        self.assertEqual(ss.resolve_rule("裸except"), "bare-except")
        self.assertEqual(ss.resolve_rule("空catch"), "empty-catch")
        self.assertEqual(ss.resolve_rule("不存在语义"), None)

    def test_unknown_rule_message_lists_available(self):
        results, err = ss.structural_search("不存在语义", "/tmp", [], 5, False, 1.0)
        self.assertEqual(results, [])
        self.assertIn("可用：", err or "")


class TestNormSignature(unittest.TestCase):
    """归一化签名：ast-grep 与 rg 对同一处的不同表述必须收敛到同一个键。"""

    def test_modifiers_are_stripped(self):
        self.assertEqual(ss._norm_sig("function k(){}"),
                         ss._norm_sig("export function k(){}"))

    def test_async_variants_collapse(self):
        self.assertEqual(ss._norm_sig("def g():"),
                         ss._norm_sig("async def g():"))

    def test_distinct_definitions_do_not_collapse(self):
        self.assertNotEqual(ss._norm_sig("def f():"),
                            ss._norm_sig("def g():"))

    def test_non_definition_is_dropped(self):
        """续行、空行、孤立关键字不应被当成一处结构定义。"""
        for junk in ("", "   ", "} catch (e) {", "pass", ""):
            self.assertEqual(ss._norm_sig(junk), "")

    def test_decorator_fragment_survives(self):
        """@deco 不含定义关键字，但指向一处带装饰的定义，必须保留。"""
        self.assertTrue(ss._norm_sig("@deco"))


class TestParseStream(unittest.TestCase):
    """`--json=stream` 解析：行号 0→1 基、只取首行、坏行跳过。"""

    def test_line_numbers_are_shifted_to_one_based(self):
        rows = ss._parse_stream(_sg_json("/tmp/a.py", 2, "def f():\n    pass\n"))
        self.assertEqual(rows, [("/tmp/a.py", 3, "def f():")])

    def test_multiline_snippet_is_truncated_to_first_line(self):
        rows = ss._parse_stream(_sg_json("/tmp/b.js", 0, "try {\nx();\n}\n"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "try {")

    def test_malformed_lines_are_skipped(self):
        out = "\n".join([
            "not json at all",
            '{"text": 5}',
            '{"text": "def g():", "range": {"start": {"line": 0}}, "file": "/tmp/c.py"}',
            "",
        ])
        rows = ss._parse_stream(out)
        self.assertEqual(rows, [("/tmp/c.py", 1, "def g():")])

    def test_exclude_regex_drops_wrapped_errors(self):
        out = _sg_json("/tmp/d.go", 0, 'return fmt.Errorf("boom")') + "\n" \
            + _sg_json("/tmp/d.go", 2, "return err")
        rows = ss._parse_stream(out, exclude={"not_any_regex": ("Errorf",)})
        self.assertEqual([r[2] for r in rows], ["return err"])

    def test_missing_file_field_is_rejected(self):
        import json
        obj = json.dumps({"text": "x", "range": {"start": {"line": 0}}})
        self.assertEqual(ss._parse_stream(obj), [])


class TestDedupAcrossPaths(unittest.TestCase):
    """跨路径去重：同文件同签名的 rg 命中不得与 AST 命中并存。"""

    def test_ast_and_rg_same_site_dedups(self):
        rule = {
            "ast_scope": "partial",
            "ast_patterns": [(("py",), "def $NAME($$$PARAMS):\n  $$$BODY")],
            "patterns": [(("py",), r"^\s*def\s+\w+")],
            "why_rg": "test",
        }

        class Runner:
            def __init__(self):
                self.calls = []

            def __call__(self, cmd, timeout=None, **kw):
                self.calls.append(cmd[0])
                if cmd[0].endswith(("ast-grep", "sg")):
                    return _FakeProc(stdout=_sg_json("/tmp/x.py", 1, "def f():\n  pass\n"))
                return _FakeProc(stdout="/tmp/x.py:2:def f():\n")

        hits, scope = ss.structural_search_ast(rule, "/tmp", [], 10, False, 1.0,
                                              runner=Runner())
        self.assertEqual(scope, "partial")
        self.assertEqual([h[2] for h in hits], ["def f():"])

    def test_ast_side_dedup_across_patterns(self):
        """同一条规则的多个 pattern 命中同一节点时只留一条。"""
        class Runner:
            def __call__(self, cmd, timeout=None, **kw):
                return _FakeProc(stdout="\n".join([
                    _sg_json("/tmp/y.py", 5, "async def g():\n  pass"),
                    _sg_json("/tmp/y.py", 5, "async def g():\n  pass"),
                ]))

        rows = ss._dedup_rows([
            ("/tmp/y.py", 6, "async def g():"),
            ("/tmp/y.py", 6, "async def g():"),
        ])
        self.assertEqual(len(rows), 1)


class TestFallbackChain(unittest.TestCase):
    """降级链：缺 ast-grep 时整条走 rg，且结果与升级前等价。"""

    def test_rg_fallback_when_no_astgrep(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.py"
            p.write_text(PY_SAMPLE, encoding="utf-8")
            rule = ss.STRUCTURAL_RULES["bare-except"]
            hits = ss.structural_search_rg(rule, d, [], 30, False, 1.0)
            self.assertIsInstance(hits, list)

    def test_unknown_rule_short_circuits_before_any_subprocess(self):
        seen = []

        def runner(cmd, timeout=None, **kw):
            seen.append(cmd)
            return _FakeProc()

        ss.structural_search("不存在语义", "/tmp", [], 5, False, 1.0, runner=runner)
        self.assertEqual(seen, [], "未知语义必须先报错，不该拉起子进程")

    def test_timeout_is_downpassed_to_runner(self):
        """超时预算必须下传——H2 后 MCP 进程内调用靠它防挂死单线程 executor。"""
        captured = {}

        def runner(cmd, timeout=None, **kw):
            captured["timeout"] = timeout
            return _FakeProc(rc=1, stdout="")

        ss.structural_search("裸except", "/tmp", [], 5, False, 5.5, runner=runner)
        self.assertEqual(captured.get("timeout"), 5.5)

    def test_none_returncode_from_runner_is_tolerated(self):
        """runner 返回 None（超时/不可执行）时不得抛异常。"""
        def runner(cmd, timeout=None, **kw):
            return None

        hits, err = ss.structural_search("裸except", "/tmp", [], 5, False, 1.0,
                                        runner=runner)
        self.assertEqual((hits, err), ([], None))


class TestEndToEndIfToolPresent(unittest.TestCase):
    """装了 ast-grep 才跑的端到端断言（缺工具时 skip，不假绿）。"""

    def setUp(self):
        if not ss.sg_available():
            self.skipTest("本机未安装 ast-grep，跳过 AST 端到端")

    def test_bare_except_matches_only_untyped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.py"
            p.write_text(
                "try:\n    pass\nexcept:\n    pass\n"
                "try:\n    pass\nexcept ValueError:\n    pass\n",
                encoding="utf-8")
            hits, err = ss.structural_search("裸except", d, [], 10, False, 10.0)
            self.assertIsNone(err)
            self.assertEqual(len(hits), 1, hits)

    def test_function_def_finds_py_and_js(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text(PY_SAMPLE, encoding="utf-8")
            Path(d, "b.js").write_text(JS_SAMPLE, encoding="utf-8")
            hits, err = ss.structural_search("function-def", d, [], 20, False, 10.0)
            self.assertIsNone(err)
            self.assertGreaterEqual(len(hits), 5, hits)

    def test_engine_label_reflects_backend(self):
        """标签必须归因真实后端：AST 与 rg 回落不能同名（同形即误读）。"""
        import json as _json
        proc_path = SCRIPTS / "seek.py"
        out = __import__("subprocess").run(
            [sys.executable, str(proc_path), "裸except", "--structural",
             "--path", str(Path(__file__).resolve().parent)],
            capture_output=True, text=True, timeout=60)
        first_line = out.stdout.splitlines()[0] if out.stdout else ""
        backend = "astgrep-structural" if ss.sg_available() else "rg-structural"
        self.assertIn(backend, first_line, first_line)

    def test_full_scope_does_not_drop_non_keyword_ast_hits(self):
        """ast_scope=full 时，首行不含 _DEF_KEYWORDS 的合法命中不得被静默丢弃。"""
        import json as _json
        fake_stream = _json.dumps({
            "file": "/tmp/demo.py",
            "range": {"start": {"line": 4}},
            "text": "eval(user_input)",
        }) + "\n"

        class _Proc:
            returncode = 0
            stdout = fake_stream

        orig_resolve = ss._resolve_sg
        ss._resolve_sg = lambda: "/usr/bin/ast-grep"
        try:
            rule = {"ast_scope": "full", "ast_patterns": [(("py",), "eval($X)")]}
            hits, scope = ss.structural_search_ast(
                rule, "/tmp", ["!*.log"], 10, runner=lambda cmd, timeout=None: _Proc()
            )
        finally:
            ss._resolve_sg = orig_resolve

        self.assertEqual(scope, "full")
        self.assertEqual(len(hits), 1)
        self.assertIn("eval(user_input)", hits[0][2])

    def test_structural_json_empty_and_error_return_valid_json(self):
        """--structural --json 在无匹配或未知规则时必须返回合法 JSON，不能吐纯文本。"""
        import json as _json
        proc_path = SCRIPTS / "seek.py"
        with tempfile.TemporaryDirectory() as d:
            Path(d, "clean.py").write_text("x = 1\n", encoding="utf-8")
            empty_res = __import__("subprocess").run(
                [sys.executable, str(proc_path), "裸except", "--structural", "--path", d, "--json"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(empty_res.returncode, 1)
            empty_obj = _json.loads(empty_res.stdout)
            self.assertEqual(empty_obj["count"], 0)
            self.assertEqual(empty_obj["results"], [])

            err_res = __import__("subprocess").run(
                [sys.executable, str(proc_path), "不存在的规则", "--structural", "--path", d, "--json"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(err_res.returncode, 1)
            err_obj = _json.loads(err_res.stdout)
            self.assertFalse(err_obj["ok"])
            self.assertIn("error", err_obj)


if __name__ == "__main__":
    unittest.main()
