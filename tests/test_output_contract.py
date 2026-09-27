#!/usr/bin/env python3
"""输出质量契约检查：质量信号字段不得被任何输出档位剥掉。

## 背景（2026-09-15 输出契约审查）

`limitations`（结果局限声明）此前只在 `envelope=True` 分支里生成，于是被
文档推荐给 agent 的两个档位——`--no-envelope` 与 `--fields agent`——整块
丢失。丢的是这类信息：

    daily tier: direct search; no pre-confirm gate
    url-seed: seed URL was not fetched; results are related discovery only
    recovery used; engine fallback may differ from primary route
    professional tier: plan metadata attached; verify top-k before hard claims

这些是**直接指导 agent 怎么用结果的元信息**：不说，agent 就会把「相关发现」
当正文用、把降级路由结果当主路由结果用、把未预确认的档位当已确认的档位用。

根因是字段边界划错了：归档专用字段（candidates/sources/coverage）与质量信号
（limitations）被绑在同一个开关上。本检查锁死修复结果——**任何档位都不允许
再丢质量信号**，无论出于「省体积」还是别的理由。

## 检查计算方式

分两类，避免把「空值省略」误判成「被剥掉」：

- **必须总在场**（MUST_SURVIVE）：有值就该到达
- **有情况才在场**（CONDITIONAL）：recovery / time_filter_warning 等仅在
  非 None 时输出，属合理的省体积设计，不作硬性要求

## 实现方式

**不跑端到端搜索**——检查必须无网络、快且稳定（本仓检查的一贯约定；
实测 `ARGO_OFFLINE` 在代码中并无实现，端到端路径无法保证离线）。改为三层：

1. 纯函数契约：`build_limitations`（零依赖）
2. 档位裁剪契约：`_strip_for_agent` 的字段白名单（纯函数）
3. 静态结构检查：局限声明的生成必须与 `if envelope:` 拆开（ast 解析源码）

每层都配了一个自检用例：先故意写坏一处，确认检查会失败。少了这一步，
检查很容易写成「永远通过」的摆设。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import candidate_envelope  # noqa: E402
import search  # noqa: E402

# 必须在场：无论 envelope 开关、无论 fields 档位
MUST_SURVIVE = (
    "query", "status", "count", "results",
    "fetch_required", "evidence_loop", "errors", "limitations",
)

# 有情况才在场（值为 None 时省略属合理设计）
CONDITIONAL = ("recovery", "time_filter_warning")

_ENVELOPE_GATED_HINT = "build_limitations 在 `if envelope:` 分支内"


def _scan_envelope_gating(src: str) -> tuple[list[str], bool]:
    """扫描源码形态，返回（被关进 `if envelope:` 的调用点, 是否存在正确形态）。

    正确形态 = `if envelope:` 的 else 分支（或同级语句）里生成局限声明。
    这是根因级保护：limitations 原本就长在 `if envelope:` 里，编码时顺手把
    它挪回去太容易了，只有静态检查拦得住这种「改回去」。
    """
    offenders: list[str] = []
    has_non_envelope_path = False
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Name) and test.id == "envelope"):
            continue
        if any(isinstance(n, ast.Name) and n.id == "build_limitations"
               for stmt in node.body for n in ast.walk(stmt)):
            offenders.append(f"line {node.lineno}: {_ENVELOPE_GATED_HINT}")
        if any(isinstance(n, ast.Name) and n.id == "build_limitations"
               for stmt in node.orelse for n in ast.walk(stmt)):
            has_non_envelope_path = True
    return offenders, has_non_envelope_path


class TestLimitationsSingleSource(unittest.TestCase):
    """局限声明：生成计算方式必须是单一实现，且覆盖全部已知信号。"""

    def test_covers_every_known_signal(self):
        """每个触发条件都必须在声明里留下痕迹。"""
        search_result = {
            "early_stopped": True,
            "recovery": {"engine": "x"},
            "cached": True,
            "cache_level": "L2",
            "semantic_hit": True,
            "semantic_query": "keywords:pi-package memory",
            "semantic_similarity": 0.875,
            "login_state_used": True,
        }
        lim = candidate_envelope.build_limitations(
            search_result, extra_limitations=["自定义局限"])
        joined = " ".join(lim)
        self.assertIn("自定义局限", joined, "extra_limitations 未透传")
        self.assertIn("engagement metrics", joined, "engagement 警告缺失")
        self.assertIn("early_stopped", joined, "early_stopped 未上报")
        self.assertIn("recovery path", joined, "recovery 未上报")
        self.assertIn("cache level=L2", joined, "cache 说明未上报")
        self.assertIn("login_state_used", joined, "登录态未上报")
        self.assertIn("keywords:pi-package memory", joined, "软命中来源查询未上报")
        self.assertIn("0.875", joined, "软命中相似度未上报")

    def test_semantic_hit_explains_foreign_query(self):
        """软命中必须说清「结果来自另一条查询」——只说 cache level 不够。"""
        lim = candidate_envelope.build_limitations(
            {"cached": True, "cache_level": "L2", "semantic_hit": True,
             "semantic_query": "keywords:pi-package memory",
             "semantic_similarity": 0.875})
        joined = " ".join(lim)
        self.assertIn("semantic cache hit", joined, "未说明是语义软命中")
        self.assertIn("not from this exact query", joined,
                      "未说明结果不属于本查询——调用方会当成自己的结果")

    def test_no_semantic_hit_no_extra_limitation(self):
        """精确命中不得出现软命中声明（不适用就不写，避免误报）。"""
        lim = candidate_envelope.build_limitations(
            {"cached": True, "cache_level": "L1"})
        self.assertFalse([x for x in lim if "semantic" in x],
                         "精确命中误报了软命中声明")

    def test_clean_result_still_has_baseline_warning(self):
        """没有任何异常信号时，仍必须给出基线警告（不能返回空列表）。"""
        lim = candidate_envelope.build_limitations({})
        self.assertTrue(lim, "局限声明为空——agent 会以为结果无任何局限")
        self.assertIn("engagement metrics", " ".join(lim))

    def test_attach_envelope_reuses_same_implementation(self):
        """envelope 路径与精简路径必须同一计算方式（防两处各写一份漂移）。"""
        src = (SCRIPTS / "candidate_envelope.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        attach = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "attach_envelope"),
            None)
        self.assertIsNotNone(attach, "attach_envelope 未找到")
        called = {
            n.func.id
            for n in ast.walk(attach)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn("build_limitations", called,
                      "attach_envelope 未复用 build_limitations（口径会漂移）")

    def test_gate_has_teeth(self):
        """故意造错：抹掉基线警告，必须被上面的检查抓住。"""
        original = candidate_envelope.build_limitations

        def broken(search_result, extra_limitations=None, candidates=None):
            return list(extra_limitations or [])  # 基线警告没了

        candidate_envelope.build_limitations = broken
        try:
            self.assertEqual(candidate_envelope.build_limitations({}), [],
                             "造错没生效，测试本身有问题")
            with self.assertRaises(AssertionError):
                self.assertIn("engagement metrics",
                              " ".join(candidate_envelope.build_limitations({})))
        finally:
            candidate_envelope.build_limitations = original


class TestAgentTierKeepsQualitySignals(unittest.TestCase):
    """--fields agent：剥遥测可以，剥质量信号不行。"""

    def _payload(self) -> dict:
        """构造字段齐全的搜索输出（含遥测，供反向检查用）。"""
        payload: dict = {
            "query": "q", "status": "completed", "count": 1,
            "results": [{"title": "t", "url": "u", "snippet": "s"}],
            "errors": [], "limitations": ["daily tier: direct search"],
            "fetch_required": False, "evidence_loop": {"suggested": []},
            # 遥测字段（应当被剥掉）
            "tfidf_scores": [0.1], "engine_outcomes": [{"engine": "x"}],
            "elapsed_ms": 12, "route_reason": "r", "cached": True,
        }
        return payload

    def test_quality_signals_survive_agent_tier(self):
        out = search._strip_for_agent(self._payload())
        missing = [k for k in MUST_SURVIVE if k not in out]
        self.assertEqual(
            missing, [],
            "--fields agent 剥掉了质量信号（agent 无从判断结果能用到什么程度）："
            + ", ".join(missing))

    def test_limitations_specifically_survive(self):
        """独立检查 limitations——本次修复的核心，值得单独钉住。"""
        out = search._strip_for_agent(self._payload())
        self.assertIn("limitations", out, "--fields agent 丢了 limitations")
        self.assertEqual(out["limitations"], ["daily tier: direct search"])

    def test_telemetry_still_stripped(self):
        """反向检查：不能矫枉过正，遥测该剥还得剥。"""
        out = search._strip_for_agent(self._payload())
        for k in ("tfidf_scores", "engine_outcomes", "elapsed_ms",
                  "route_reason", "cached"):
            self.assertNotIn(k, out, f"--fields agent 未剥掉遥测字段 {k}")

    def test_gate_has_teeth(self):
        """故意造错：让 _strip_for_agent 丢掉 limitations，检查必须报红。"""
        original = search._strip_for_agent

        def broken(payload):
            out = original(payload)
            out.pop("limitations", None)
            return out

        search._strip_for_agent = broken
        try:
            out = search._strip_for_agent(self._payload())
            self.assertNotIn("limitations", out, "造错没生效，测试本身有问题")
            with self.assertRaises(AssertionError):
                self.assertIn("limitations", out)
        finally:
            search._strip_for_agent = original


class TestLimitationsNotGatedByEnvelope(unittest.TestCase):
    """局限声明必须与归档开关拆开：一条静态形态检查 + 一条行为断言。

    行为断言是主检查（它验的是契约），静态检查是补充（拦住「把调用挪回
    `if envelope:` 里」这种形态回退）。
    """

    def test_limitations_present_without_envelope_behaviourally(self):
        """envelope=False（精简档）时 limitations 必须仍然生成。"""
        from search_output import _ShapeContext, shape_response

        class _NullCache:
            def local_status(self, urls):
                return {}

        ctx = _ShapeContext(
            query="q", kind="keyword", tier="daily", envelope=False,
            decision={}, extra_lim=["测试局限"], cache=_NullCache(),
            include_domains=[], exclude_domains=[], include_local=False, n=1,
            run_local_seek=lambda *a, **k: [],
        )
        result = {"results": [{"url": "https://a", "title": "t", "snippet": "s"}]}
        out = shape_response(ctx, result)
        self.assertIn("limitations", out, "精简档丢了局限声明")
        self.assertTrue(out["limitations"], out)

    def test_not_gated_in_real_source(self):
        # 局限声明的生成逻辑住在 search_output.shape_response（响应契约层）；
        # search.py 只是调用方。扫源码要扫**实现处**，否则重构一次就假红。
        src = (SCRIPTS / "search_output.py").read_text(encoding="utf-8")
        offenders, has_non_envelope_path = _scan_envelope_gating(src)
        self.assertEqual(offenders, [],
                         "局限声明又被关回归档开关：\n  " + "\n  ".join(offenders))
        self.assertTrue(
            has_non_envelope_path,
            "未找到「非 envelope 路径生成 limitations」的分支——"
            "局限声明可能又回到只跟 envelope 走的状态（精简档将再次丢失它）")

    def test_gate_has_teeth(self):
        """故意造错：构造把调用关回 `if envelope:` 的源码，扫描必须报红。"""
        bad_src = (
            "def _fake():\n"
            "    if envelope:\n"
            "        result['limitations'] = build_limitations(result, extra_lim)\n"
            "    return result\n"
        )
        offenders, ok = _scan_envelope_gating(bad_src)
        self.assertTrue(offenders, "造错样本没被抓住，静态检查失效")
        self.assertFalse(ok, "造错样本被误判为正确")

    def test_scanner_accepts_correct_shape(self):
        """反向验证：正确形态必须被识别为「有非 envelope 路径」。"""
        good_src = (
            "def _fake():\n"
            "    if envelope:\n"
            "        attach_envelope(result)\n"
            "    else:\n"
            "        result['limitations'] = build_limitations(result)\n"
            "    return result\n"
        )
        offenders, ok = _scan_envelope_gating(good_src)
        self.assertEqual(offenders, [])
        self.assertTrue(ok, "正确形态被误判")


class TestDerivedScalarsFollowResults(unittest.TestCase):
    """后置过滤改了 `results`，派生字段必须跟着走（2026-09-27）。

    实测：`--include-domains example.com` 返回 `count=4` 而 `results=[]`——
    JSON 消费者按 `count` 循环会取到空气，而按 `results` 判断又对不上账。
    根因是域过滤在**后置**阶段改了 results，而 count 与 evidence_loop 是更早
    算出来的。修法不是在过滤处补一行（下一个后置阶段照样会漏），而是把重算
    放进 `shape_response` 的唯一出口。

    这批用例全部离线：直接喂合成 results 给 shape_response，不跑真实搜索。
    """

    def _shape(self, results, **overrides):
        from search_output import _ShapeContext, shape_response

        class _NullCache:
            def local_status(self, urls):
                return {}

        fields = dict(
            query="q", kind="keyword", tier="daily", envelope=False,
            decision={}, extra_lim=[], cache=_NullCache(),
            include_domains=[], exclude_domains=[], include_local=False, n=5,
            run_local_seek=lambda *a, **k: [],
        )
        fields.update(overrides)
        return shape_response(_ShapeContext(**fields), {"results": list(results)})

    _TWO = [{"url": "https://kept.example/a", "title": "a"},
            {"url": "https://dropped.example/b", "title": "b"}]

    def test_count_follows_include_filter_to_empty(self):
        out = self._shape(self._TWO, include_domains=["none.example"])
        self.assertEqual(out["count"], 0, "count 停在过滤前的值")
        self.assertEqual(len(out["results"]), 0)
        # 非空断言：确认过滤真的发生了。少了它，一旦过滤失效这条用例会假绿
        # （空的 results 与 count=0 恰好都成立）。
        self.assertIn("dropped 2", str(out.get("domain_filter") or ""),
                      "域过滤没生效，本用例失去意义")
        self.assertTrue(out["evidence_loop"]["suggested"] == [], out["evidence_loop"])

    def test_count_follows_include_filter_partial(self):
        out = self._shape(self._TWO, include_domains=["kept.example"])
        self.assertEqual(out["count"], 1)
        self.assertEqual(len(out["results"]), 1)

    def test_count_follows_exclude_filter(self):
        out = self._shape(self._TWO, exclude_domains=["dropped.example"])
        self.assertEqual(out["count"], 1)

    def test_count_matches_results_without_any_filter(self):
        out = self._shape(self._TWO, exclude_domains=["unrelated.example"])
        self.assertEqual(out["count"], len(out["results"]))

    def test_evidence_loop_never_suggests_dropped_urls(self):
        """建议核验的 URL 必须仍在这批结果里，pending 计数与 suggested 同长。"""
        out = self._shape(self._TWO, include_domains=["kept.example"])
        ev = out["evidence_loop"]
        live = {r.get("url") for r in out["results"]}
        self.assertEqual(ev["pending_count"], len(ev["suggested"]))
        for url in ev["suggested"]:
            self.assertIn(url, live, f"建议核验了不在结果里的 URL：{url}")


if __name__ == "__main__":
    unittest.main()
