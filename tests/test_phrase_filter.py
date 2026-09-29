#!/usr/bin/env python3
"""Step 3 —— 短语精确匹配后置过滤回归门（phrase_filter）。

锁契约：
  1. extract_phrases 只取引号内 ≥2 字符短语，去重保序，无引号返回空；
  2. apply_phrase_filter 按「任一命中」大小写不敏感过滤 title+snippet；
  3. 保守：过滤会清空时回退不过滤（绝不误伤唯一结果）；无短语 no-op。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo-phrase-"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from phrase_filter import apply_phrase_filter, extract_phrases  # noqa: E402


def test_extract_phrases():
    assert extract_phrases('search "sqlite-vec" benchmark') == ["sqlite-vec"]
    assert extract_phrases("no quotes here") == []
    assert extract_phrases('just "x" here') == []    # 单字符无区分度
    assert extract_phrases('"dup" x "dup"') == ["dup"]  # 去重


def test_apply_phrase_filter_hits():
    res = [{"title": "sqlite-vec guide", "snippet": "...", "url": "u1"},
           {"title": "unrelated", "snippet": "foo", "url": "u2"}]
    kept, dropped = apply_phrase_filter(res, ["sqlite-vec"])
    assert [r["url"] for r in kept] == ["u1"]
    assert dropped == 1


def test_apply_phrase_filter_case_insensitive():
    res = [{"title": "SQLITE-VEC Guide", "snippet": "", "url": "u1"}]
    kept, _ = apply_phrase_filter(res, ["sqlite-vec"])
    assert len(kept) == 1


def test_apply_phrase_filter_or_semantics():
    res = [{"title": "alpha", "snippet": "", "url": "u1"},
           {"title": "beta", "snippet": "", "url": "u2"},
           {"title": "gamma", "snippet": "", "url": "u3"}]
    kept, _ = apply_phrase_filter(res, ["alpha", "beta"])
    assert {r["url"] for r in kept} == {"u1", "u2"}  # 任一命中


def test_apply_phrase_filter_conservative_fallback():
    # 过滤会清空 → 回退不过滤（不误伤唯一结果）
    res = [{"title": "nomatch", "snippet": "x", "url": "u1"}]
    kept, dropped = apply_phrase_filter(res, ["zzz"])
    assert kept == res and dropped == 0


def test_apply_phrase_filter_empty_phrases_noop():
    res = [{"title": "t", "url": "u1"}]
    kept, dropped = apply_phrase_filter(res, [])
    assert kept == res and dropped == 0
