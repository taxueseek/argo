#!/usr/bin/env python3
"""tfidf_router 热重载边界回归（2026-09-28）。

domain_profiles.json 在「mtime 判变 → vectors 已清空 → 文件消失」的重载窗口
里，_ensure_loaded 走 not-exists 分支置 _loaded=True；若 _profiles_mtime 不
复位，文件以**原 mtime**还原（回收站恢复 / 原子写回滚）时被判「未变化」，
路由从此带着空 vectors 永久回退——静默，无任何报错。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_tfidf_reload.py -q
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tfidf_router as t  # noqa: E402

_PAYLOAD = ('{"anysearch": {"documents": ["天气 查询"], '
            '"boost_keywords": {}, "boost_combos": {}}}')


class TestHotReloadEdge(unittest.TestCase):
    def test_not_exists_resets_mtime_and_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            prof = Path(d) / "domain_profiles.json"
            prof.write_text(_PAYLOAD, encoding="utf-8")
            with patch.object(t, "DOMAIN_PROFILES_PATH", prof):
                r = t.SemanticRouter()
                r._ensure_loaded()
                self.assertEqual(r.engine_names, ["anysearch"])
                stale = prof.stat().st_mtime
                # 模拟重载窗口竞态：mtime 判变 → vectors 已清空 → 文件消失
                r._loaded = False
                r.engine_names.clear()
                r.engine_vectors.clear()
                prof.unlink()
                r._ensure_loaded()  # 走 not-exists 分支
                self.assertTrue(r._loaded)
                self.assertEqual(r.engine_names, [])
                # 文件以原 mtime 还原
                prof.write_text(_PAYLOAD, encoding="utf-8")
                os.utime(prof, (stale, stale))
                r._ensure_loaded()
                self.assertEqual(
                    r.engine_names, ["anysearch"],
                    "同 mtime 还原后必须重载——mtime 复位是本测试守的契约")


if __name__ == "__main__":
    unittest.main()
