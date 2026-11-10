#!/usr/bin/env python3
"""tests/test_signal_and_facet.py — 方案一（两级信号驱动）与方案三（分面通道隔离）验收测试。

守三条：
  1. 方案一：代码异常类、内部私有标识符、工程配置、函数调用特征均能精准触发本地意图；
  2. 方案一零污染：纯公域（天气/金融/学术等）与普通泛查询在无本地意图时不跑本地轨；
  3. 方案三通道隔离：shape_response 与 _strip_for_agent 均产出独立的 local_results、web_results 与 facets，
     且 agent 档位完整保留分面通道与 is_local 标志，避免本地文件挤占公网配额。
"""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_intent
import local_seek
import search_output


def test_scheme1_two_tier_engineering_signals():
    """方案一：两级信号驱动对异常类、私有标识符、工程文件的精准识别。"""
    # 1. 常见代码异常与错误类名
    assert local_intent.has_strong_local_signal("ZeroDivisionError 怎么排查") is True
    assert local_intent.has_strong_local_signal("AssertionError in test_dispatch") is True
    assert local_intent.has_strong_local_signal("ModuleNotFoundError: No module named pytest") is True
    assert local_intent.has_strong_local_signal("ConnectionResetError timeout") is True

    # 2. 内部私有方法/变量名
    assert local_intent.has_strong_local_signal("_run_local_seek 函数实现") is True
    assert local_intent.has_strong_local_signal("重写 __init__ 方法") is True
    assert local_intent.has_strong_local_signal("_resolve_include_local 逻辑") is True

    # 3. 工程配置文件与新增扩展名
    assert local_intent.has_strong_local_signal("pyproject.toml 中的 dependencies") is True
    assert local_intent.has_strong_local_signal("tsconfig.json 配置") is True
    assert local_intent.has_strong_local_signal("go.mod 版本要求") is True
    assert local_intent.has_strong_local_signal("service.conf 端口设置") is True

    # 4. 函数调用特征
    assert local_intent.has_strong_local_signal("resolve_local() 是谁调用的") is True


def test_scheme1_zero_pollution_on_public_domains():
    """方案一：纯公域与无本地意图泛查询零污染，默认跳过。"""
    # 纯公域答案型查询
    assert local_seek.resolve_include_local(None, "fast", "上海天气预报", "weather_query") is False
    assert local_seek.resolve_include_local(None, "fast", "腾讯控股今日股价", "stock_query") is False
    assert local_seek.resolve_include_local(None, "fast", "人民币对美元中间价", "fx_query") is False
    assert local_seek.resolve_include_local(None, "fast", "最新美联储议息会议纪要", "financial_news") is False

    # 无本地意图的日常泛查询
    assert local_seek.resolve_include_local(None, "fast", "红烧肉的最佳家常做法", "general") is False
    assert local_seek.resolve_include_local(None, "fast", "transformer attention mechanism paper", "scholar_search") is False
    assert local_seek.resolve_include_local(None, "fast", "2026年春节放假安排", None) is False


def test_scheme3_facet_channel_isolation_and_agent_strip():
    """方案三：分面通道物理隔离与 agent 瘦身档位保留。"""
    # 构造一次包含本地命中与公网命中的混合响应载荷
    mock_payload = {
        "query": "test query",
        "status": "ok",
        "count": 3,
        "results": [
            {"title": "Web Hit 1", "url": "https://example.com/1", "snippet": "web 1", "score": 0.8},
            {"title": "Local Hit 1", "url": "file:///path/to/script.py", "snippet": "local 1", "source": "local_files", "score": 0.9},
            {"title": "Web Hit 2", "url": "https://example.com/2", "snippet": "web 2", "score": 0.75},
        ],
        "local_results": [
            {"title": "Local Hit 1", "url": "file:///path/to/script.py", "snippet": "local 1", "source": "local_files", "score": 0.9, "is_local": True},
        ],
        "web_results": [
            {"title": "Web Hit 1", "url": "https://example.com/1", "snippet": "web 1", "score": 0.8},
            {"title": "Web Hit 2", "url": "https://example.com/2", "snippet": "web 2", "score": 0.75},
        ],
        "facets": {
            "web_count": 2,
            "local_count": 1,
            "total_count": 3,
        },
        "fetch_required": False,
        "evidence_loop": {"pending_count": 0},
        "limitations": ["daily tier"],
    }

    # 测试 _strip_for_agent 瘦身
    slim = search_output._strip_for_agent(mock_payload)

    # 1. 验证通用 results 仍在
    assert len(slim["results"]) == 3
    assert slim["count"] == 3

    # 2. 验证分面通道 local_results 与 web_results 独立保留
    assert "local_results" in slim
    assert "web_results" in slim
    assert len(slim["local_results"]) == 1
    assert len(slim["web_results"]) == 2

    # 3. 验证 is_local 标签不被剥除
    assert slim["local_results"][0].get("is_local") is True
    assert slim["local_results"][0]["url"] == "file:///path/to/script.py"

    # 4. 验证 facets 统计元数据保留
    assert slim.get("facets") == {"web_count": 2, "local_count": 1, "total_count": 3}
