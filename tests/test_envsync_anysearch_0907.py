#!/usr/bin/env python3
"""test_envsync_anysearch_0907 — env 同步 + anysearch 升权/多语言注入 + 配额计算方式 回归门。

2026-09-07 数据源权重盘点轮的三组修复：
  1. env 文件 → os.environ 同步（只填缺失、不覆盖已有、重复执行结果一致）——兼容计算方式：
     读取方保持标准 os.environ 直读不动，入口同步一份过去；
  2. ja/ko 查询 anysearch 前二注入（策略/预算截断之后，防 must_keep 换位
     挤出）；english_tech/chinese_general 升权；
  3. quota_profiles 保持一致服务商真实计算方式（zhihu 5000/anysearch 2000/
     zhihu_global 5000）+ null 引擎计数周期归零。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import engine_env  # noqa: E402
import route_combo  # noqa: E402  # noqa: F401  （_isolated_route 打桩 learner 单例）
from route import route_query  # noqa: E402
from quota import QuotaManager  # noqa: E402


class TestEnvFileSync(unittest.TestCase):
    def _sync_with_envfile(self, content: str):
        """读一份临时 env 文件并同步进环境，前后都把缓存清干净。

        此前是「存旧签名 → 置空强制重读 → 用完恢复旧签名」：缓存内容与签名
        是两份状态，只恢复签名会让二者错位——恢复的是真实文件的签名，缓存里
        却是临时文件的内容，于是此后 get_env 直接返回这份缓存，
        ~/.config/argo/env 里的密钥在本进程内全部读不到。实测现场：同进程内
        先走过一次路由，再跑本类，后续带密钥的引擎全被判为不可用。
        清空（而不是恢复）才是安全终态：下次读取必重读真实文件。
        """
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(content)
        tmp.close()
        engine_env.reset_envfile_cache()
        try:
            with patch.object(engine_env, "_envfile_paths",
                              return_value=[Path(tmp.name)]):
                injected = engine_env.sync_envfile_to_environ()
        finally:
            engine_env.reset_envfile_cache()
            Path(tmp.name).unlink(missing_ok=True)
        return injected

    def test_fill_missing(self):
        injected = self._sync_with_envfile("export TEST_SYNC_A=tok_a\n")
        try:
            self.assertIn("TEST_SYNC_A", injected)
            self.assertEqual(engine_env.os.environ.get("TEST_SYNC_A"), "tok_a")
        finally:
            engine_env.os.environ.pop("TEST_SYNC_A", None)

    def test_no_overwrite_existing(self):
        engine_env.os.environ["TEST_SYNC_B"] = "keep_me"
        try:
            injected = self._sync_with_envfile("TEST_SYNC_B=file_val\n")
            self.assertNotIn("TEST_SYNC_B", injected)
            self.assertEqual(engine_env.os.environ["TEST_SYNC_B"], "keep_me")
        finally:
            engine_env.os.environ.pop("TEST_SYNC_B", None)

    def test_idempotent(self):
        injected1 = self._sync_with_envfile("TEST_SYNC_C=v1\n")
        injected2 = self._sync_with_envfile("TEST_SYNC_C=v1\n")
        try:
            self.assertIn("TEST_SYNC_C", injected1)
            self.assertEqual(injected2, [])
        finally:
            engine_env.os.environ.pop("TEST_SYNC_C", None)


class TestEnvfileCacheInvariant(unittest.TestCase):
    """清空 env 缓存必须让下次读取真的重读磁盘。

    这是上面 TestEnvFileSync 曾经的翻车点：缓存内容与签名是两份状态，
    「只恢复签名、不恢复内容」会让二者错位——签名与真实文件对得上，函数
    直接返回那份不属于该文件的缓存，本进程内密钥读取全部失效。现场表现是
    「单独跑通过、与别的测试同跑就失败」，排查成本极高。所以把清空后的
    行为钉成契约：哪怕缓存里塞了假内容，清空一次之后也读不回来。
    """

    def test_reset_discards_cache_and_rereads_disk(self):
        """清空之后必须真的重读磁盘，而不是返回空表或被顶替的内容。

        只清内容不清签名是最危险的一种写法：签名与真实文件对得上，读取函数
        直接返回那张空表，本进程内所有密钥凭空消失（路由把带密钥的引擎全判
        为不可用）。所以这里断言的是「清空后仍读得到真实文件的内容」。

        构造「签名与内容错位」属于外部乱写内部状态，由静态检查
        （test_no_external_writes_to_envfile_internals）负责拦，不在这里做。
        """
        engine_env._envfile_load()  # 先让缓存就位
        engine_env.reset_envfile_cache()
        keys = engine_env._envfile_load()
        if any(p.exists() for p in engine_env._envfile_paths()):
            self.assertTrue(keys, "真实 env 文件存在，清空后却读不到内容")
        engine_env.reset_envfile_cache()


class TestMultilingualAnysearchInjection(unittest.TestCase):
    """ja/ko 查询 anysearch 前二（策略截断后注入，防 must_keep 换位挤出）。"""

    def test_ja_geo_anysearch_front2(self):
        d = _isolated_route("東京 おすすめ ラーメン 屋 はどこ")
        combo = d.get("engines_combo") or []
        self.assertIn("anysearch", combo[:2],
                      f"ja 查询 anysearch 应在前二: {combo}")

    def test_ko_anysearch_front2(self):
        d = _isolated_route("서울 최고의 카페 추천 위치")
        combo = d.get("engines_combo") or []
        self.assertIn("anysearch", combo[:2],
                      f"ko 查询 anysearch 应在前二: {combo}")

    def test_ja_catchall_anysearch_front2(self):
        d = _isolated_route("東京タワー の高さ は いくつ")
        combo = d.get("engines_combo") or []
        self.assertIn("anysearch", combo[:2],
                      f"ja catch-all 也应注入 anysearch: {combo}")

    def test_zh_vertical_not_injected(self):
        d = _isolated_route("贵州茅台 股价")
        combo = d.get("engines_combo") or []
        self.assertEqual(combo[0], "sina_quote",
                         f"zh 点查域 primary 不得被注入顶掉: {combo}")

    def test_domain_primary_stays_first(self):
        d = _isolated_route("東京 おすすめ ラーメン 屋 はどこ")
        self.assertEqual(d.get("engines_combo", [None])[0],
                         "local_openstreetmap",
                         f"注入不得顶掉域主源: {d.get('reason')}")


class TestQuotaProfilesAligned(unittest.TestCase):
    """保持一致服务商面板真实计算方式（2026-09-06）：知乎搜索 5000/天、AnySearch
    2000/天、知乎全网搜 5000/天。修复前 zhihu=1000 会在本地提前封禁
    （浪费 80% 额度）、anysearch=null 则完全没有次数限制保护。"""

    def setUp(self):
        # 状态文件隔离 + 同模块对象构造（2026-09-27）：
        # 1) get_remaining_ratio 的周期重置会在文件锁内 _load_state() 重读磁盘。
        #    不隔离时，同进程前序搜索写出的 quota.json 会把本用例注入的内存状态
        #    整份冲掉（实测 KeyError: '_test_null_eng'）。
        # 2) **必须函数内 import**：test_argo_paths 的参数化用例会
        #    sys.modules.pop("quota") 后重新 import——本文件顶层的
        #    `from quota import QuotaManager` 绑的是旧模块对象，override 打在新
        #    对象上，两侧错位后隔离静默失效（与 test_quota_lock_scope 的 _quota()
        #    同一理由：被测代码经模块全局查找，补丁必须打在同一个对象上）。
        import quota as _quota_mod
        self._d = tempfile.mkdtemp()
        self._orig_state_path = _quota_mod.QUOTA_STATE_PATH
        _quota_mod.QUOTA_STATE_PATH = Path(self._d) / "quota.json"
        self.addCleanup(self._restore)

        self.qm = _quota_mod.QuotaManager()

    def _restore(self):
        import quota as _quota_mod
        _quota_mod.QUOTA_STATE_PATH = self._orig_state_path

    def test_limits(self):
        cases = {"zhihu": 5000, "anysearch": 2000, "zhihu_global": 5000}
        for eng, limit in cases.items():
            p = self.qm._profiles.get(eng, {})
            self.assertEqual(p.get("limit"), limit, eng)
            self.assertEqual(p.get("period"), "day", eng)

    def test_null_limit_engine_counter_resets_periodically(self):
        """null 引擎 get_remaining_ratio 也要按周期归零（原实现提前 return
        跳过重置，遥测永久累计）。"""
        qm = self.qm
        qm._state["_test_null_eng"] = {
            "used": 123, "errors": 0, "calls": [],
            "last_reset": time.time() - 2 * 86400,
        }
        with patch.object(qm, "_save_state", lambda: None):
            ratio = qm.get_remaining_ratio("_test_null_eng")
        try:
            self.assertEqual(ratio, 1.0)
            self.assertEqual(qm._state["_test_null_eng"]["used"], 0,
                             "null 引擎计数未按周期归零")
        finally:
            qm._state.pop("_test_null_eng", None)


class _AllowAllBreaker:
    """隔离共享熔断/配额状态：combo 检查只验路由语义，不验运行时健康。"""

    def allow(self, eng):
        return True, "closed"

    def get_negative(self, *a, **k):
        return None

    def status(self, eng):
        return {"state": "closed"}

    def record_success(self, *a, **k):
        pass

    def record_failure(self, *a, **k):
        pass

    def set_negative(self, *a, **k):
        pass

    def clear_negative(self, *a, **k):
        pass


def _isolated_route(query: str, **kwargs):
    """route_query 但打桩熔断器、配额与自适应学习器（并发 live 评测会写共享
    状态文件/DB，直连真实单例会让 combo 检查偶发抖动）。

    learner 是第三个共享单例：engine_perf 表在同进程的用例之间累积，
    test_cache_engine_isolation 等跑完后 byted/octen 的分数足以在
    「同族按分重排」里把 zhihu_global 挤出 news_realtime 的前二
    （实测 2026-09-27：组合跑必挂、单独跑全绿）。打桩成 None 后 learner
    的过滤与重排都不参与，combo 只由域声明与注入逻辑决定——正是本类
    断言想锁的行为。
    """
    from unittest.mock import MagicMock
    with patch("circuit_breaker.get_breaker",
               return_value=_AllowAllBreaker()), \
         patch("quota.get_quota_manager", return_value=MagicMock()), \
         patch("route_combo._adaptive_learner", None):
        return route_query(query, **kwargs)


class TestZhihuGlobalUtilization(unittest.TestCase):
    """zhihu_global（全网搜 SearchDB=all，5000/天）防饿死回归门。

    死因链：learner 同族按分重排把它挪到 anysearch 之后 + auto 预算=2 截断
    → 自家主域永远轮不上（37 天仅 53 次）。修复：zh 查询下 zhihu_content
    固定 [zhihu, zhihu_global] 成对、learner 过滤豁免；news_realtime 接入 #2。
    """

    def test_zh_opinion_pair(self):
        d = _isolated_route("怎么看待 AI 编程工具取代程序员")
        combo = d.get("engines_combo") or []
        self.assertEqual(combo[:2], ["zhihu", "zhihu_global"],
                         f"观点查询应为站内+全网搜成对: {combo}")

    def test_news_intent_pair(self):
        d = _isolated_route("新能源车 销量 最新新闻")
        self.assertEqual(d.get("domain"), "news_realtime")
        combo = d.get("engines_combo") or []
        self.assertIn("zhihu_global", combo[:2],
                      f"新闻意图 zhihu_global 应在前二: {combo}")

    def test_error_item_not_silent_empty(self):
        """HTTP 失败必须返回 error item（原静默 []，把鉴权失败伪装成没结果）。"""
        import engines
        import urllib.error
        env = patch.dict("os.environ", {"ZHIHU_ACCESS_SECRET": "test_secret"})
        with env, patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "u", 401, "Unauthorized", {}, None)):
            res = engines.search("测试查询", "zhihu_global", n=5, timeout=5)
        self.assertTrue(res and isinstance(res[0], dict) and "error" in res[0],
                        f"HTTP 401 应产生 error item: {res}")


class TestZhihuUserEngine(unittest.TestCase):
    """zhihu_user（个人数据：本人创作/收藏/关注）接入回归门。

    查本人数据用 Access Secret 直调无需 OAuth；路由上须压过 social 泛域
    （收藏/关注是社交平台通用功能词）与 zhihu_content（知乎词面）。
    """

    def test_registered(self):
        from config import load_config, get_engines
        spec = get_engines(load_config()).get("zhihu_user")
        self.assertIsNotNone(spec, "zhihu_user 未注册")
        self.assertEqual(spec.get("type"), "zhihu_user")
        self.assertEqual(spec.get("family"), "personal_data")

    def test_routing_trio_distinct(self):
        """站内/站外/个人数据三源语义各自路由（知乎源分工的根）。"""
        d1 = route_query("怎么看待 AI 编程工具取代程序员")
        self.assertEqual(d1.get("domain"), "zhihu_content", "观点→站内")
        d2 = route_query("我的收藏")
        self.assertEqual(d2.get("domain"), "zhihu_user_data", "个人数据意图→个人域")
        d3 = route_query("我的知乎")
        self.assertEqual(d3.get("domain"), "zhihu_user_data")

    def test_sub_intent_parsing(self):
        from engines_builders_cn import _parse_zhihu_user_intent
        cases = [
            ("我的回答 点赞最多", ("contents", {"ContentType": "answer"})),
            ("我的文章", ("contents", {"ContentType": "article"})),
            ("我的收藏", ("collections", {})),
            ("我的收藏夹", ("favlists", {})),
            ("我关注的人", ("followees", {})),
            ("我的知乎", ("contents", {"ContentType": "all"})),
        ]
        for q, (endpoint, extra) in cases:
            got_endpoint, got_extra, _ = _parse_zhihu_user_intent(q)
            self.assertEqual((got_endpoint, got_extra), (endpoint, extra), q)

    def test_error_item_not_silent_empty(self):
        import engines
        import urllib.error
        env = patch.dict("os.environ", {"ZHIHU_ACCESS_SECRET": "test_secret"})
        with env, patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "u", 401, "Unauthorized", {}, None)):
            res = engines.search("我的回答", "zhihu_user", n=5, timeout=5)
        self.assertTrue(res and isinstance(res[0], dict) and "error" in res[0],
                        f"HTTP 401 应产生 error item: {res}")


if __name__ == "__main__":
    unittest.main()
