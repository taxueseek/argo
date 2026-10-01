#!/usr/bin/env python3
"""缓存命中路径 import lint —— 重模块不得出现在每次搜索的固定开销里。

事故背景（2026-10-01 量化审计）：
  缓存命中的搜索内部工作仅 ~1 ms，但进程墙钟 ~100 ms 起：其中
  engines 全家（engines/builders/urllib/http/xml/email，实测 ~115 ms）由
  search.py 在 engine='auto' 时「顺手」import（该分支下判据函数恒返 []），
  evidence 证据质量栈（~9 ms）由 evidence_loop 在结果循环内 import。
  两处都是「默认路径零收益、每次调用全付费」——CLI 每次调用全付，
  MCP 服务器启动付一次。

本测试锁住这条线：缓存命中的一次搜索不得新触发下列重模块导入。
任何人把重模块塞回热路径，这里立刻变红——不必再靠人肉 importtime 巡查。

运行：
  python3 -m pytest tests/test_hotpath_import_lint.py -v
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HEAVY_TOP = frozenset({
    "engines",           # 引擎适配层 → builders/engines_base/recovery/urllib…
    "engines_base",
    "engines_builders",
    "http_client",
    "evidence",          # 证据质量栈 → content_signals/stylometry/burstiness…
    "content_signals",
    "stylometry_detector",
    "burstiness_detector",
    "fetch_v3",
    "recovery",
    "image_ops",
    "archive_run",
})

# 高相关低方差查询：退化守卫（relevance<0.25 多数拒写缓存）对它放行，
# 首次调用必然写缓存、二次必然命中；断网时守卫消息会指明探针前提不成立。
QUERY = "GitHub"


def _tracked(search_mod, cache):
    """跑一次搜索并记录其间新发生的 import（含函数内延迟 import）。"""
    events: list[str] = []
    orig = builtins.__import__

    def tracking(name, *args, **kwargs):
        events.append(name)
        return orig(name, *args, **kwargs)

    builtins.__import__ = tracking
    try:
        result = search_mod.super_search(QUERY, engine="auto", n=3, cache=cache)
    finally:
        builtins.__import__ = orig
    return result, events


def test_cache_hit_path_pulls_no_heavy_imports():
    import search as search_mod
    from cache import SearchCache

    # 同一 SearchCache 实例传两次：第一次冷路径写入缓存，第二次走命中路径
    # （与 CLI 单进程内的真实用法同构；跨进程 L2 行为由 cache.py 自身测试覆盖）。
    cache = SearchCache()
    runs = [_tracked(search_mod, cache) for _ in range(2)]
    hits = [(r, e) for r, e in runs if r.get("cached")]
    assert hits, "两次调用均未命中缓存——探针前提不成立"
    _, events = hits[0]
    dragged = sorted({n.split(".")[0] for n in events
                      if n.split(".")[0] in HEAVY_TOP})
    assert not dragged, (
        f"缓存命中路径拖入重模块 {dragged}：每次调用白付的固定开销。"
        "import 应下沉到真正用到它的分支（参 search.py 的 "
        "unknown_requested_engines 调用点与 evidence_loop 的 "
        "is_serp_or_jump_url 调用点）"
    )
