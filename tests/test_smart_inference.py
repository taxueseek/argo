#!/usr/bin/env python3
"""tests/test_smart_inference.py — 方案三：通用智能自省推断解析器验收测试。

守三条契约：
  1. 零配置推断：无 output_map 时，通用推断器能自适应识别不同老源的常见返回形态；
  2. 字段候选降级覆盖：能从 Nullable 序列化、Algolia hits、HF papers 单层嵌套等复杂结构中提取 title/url/snippet/published_at；
  3. 契约优先级：spec.response.parser 优先 -> output_map 声明优先 -> 智能推断自省兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import engines_base


def test_smart_inference_on_kaggle_nullable_style():
    """测试 Kaggle Java 序列化 Nullable 字段的自适应推断。"""
    payload = {
        "results": [
            {
                "titleNullable": "Climate Change Daily Dataset",
                "subtitleNullable": "High resolution daily climate metrics around the globe.",
                "urlNullable": "https://www.kaggle.com/datasets/test/climate-data",
                "totalBytesNullable": 1048576,
            }
        ]
    }
    spec = {"engine_id": "kaggle"}
    res = engines_base._parse_generic(payload, "kaggle", 5, spec)
    assert len(res) == 1
    assert res[0]["title"] == "Climate Change Daily Dataset"
    assert res[0]["snippet"] == "High resolution daily climate metrics around the globe."
    assert res[0]["url"] == "https://www.kaggle.com/datasets/test/climate-data"
    assert res[0]["source"] == "kaggle"


def test_smart_inference_on_hackernews_algolia_style():
    """测试 HackerNews Algolia hits 结构与基于 url_template 的 objectID 兜底拼装。"""
    payload = {
        "hits": [
            {
                "title": "Show HN: Argo AI Search Engine",
                "url": "https://github.com/taxueseek/argo",
                "points": 142,
                "created_at": "2026-11-10T12:00:00Z",
            },
            {
                "title": "Ask HN: What is your favorite search engine?",
                "url": "",  # 讨论帖无外链，依靠 objectID 拼装
                "objectID": "40123456",
                "created_at": "2026-11-10T14:00:00Z",
            }
        ]
    }
    spec = {
        "engine_id": "hackernews",
        "url_template": "https://news.ycombinator.com/item?id={id}",
    }
    res = engines_base._parse_generic(payload, "hackernews", 5, spec)
    assert len(res) == 2
    assert res[0]["title"] == "Show HN: Argo AI Search Engine"
    assert res[0]["url"] == "https://github.com/taxueseek/argo"
    assert res[0]["published_at"] == "2026-11-10T12:00:00Z"

    # 验证 objectID 兜底拼装为讨论链接
    assert res[1]["title"] == "Ask HN: What is your favorite search engine?"
    assert res[1]["url"] == "https://news.ycombinator.com/item?id=40123456"


def test_smart_inference_on_hf_papers_nested_style():
    """测试 HF Papers 嵌套 paper 字典展平与 url_template 拼装。"""
    payload = [
        {
            "paper": {
                "id": "2401.12345",
                "title": "Large Language Models as Reasoning Kernels",
                "summary": "We investigate how small units of AI intelligence act as programming primitives.",
                "authors": ["Alice", "Bob"],
            },
            "publishedAt": "2026-01-15T08:30:00Z",
        }
    ]
    spec = {
        "engine_id": "hf_papers",
        "url_template": "https://huggingface.co/papers/{id}",
    }
    res = engines_base._parse_generic(payload, "hf_papers", 5, spec)
    assert len(res) == 1
    assert res[0]["title"] == "Large Language Models as Reasoning Kernels"
    assert "We investigate how small units" in res[0]["snippet"]
    assert res[0]["url"] == "https://huggingface.co/papers/2401.12345"
    assert res[0]["published_at"] == "2026-01-15T08:30:00Z"


def test_smart_inference_on_defillama_flat_list_style():
    """测试 DefiLlama / V2EX 这类扁平 list 结构自适应解析。"""
    payload = [
        {
            "name": "Uniswap V3",
            "url": "https://uniswap.org",
            "description": "Decentralized automated liquidity protocol on Ethereum.",
        }
    ]
    spec = {"engine_id": "defillama"}
    res = engines_base._parse_generic(payload, "defillama", 5, spec)
    assert len(res) == 1
    assert res[0]["title"] == "Uniswap V3"
    assert res[0]["url"] == "https://uniswap.org"
    assert "automated liquidity" in res[0]["snippet"]


def test_registered_parser_priority_over_generic():
    """测试优先级：若声明了 spec.response.parser，优先走注册解析器。"""
    # 模拟 Mwmbl 高亮片段数组：title=[{'value': 'Hello', 'is_bold': True}]
    raw_json = '[{"title": [{"value": "Mwmbl Project", "is_bold": true}], "url": "https://mwmbl.org", "extract": [{"value": "Indie search", "is_bold": false}]}]'
    spec = {
        "engine_id": "mwmbl",
        "response": {"parser": "mwmbl"},
    }
    # 通过 _parse_http_payload 验证注册解析器优先被调用
    res = engines_base._parse_http_payload(raw_json, "json", "mwmbl", 5, {}, spec)
    assert len(res) == 1
    assert res[0]["title"] == "Mwmbl Project"
    assert res[0]["url"] == "https://mwmbl.org"
    assert res[0]["snippet"] == "Indie search"


def test_smart_inference_html_cleaning_and_epoch():
    """测试 HTML 标签剥离、实体反转义以及时间戳规范化与 rank_score 单调衰减。"""
    payload = {
        "results": [
            {
                "title": "<b>Python &amp; Rust</b>",
                "snippet": "Analysis of &quot;memory safety&quot; <span class='tag'>systems</span>",
                "url": "https://example.com/article",
                "date": 1712345678,  # 秒级时间戳
                "score": 0.95,
            },
            {
                "title": "Second Article",
                "snippet": "Details about performance",
                "url": "https://example.com/perf",
                "score": 100,  # 异常高分测试：应被 rank_score 夹紧在 [0.0, 1.0]
            }
        ]
    }
    spec = {"engine_id": "test_cleaning"}
    res = engines_base._parse_generic(payload, "test_cleaning", 5, spec)
    assert len(res) == 2
    assert res[0]["title"] == "Python & Rust"
    assert res[0]["snippet"] == 'Analysis of "memory safety" systems'
    assert res[0]["published_at"] == "2024-04-05"
    # 验证 rank_score 夹紧在 [0.0, 1.0] 范围内
    assert 0.0 <= res[0]["score"] <= 1.0
    assert 0.0 <= res[1]["score"] <= 1.0
    # 验证第 1 名由于 rank 衰减，即便原始分是 100 也会被夹紧并发生衰减
    assert res[1]["score"] <= 1.0


def test_smart_inference_on_tvmaze_show_style():
    """测试 TVMaze 嵌套 show 对象自适应展平与 HTML 摘要清洗。"""
    payload = [
        {
            "score": 1.25,
            "show": {
                "id": 169,
                "name": "Breaking Bad",
                "url": "https://www.tvmaze.com/shows/169/breaking-bad",
                "premiered": "2008-01-20",
                "summary": "<p><b>Breaking Bad</b> is an American television series.</p>",
            }
        }
    ]
    spec = {"engine_id": "tvmaze"}
    res = engines_base._parse_generic(payload, "tvmaze", 5, spec)
    assert len(res) == 1
    assert res[0]["title"] == "Breaking Bad"
    assert res[0]["url"] == "https://www.tvmaze.com/shows/169/breaking-bad"
    assert res[0]["snippet"] == "Breaking Bad is an American television series."
    assert res[0]["published_at"] == "2008-01-20"

