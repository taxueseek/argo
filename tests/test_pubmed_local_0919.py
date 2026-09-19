#!/usr/bin/env python3
"""test_pubmed_local_0919.py — local_pubmed 静默失效修复的回归锁。

## 守的是什么缺陷

`local_pubmed` 在 `--list-engines` 里长期显示 `ready / routable`，实测却
**稳定返回 0 条**，且引擎自报 `insufficient-signal` 后由 recovery 换源补位
——用户完全看不出这个源是坏的。两处结构性错误叠加：

  ① **`format` 被当查询参数注入 URL**。engines_base 把 spec 的 `format`
     字段（本意是解析提示，供 `_parse_http_payload` 区分 xml/json）无条件
     拼进 query string。绝大多数 API 忽略未知参数所以没暴露；但 NCBI
     E-utilities 的 `format` 是**输出格式**参数且不接受 json——实测同一条
     URL 加 `&format=json` 返回 HTTP 400、去掉返回 200。
  ② **`idlist` 是字符串数组**。`_make_field_parser` 对非 dict 条目
     `continue`，所以即便请求通了也解析不出任何条目；何况 esearch 只给
     PMID，本就没有标题/摘要可填，必须再打一次 esummary。

## 三条不变式

  1. **开关语义**：`format_is_query_param` 默认 True（47 个既有引擎行为
     一字不变），显式 false 时 URL 里不得出现 `format=`。
  2. **两段式取全**：`_build_pubmed_engine` 必须产出**带标题**的结果，
     而不是裸 PMID；URL 必须指向可点的 PubMed 详情页。
  3. **降级不空手**：esummary 失败时仍须返回带 PMID 的最小条目，
     不得整体返回空（宁可信息少，不可假装没有）。
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestFormatQueryParamSwitch:
    """不变式 1：format 注入开关。"""

    def _url_for(self, spec_extra: dict) -> str:
        import engines_base as eb

        base = {
            "url": "https://example.com/api",
            "query_param": "q",
            "format": "json",
            "method": "GET",
            "timeout": 5,
        }
        base.update(spec_extra)
        seen: dict[str, str] = {}
        orig = eb._http_get_raw

        def spy(url, *a, **k):
            seen["url"] = url
            return '{"items": []}'

        eb._http_get_raw = spy
        try:
            eb._build_http_engine(base)("q", 1)
        finally:
            eb._http_get_raw = orig
        return seen["url"]

    def test_default_still_injects_format(self):
        """默认必须保持不变——47 个既有引擎依赖这个行为。"""
        url = self._url_for({})
        assert "format=json" in url, (
            f"默认行为被改变：{url}。这会影响 47 个既有 GET 引擎，"
            "format 注入的收敛必须走 format_is_query_param 显式开关"
        )

    def test_opt_out_removes_format(self):
        """显式关闭后，URL 里不得再有 format=（NCBI 会因此 400）。"""
        url = self._url_for({"format_is_query_param": False})
        assert "format=" not in url, f"开关未生效，仍注入了 format：{url}"
        assert "q=q" in url, f"其余查询参数不应受影响：{url}"

    def test_format_still_used_for_parsing_when_not_injected(self):
        """关掉注入不能把解析提示一起关掉——fmt 仍须传给解析器。

        这是本改动最容易改坏的地方：`fmt` 同时承担「注入 URL」与
        「选择解析分支」两个职责，只该掐掉前者。
        """
        import engines_base as eb

        spec = {
            "url": "https://example.com/api",
            "query_param": "q",
            "format": "xml",
            "format_is_query_param": False,
            "method": "GET",
            "timeout": 5,
            "output_map": {"items": "."},
        }
        # 必须是 Atom（_parse_xml 只认 atom:entry 命名空间），RSS 会被判为空
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><title>T</title><id>https://e.com/1</id>"
            "<summary>S</summary></entry></feed>"
        )
        orig = eb._http_get_raw
        eb._http_get_raw = lambda *a, **k: atom
        try:
            res = eb._build_http_engine(spec)("q", 1)
        finally:
            eb._http_get_raw = orig
        # 走 xml 分支才会解析出条目；若 fmt 被一并关掉会退化成 JSON 解析失败
        assert res, "format 关闭注入后解析分支失效——fmt 被误当作仅注入用"


class TestPubmedTwoStage:
    """不变式 2/3：两段式取全与降级。"""

    def test_produces_titles_not_bare_pmids(self):
        """必须在无网络打桩的情况下也证明「不是裸 PMID」。"""
        import engines_builders_batch9 as b9

        built = b9._build_pubmed_engine({"_name": "local_pubmed", "timeout": 10})
        assert callable(built)

        # 打桩两段：esearch 给 idlist（字符串数组，正是旧实现解析不出的形态），
        # esummary 给详情。
        calls: list[str] = []

        def fake_json(url, to, engine=""):
            calls.append(url)
            if "esearch.fcgi" in url:
                return {"esearchresult": {"idlist": ["111", "222"]}}
            return {"result": {
                "111": {"title": "A study of CRISPR", "source": "Nature",
                        "pubdate": "2026 Jan", "elocationid": "doi: 10.1000/abc"},
                "222": {"title": "Another trial", "source": "Cell",
                        "pubdate": "2025 Mar", "elocationid": ""},
            }}

        orig = b9._json
        b9._json = fake_json
        try:
            res = built("CRISPR", 2)
        finally:
            b9._json = orig

        assert len(calls) == 2, f"必须是两段式（esearch + esummary），实际 {calls}"
        assert len(res) == 2, f"应产出 2 条，实际 {len(res)}"
        for r in res:
            assert r["title"] and r["title"] != "111", f"标题是裸 PMID：{r}"
            assert r["url"].startswith("https://pubmed.ncbi.nlm.nih.gov/"), r["url"]
        assert "Nature" in res[0]["snippet"], f"应带期刊名：{res[0]['snippet']}"
        assert "10.1000/abc" in res[0]["snippet"], f"应带 DOI：{res[0]['snippet']}"

    def test_degrades_to_pmid_entries_when_summary_fails(self):
        """不变式 3：esummary 失败不得整体返回空。"""
        import engines_builders_batch9 as b9

        built = b9._build_pubmed_engine({"_name": "local_pubmed", "timeout": 10})

        def fake_json(url, to, engine=""):
            if "esearch.fcgi" in url:
                return {"esearchresult": {"idlist": ["999"]}}
            raise RuntimeError("esummary 挂了")

        orig = b9._json
        b9._json = fake_json
        try:
            res = built("anything", 1)
        finally:
            b9._json = orig

        assert len(res) == 1, f"降级路径应仍给 1 条，实际 {len(res)}"
        assert "999" in res[0]["url"], f"降级条目应指向该 PMID：{res[0]['url']}"

    def test_empty_idlist_returns_empty(self):
        """esearch 真的没命中时，返回空是对的（不能编造）。"""
        import engines_builders_batch9 as b9

        built = b9._build_pubmed_engine({"_name": "local_pubmed", "timeout": 10})
        orig = b9._json
        b9._json = lambda url, to, engine="": {"esearchresult": {"idlist": []}}
        try:
            res = built("zzzz-no-such-term", 3)
        finally:
            b9._json = orig
        assert res == [], f"无命中不应编造条目：{res}"


class TestPubmedRegistryWiring:
    """注册链路：spec.type 必须能找到 builder，否则会静默回退。"""

    def test_builder_registered_under_pubmed_type(self):
        import engines

        assert "pubmed" in engines._BUILDERS, (
            "engines._BUILDERS 未注册 pubmed——spec.type=pubmed 会解析失败"
        )

    def test_config_type_is_pubmed(self):
        from config import load_config

        eng = (load_config().get("engines") or {}).get("local_pubmed")
        assert eng is not None, "local_pubmed 不在 config.engines 里"
        assert eng.get("type") == "pubmed", (
            f"local_pubmed 的 type 应为 pubmed（两段式 builder），"
            f"实际 {eng.get('type')}——声明式 http 路径解析不出 idlist 字符串数组"
        )
        # 旧实现的坏 output_map 必须已移除，否则读者会以为仍走声明式路径
        assert not eng.get("output_map"), (
            "local_pubmed 不应再保留 output_map：它把 title/url/summary "
            "全指向 pmid，是旧实现静默返回空的直接原因"
        )


class TestAcademicDomainWiring:
    """接线：两个源必须真的进得了 academic 域的 combo，且不挤掉既有源。

    这一层单独锁，是因为 argo 的 combo 会被 `_apply_engine_policy` 按预算
    截断：fast 档 base=2，新源若只声明在队尾就会被裁掉，表现为「接了但永不
    参与自动路由」——上次批次九就踩过这个坑（见 route._VERTICAL_NEW_SOURCE
    的 docstring）。故必须断言**实测 combo**，而不是只看 config 里写了没。
    """

    def _combo(self, query: str, mode: str):
        import os

        os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-test-academic")
        from route import route_query

        r = route_query(query, mode=mode)
        return r.get("domain"), list(r.get("engines_combo") or [])

    def test_new_sources_reach_fast_combo(self):
        dom, combo = self._combo("machine learning survey", "fast")
        assert dom == "academic", f"查询未落 academic 域，实际 {dom}"
        assert "local_pubmed" in combo, f"local_pubmed 被预算裁掉了：{combo}"
        assert "core" in combo, f"core 被预算裁掉了：{combo}"

    def test_existing_sources_not_displaced(self):
        """加槽而非顶位：既有的前排源必须仍在声明 combo 的前列。

        这里断言的是**声明位次**而不是运行时 combo。原因：运行时 combo 会被
        `_apply_engine_policy` 按预算与 live_combo 裁切，而 live_combo 受
        `route._adaptive_learner`（跨测试共享的模块级状态）影响——实测在全量
        套件里与单跑时的裁切结果不同。用运行时 combo 断言位次，等于把测试
        绑到别的用例是否先跑过，是脆的。声明位次是确定的，且「新源排在既有源
        之后」正是「加槽不顶位」这条不变式的真身。
        """
        from config import load_config

        cfg = load_config()
        dom = next(d for d in cfg["domains"] if d.get("name") == "academic")
        combo = list(dom["engines_combo"])
        assert combo, "academic 域没有 engines_combo"
        # 既有前排源：arxiv（primary）与 openreview 必须仍排在两个新源之前
        for prior in ("arxiv", "openreview", "biorxiv", "openalex"):
            assert prior in combo, f"既有源 {prior} 从 academic combo 里消失了"
        first_new = min(combo.index(e) for e in ("local_pubmed", "core")
                        if e in combo)
        for prior in ("arxiv", "openreview", "biorxiv", "openalex"):
            assert combo.index(prior) < first_new, (
                f"{prior}（位次 {combo.index(prior)}）被排到了新源"
                f"（位次 {first_new}）之后——加槽变成了顶位"
            )

    def test_new_sources_declared_inside_budget_cap(self):
        """新源位次必须落在加槽额度能覆盖的范围内（否则装了也路由不到）。

        route._new_source_extra 的额度 = min(最深新源位次 - base, cap)，
        cap 在 auto/balanced 档为 4。若把新源放在队尾（如位次 9/10），
        额度被封顶到 4、预算 3+4=7 < 9，两个源会被整段截断——这正是本次
        接线过程中实测踩到的坑（先放队尾失败，改到第 3/4 位又挤掉 biorxiv，
        最终放第 5/6 位才同时满足两个约束）。本用例把这个算术约束固化下来。
        """
        from config import load_config

        cfg = load_config()
        dom = next(d for d in cfg["domains"] if d.get("name") == "academic")
        combo = list(dom["engines_combo"])
        new = [e for e in ("local_pubmed", "core") if e in combo]
        assert new, "两个新源都不在 combo 里"
        deepest = max(combo.index(e) + 1 for e in new)  # 1-based 位次
        # auto/balanced: base=3, cap=4 → 最深位次须 <= 7
        assert deepest <= 7, (
            f"最深新源位次 {deepest} 超出加槽额度覆盖上限 7"
            f"（base=3 + cap=4）——路由会截断它，表现为「装了但路由不带它」。"
            f"combo={combo}"
        )

    def test_core_engine_is_registered(self):
        import engines

        assert "core" in engines._BUILDERS, "core builder 未注册"

    def test_core_spec_loads(self):
        from config import load_config

        eng = (load_config().get("engines") or {}).get("core")
        assert eng is not None, "core 未进入 config.engines"
        assert eng.get("type") == "core", f"core 的 type 应为 core，实际 {eng.get('type')}"
        assert eng.get("enabled") is True, "core 应默认启用"


class TestCoreThrottleEnvelope:
    """CORE 的限流是 HTTP 200 + 错误封套，必须被识别成失败而非「无结果」。

    通用 `_envelope_error` 要求 Code 与 message 同时在场（火山/知乎那种），
    而 CORE 只给 message —— 识别不出就会把「上游限流」静默当成「这个词没
    结果」，熔断器也拿不到失败信号。这是本引擎最容易被改坏的一点。
    """

    def test_throttle_envelope_logs_error_not_silent(self):
        """限流封套必须被识别成失败（日志留痕），而不是静默返回空。

        契约层次：builder 抛 RuntimeError，外层 `@safe_search` 统一兜住并
        **logger.error 留痕**、返回 []。所以「有牙」的判据不是异常穿透，而是
        **必须打出错误日志**——否则它和无命中长得一模一样，用户与熔断器都拿不到
        失败信号。这是本引擎最容易被改坏的地方。
        """
        import logging

        import engines_builders_batch9 as b9

        built = b9._build_core_engine({"_name": "core", "timeout": 10})
        orig = b9._json
        # Azure Search 被限流时的真实响应形态（HTTP 200 但只有 message）
        b9._json = lambda url, to, engine="": {
            "message": "Azure search failed with status code: 503. ..."
        }
        records: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        h = _Cap()
        b9.logger.addHandler(h)
        try:
            res = built("anything", 3)
        finally:
            b9.logger.removeHandler(h)
            b9._json = orig

        assert res == [], f"限流时不应产出条目，实际 {res}"
        joined = " | ".join(records)
        assert "503" in joined or "upstream error" in joined or "封套" in joined, (
            "限流封套未被识别：没有错误日志，会被当成「无结果」静默吞掉。"
            f"实际日志: {joined!r}"
        )

    def test_real_empty_result_stays_silent(self):
        """真·空结果不得报错——否则每次无命中都刷错误日志。"""
        import logging

        import engines_builders_batch9 as b9

        built = b9._build_core_engine({"_name": "core", "timeout": 10})
        orig = b9._json
        b9._json = lambda url, to, engine="": {"results": [], "totalHits": 0}
        records: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        h = _Cap()
        b9.logger.addHandler(h)
        try:
            out = built("zzz-none", 3)
        finally:
            b9.logger.removeHandler(h)
            b9._json = orig

        assert out == []
        joined = " | ".join(records)
        assert "封套" not in joined and "upstream error" not in joined, (
            f"真空结果被误报为上游错误：{joined!r}"
        )
