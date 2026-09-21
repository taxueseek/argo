#!/usr/bin/env python3
"""test_context_budget — 输出与文档的上下文预算检查（离线、确定性）。

## 守的是什么

skill 的上下文开销不是「感觉大不大」，而是可以逐字节算出来的。实测（2026-09-13）：

  - 一次 `search --json` = 14.9 KB ≈ 5.0k token，其中**三个视图各说一遍**
    （candidates 4.9 KB + results 2.4 KB + sources 1.0 KB = 78%）；
  - `--no-envelope` 降到 6.8 KB ≈ 2.3k，叠 `-n 3` 降到 4.5 KB ≈ 1.5k；
  - `--list-engines --detail` 全量 = **186 KB ≈ 62k token**（216 条 × ~0.9 KB）。

这些数字本身不会自己变坏，**是字段一个两个加上去后悄悄变坏的**。所以本文件不看
「当前多大」，而是冻结三类会增长的形状：

  1. 顶层 `sources` 投影的字段集（稳定 5 字段，别往链接列表里塞东西）；
  2. `candidates` 单条的字段集（最大的一块，加字段必须显式登记）；
  3. 合成载荷的序列化字节预算（真正挡住「慢慢变大」）。

另锁两侧一致性：SKILL.md 教了 `--no-envelope`，CLI 就必须真的有这个开关；
`--list-engines --detail` 的 `--engine` 过滤必须一直有效（曾静默失效，
单引擎查询被迫付 186 KB）。
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 冻结集：改动这些集合 = 改动 agent 每次调用的上下文成本，必须有意识
SOURCES_FIELDS = {"ref", "title", "url", "engine", "score", "snippet"}
CANDIDATE_FIELDS = {
    "candidate_id", "query", "platform", "backend", "rank", "title", "url",
    "canonical_url", "snippet", "author", "published_at", "language",
    "content_type", "access", "metrics", "provenance", "verification",
    "limitations",
}

# 合成样本：5 条典型结果，序列化后不得越线（字节）
_FIVE_RESULTS = [
    {"title": f"示例结果 {i}", "url": f"https://example.org/a{i}",
     "snippet": "摘要" * 40, "source": "demo", "score": 0.8 - i * 0.01,
     "_engine": "demo", "_rrf_score": 0.02, "consensus_engines": ["demo"],
     "fetch_suggested": True}
    for i in range(5)
]

# 预算阈值：留 ~30% 余量，超了说明输出层在悄悄变胖
BUDGET_SOURCES_BYTES = 1200
BUDGET_CANDIDATES_BYTES = 6500
BUDGET_SKILL_MD_BYTES = 7600

# 子技能文档预算（触发即注入的部分）。
# ego-search 实测 18.8 KB ≈ 6.2k token。2026-09-13 两轮治理：
#   ① 维护者向/低频内容搬到 references/（heredoc 全 helper 速查）；
#   ② 自我描述内容**直接删除**（架构图与隔离表重复、能力对照是自我评价、
#      文件结构是复述文件系统且已漂移）——13.1 KB。
# local-search 的同轮治理：设计原则只留操作性判据、删文件结构树（保留两条
# 不显然的约定），6.3 KB → 5.2 KB。
BUDGET_SUBSKILL_MD_BYTES = {
    "sub-skills/ego-search/SKILL.md": 13500,
    "sub-skills/local-search/SKILL.md": 5600,
    "sub-skills/local-seek/SKILL.md": 7000,
}


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


class TestViewFieldSetsFrozen(unittest.TestCase):
    def test_sources_projection_fields(self):
        from search import build_sources
        got = set(build_sources(_FIVE_RESULTS)[0])
        self.assertEqual(
            got, SOURCES_FIELDS,
            f"sources 投影字段变了（{sorted(got ^ SOURCES_FIELDS)} 差异）——"
            "它是「底部相关链接」形态的稳定投影，加字段会抬高每次调用的上下文成本；"
            "确实需要就同步更新本测试与 references/usage.md 的体积表")

    def test_candidate_fields(self):
        from candidate_envelope import result_to_candidate
        got = set(result_to_candidate(_FIVE_RESULTS[0], "查询", 1))
        self.assertEqual(
            got, CANDIDATE_FIELDS,
            f"candidate 字段变了（{sorted(got ^ CANDIDATE_FIELDS)} 差异）——"
            "candidates 是输出里最大的一块（归档视图），加字段必须显式登记")


class TestSerializedSizeBudget(unittest.TestCase):
    def test_sources_within_budget(self):
        from search import build_sources
        n = _size(build_sources(_FIVE_RESULTS))
        self.assertLessEqual(n, BUDGET_SOURCES_BYTES,
                             f"5 条结果的 sources 视图 {n} B，超预算 {BUDGET_SOURCES_BYTES} B")

    def test_candidates_within_budget(self):
        from candidate_envelope import result_to_candidate
        payload = [result_to_candidate(r, "查询", i + 1)
                   for i, r in enumerate(_FIVE_RESULTS)]
        n = _size(payload)
        self.assertLessEqual(n, BUDGET_CANDIDATES_BYTES,
                             f"5 条结果的 candidates 视图 {n} B，超预算 {BUDGET_CANDIDATES_BYTES} B")

    def test_skill_md_within_budget(self):
        """常驻文档预算：SKILL.md 是技能触发即注入的部分，按字节守。"""
        n = (ROOT / "SKILL.md").stat().st_size
        self.assertLessEqual(n, BUDGET_SKILL_MD_BYTES,
                             f"SKILL.md {n} B 超预算 {BUDGET_SKILL_MD_BYTES} B——"
                             "常驻内容请挪到 references/（按需读取）")

    def test_subskill_docs_within_budget(self):
        over = []
        for rel, budget in BUDGET_SUBSKILL_MD_BYTES.items():
            p = ROOT / rel
            self.assertTrue(p.exists(), f"{rel} 不存在——预算表过期了")
            n = p.stat().st_size
            if n > budget:
                over.append(f"{rel}: {n} B > {budget} B")
        self.assertFalse(over, (
            "子技能 SKILL.md 超预算——触发即注入的内容请挪到该子技能的 "
            "references/（按需读取），别让低频细节常驻：\n  " + "\n  ".join(over)))

    def test_moved_content_still_exists(self):
        """从 SKILL.md 搬出的内容必须在 references 里有承接（防「搬走=删掉」）。"""
        moved = {
            "sub-skills/ego-search/SKILL.md": [
                "references/browser-runtime.md",
            ],
        }
        for md_rel, refs in moved.items():
            for r in refs:
                p = (ROOT / md_rel).parent / r
                self.assertTrue(p.exists(), f"{md_rel} 指向的 {r} 不存在")
                self.assertGreater(p.stat().st_size, 500,
                                   f"{r} 体量异常小，搬移时可能丢了内容")


class TestNoSelfDescriptionInSkillDocs(unittest.TestCase):
    """技能文档只写「怎么用」，不写「我是什么/我有哪些文件」。

    第一性原理：技能文档的唯一职责是**改变调用方的行为**。描述自身结构的
    段落不改变任何行为，却有三重代价——
      ① 每次触发都占上下文；
      ② 它会漂移（实测 ego-search 的「文件结构」段列了 8 个文件，漏了 tests/
         与后来的两个 reference，读者据此判断会出错）；
      ③ 它鼓励「靠文档同步」而不是靠检查，本仓已多次被这类漂移咬到。
    该判据可机械执行：文件树是**可以从文件系统读出**的信息，凡是把 `├──`
    `└──` 这类树形画进 SKILL.md 的，一律判定为自描述冗余。
    """

    TREE_MARKERS = ("├──", "└──", "│  ")
    SELF_DESC_HEADS = (
        "文件结构", "目录结构", "完全态架构", "实现说明",
        "与原版", "与其他技能的关系", "能力基础", "版本历史",
    )

    def _skill_docs(self):
        docs = [ROOT / "SKILL.md"]
        docs += sorted((ROOT / "sub-skills").glob("*/SKILL.md"))
        return [d for d in docs if d.exists()]

    def test_no_filesystem_tree(self):
        bad = []
        for d in self._skill_docs():
            text = d.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if any(m in line for m in self.TREE_MARKERS):
                    bad.append(f"{d.relative_to(ROOT)}:{i} {line.strip()[:48]}")
        self.assertFalse(bad, (
            "SKILL.md 里画了文件树——文件系统自己能回答，写进文档只会漂移"
            "（并每次触发都占上下文）。删掉，或把**不显然的那两条**用一句话写出来：\n  "
            + "\n  ".join(bad)))

    def test_no_self_description_headings(self):
        bad = []
        for d in self._skill_docs():
            for i, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
                if not line.startswith("#"):
                    continue
                head = line.lstrip("# ").strip()
                for kw in self.SELF_DESC_HEADS:
                    if kw in head:
                        bad.append(f"{d.relative_to(ROOT)}:{i} {head[:56]}")
        self.assertFalse(bad, (
            "SKILL.md 出现自描述型段落标题——技能文档只该写「怎么用」。"
            "若其中确实含操作性信息，请把它挪进对应的使用章节，其余删除：\n  "
            + "\n  ".join(bad)))

    def test_scan_surface_not_empty(self):
        """扫描面不能空——glob 失效时上面两条会静默全过。"""
        self.assertGreaterEqual(len(self._skill_docs()), 4)


class TestContextGuidanceIsReal(unittest.TestCase):
    """文档承诺的省上下文开关必须真实存在且未失效。"""

    def test_skill_md_documents_envelope_opt_in(self):
        """契约在 2026-09-17 反转：归档视图改为默认关，要时加 --envelope。

        门禁的**意图没变**——「SKILL.md 必须教 Agent 怎么控制输出体积，别让
        默认档翻倍」。变的只是开关方向：此前是「默认全量、记得减
        （--no-envelope）」，现在是「默认精简、要用时打开（--envelope）」。
        旧的 `--no-envelope` 仍在 CLI 上兼容保留。
        """
        md = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--envelope", md,
                      "SKILL.md 未教 Agent 用 --envelope——要来源追溯时无从打开")
        self.assertIn("--fields agent", md,
                      "SKILL.md 未教 Agent 用精简档——默认输出体积会翻倍")

    def test_cli_really_supports_documented_flags(self):
        """对着**解析器对象**断言，不 grep 源码文本。

        源码文本断言在重构时会假红（2026-09-21 把 CLI 搬到 search_cli 时正是
        如此），而且它断言的是字面量不是行为。`build_parser()` 就是为此抽出来的。
        """
        from search_cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["q", "--envelope", "--archive"])
        self.assertTrue(args.envelope, "文档在教 --envelope，CLI 却不支持")
        args = parser.parse_args(["q", "--no-envelope"])
        self.assertFalse(args.envelope,
                         "--no-envelope 是兼容保留项，删掉会打断既有脚本")

    def test_list_engines_detail_filter_by_engine(self):
        """单引擎详细查询必须真的被过滤——曾静默忽略 --engine 吐全量 186 KB。"""
        from engine_status import list_engines_detail
        one = list_engines_detail(engines=["egov_law"])
        self.assertEqual([r["engine_id"] for r in one], ["egov_law"],
                         "list_engines_detail 的 engines 过滤失效")
        many = list_engines_detail(engines=["egov_law", "kor_law"])
        self.assertEqual(sorted(r["engine_id"] for r in many), ["egov_law", "kor_law"])

    def test_filter_actually_shrinks_payload(self):
        """过滤后的体积必须小到 KB 级（防「过滤了但每行变胖」）。"""
        from engine_status import list_engines_detail
        one = _size(list_engines_detail(engines=["egov_law"]))
        self.assertLessEqual(one, 3000,
                             f"单引擎详细行 {one} B，超 3 KB——诊断字段在膨胀")

    def test_unknown_engine_yields_empty_not_full_list(self):
        """查了不存在的引擎要返回空（配合 CLI 的 stderr 提示），不能回退成全量。"""
        from engine_status import list_engines_detail
        self.assertEqual(list_engines_detail(engines=["__no_such_engine__"]), [])


class TestMcpToolsListBudget(unittest.TestCase):
    """tools/list 默认 CORE 三件套；全量仍 14；未知过滤不得吐全量。"""

    CORE = ("argo_search", "argo_fetch", "argo_local_search")
    BUDGET_CORE_BYTES = 4000

    def test_default_is_core_three(self):
        from mcp_tools import listed_tools, TOOLS
        names = [t["name"] for t in listed_tools("")]
        self.assertEqual(tuple(names), self.CORE)
        self.assertEqual(len(TOOLS), 14, "全量 TOOLS 必须仍是 14——能力不删")

    def test_all_returns_fourteen(self):
        from mcp_tools import listed_tools, TOOLS
        self.assertEqual([t["name"] for t in listed_tools("all")],
                         [t["name"] for t in TOOLS])

    def test_core_payload_within_budget(self):
        from mcp_tools import listed_tools
        n = _size(listed_tools("core"))
        self.assertLessEqual(n, self.BUDGET_CORE_BYTES,
                             f"CORE tools/list {n} B 超预算 {self.BUDGET_CORE_BYTES} B")

    def test_unknown_names_do_not_fallback_to_all(self):
        from mcp_tools import listed_tools, TOOLS
        got = listed_tools("__no_such_tool__")
        self.assertEqual(got, [])
        self.assertNotEqual(len(got), len(TOOLS))

    def test_comma_list_and_bare_names(self):
        from mcp_tools import listed_tools
        names = [t["name"] for t in listed_tools("search,argo_job")]
        self.assertEqual(names, ["argo_search", "argo_job"])

    def test_handle_rpc_tools_list_uses_listed_tools(self):
        import os
        from mcp_transport import handle_rpc
        old = os.environ.pop("ARGO_MCP_TOOLS", None)
        try:
            r = handle_rpc("tools/list", {})
            self.assertEqual([t["name"] for t in r["tools"]], list(self.CORE))
        finally:
            if old is not None:
                os.environ["ARGO_MCP_TOOLS"] = old


class TestStdoutJsonIsCompact(unittest.TestCase):
    """CLI 的 stdout JSON 必须走 cli_io.dumps（紧凑），不得各处写 indent=2。

    `--json` 是给 Agent / 脚本读的，缩进只增加传输体积与 token，不增加信息。
    MCP 侧一直是紧凑的（`mcp_handlers._dumps` 原实现 separators=(",", ":")），
    而 CLI 侧曾在 45 处各写一遍 `json.dumps(..., indent=2)`——同一份载荷两套
    计算方式，实测多占 23%（25626 B → 19928 B）。

    这里同时锁两侧：序列化函数的**行为**，以及源码里不再出现美化 stdout 的
    新写法（否则第 46 处会悄悄长出来）。
    """

    # 允许的美化 stdout：无（归档文件写入走 dumps_pretty，不属于 stdout）
    def test_helper_is_compact(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import cli_io
        obj = {"a": [1, 2], "b": {"c": "中文"}, "d": "x\ny"}
        compact = cli_io.dumps(obj)
        self.assertNotIn(": ", compact, "dumps 仍带分隔空格，不是紧凑输出")
        self.assertNotIn(", ", compact)
        self.assertEqual(json.loads(compact), obj, "紧凑化改变了数据")
        # 人读档仍在：缩进版本必须与紧凑版本语义一致
        pretty = cli_io.dumps_pretty(obj)
        self.assertIn("\n", pretty)
        self.assertEqual(json.loads(pretty), obj)
        self.assertLess(len(compact), len(pretty), "紧凑版应小于美化版")

    def test_mcp_and_cli_share_one_source(self):
        """MCP 的 _dumps 必须复用 cli_io，而不是再写一份自己的分隔符。"""
        sys.path.insert(0, str(ROOT / "scripts"))
        import cli_io
        import mcp_handlers
        probe = {"k": [1, 2], "z": "中文"}
        self.assertEqual(mcp_handlers._dumps(probe), cli_io.dumps(probe))
        self.assertEqual(mcp_handlers._dumps(probe, pretty=True),
                         cli_io.dumps_pretty(probe))

    def test_no_pretty_stdout_in_scripts(self):
        """源码检查：stdout 不得输出缩进 JSON。

        认两种写法：老的 `print(json.dumps(..., indent=))`，和改用统一入口后的
        `print(dumps_pretty(...))`。只认前一种的话，后者会从检查底下溜过去——
        实测 crawl.py / extract.py 就是这么把 stdout 又变回美化格式的
        （`argo crawl` 每次多占约三成体积）。
        写盘文件不在此列：那是给人翻的，用 dumps_pretty 是对的。
        """
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    continue
                for inner in ast.walk(node):
                    pretty_by_indent = (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "dumps"
                        and any(k.arg == "indent" for k in inner.keywords))
                    pretty_by_helper = (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "dumps_pretty")
                    if pretty_by_indent or pretty_by_helper:
                        offenders.append(f"{path.relative_to(ROOT)}:{inner.lineno}")
        self.assertEqual(
            offenders, [],
            "CLI stdout 出现缩进 JSON（应改用 cli_io.dumps）：\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()


class TestListEnginesCompactProjection:
    """全量 detail 的瘦身投影（2026-09-16）：152 KB → ~51 KB。

    守的缺陷：--list-engines --detail 不带过滤是 232 行全字段转储，
    runtime/admission 嵌套占三成，Agent 拉进上下文就是 ~50k token 事故。
    契约：① 瘦身面只删键不改值，判定旗标一个不少；② failure/配额原因/
    缺依赖条件性出现（健康源不带）；③ 压缩后全量体积封顶。
    """

    def _rows(self):
        from engine_status import list_engines_detail
        return list_engines_detail()

    def test_healthy_row_is_minimal(self):
        from engine_status import compact_engine_row
        slim = compact_engine_row({
            "engine_id": "x", "enabled": True, "type": "http",
            "cost_tier": "free", "status": "ready", "explicit_only": False,
            "env_ready": True, "required_env": [], "missing_env": [],
            "allowed_by_env": True, "blocked": False, "admitted": True,
            "routable": True, "quota_exhausted": False,
            "quota_exhausted_until": None, "quota_exhausted_reason": "",
            "dep_ready": True, "requires": [], "missing_deps": [],
            "dep_fixes": [], "admission": {"quality_score": 1.0},
            "runtime": {"adaptive": 0.5, "failure": None},
        })
        assert "admission" not in slim and "runtime" not in slim
        assert "missing_env" not in slim and "missing_deps" not in slim
        assert slim["status"] == "ready" and slim["routable"] is True

    def test_failure_details_conditional(self):
        from engine_status import compact_engine_row
        slim = compact_engine_row({
            "engine_id": "x", "enabled": True, "type": "http",
            "cost_tier": "free", "status": "blocked", "explicit_only": False,
            "env_ready": True, "missing_env": ["SOME_KEY"],
            "allowed_by_env": True, "blocked": True, "admitted": False,
            "routable": False, "quota_exhausted": True,
            "quota_exhausted_reason": "quota exhausted",
            "dep_ready": True, "missing_deps": ["yt-dlp"],
            "runtime": {"failure": {"category": "blocked",
                                    "evidence": "cf challenge"}},
        })
        assert slim["missing_env"] == ["SOME_KEY"]
        assert slim["failure"]["category"] == "blocked"
        assert slim["quota_exhausted_reason"] == "quota exhausted"

    def test_compact_dump_size_capped(self):
        """压缩后全量体积封顶：投影再膨胀说明有字段忘了登记。"""
        import json
        from engine_status import compact_engine_row
        rows = self._rows()
        total = sum(len(json.dumps(compact_engine_row(r), ensure_ascii=False))
                    for r in rows)
        assert total < 64 * 1024, f"瘦身全量超 64KB：{total}B（检查投影字段集）"
