#!/usr/bin/env python3
"""research 管线族的冒烟门（2026-09-16 盘点发现该族无任何直测引用）。

research_cli/social_research/research_expand/research_strategy 此前零测试覆盖；
完整研究链依赖网络，不适合单测。本文件只锁三件不依赖网络的事：
  1. 五个模块可导入且入口函数存在（拆分后 import 链断裂是最高频回归形态）；
  2. research_cli --help 正常退出（argparse 层语法防回归）；
  3. 报告落盘目录行为（persist 无副作用契约归 DSH 插件测试管，此处不重复）。
"""
import os
import subprocess
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)


def test_research_modules_importable():
    import research  # noqa: F401
    import research_cli  # noqa: F401
    import research_expand  # noqa: F401
    import research_report  # noqa: F401
    import research_strategy  # noqa: F401
    import social_research  # noqa: F401


def test_deep_research_entry_exists():
    import research
    assert callable(research.deep_research)
    assert callable(research.social_sentiment_research)


def test_research_cli_help_exits_zero():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "research_cli.py"), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "用法" in (out.stdout + out.stderr) or "usage" in (out.stdout + out.stderr).lower()


@pytest.mark.parametrize("mod", ["time_utils", "query_signals", "stage_timing", "engine_dispatch"])
def test_extracted_modules_importable(mod):
    """8f62952 拆出的四模块：import 链断裂防回归（按名直测，不依赖间接路径）。"""
    __import__(mod)


# ── 方案 E：research 管线族深入直测（2026-09-30 新增）────────────────────────
# 背景：2026-09-16 盘点发现 research 管线族（research_cli/research_expand/
# research_strategy/social_research，约 700 行）零直测，仅靠 local_bridge 间接覆盖。
# 本类补的是「按名直测」：每个模块的核心纯函数，不依赖网络。


class TestResearchExpand:
    """research_expand.expand_query — 检索扩词逻辑直测。"""

    def test_expand_returns_list_of_dicts(self):
        from research_expand import expand_query
        result = expand_query("Python asyncio 教程", num_sub=4)
        assert isinstance(result, list)
        assert len(result) > 0
        for item in result:
            assert isinstance(item, dict)
            assert "query" in item
            assert "intent" in item
            assert "strategy" in item

    def test_expand_respects_num_sub(self):
        from research_expand import expand_query
        result = expand_query("Python asyncio 教程", num_sub=2)
        assert len(result) <= 2

    def test_expand_includes_anchor(self):
        """原查询本身必须占一席（anchor），保证与用户意图对齐。"""
        from research_expand import expand_query
        query = "Python asyncio 教程"
        result = expand_query(query, num_sub=4)
        queries = [r["query"] for r in result]
        assert query in queries, f"原查询未出现在扩词结果中：{queries}"

    def test_expand_compare_split(self):
        """对比查询应拆分为独立子查询。"""
        from research_expand import expand_query
        result = expand_query("Python vs Go 对比", num_sub=4)
        queries = [r["query"] for r in result]
        # 应包含拆分后的子查询
        assert any("Python" in q for q in queries)
        assert any("Go" in q for q in queries)

    def test_expand_academic_trigger(self):
        """学术查询应触发学术文献补充。"""
        from research_expand import expand_query
        result = expand_query("transformer 论文", num_sub=4)
        strategies = [r["strategy"] for r in result]
        assert "academic" in strategies

    def test_expand_security_trigger(self):
        """安全查询应触发安全数据源补充。"""
        from research_expand import expand_query
        result = expand_query("log4j CVE 漏洞", num_sub=4)
        strategies = [r["strategy"] for r in result]
        assert "security" in strategies

    def test_expand_finance_trigger(self):
        """金融查询应触发金融数据补充。"""
        from research_expand import expand_query
        result = expand_query("贵州茅台 财报 业绩", num_sub=4)
        strategies = [r["strategy"] for r in result]
        assert "finance" in strategies

    def test_expand_deduplicates(self):
        """扩词结果应去重（新信息率 < 0.25 的子查询被剔除）。"""
        from research_expand import expand_query
        result = expand_query("Python 教程", num_sub=4)
        queries = [r["query"] for r in result]
        assert len(queries) == len(set(queries)), f"扩词结果有重复：{queries}"

    def test_expand_empty_query(self):
        """空查询返回 anchor 条目（原查询本身），保证与用户意图对齐。"""
        from research_expand import expand_query
        result = expand_query("", num_sub=4)
        # 空查询也返回 anchor（strategy=general），不是空列表
        assert len(result) == 1
        assert result[0]["strategy"] == "general"
        assert result[0]["query"] == ""

    def test_expand_bilingual(self):
        """中英混合查询应触发英文核心概念搜索。"""
        from research_expand import expand_query
        result = expand_query("Python 异步编程", num_sub=4)
        strategies = [r["strategy"] for r in result]
        assert "english_focused" in strategies


