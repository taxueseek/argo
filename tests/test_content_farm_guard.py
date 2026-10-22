#!/usr/bin/env python3
"""test_content_farm_guard.py — 内容农场判定的正反例（2026-10-23 接线标定）。

## 治什么

`is_content_farm` 2026-10-23 写进 serp_guard 后一度是**死代码**（只被
`is_unusable_url` / `filter_content_farms` 调用，而这两个函数全仓零调用），
直到接线进 `search_pipeline` 的 SERP 过滤才真正生效。

接线前用 5 条正例 + 4 条边界标定，修掉两处判据错误：

  1. **误杀正常文档**：旧判据 `hit_conn >= 4`（序数连接词 ≥4 就判农场）会把
     正常的安装指南判成农场——「首先下载/其次解压/然后运行/接下来配置/
     最后重启」正好 5 个。现在要求同时出现 ≥1 个 AI 套话。
  2. **漏召虚假实测**：旧判据要求标题里「自费 N 元实测」后面跟「最新」，
     而真实的虚假实测标题（「自费5000元实测10款耳机」）不带「最新」。

本文件锁住这两处修正和「真农场能拦、正常内容不误杀」的双向边界。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from serp_guard import is_content_farm, is_unusable_url  # noqa: E402

# 流水线稿：序数连接词 + AI 套话同时出现
FARM_CONNECTORS = (
    "https://example-farm.com/a",
    "2026年蓝牙耳机推荐",
    "首先,我们要看音质。其次,降噪很重要。然后,续航也不能忽视。接下来,价格要合理。"
    "最后,总结一下。业内人士表示,消费者认为,数据显示,有望成为爆款。",
)

# 虚假实测：标题带「自费 N 元实测」，且不带「最新」
FARM_FAKE_TEST = (
    "https://spam.example.com/b",
    "自费5000元实测10款耳机",
    "自费5000元实测,看完你就懂了。建议收藏。全解析。必看。",
)

# 正常技术长文
NORMAL_TECH = (
    "https://react.dev/learn",
    "React Hooks 入门",
    "Hooks 让你在不编写 class 的情况下使用 state 以及其他的 React 特性。"
    "useState 返回一个状态值和更新它的函数。",
)

# 正常新闻
NORMAL_NEWS = (
    "https://news.example.com/c",
    "北京长亭科技增资至3.4亿",
    "天眼查App显示,近日北京长亭科技有限公司发生工商变更,注册资本由5000万增至3.4亿。",
)

# 边界：正常安装指南，含 5 个序数连接词但没有 AI 套话——旧判据的误杀点
NORMAL_GUIDE = (
    "https://docs.example.com/d",
    "安装指南",
    "首先下载安装包。其次解压到目标目录。然后运行安装脚本。接下来配置环境变量。最后重启服务。",
)


def test_pipeline_farm_is_flagged():
    assert is_content_farm(*FARM_CONNECTORS) is True


def test_fake_testimony_is_flagged_without_latest_word():
    """「自费 N 元实测」不要求带「最新」——旧判据的漏召点。"""
    assert is_content_farm(*FARM_FAKE_TEST) is True


def test_normal_tech_article_not_flagged():
    assert is_content_farm(*NORMAL_TECH) is False


def test_normal_news_not_flagged():
    assert is_content_farm(*NORMAL_NEWS) is False


def test_normal_guide_with_ordinal_connectors_not_flagged():
    """含 5 个序数连接词的正常文档不得被判农场——旧判据的误杀点。"""
    assert is_content_farm(*NORMAL_GUIDE) is False


def test_unusable_url_covers_both_serp_and_farm():
    """接线用的是 is_unusable_url：SERP 壳与内容农场都要被它拦下。"""
    assert is_unusable_url("https://www.baidu.com/link?url=abc") is True
    assert is_unusable_url(*FARM_CONNECTORS) is True
    assert is_unusable_url(*NORMAL_GUIDE) is False


def test_escape_hatch_disables_farm_detection(monkeypatch):
    """ARGO_SERP_GUARD=0 时整体关闭（与 SERP 守卫共用同一个逃生门）。"""
    monkeypatch.setenv("ARGO_SERP_GUARD", "0")
    assert is_content_farm(*FARM_CONNECTORS) is False


def test_pipeline_imports_the_wired_entry():
    """接线回归：search_pipeline 必须 import is_unusable_url（而不是只管 SERP 的旧入口）。

    这条钉的是「实现好了但没接线」这个形态——2026-10-23 之前
    is_unusable_url 全仓零调用，判定写好了也等于没写。
    """
    src = (SCRIPTS / "search_pipeline.py").read_text(encoding="utf-8")
    assert "is_unusable_url" in src, "search_pipeline 未接线内容农场过滤"
    assert "is_serp_or_jump_url as _is_serp" not in src, \
        "仍在使用只管 SERP 的旧入口，内容农场判定被绕过"
