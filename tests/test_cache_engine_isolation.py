#!/usr/bin/env python3
"""test_cache_engine_isolation.py — 软命中（find_similar）的引擎隔离回归测试。

背景（v2.4.2 修复）：SearchCache.get 的语义软命中分支调用
`self._l2.find_similar(query, engine, domain)` 时传了 engine，但
find_similar 的 SQL WHERE 子句只过滤 domain、从未使用 engine 形参。
后果是软命中跨引擎串味，实测两类：

  1. `argo search --engine v2ex` 可命中 bilibili 的缓存载荷
     （结果 source 全变成 bilibili）；
  2. fetch 与 evidence 同 domain 下 URL 词面高度相似
     （`.../x` 与 `.../x.md` 相似度 0.875；两个不同站点 URL 相似度 0.75）
     会互相串正文——抓 A 站可能拿到 B 站内容。

另修一处标记丢失：软命中回填 L1 时存的是他人原始 s_hit，缺少
_semantic_hit / _semantic_similarity 等标记，导致 L1 二次命中
伪装成硬命中，调用方无法区分「精确命中」与「相似查询命中」。

本文件锁定三条契约：engine 精确隔离、auto 显式通配、标记不丢失。

另追加一条（2026-09-18 修复）——**限定符隔离**：限定符（keywords:/site:/
author: 等）是过滤器而非内容，整串 minhash 相似度会被它主导。实测
`keywords:pi-package mcp` 与 `keywords:pi-package memory` 整串相似度 0.875
（远超 0.7 阈值），判别词只占几个字符，于是软命中把 memory 那批包当成 mcp
的结果交了出去（`argo search --engine npm` 实测复现）。现在先要求限定符集合
相同，再比**载荷**的相似度。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import cache  # noqa: E402


@pytest.fixture()
def sc(tmp_path):
    """独立 SQLite 缓存（不碰真实 ~/.cache）。"""
    return cache.SearchCache(db_path=str(tmp_path / "cache.db"))


def _payload(title, url):
    return {"results": [{"title": title, "url": url}]}


class TestEngineIsolation:
    """engine 维度必须参与软命中过滤。"""

    def test_similar_does_not_cross_engines(self, sc):
        """同 domain、不同 engine、查询词面相似 → 各自只看自己的缓存。"""
        sc.set("苹果 2025 营收", "eastmoney", 10,
               _payload("东财", "https://e/1"), domain="financial")
        sc.set("苹果 2025 年营收", "xueqiu", 10,
               _payload("雪球", "https://x/1"), domain="financial")

        q = "苹果 2025 年营收预测"  # 与两条都相似，但与两者都不完全相同
        got_em = {c["query"] for c in sc._l2.find_similar(q, "eastmoney", "financial", limit=50)}
        got_xq = {c["query"] for c in sc._l2.find_similar(q, "xueqiu", "financial", limit=50)}

        assert got_em == {"苹果 2025 营收"}, f"eastmoney 只应看见自己的缓存，实际 {got_em}"
        assert got_xq == {"苹果 2025 年营收"}, f"xueqiu 只应看见自己的缓存，实际 {got_xq}"

    def test_auto_is_not_a_wildcard(self, sc):
        """engine='auto' 现在是**真实的请求身份**，不再是通配。

        语义变更（2026-09-19）：engine 列从「路由出来的引擎组合」改存「请求侧
        身份」（用户点名的引擎，或 auto）——因为组合是决策结果、会被 adaptive
        学习器逐次改写，拿它当缓存键等于「用缓存让缓存失效」（实测 30% 的重复
        查询因此白跑网络）。

        通配的含义随之一并改变：以前 `auto` 不是列里会出现的值（组合列要么是
        `a+b` 要么是单引擎名），通配分支其实从未被走到；现在 `auto` 是真实
        请求身份，再通配就等于让「自动路由」的请求去吃「显式指定 eastmoney」
        的缓存，正是 v2.4.2 要挡的跨引擎串味。请求不同 → 结果不可互换。
        """
        sc.set("苹果 2025 营收", "eastmoney", 10,
               _payload("东财", "https://e/1"), domain="financial")
        sc.set("苹果 2025 年营收", "auto", 10,
               _payload("自动", "https://a/1"), domain="financial")

        got = {c["query"] for c in sc._l2.find_similar(
            "苹果 2025 年营收预测", "auto", "financial", limit=50)}
        assert got == {"苹果 2025 年营收"}, \
            f"auto 请求只应看见同为 auto 的缓存，实际 {got}"

    def test_multi_engine_request_excluded_from_soft_hit(self, sc):
        """逗号多引擎请求（`--engine a,b`）的融合产物不参与软命中。

        engine 列存请求身份后，多引擎的形状从 `a+b`（旧：路由组合）变成
        `a,b`（新：请求原串）。只挡 `+` 会让逗号多引擎请求偷偷进入软命中。
        """
        sc.set("苹果 2025 营收", "eastmoney,zhihu", 10,
               _payload("融合", "https://f/1"), domain="financial")
        got = sc._l2.find_similar("苹果 2025 年营收预测", "eastmoney,zhihu",
                                  "financial", limit=50)
        assert got == [], "多引擎融合产物不应被当作单引擎结果复用"

    def test_unknown_engine_sees_nothing(self, sc):
        """不存在的引擎名 → 空结果（修复前会返回任意引擎的缓存）。"""
        sc.set("小红书 AI 绘画 评价", "bilibili", 10,
               _payload("B站", "https://b/1"), domain="social")
        got = sc._l2.find_similar("小红书 AI 绘画 评价精选", "不存在的引擎", "social", limit=50)
        assert got == [], "未知引擎不应命中他人缓存"

    def test_fetch_and_evidence_are_isolated(self, sc):
        """fetch 与 evidence 作为不同 engine 值必须隔离（URL 词面近似场景）。"""
        url = "https://developers.openai.com/api/docs/guides/image-prompting"
        sc.set(url, "fetch", 10, _payload("正文", url), domain="general")
        # evidence 缓存的是「另一个 URL」，但词面与上面高度相似
        sc.set(url + ".md", "evidence", 10, _payload("证据分", url + ".md"), domain="general")

        got_fetch = {c["query"] for c in sc._l2.find_similar(url, "fetch", "general", limit=50)}
        # 同 engine 下 url 与 url.md 相似度 0.875，属合法的近重复软命中
        got_ev = {c["query"] for c in sc._l2.find_similar(url, "evidence", "general", limit=50)}

        assert got_ev == {url + ".md"}, f"evidence 不应串到 fetch 载荷，实际 {got_ev}"
        assert url not in got_fetch  # 精确同名被 cached_q == nq 跳过

    def test_combo_keys_excluded(self, sc):
        """组合键（多引擎拼接 `a+b`）不参与软命中：组合结果集不可与单引擎互换。"""
        sc.set("苹果 2025 营收", "eastmoney+xueqiu", 10,
               _payload("组合", "https://c/1"), domain="financial")
        got = sc._l2.find_similar("苹果 2025 年营收", "auto", "financial", limit=50)
        assert got == [], "组合键不应作为单引擎查询的软命中来源"


class TestSemanticMarkerPreserved:
    """软命中标记必须在 L1 往返后保留。"""

    def test_marker_survives_l1_second_hit(self, sc):
        """第 1 次软命中(L2) → L1 回填 → 第 2 次命中(L1) 仍须带软命中标记。"""
        sc.set("苹果 2025 营收", "eastmoney", 5,
               _payload("东财", "https://e/1"), domain="financial")

        o1 = sc.get("苹果 2025 年营收", "eastmoney", 5,
                    domain="financial", mode="auto", depth="fast")
        assert o1 is not None and o1.get("_semantic_hit") is True

        o2 = sc.get("苹果 2025 年营收", "eastmoney", 5,
                    domain="financial", mode="auto", depth="fast")
        assert o2 is not None
        assert o2.get("_semantic_hit") is True, (
            "L1 二次命中丢失软命中标记 → 伪装成硬命中（修复前行为）"
        )
        assert o2.get("_semantic_query") == "苹果 2025 营收"
        assert o2.get("_semantic_similarity") is not None

    def test_l1_payload_carries_ttl(self, sc):
        """L1 回填必须带 _ttl/_ts，否则 L1 分支的过期判断失效。"""
        sc.set("苹果 2025 营收", "eastmoney", 5,
               _payload("东财", "https://e/1"), domain="financial")
        sc.get("苹果 2025 年营收", "eastmoney", 5,
               domain="financial", mode="auto", depth="fast")

        key = sc._key("苹果 2025 年营收", "eastmoney", 5,
                      "financial", "auto", "fast", kind="combo")
        l1v = sc._l1.get(key) or {}
        assert l1v.get("_ttl"), "L1 载荷缺 _ttl → 过期判断失效"
        assert l1v.get("_ts"), "L1 载荷缺 _ts → 过期判断失效"


class TestQualifierIsolation:
    """限定符是过滤器：集合不同或载荷不同都不得软命中。"""

    def test_same_qualifier_different_payload_does_not_hit(self, sc):
        """原 bug 复现：同限定符、载荷不同（mcp vs memory）。

        修复前整串相似度 0.875 ≥ 0.7，软命中把 memory 的结果当 mcp 的交出去。
        """
        sc.set("keywords:pi-package memory", "npm", 10,
               _payload("pi-memory", "https://n/1"), domain="package_search")
        got = sc._l2.find_similar(
            "keywords:pi-package mcp", "npm", "package_search", limit=50)
        assert got == [], f"同限定符不同载荷不得软命中，实际 {got}"

    def test_different_qualifier_same_payload_does_not_hit(self, sc):
        """限定符不同 → 结果全集不同，载荷一模一样也不得互换。"""
        sc.set("keywords:mcp-server memory", "npm", 10,
               _payload("mcp", "https://n/1"), domain="package_search")
        got = sc._l2.find_similar(
            "keywords:pi-package memory", "npm", "package_search", limit=50)
        assert got == [], f"限定符不同不得软命中，实际 {got}"

    def test_same_qualifier_near_duplicate_still_hits(self, sc):
        """同限定符 + 载荷近重复 → 仍须软命中（修复不得把正常召回一起砍掉）。"""
        sc.set("keywords:pi-package memory", "npm", 10,
               _payload("pi-memory", "https://n/1"), domain="package_search")
        got = {c["query"] for c in sc._l2.find_similar(
            "keywords:pi-package memorys", "npm", "package_search", limit=50)}
        assert got == {"keywords:pi-package memory"}, f"同限定符近重复应命中，实际 {got}"

    def test_url_is_not_a_qualifier(self):
        """URL 的 `https:` 不得被当成限定符：否则 URL 近重复软命中被误杀。"""
        quals, payload = cache.split_qualifiers(
            "https://developers.openai.com/api/docs/guides/image-prompting")
        assert quals == frozenset(), f"URL 被误判成限定符：{quals}"
        assert payload.startswith("https://"), "载荷不应被切掉"

    def test_clock_time_is_not_a_qualifier(self):
        """`10:30` 不是限定符（键须以字母开头）。"""
        quals, payload = cache.split_qualifiers("10:30 的会议")
        assert quals == frozenset(), f"时间写法被误判成限定符：{quals}"
        assert payload == "10:30 的会议"

    def test_payload_kept_for_plain_query(self):
        """无限定符的查询：载荷即整串，行为不变。"""
        quals, payload = cache.split_qualifiers("苹果 2025 年营收")
        assert quals == frozenset()
        assert payload == "苹果 2025 年营收"


class TestModeDepthIsolation:
    """mode / depth 维度必须参与软命中过滤（2026-09-19 修复）。

    这是同一类串味的第三处，前两处（engine、限定符）修的时候漏了它。
    `_key` 刻意把 mode/depth 编进键、类 docstring 也承诺「depth / mode 隔离，
    防 fast/deep、budget 污染」，但软命中的 WHERE 只有 domain+engine：
    `--depth deep` 请求会软命中一条按 fast 写的条目（或反过来），拿到的结果
    集与请求档位不匹配，而结果里还报 `cached: true` / `reranker:
    "skipped_cache"`，调用方无从察觉。
    """

    def test_different_depth_does_not_hit(self, sc):
        """同 engine、同 domain、查询近重复，只有 depth 不同 → 不得软命中。"""
        sc.set("苹果 2025 营收", "octen", 10, _payload("fast", "https://a/1"),
               domain="financial_news", mode="auto", depth="fast")
        hit = sc.get("苹果 2025年 营收", "octen", 5, domain="financial_news",
                     mode="auto", depth="deep")
        assert hit is None, "depth 不同却软命中了 fast 条目"

    def test_different_mode_does_not_hit(self, sc):
        sc.set("苹果 2025 营收", "octen", 10, _payload("auto", "https://a/1"),
               domain="financial_news", mode="auto", depth="fast")
        hit = sc.get("苹果 2025年 营收", "octen", 5, domain="financial_news",
                     mode="budget", depth="fast")
        assert hit is None, "mode 不同却软命中了 auto 条目"

    def test_same_scope_still_hits(self, sc):
        """对照面：档位一致时近重复查询必须照旧软命中（能力不得被砍掉）。"""
        sc.set("苹果 2025 营收", "octen", 10, _payload("A", "https://a/1"),
               domain="financial_news", mode="auto", depth="fast")
        hit = sc.get("苹果 2025年 营收", "octen", 5, domain="financial_news",
                     mode="auto", depth="fast")
        assert hit is not None and hit.get("_semantic_hit"), "同档位应软命中"

    def test_find_similar_filters_by_scope(self, sc):
        """直接打 find_similar：不同档位的条目不得出现在候选里。"""
        sc.set("苹果 2025 营收", "octen", 10, _payload("A", "https://a/1"),
               domain="financial_news", mode="auto", depth="fast")
        sc.set("苹果 2025 年营收", "octen", 10, _payload("B", "https://b/1"),
               domain="financial_news", mode="deep", depth="deep")
        q = "苹果 2025 年营收预测"
        fast = {c["query"] for c in sc._l2.find_similar(
            q, "octen", "financial_news", limit=50, mode="auto", depth="fast")}
        deep = {c["query"] for c in sc._l2.find_similar(
            q, "octen", "financial_news", limit=50, mode="deep", depth="deep")}
        assert fast == {"苹果 2025 营收"}, f"fast 视角应只见 fast 条目，实际 {fast}"
        assert deep == {"苹果 2025 年营收"}, f"deep 视角应只见 deep 条目，实际 {deep}"

    def test_legacy_rows_without_scope_never_hit(self, sc):
        """迁移前的历史行（mode/depth 未知，存空串）不得参与软命中。

        空串与任何真实 mode/depth 都不相等，这是刻意的：默认成 auto/fast
        等于替历史行猜一个档位，deep 请求会命中一条其实按 fast 写的条目——
        正是要修的那个 bug。历史行由 `ALTER TABLE ... DEFAULT ''` 产生，
        这里直接走底层写入复现那个形态（上层 SearchCache.set 总会带上档位）。
        """
        sc._l2.set("legacy-row-key", "苹果 2025 营收", "octen", 10,
                   {"results": [{"title": "legacy", "url": "https://a/1"}]},
                   "financial_news", 3600)  # 不传 mode/depth → 存空串
        assert sc._l2.find_similar("苹果 2025年 营收", "octen", "financial_news",
                                   limit=50) == []


class TestCacheKeyIsRequestIdentity:
    """缓存键的引擎维度必须是**请求侧身份**，不是路由出来的组合（2026-09-19）。

    根因：`engines_combo` 是决策结果，被 adaptive 学习器按上一次搜索的成败逐次
    改写。拿结果当键就是「用缓存让缓存失效」。确定性复现（不靠网络运气）：

        route_query("python asyncio 教程")            -> [octen, anysearch]
        写入 12 次「anysearch 失败 / octen 成功」
        route_query("python asyncio 教程")            -> [octen, exa]

    同一查询、同一配置，只因为上一次搜索的结果就让组合变了。实测 20 条样本里
    最多 6 条（30%）重复查询因此白跑一遍网络，而查询/域/档位全都没变。

    route 决策缓存早就识别过同一模式（见 `_route_state_fingerprint` 刻意排除
    adaptive.db 的说明），结果缓存这条只是绕了一层。
    """

    def test_decision_carries_request_identity(self):
        from route import route_query
        assert route_query("随便什么查询")["engine_request"] == "auto"
        d = route_query("asyncio", engine_override="pypi")
        assert d["engine_request"] == "pypi", "用户点名的引擎必须原样进请求身份"

    def _decision(self, combo, domain="english_tech"):
        return {"domain": domain, "engine": combo[0], "engines_combo": list(combo),
                "engines": list(combo), "engine_request": "auto",
                "parallel": False, "mode": "auto", "depth": "fast",
                "reason": "test", "features": {}}

    def test_combo_drift_still_hits(self, monkeypatch, tmp_path):
        """打真实链路：同一请求身份、路由组合漂移 → 仍然命中。

        必须走 execute_search（而不是直接调 SearchCache.set/get）——直接调缓存
        只锁住了缓存自己的契约，键怎么算出来那一段没被覆盖，改回旧写法测试照样绿。
        """
        import search as S
        from engine_dispatch import DispatchResult

        def fake_dispatch(**kw):
            eng = kw["engines"][0]
            res = [{"title": f"{eng} 标题", "url": f"https://{eng}.example/1",
                    "snippet": "s", "source": eng}]
            return DispatchResult({eng: res},
                                  [{"engine": eng, "status": "ok",
                                    "results_count": 1, "latency_ms": 10}],
                                  {eng: 10}, 0, True, None, None, 10, None, 10)

        monkeypatch.setattr(S, "run_dispatch", fake_dispatch)
        # 恢复链走的是 engine_search 钩子（不是 run_dispatch），不打桩会真连
        # 网——2026-09-29 实测：env 隔离后恢复引擎报 skipped-missing-env，
        # 叠加失败态守卫，空载荷被拒绝入缓存，本用例随网络运气时红时绿。
        monkeypatch.setattr(S, "engine_search", lambda *a, **k: [])
        # octen 自备密钥（2026-09-29）：无密钥时真实 dispatch 判
        # skipped-missing-env，失败态守卫据此拒绝负缓存——本用例锁的
        # 是「请求身份键」契约，需要一次可缓存的运行（含空结果负缓存）
        monkeypatch.setenv("ARGO_OCTEN_API_KEY", "test-key")
        c = cache.SearchCache(db_path=str(tmp_path / "c.db"))

        r1 = S.execute_search("python asyncio 教程", self._decision(["octen", "anysearch"]),
                              3, 10, "fast", c, False, mode="auto")
        assert not r1.get("cached"), "第一次应为 miss"

        # adaptive 学习把组合改掉了，请求身份仍是 auto
        r2 = S.execute_search("python asyncio 教程", self._decision(["octen", "exa"]),
                              3, 10, "fast", c, False, mode="auto")
        assert r2.get("cached"), "组合漂移不应导致 miss（旧键会在这里失效）"

    def test_explicit_engine_does_not_share_auto_cache(self, monkeypatch, tmp_path):
        """对照面：显式 --engine 的隔离不能被这次改动削弱。"""
        import search as S
        from engine_dispatch import DispatchResult

        def fake_dispatch(**kw):
            eng = kw["engines"][0]
            res = [{"title": f"{eng} 标题", "url": f"https://{eng}.example/1",
                    "snippet": "s", "source": eng}]
            return DispatchResult({eng: res},
                                  [{"engine": eng, "status": "ok",
                                    "results_count": 1, "latency_ms": 10}],
                                  {eng: 10}, 0, True, None, None, 10, None, 10)

        monkeypatch.setattr(S, "run_dispatch", fake_dispatch)
        # 恢复链走的是 engine_search 钩子（不是 run_dispatch），不打桩会真连
        # 网——2026-09-29 实测：env 隔离后恢复引擎报 skipped-missing-env，
        # 叠加失败态守卫，空载荷被拒绝入缓存，本用例随网络运气时红时绿。
        monkeypatch.setattr(S, "engine_search", lambda *a, **k: [])
        # octen 自备密钥（2026-09-29）：无密钥时真实 dispatch 判
        # skipped-missing-env，失败态守卫据此拒绝负缓存——本用例锁的
        # 是「请求身份键」契约，需要一次可缓存的运行（含空结果负缓存）
        monkeypatch.setenv("ARGO_OCTEN_API_KEY", "test-key")
        c = cache.SearchCache(db_path=str(tmp_path / "c.db"))

        auto_d = self._decision(["octen", "exa"], domain="general_search")
        S.execute_search("asyncio", auto_d, 3, 10, "fast", c, False, mode="auto")

        exp_d = self._decision(["pypi"], domain="general_search")
        exp_d["engine_request"] = "pypi"
        r = S.execute_search("asyncio", exp_d, 3, 10, "fast", c, False, mode="auto")
        assert not r.get("cached"), "auto 的缓存不得被显式 pypi 请求命中"

    def test_cache_hit_reports_the_run_that_produced_it(self, monkeypatch, tmp_path):
        """命中响应必须自描述：报产出这批结果的那次运行，不是本次路由。

        键改成请求身份后，「本次路由」与「产出结果的运行」第一次可以不同——
        而本次路由根本没执行。报它等于报一个没跑过的计划，还会与同样来自缓存的
        engines_used / engine_outcomes 自相矛盾。
        """
        import search as S
        from engine_dispatch import DispatchResult

        def fake_dispatch(**kw):
            eng = kw["engines"][0]
            res = [{"title": f"{eng} 结果", "url": f"https://{eng}/1",
                    "snippet": "s", "source": eng}]
            return DispatchResult({eng: res},
                                  [{"engine": eng, "status": "ok",
                                    "results_count": 1, "latency_ms": 9}],
                                  {eng: 9}, 0, True, None, None, 9, None, 9)

        monkeypatch.setattr(S, "run_dispatch", fake_dispatch)
        # 恢复链走的是 engine_search 钩子（不是 run_dispatch），不打桩会真连
        # 网——2026-09-29 实测：env 隔离后恢复引擎报 skipped-missing-env，
        # 叠加失败态守卫，空载荷被拒绝入缓存，本用例随网络运气时红时绿。
        monkeypatch.setattr(S, "engine_search", lambda *a, **k: [])
        # octen 自备密钥（2026-09-29）：无密钥时真实 dispatch 判
        # skipped-missing-env，失败态守卫据此拒绝负缓存——本用例锁的
        # 是「请求身份键」契约，需要一次可缓存的运行（含空结果负缓存）
        monkeypatch.setenv("ARGO_OCTEN_API_KEY", "test-key")
        c = cache.SearchCache(db_path=str(tmp_path / "c.db"))

        d1 = self._decision(["octen", "anysearch"])
        d1["reason"] = "第一次路由"
        S.execute_search("q", d1, 3, 10, "fast", c, False, mode="auto")

        d2 = self._decision(["octen", "exa"])
        d2["reason"] = "第二次路由（未执行）"
        r2 = S.execute_search("q", d2, 3, 10, "fast", c, False, mode="auto")

        assert r2.get("cached")
        assert r2["engines"] == ["octen", "anysearch"], \
            f"命中却报了本次路由的组合：{r2['engines']}"
        assert r2["engines_combo"] == ["octen", "anysearch"]
        assert r2["route_reason"] == "第一次路由", \
            f"命中却报了没执行的那次路由理由：{r2['route_reason']}"
