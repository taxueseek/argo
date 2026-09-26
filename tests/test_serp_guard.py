#!/usr/bin/env python3
"""test_serp_guard.py — SERP 垃圾结果守卫回归测试。

覆盖：
  1. is_junk_serp 量化口径：CJK / 拉丁 / 混合 query 三组、token<2 无裁决权、
     前 5 条全零重叠才拦、空/None 边界
  2. 守卫开关 ARGO_SERP_GUARD=0
  3. engines_base 接线：冻结引擎集合范围、判垃圾整页丢弃、直连分支同样过守卫、
     完整 html 引擎链路（mock HTTP）
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from serp_guard import is_junk_serp, guard_enabled  # noqa: E402
import engines_base  # noqa: E402

# 与任何测试 query 都零 token 重叠的垃圾结果（缓存 SERP 形态）
JUNK_EN = [
    {"title": "Easy French onion soup recipe", "snippet": "Caramelize the onions slowly for best flavor."},
    {"title": "Best chocolate cake baking guide", "snippet": "Preheat oven and whisk the cocoa batter."},
    {"title": "Tokyo travel diary", "snippet": "Shibuya crossing at night with street food."},
    {"title": "Knitting patterns for winter", "snippet": "Chunky yarn and simple stitches."},
    {"title": "Home gym setup ideas", "snippet": "Adjustable dumbbells save space."},
]
JUNK_CJK = [
    {"title": "红烧肉的家常做法", "snippet": "五花肉焯水后小火慢炖，收汁即可。"},
    {"title": "清蒸鲈鱼怎么做才鲜", "snippet": "大火蒸八分钟，淋上热油和蒸鱼豉油。"},
    {"title": "家常豆腐煲", "snippet": "嫩豆腐煎至金黄，加香菇焖煮。"},
    {"title": "番茄鸡蛋面", "snippet": "先炒番茄出沙，再下面条。"},
    {"title": "腌萝卜条的爽口做法", "snippet": "盐渍脱水后加糖醋冷藏一夜。"},
]


class TestQuantifiedRules:
    """量化口径逐条锁定：拦与不拦都必须能说清原因。"""

    def test_relevant_results_not_blocked(self):
        related = [
            {"title": "Quantum computing tutorial for beginners", "snippet": "Learn qubits and superposition."},
            {"title": "Another quantum computing intro", "snippet": "Basic gates explained."},
        ]
        assert is_junk_serp("quantum computing tutorial", related) is False

    def test_all_zero_overlap_blocked(self):
        assert is_junk_serp("quantum computing tutorial", JUNK_EN) is True

    def test_partial_overlap_not_blocked(self):
        """5 条里只要有 1 条命中 query token 就不拦（只拦「全部」零重叠）。"""
        mixed = JUNK_EN[:4] + [{"title": "Quantum computing basics", "snippet": ""}]
        assert is_junk_serp("quantum computing tutorial", mixed) is False

    def test_window_is_first_five(self):
        """只看前 5 条：第 6 条起的相关结果救不了全零重叠的前 5 条。"""
        six = JUNK_EN + [{"title": "quantum computing tutorial", "snippet": ""}]
        assert is_junk_serp("quantum computing tutorial", six) is True

    def test_url_excluded_from_overlap(self):
        """URL 不参与：缓存/跳转链接回显 query 参数不得放行垃圾页。"""
        echo = [{"title": "Totally unrelated widgets", "snippet": "Sale on widgets.",
                 "url": "https://j.example.com/l/?uddg=https%3A%2F%2Fx.com%2Fquantum%20computing%20tutorial"}]
        assert is_junk_serp("quantum computing tutorial", echo) is True

    def test_fewer_than_five_results(self):
        assert is_junk_serp("quantum computing tutorial", JUNK_EN[:2]) is True


class TestQueryTokenThreshold:
    """token<2 无裁决权：恒 False，无论结果多无关。"""

    def test_single_cjk_char(self):
        assert is_junk_serp("猫", JUNK_CJK) is False  # 0 个二元组

    def test_two_cjk_chars(self):
        assert is_junk_serp("猫粮", JUNK_CJK) is False  # 只有 1 个二元组

    def test_single_latin_word(self):
        assert is_junk_serp("Python", JUNK_EN) is False  # 1 个词

    def test_empty_and_none_query(self):
        assert is_junk_serp("", JUNK_EN) is False
        assert is_junk_serp(None, JUNK_EN) is False


class TestCJKLatinMixed:
    """CJK / 拉丁 / 混合三组 query 各自验证判定。"""

    def test_cjk_junk_blocked(self):
        assert is_junk_serp("量子计算入门教程", JUNK_CJK) is True

    def test_cjk_relevant_not_blocked(self):
        related = [{"title": "量子计算的基础概念", "snippet": "叠加态与纠缠通俗讲解。"}]
        assert is_junk_serp("量子计算入门教程", related) is False

    def test_latin_junk_blocked(self):
        assert is_junk_serp("rust async runtime", JUNK_EN) is True

    def test_mixed_query_blocked_on_foreign_results(self):
        assert is_junk_serp("python 异步编程指南", JUNK_EN) is True

    def test_mixed_query_relevant_not_blocked(self):
        related = [{"title": "用 Python 实现异步编程", "snippet": "asyncio 事件循环入门。"}]
        assert is_junk_serp("python 异步编程指南", related) is False


class TestBoundaries:
    def test_empty_results(self):
        assert is_junk_serp("quantum computing tutorial", []) is False

    def test_none_results(self):
        assert is_junk_serp("quantum computing tutorial", None) is False

    def test_non_dict_items_count_as_zero_overlap(self):
        """非 dict 条目按空文本处理：全空结果页本身就是解析噪声。"""
        assert is_junk_serp("quantum computing tutorial", [None, 42]) is True

    def test_missing_fields(self):
        assert is_junk_serp("quantum computing tutorial", [{"url": "https://x.com"}]) is True


class TestKillSwitch:
    """ARGO_SERP_GUARD=0 整体关闭；其余取值（含未设置）均开启。"""

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("ARGO_SERP_GUARD", "0")
        assert is_junk_serp("quantum computing tutorial", JUNK_EN) is False

    def test_other_values_still_enabled(self, monkeypatch):
        monkeypatch.setenv("ARGO_SERP_GUARD", "off")
        assert is_junk_serp("quantum computing tutorial", JUNK_EN) is True
        monkeypatch.setenv("ARGO_SERP_GUARD", "")
        assert guard_enabled() is True


class TestEnginesBaseWiring:
    """接线层：引擎范围、整页丢弃语义、开关穿透。"""

    def test_frozen_engine_set(self):
        assert engines_base.SERP_GUARD_ENGINES == frozenset({
            "local_bing", "local_google", "local_baidu", "local_sogou",
            "local_yandex", "local_startpage", "local_mojeek", "local_duckduckgo",
        })

    def test_member_engine_junk_dropped(self):
        assert engines_base._serp_guard_apply("local_bing", "quantum computing tutorial", JUNK_EN) == []

    def test_non_member_engine_untouched(self):
        """集合外引擎（API/垂直源）不受守卫影响。"""
        assert engines_base._serp_guard_apply("moegirl", "quantum computing tutorial", JUNK_EN) == JUNK_EN

    def test_member_engine_relevant_kept(self):
        related = [{"title": "Quantum computing tutorial", "snippet": "start here"}]
        assert engines_base._serp_guard_apply("local_bing", "quantum computing tutorial", related) == related

    def test_switch_penetrates_wiring(self, monkeypatch):
        monkeypatch.setenv("ARGO_SERP_GUARD", "0")
        assert engines_base._serp_guard_apply("local_bing", "quantum computing tutorial", JUNK_EN) == JUNK_EN


@pytest.fixture()
def bing_engine():
    """用真实 parse_maps 选择器（li.b_algo）构建 local_bing 引擎实例。"""
    return engines_base._build_html_engine({
        "_name": "local_bing",
        "url": "https://www.bing.com/search",
        "query_param": "q",
    })


def _serp_html(rows: list[tuple[str, str, str]]) -> str:
    items = "".join(
        f'<li class="b_algo"><h2><a href="{u}">{t}</a></h2>'
        f'<div class="b_caption"><p>{s}</p></div></li>'
        for t, u, s in rows)
    # 页脚惰性填充：_detect_anti_bot 对 <500 字符的页面按拦截页处理，
    # 单条结果的迷你 SERP 需要补足长度才能走到解析与守卫
    filler = '<div class="b_footer">lorem ipsum dolor sit amet consectetur</div>' * 8
    return f"<html><head><title>Bing</title></head><body>{items}{filler}</body></html>"


class TestFullEngineChain:
    """mock HTTP 的完整链路：垃圾页进引擎 → 出来诚实空。"""

    def _run(self, bing_engine, monkeypatch, html, query):
        monkeypatch.setattr(engines_base, "_http_get_raw",
                            lambda url, headers, timeout, engine="?": html)
        return bing_engine(query)

    def test_junk_serp_becomes_honest_empty(self, bing_engine, monkeypatch):
        html = _serp_html([(r["title"], "https://j.example.com/a", r["snippet"]) for r in JUNK_EN])
        assert self._run(bing_engine, monkeypatch, html, "quantum computing tutorial") == []

    def test_relevant_serp_passes_through(self, bing_engine, monkeypatch):
        html = _serp_html([("Quantum computing tutorial", "https://x.example.com/q", "qubits explained")])
        out = self._run(bing_engine, monkeypatch, html, "quantum computing tutorial")
        assert len(out) == 1 and out[0]["source"] == "local_bing"

    def test_interstitial_page_via_direct_hit_branch(self, bing_engine, monkeypatch):
        """无列表容器但有页面标题（拦截页/重定向页形态）同样被守卫拦下。"""
        html = ("<html><head><title>DuckDuckGo</title></head><body>"
                + "lorem ipsum dolor sit amet " * 40 + "</body></html>")
        assert self._run(bing_engine, monkeypatch, html, "quantum computing tutorial") == []

    def test_direct_hit_branch_relevant_passes(self, bing_engine, monkeypatch):
        html = ("<html><head><title>quantum computing tutorial - Notes</title></head><body>"
                + "lorem ipsum dolor sit amet " * 40 + "</body></html>")
        out = self._run(bing_engine, monkeypatch, html, "quantum computing tutorial")
        assert len(out) == 1

    def test_switch_disables_full_chain(self, bing_engine, monkeypatch):
        monkeypatch.setenv("ARGO_SERP_GUARD", "0")
        html = _serp_html([(r["title"], "https://j.example.com/a", r["snippet"]) for r in JUNK_EN])
        out = self._run(bing_engine, monkeypatch, html, "quantum computing tutorial")
        assert len(out) == 5
