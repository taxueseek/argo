#!/usr/bin/env python3
"""test_time_capable_hotpath.py — 无时间窗搜索不付 307 份 spec 的解析成本。

## 守的是什么

`is_time_capable()` 背后是 `_spec_time_engines()`：首次调用会逐个
`yaml.safe_load` 读 engines/specs/*.yaml。实测一次缓存命中的搜索里它被调用
2 次（`search_entry.prepare` 与 `engine_dispatch._run_one`）、解析 307 份
spec，占冷路径 236 ms 中的约 130–150 ms。

而 `--since/--until` 是显式可选参数：`since_iso=None` 时 `eng_since` 恒为
`None`、`time_aware` 也不影响后过滤。也就是说，默认路径上这轮全量解析的
结果**从未被消费**。

本文件锁三件事：
1. 无时间窗时两处调用点都不触发 `_spec_time_engines()`（性能契约）；
2. 有时间窗时判据与原实现逐位一致（等价性契约）；
3. 时间能力集合与 config 已合并的引擎声明一致（单一真源契约）。

按 TDD 写的：把旧实现的无条件求值放回去，第 1 组用例必须挂。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import engine_dispatch  # noqa: E402
import search_entry  # noqa: E402
import time_utils  # noqa: E402


def _reference_spec_time_engines() -> frozenset[str]:
    """改动前的实现（原样复刻），作为等价性基准。"""
    import yaml

    specs_dir = ROOT / "engines" / "specs"
    found: set[str] = set()
    for p in specs_dir.glob("*.yaml"):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        eng_id = str(data.get("engine_id") or p.stem)
        omap = data.get("output_map") or {}
        req = data.get("request") or {}
        if (data.get("type") == "rss_feed"
                or data.get("feeds")
                or (isinstance(req, dict) and req.get("feed_url"))
                or (isinstance(omap, dict) and omap.get("item_published_at"))):
            found.add(eng_id)
    return frozenset(found)


# ── 1. 无时间窗不触发全量扫描 ──────────────────────────────────────────────

def test_prepare_without_time_window_skips_spec_scan():
    """无 since/until：prepare 不得调用 _spec_time_engines()，缓存键不变。"""
    hit_payload = {
        "results": [{"title": "t", "url": "https://e.com", "snippet": "s"}],
        "engines_combo": ["anysearch"], "engine_outcomes": [],
    }

    class _Cache:
        def __init__(self):
            self.keys = []

        def get(self, *args, **kwargs):
            self.keys.append((args, kwargs))
            return hit_payload

    cache = _Cache()
    with patch.object(time_utils, "_spec_time_engines",
                      side_effect=AssertionError("无时间窗不该扫 spec")) as scan:
        prepared = search_entry.prepare(
            "q", {"domain": "general", "engine": "anysearch",
                  "engines": ["anysearch"]},
            5, 5, "fast", cache, False,
            mode="auto", since=None, until=None, hooks=None,
        )
    assert scan.call_count == 0
    assert prepared.cached is not None
    assert cache.keys, "缓存查询应照常发生"


def test_dispatch_without_time_window_skips_spec_scan():
    """无 since/until：engine_dispatch 的 per-engine 时间窗不得查 spec。"""
    import ast
    import inspect

    src = inspect.getsource(engine_dispatch)
    tree = ast.parse(src)
    # 定位 _run_one 里给 eng_since 赋值的那条语句，核对短路在条件位
    target = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "eng_since"
                        for t in node.targets)):
            target = node.value
            break
    assert target is not None, "未找到 eng_since 的赋值"
    assert isinstance(target, ast.IfExp), "eng_since 必须由条件表达式短路"
    # 条件体里必须先把 since/until 判空放在 is_time_capable 之前
    cond_src = ast.get_source_segment(src, target.test) or ""
    assert cond_src.index("since_iso") < cond_src.index("is_time_capable"), (
        "since/until 判空必须先于 is_time_capable，否则默认路径仍会全量扫 spec"
    )


# ── 2. 有时间窗时判据等价 ─────────────────────────────────────────────────

def test_time_capable_matches_reference():
    """集合逐位一致：不能为了提速把某个带时间能力的源漏掉。"""
    reference = _reference_spec_time_engines()
    actual = time_utils._spec_time_engines()
    assert actual == reference
    assert len(reference) > 0, "基准集合为空说明测试环境不对，不能算通过"


def test_known_members_and_non_members():
    """带时间能力的 spec 源仍为 True，普通源仍为 False。"""
    assert time_utils.is_time_capable("gdelt") is True
    assert time_utils.is_time_capable("cisa_alerts") is True
    assert time_utils.is_time_capable("deepseek_search") is True
    assert time_utils.is_time_capable("octen") is False
    assert time_utils.is_time_capable("anysearch") is False


# ── 3. 单一真源：集合来自 config 已合并的声明 ────────────────────────────

def test_spec_time_engines_reuses_config_declarations():
    """时间能力集合必须由 config.load_config() 派生，不再自己扫盘。"""
    import ast
    import inspect

    src = inspect.getsource(time_utils._spec_time_engines)
    tree = ast.parse(src)
    # 取函数体各语句的源码片段，避开 docstring 里对旧实现的文字说明
    body = "".join(
        ast.get_source_segment(src, stmt) or ""
        for stmt in tree.body[0].body[1:]
    )
    assert "load_config" in body, "应复用 config 已合并的引擎声明"
    assert "yaml.safe_load" not in body, "函数体不应再逐个解析 spec 文件"
    assert "glob(" not in body, "函数体不应再扫 engines/specs 目录"


def test_config_declarations_are_the_same_set():
    """config 合并后的声明与磁盘 spec 是同一份数据（防派生口径漂移）。"""
    engines = config.load_config(force=True)["engines"]
    from_config = frozenset(
        eid for eid, spec in engines.items()
        if isinstance(spec, dict) and (
            spec.get("type") == "rss_feed"
            or spec.get("feeds")
            or (isinstance(spec.get("request"), dict)
                and spec["request"].get("feed_url"))
            or (isinstance(spec.get("output_map"), dict)
                and spec["output_map"].get("item_published_at"))
        )
    )
    assert time_utils._spec_time_engines() == from_config


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
