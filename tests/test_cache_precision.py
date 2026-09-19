#!/usr/bin/env python3
"""test_cache_precision.py — 缓存精度契约：不销毁好数据。

「效率」问的是命中率，「精度」问的是命中的东西对不对、以及缓存动作本身会不会
损坏已存的好数据。本文件锁后者。

历史 bug（2026-09-19 复现）：`EMPTY_RESULT_TTL`（45s）的意图是「别把一次失败
固化成『这个查询没结果』」，但写入走 `INSERT OR REPLACE`——同键上一条还有一小
时寿命的有效缓存，会被一次网络抖动产生的空结果整条覆盖掉。失败覆盖成功，方向
反了：有活条目恰恰说明上一次取到了东西，它比这一次的失败更可信。
"""

import os
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import cache  # noqa: E402


@pytest.fixture()
def sc(tmp_path):
    return cache.SearchCache(db_path=str(tmp_path / "cache.db"))


def _n(sc, q="测试 查询"):
    hit = sc.get(q, "auto", 5, domain="general", mode="auto", depth="fast")
    return None if hit is None else len(hit.get("results") or [])


_GOOD = {"results": [{"title": "好结果", "url": "https://a/1"}]}
_EMPTY = {"results": []}


def test_transient_empty_does_not_destroy_valid_entry(sc):
    """瞬时失败（空结果）不得覆盖同键上的有效缓存。"""
    sc.set("测试 查询", "auto", 5, _GOOD, domain="general",
           mode="auto", depth="fast")
    assert _n(sc) == 1
    sc.set("测试 查询", "auto", 5, _EMPTY, domain="general",
           mode="auto", depth="fast")
    assert _n(sc) == 1, "有效缓存被一次空结果销毁了"


def test_negative_caching_still_works_for_fresh_key(sc):
    """对照面：负缓存不能被这次修复顺手砍掉——它是 EMPTY_RESULT_TTL 的本意。"""
    sc.set("全新 查询", "auto", 5, _EMPTY, domain="general",
           mode="auto", depth="fast")
    hit = sc.get("全新 查询", "auto", 5, domain="general",
                 mode="auto", depth="fast")
    assert hit is not None, "全新键的空结果应当落库（负缓存）"
    assert hit.get("results") == []


def test_expired_entry_can_be_replaced_by_empty(sc):
    """对照面：过期条目不算「有效数据」，空结果应当能把它换掉。

    判据必须是「未过期」而不是「存在」——否则一条早已失效的行会永久挡住
    负缓存的写入，而它在读取路径上本来就不可见。
    """
    sc.set("q", "auto", 5, _GOOD, domain="general",
           mode="auto", depth="fast", ttl=1)
    time.sleep(1.2)
    sc.set("q", "auto", 5, _EMPTY, domain="general",
           mode="auto", depth="fast")
    hit = sc.get("q", "auto", 5, domain="general", mode="auto", depth="fast")
    assert hit is not None, "过期行不该挡住空结果写入"


def test_has_live_is_read_only(sc):
    """has_live 是只读探测：不碰 accessed_at、不计命中/未命中。

    用 get() 兼职探测会污染 LRU 的访问时间、也会让命中率统计失真。
    """
    sc.set("q", "auto", 5, _GOOD, domain="general", mode="auto", depth="fast")
    before = sc._l2.stats
    assert sc._l2.has_live(sc._key("q", "auto", 5, "general", "auto", "fast",
                                   kind="combo")) is True
    assert sc._l2.has_live("不存在的键") is False
    after = sc._l2.stats
    assert after["hits"] == before["hits"] and after["misses"] == before["misses"], \
        "has_live 不应改变命中/未命中计数"
