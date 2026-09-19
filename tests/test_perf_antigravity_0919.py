#!/usr/bin/env python3
"""test_perf_antigravity_0919.py — Antigravity 会话 449c220f 三项修复的回归锁。

## 背景

2026-09-19 的 Antigravity 会话（claude-opus-4-6-thinking）对 argo 起过一轮
「性能 + 缺陷」审查，三个子代理产出了报告后因 429 QUOTA_EXHAUSTED 中断，
结论**一条都没落地**。本文件锁的是其中经实测确认、且逐个复核后决定采纳的
三项（第四项 deepcopy→json 经实测为**反向优化**，已否决，理由见下）。

## 三项不变式

  1. **引擎权重缓存等价 + 覆盖**：`_engine_weight` 加结果缓存后，返回**值**
     必须与无缓存实现逐个一致（含 lang 维度），且 300 次调用的重复计算被
     消除。缓存 TTL 必须 ≤ 底层可靠性窗口 `_REL_FACTOR_TTL`，否则会把熔断
     状态变化多冻结一段时间——这是「加缓存」最容易引入的静默缺陷。

  2. **:memory: 缓存跨线程可用**：`SQLiteCache(":memory:")` 随 SearchCache
     常驻，MCP 长驻进程里首次访问的线程未必是建连接的线程。默认
     sqlite3 的同线程校验会抛 `ProgrammingError` 让整层内存缓存不可用。

  3. **tfidf_router 延迟导入**：`import route` 不得拉起 tfidf_router；但
    真正走语义路由时仍须正常工作（延迟导入不能变成「不导入」）。

## 为什么第 4 项（deepcopy → json）被否决

子代理报告称 `cache.py` 的 `copy.deepcopy` 比 `json.loads(json.dumps())`
慢，建议替换。**实测相反**：真实线上 cache.db 载荷（14.8KB flat dict，
键为 title/url/snippet/source/_engine/_elapsed）实测 deepcopy 0.0147ms
vs json 往返 0.0553ms，deepcopy **快 3.7 倍**。json 仅在深层嵌套 dict 上
占优（nested-300：1.94 vs 1.57ms），而 argo 的缓存载荷正是 flat 结构。
采纳该建议会引入 3.7 倍回归——本文件用计时断言把这个结论钉死，防止将来
有人照着那份报告再改一次。
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestEngineWeightCache:
    """不动式 1：_engine_weight 结果缓存。"""

    def test_cache_preserves_values_exactly(self):
        """缓存不得改变任何 (source, lang) 组合的返回值。"""
        import search

        cases = [
            ("wikipedia", None), ("twitter", None), ("arxiv", None),
            ("不存在的引擎", None), ("local_bing/sina_quote", None),
            ("wikipedia/不存在的引擎", None), ("", None),
            ("wikipedia", "zh"), ("arxiv", "en"), ("eastmoney", "zh"),
        ]
        search.invalidate_engine_weight_cache()
        cold = [search._engine_weight(s, lang=l) for s, l in cases]
        # 预热后再取一次：必须逐一相等（缓存命中路径）
        warm = [search._engine_weight(s, lang=l) for s, l in cases]
        assert cold == warm, f"缓存改变了返回值: {cold} != {warm}"
        # 再全量 invalidate 后重算，仍须一致
        search.invalidate_engine_weight_cache()
        again = [search._engine_weight(s, lang=l) for s, l in cases]
        assert cold == again

    def test_cache_actually_hits(self):
        """缓存必须真的被命中——否则本优化是自证式（只断言值相等抓不到没生效）。"""
        import search

        search.invalidate_engine_weight_cache()
        search._engine_weight("wikipedia", lang="zh")
        # 篡改缓存值：若实现真的读缓存，这里必须读到我塞的哨兵值
        search._weight_cache[("wikipedia", "zh")] = (0.123456, time.time() + 60)
        try:
            assert search._engine_weight("wikipedia", lang="zh") == 0.123456, (
                "缓存未被读取：返回值不是哨兵，说明 _engine_weight 仍在每次重算"
            )
        finally:
            search.invalidate_engine_weight_cache()

    def test_ttl_not_longer_than_reliability_window(self):
        """缓存 TTL 不得超过底层可靠性窗口，否则会把熔断状态多冻结一段时间。

        这是「加缓存」最容易引入的静默缺陷：_single_reliability 自己带 30s
        TTL，若外层缓存比它长，熔断打开后权重会被旧值压住。
        """
        import search

        search.invalidate_engine_weight_cache()
        search._engine_weight("wikipedia", lang=None)
        _, expires_at = search._weight_cache[("wikipedia", "")]
        remaining = expires_at - time.time()
        assert remaining <= search._REL_FACTOR_TTL + 1.0, (
            f"权重缓存 TTL({remaining:.1f}s) 超过可靠性窗口"
            f"({search._REL_FACTOR_TTL}s)：熔断状态变化会被多冻结"
        )

    def test_repeated_calls_are_cheaper(self):
        """300 条结果的重复调用必须显著快于逐次重算（计时断言，非计数自证）。"""
        import search

        srcs = ["local_bing/sina_quote", "arxiv", "eastmoney", "byted",
                "duckduckgo", "openalex", "crossref"]
        N = 3000

        search.invalidate_engine_weight_cache()
        # 未预热：每次都 miss（最坏情况，等价于旧实现）
        t0 = time.perf_counter()
        for i in range(N):
            search.invalidate_engine_weight_cache()
            search._engine_weight(srcs[i % len(srcs)], lang="zh")
        cold = (time.perf_counter() - t0) / N

        # 预热后：全部命中
        t0 = time.perf_counter()
        for i in range(N):
            search._engine_weight(srcs[i % len(srcs)], lang="zh")
        warm = (time.perf_counter() - t0) / N

        assert warm < cold, f"缓存后并未更快: warm={warm:.3e} cold={cold:.3e}"
        assert warm * 3 < cold, (
            f"提速不足 3 倍（warm={warm*1e6:.2f}us cold={cold*1e6:.2f}us），"
            "缓存可能没真正生效"
        )


class TestMemoryCacheThreadSafety:
    """不动式 2：:memory: 缓存的跨线程可用性。"""

    def test_memory_cache_usable_from_another_thread(self):
        """读到另一个线程建的 :memory: 连接不得抛 ProgrammingError。

        旧实现必红：sqlite3.connect(":memory:") 默认 check_same_thread=True，
        子线程访问抛 ProgrammingError: SQLite objects created in a thread
        can only be used in that same thread.
        """
        from cache import SQLiteCache

        c = SQLiteCache(db_path=":memory:")
        c.set("k1", "q", "e", 5, {"results": [{"title": "t", "url": "u"}]})
        assert c.get("k1") is not None, "主线程自读失败，前置条件不成立"

        errors: list[Exception] = []

        def worker():
            try:
                c.get("k1")
            except Exception as e:  # noqa: BLE001 — 要把任何异常形态都记下来
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)

        assert not errors, f"子线程访问 :memory: 缓存失败: {errors[0]!r}"

    def test_memory_connection_allows_cross_thread(self):
        """直接验证连接参数——比走 API 更贴近根因，报错信息也更明确。"""
        from cache import SQLiteCache

        c = SQLiteCache(db_path=":memory:")
        conn = c._connect()
        assert conn is not None
        # 真正有牙的判据：在别的线程里用它
        err: list[Exception] = []

        def worker():
            try:
                conn.execute("SELECT 1").fetchone()
            except Exception as e:  # noqa: BLE001
                err.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not err, f"连接不可跨线程使用: {err[0]!r}"


class TestTfidfLazyImport:
    """不动式 3：tfidf_router 延迟导入。"""

    def test_route_import_does_not_pull_tfidf_router(self):
        """import route 不得拉起 tfidf_router（延迟导入不能退化成顶层导入）。"""
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r);"
            "import route;"
            "print('tfidf_router' in sys.modules)"
        ) % str(SCRIPTS_DIR)
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, f"子进程导入 route 失败: {out.stderr[-500:]}"
        assert out.stdout.strip() == "False", (
            "import route 把 tfidf_router 一起拉起来了——延迟导入失效"
        )

    def test_semantic_route_stays_patchable(self):
        """延迟导入**不得**删掉 route.semantic_route 这个可 patch 的模块属性。

        这是本项改动第一次落地的真实翻车点：最初把 import 挪进函数内部，
        导致 `patch("route.semantic_route")` 直接 AttributeError，
        tests/test_multilingual_routing.py 的 3 条用例变红。改用模块级
        __getattr__（PEP 562）后两个目标同时成立。本用例把该契约钉死。
        """
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r);"
            "import route;"
            "from unittest.mock import patch;"
            "scores=[('arxiv',0.9,'')];"
            "p=patch('route.semantic_route', return_value=scores);"
            "m=p.start();"
            "print('PATCHED' if route.semantic_route('q')==scores else 'WRONG')"
        ) % str(SCRIPTS_DIR)
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, (
            "route.semantic_route 不可 patch（延迟导入把模块属性删掉了）: "
            f"{out.stderr[-600:]}"
        )
        assert "PATCHED" in out.stdout, out.stdout

    def test_routing_still_works_after_lazy_import(self):
        """延迟导入不能变成「不导入」：真正走语义路由时必须仍能工作。"""
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r);"
            "from route import route_query;"
            "r = route_query('量子纠缠的实验验证', mode='auto');"
            "print('OK' if r.get('domain') else 'NO_DOMAIN')"
        ) % str(SCRIPTS_DIR)
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=180,
        )
        assert out.returncode == 0, f"路由调用失败: {out.stderr[-800:]}"
        assert "OK" in out.stdout, f"路由未产出 domain: {out.stdout!r}"


class TestDeepcopyIsNotRegressedToJson:
    """反例锁：防止有人照搬那份报告把 deepcopy 改成 json 往返。"""

    def test_deepcopy_beats_json_on_flat_payload(self):
        """flat 载荷（argo 缓存的实际形态）上 deepcopy 必须快于 json 往返。

        此断言的作用是**否决一项错误的优化建议**：若将来有人把
        cache.py 的 copy.deepcopy 换成 json.loads(json.dumps(...))，
        本用例会指出该方向在真实载荷形态上就是慢的。
        """
        hit = {
            "results": [
                {"title": f"标题{i}" + "x" * 60,
                 "url": f"https://example.com/{i}",
                 "snippet": "摘要内容" * 40,
                 "source": f"engine_{i % 7}",
                 "_engine": f"engine_{i % 7}",
                 "_elapsed": 1.23}
                for i in range(60)
            ],
            "_ttl": 3600, "_ts": time.time(),
        }
        N = 200
        t0 = time.perf_counter()
        for _ in range(N):
            copy.deepcopy(hit)
        t_deep = (time.perf_counter() - t0) / N
        t0 = time.perf_counter()
        for _ in range(N):
            json.loads(json.dumps(hit))
        t_json = (time.perf_counter() - t0) / N

        assert t_deep < t_json, (
            f"flat 载荷上 deepcopy({t_deep*1e6:.1f}us) 应当快于 "
            f"json 往返({t_json*1e6:.1f}us)；若此断言失败说明运行环境变了，"
            "需要重新评估 cache.py 的深拷贝策略"
        )

    def test_cache_still_uses_deepcopy(self):
        """现状锁：_read 的深拷贝语义仍在（缓存持有数据所有权，下游不得污染）。"""
        import inspect

        import cache

        src = inspect.getsource(cache.SearchCache._read)
        assert "deepcopy" in src, (
            "cache._read 的深拷贝被移除了——下游对 results 的原地改写"
            "（_engine 标记、rerank 字段）会污染 L1 缓存中的同一份对象"
        )
