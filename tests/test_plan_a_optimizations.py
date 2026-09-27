#!/usr/bin/env python
"""路由预热的针对性测试。

历史沿革（2026-09-27）：本文件原有第二组用例 `TestConfigCachePickleCompat`，
把「config 磁盘缓存从 JSON 换成 pickle（schema 3→4）」当作既定优化来锁定。
该优化已被回退——实测 pickle 只省 0.3ms（dumps 0.70→0.51ms / loads 0.84→0.55ms），
却用「反序列化=执行任意代码」换掉了 `_json_round_trip_safe` 这条
「缓存不得改变语义」的安全守卫，且 0.3ms 在 80ms 固定开销里是噪声。
守卫一个已撤销的改动，等于让下一次想重新引入它的人拿到虚假的安全感，
故随改动一并删除，而不是留着让它红。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# ── 1. 路由预热 ──────────────────────────────────────────────────────────────

class TestPrewarmRoute:
    """验证预热线程能在 500ms 内完成域正则编译。"""

    def test_prewarm_completes_within_500ms(self):
        """预热线程启动后，500ms 内域正则应已编译完成。"""
        import threading
        from route import match_domains
        from config import get_domains, load_config

        completed = threading.Event()

        def _do_prewarm():
            try:
                cfg = load_config()
                match_domains("argo-prewarm", get_domains(cfg))
            finally:
                completed.set()

        t0 = time.perf_counter()
        t = threading.Thread(target=_do_prewarm, daemon=True)
        t.start()
        completed.wait(timeout=0.5)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert completed.is_set(), "预热线程在 500ms 内未完成"
        assert elapsed_ms < 500, f"预热耗时 {elapsed_ms:.1f}ms，超过 500ms 预算"
