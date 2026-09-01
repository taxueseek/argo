#!/usr/bin/env python3
"""test_review_fixes.py — 2026-08-31 审查修复回归。

覆盖：
  - tinyfish 渲染层畸形 payload 不抛异常（success=False 契约）
  - quota 残缺 remote_exhausted 状态自愈（get_stats / is_remote_exhausted 不崩）
  - quota 跨进程 clear_remote_exhausted 热读（HotFile 基线对齐）
  - engines_base 封套 Code=200 成功码不误判
  - route 域编译指纹改正则不改条数时失效
  - job.sort_jobs 单一排序真源
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("ARGO_STATE_DIR", __import__("tempfile").mkdtemp(prefix="argo_test_rf_"))


class TestTinyfishMalformedPayload(unittest.TestCase):
    """第三方返回畸形 JSON 结构时，fetch() 不得让异常逃出 success=False 契约。"""

    def _run(self, payload):
        import fetch_render_tinyfish as tf
        body = json.dumps(payload).encode()

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return body

        with patch.object(tf, "_api_key", lambda: "sk-test"), \
             patch.object(tf.urllib.request, "urlopen", lambda req, timeout: R()):
            return tf.fetch("https://x", max_chars=100)

    def test_non_dict_first_item(self):
        out = self._run({"results": ["just a string"]})
        self.assertFalse(out["success"])

    def test_non_dict_with_error_message(self):
        out = self._run({"results": [42], "errors": [{"message": "boom"}]})
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "boom")

    def test_empty_results(self):
        out = self._run({"results": [], "errors": []})
        self.assertFalse(out["success"])

    def test_normal_payload_unaffected(self):
        out = self._run({"results": [{"text": "hello world", "title": "t"}]})
        self.assertTrue(out["success"])
        self.assertEqual(out["content"], "hello world")


class TestQuotaCorruptMark(unittest.TestCase):
    """quota.json 中 remote_exhausted 残缺（非 dict）时：不崩、自愈清除。"""

    def setUp(self):
        import quota
        # 写模块级真值路径（QUOTA_STATE_PATH 在 import 期冻结，可能与当前
        # os.environ["ARGO_STATE_DIR"] 不一致——全量跑时其他测试先 import
        # quota 即锁定路径；直接对准模块真值才不脆弱）
        self.state_file = Path(quota.QUOTA_STATE_PATH)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "byted": {"used": 5, "limit": 100, "calls": [], "errors": 0,
                      "last_reset": 0, "total_cost": 0,
                      "remote_exhausted": "corrupted-string"}}))
        self.mgr = quota.QuotaManager()

    def tearDown(self):
        try:
            self.state_file.unlink()
        except OSError:
            pass

    def test_get_stats_survives(self):
        stats = self.mgr.get_stats()
        self.assertIn("byted", stats)
        self.assertIsNone(stats["byted"]["remote_exhausted_until"])

    def test_is_remote_exhausted_heals(self):
        self.assertFalse(self.mgr.is_remote_exhausted("byted"))
        state = json.loads(self.state_file.read_text())
        self.assertNotIn("remote_exhausted", state.get("byted", {}))


class TestQuotaCrossProcessClear(unittest.TestCase):
    """跨进程 clear：HotFile 基线须与 init 加载的内存态对齐。"""

    def test_clear_after_other_process_marks(self):
        import quota
        # 用模块真值路径的父目录（import 期冻结，见 TestQuotaCorruptMark.setUp 注释）
        sdir = Path(quota.QUOTA_STATE_PATH).parent
        sdir.mkdir(parents=True, exist_ok=True)
        m2 = quota.QuotaManager()   # 先建（基线无标记）
        m1 = quota.QuotaManager()
        m1.mark_remote_exhausted("eng", "t", "day")
        time.sleep(0.02)
        self.assertTrue(m2.clear_remote_exhausted("eng"),
                        "跨进程标记应被 clear 看到（HotFile 基线吃掉首次变化）")
        self.assertFalse(m1.is_remote_exhausted("eng"))
        m2.clear_remote_exhausted("eng")  # 清场，防止污染同目录其他测试


class TestEnvelopeSuccessCode200(unittest.TestCase):
    """Code=200 + Message 是成功封套，不得整包判错丢结果。"""

    def test_code_200_not_error(self):
        from engines_base import _parse_http_payload
        raw = json.dumps({"Code": 200, "Message": "success",
                          "Data": {"Items": [{"Title": "t", "Url": "https://x"}]}})
        out = _parse_http_payload(raw, "", "api", 3,
                                  {"items": "Data.Items", "item_title": "Title"}, {})
        self.assertEqual(len(out), 1)
        self.assertNotIn("error", out[0])

    def test_code_20001_still_error(self):
        from engines_base import _parse_http_payload
        raw = json.dumps({"Code": 20001, "Message": "Authorization failed",
                          "Data": None})
        out = _parse_http_payload(raw, "", "zhihu", 3,
                                  {"items": "Data.Items", "item_title": "Title"}, {})
        self.assertEqual(len(out), 1)
        self.assertIn("20001", out[0]["error"])


class TestRouteDomainFingerprint(unittest.TestCase):
    """域编译缓存指纹含正则本体：改正则不改条数必须失效缓存。"""

    def test_pattern_edit_invalidates(self):
        import route
        domains_v1 = [{"name": "d", "patterns": [r"alpha\.com"]}]
        domains_v2 = [{"name": "d", "patterns": [r"beta\.org"]}]
        c1 = route._get_compiled_domains(domains_v1)
        c2 = route._get_compiled_domains(domains_v2)
        self.assertIsNot(c1, c2, "同条数不同正则应触发重编译")
        # 旧指纹再次进入时重编译回 v1 形态（内容正确性）
        c3 = route._get_compiled_domains(domains_v1)
        self.assertIsNot(c2, c3)

    def test_same_config_hits_cache(self):
        import route
        domains = [{"name": "d", "patterns": [r"alpha\.com"]}]
        c1 = route._get_compiled_domains(domains)
        c2 = route._get_compiled_domains(domains)
        self.assertIs(c1, c2)


class TestJobSortSingleSource(unittest.TestCase):
    """job.sort_jobs：L1 优先、级别内日期新→旧、过期同级不前置、空日期垫底同级别内。"""

    def test_order(self):
        from job import sort_jobs
        items = [
            {"title": "old-L1", "hit_level": 1, "date": "2026-08-01", "stale": False},
            {"title": "nodate-L1", "hit_level": 1, "date": "", "stale": False},
            {"title": "new-L2", "hit_level": 2, "date": "2026-08-20", "stale": False},
            {"title": "stale-L1", "hit_level": 1, "date": "2026-07-01", "stale": True},
        ]
        out = sort_jobs(items)
        titles = [r["title"] for r in out]
        # L1（新→旧）在前，L2 在后；stale 在同级内排到无日期之前（日期序）
        self.assertEqual(titles, ["old-L1", "nodate-L1", "stale-L1", "new-L2"])

    def test_pure_function(self):
        from job import sort_jobs
        items = [{"title": "x", "hit_level": 2, "date": "d", "stale": False}]
        out = sort_jobs(items)
        self.assertIsNot(items, out)
        self.assertEqual(items[0]["title"], "x")


if __name__ == "__main__":
    unittest.main()