class TestResearchStrategy:
    """research_strategy.resolve_route_strategy — 路由策略解析直测。"""

    def test_explicit_local_first(self):
        from research_strategy import resolve_route_strategy
        assert resolve_route_strategy("local_first", "auto") == "local_first"

    def test_explicit_full(self):
        from research_strategy import resolve_route_strategy
        assert resolve_route_strategy("full", "auto") == "full"

    def test_explicit_cost_aware(self):
        from research_strategy import resolve_route_strategy
        assert resolve_route_strategy("cost_aware", "auto") == "cost_aware"

    def test_fast_mode_defaults_to_local_first(self):
        """未指定策略时，fast 模式自动走 local_first。"""
        from research_strategy import resolve_route_strategy
        assert resolve_route_strategy(None, "fast") == "local_first"

    def test_auto_mode_defaults_to_cost_aware(self):
        """未指定策略时，auto 模式默认 cost_aware。"""
        from research_strategy import resolve_route_strategy
        assert resolve_route_strategy(None, "auto") == "cost_aware"

    def test_deep_mode_defaults_to_cost_aware(self):
        """未指定策略时，deep 模式默认 cost_aware。"""
        from research_strategy import resolve_route_strategy
        assert resolve_route_strategy(None, "deep") == "cost_aware"

    def test_should_use_local_first(self):
        from research_strategy import should_use_local_first
        assert should_use_local_first("local_first") is True
        assert should_use_local_first("cost_aware") is False
        assert should_use_local_first("full") is False


class TestSocialResearch:
    """social_research — 社交舆情聚合逻辑直测（不依赖网络）。"""

    def test_aggregate_social_sentiment(self):
        """聚合互动数据汇总。"""
        from social_research import aggregate_social_sentiment
        platform_results = {
            "twitter": [
                {"social_meta": {"likes": 10, "comments": 2, "retweets": 3}},
                {"social_meta": {"likes": 5, "comments": 1, "retweets": 0}},
            ],
            "reddit": [
                {"social_meta": {"likes": 100, "comments": 20, "shares": 5}},
            ],
        }
        result = aggregate_social_sentiment("test", ["twitter", "reddit"], platform_results)
        assert result["total_posts"] == 3
        assert result["platform_breakdown"]["twitter"] == 2
        assert result["platform_breakdown"]["reddit"] == 1
        # twitter: likes=10+5=15, comments=2+1=3, shares=0+0=0
        # reddit: likes=100, comments=20, shares=5
        assert result["engagement_totals"]["likes"] == 115
        assert result["engagement_totals"]["comments"] == 23
        assert result["engagement_totals"]["shares"] == 5

    def test_extract_topics_english(self):
        """英文话题提取。"""
        from social_research import _extract_topics
        titles = [
            "Python async tutorial for beginners",
            "Python asyncio guide",
            "JavaScript async patterns",
        ]
        topics = _extract_topics(titles, top_k=5)
        assert isinstance(topics, list)
        assert len(topics) > 0
        # "python" 应出现 2 次
        python_topic = next((t for t in topics if t["topic"] == "python"), None)
        assert python_topic is not None
        assert python_topic["mentions"] == 2

    def test_extract_topics_chinese(self):
        """中文话题提取（bigram）。"""
        from social_research import _extract_topics
        titles = [
            "Python异步编程教程",
            "Python异步编程指南",
            "Java异步编程实践",
        ]
        topics = _extract_topics(titles, top_k=10)
        assert isinstance(topics, list)
        # "异步" 应出现 3 次
        async_topic = next((t for t in topics if t["topic"] == "异步"), None)
        assert async_topic is not None
        assert async_topic["mentions"] == 3

    def test_extract_topics_filters_stopwords(self):
        """停用词应被过滤。"""
        from social_research import _extract_topics
        titles = ["的了是在", "的了是在"]
        topics = _extract_topics(titles, top_k=10)
        # 停用词 bigram 不应出现
        for t in topics:
            assert t["topic"] not in ("的了", "的了是在")

    def test_extract_topics_empty(self):
        """空标题列表应返回空话题。"""
        from social_research import _extract_topics
        topics = _extract_topics([], top_k=10)
        assert topics == []


class TestResearchCliArgparse:
    """research_cli — argparse 层直测（不依赖网络）。"""

    def test_work_packages_json_parsing(self):
        """工作包 JSON 解析。"""
        from research_cli import _load_work_packages
        raw = '[{"id":"d","question":"定义"},{"id":"r","question":"风险","depends_on":["d"]}]'
        result = _load_work_packages(raw)
        assert result == raw  # 内联 JSON 原样返回

    def test_work_packages_file_loading(self, tmp_path):
        """工作包从文件加载。"""
        from research_cli import _load_work_packages
        pkg_file = tmp_path / "pkgs.json"
        pkg_file.write_text('[{"id":"d","question":"定义"}]', encoding="utf-8")
        result = _load_work_packages(str(pkg_file))
        assert result == '[{"id":"d","question":"定义"}]'

    def test_work_packages_none(self):
        """None 输入返回 None。"""
        from research_cli import _load_work_packages
        assert _load_work_packages(None) is None
