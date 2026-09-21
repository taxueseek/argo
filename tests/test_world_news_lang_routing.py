#!/usr/bin/env python3
"""world_news 语言绑定选源的回归门禁（2026-09-21）。

背景：接入 14 个本地语言新闻源后，**只声明不接线等于没接**——实测 ko 查询
的 combo 是 `[anysearch, local_bing]`，yna 排在预算窗口之外；es 查询被判成
en（lang_detect 对拉丁字母语言只给到 en/latin），elpais 同样选不中。

本文件锁住四件事：

1. 每种语言都能选到对应的本地源（14 语言 × 1 源）；
2. 书写系统标签（cyrillic / arabic / thai …）能匹配该语系下的具体语言源
   （`новости сегодня` 判成 cyrillic，而 tass 声明的是 ru）；
3. 拉丁字母语言靠查询实词兜底（`noticias de hoy` → es）；
4. 反向：别的语言的一手源不会被选中（英文查询不该拿到韩联社）。

第 4 条与第 1 条同等重要：语言绑定族的错误代价是「拿到另一种语言的一手
新闻」，比「没有一手源」更难被用户察觉。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import route  # noqa: E402
from engine_families import (  # noqa: E402
    _LANG_HINT_WORDS,
    _SCRIPT_FAMILY_LANGS,
    family_of,
    lang_allows,
    lang_hint_from_query,
)

# 查询 → 该语言应被选中的本地源。查询文本取该语言的自然说法（「今天的新闻」），
# 而不是人为构造的触发词——用户就是这么问的。
LANG_CASES = [
    ("오늘 뉴스", "ko", "yna"),
    ("новости сегодня", "ru", "tass"),
    ("أخبار اليوم", "ar", "aljazeera"),
    ("noticias de hoy", "es", "elpais"),
    ("notícias de hoje", "pt", "folha"),
    ("actualités du jour", "fr", "lefigaro"),
    ("aktuelle Nachrichten", "de", "faz"),
    ("今日のニュース", "ja", "nhk"),
    ("ข่าววันนี้", "th", "matichon"),
    ("tin tức hôm nay", "vi", "vnpress"),
    ("berita hari ini", "id", "antara"),
    ("bugün haberler", "tr", "hurriyet"),
    ("חדשות היום", "he", "ynet"),
    ("dnešní zprávy", "cs", "ct24"),
]

WORLD_NEWS_ENGINES = {e for _, _, e in LANG_CASES}


def _combo(query: str, depth: str = "fast") -> list[str]:
    return list(route.route_query(
        query, engine_override="auto", mode="auto", depth=depth
    ).get("engines_combo") or [])


class TestEachLanguagePicksItsOwnSource:
    """14 语言 × 1 源：查询语言决定选中哪个本地源。"""

    @pytest.mark.parametrize("query,lang,engine", LANG_CASES)
    def test_language_source_selected(self, query, lang, engine):
        combo = _combo(query)
        assert engine in combo, (
            f"{lang} 查询 {query!r} 未选中本地源 {engine}；实际 combo={combo}"
        )

    @pytest.mark.parametrize("query,lang,engine", LANG_CASES)
    def test_own_source_is_not_squeezed_out(self, query, lang, engine):
        """预算窗口内必须给本地源留位——它排在第 2 位（anysearch 之后）。"""
        combo = _combo(query)
        assert combo.index(engine) <= 1, (
            f"{lang} 的本地源 {engine} 落在预算窗口外：combo={combo}"
        )

    @pytest.mark.parametrize("query,lang,engine", LANG_CASES)
    def test_no_foreign_language_source_at_any_depth(self, query, lang, engine):
        """deep 档不截断预算，别语言的一手源必须已被摘除（不是「移尾」）。

        实测教训：移尾方案下 deep 档的韩语查询会连跑 elpais/tass/aljazeera
        等 16 个源——白付网络与配额，且拿回的是别国语言的内容。
        """
        for depth in ("fast", "balanced", "deep"):
            combo = _combo(query, depth=depth)
            foreign = (set(combo) & WORLD_NEWS_ENGINES) - {engine}
            assert not foreign, (
                f"{lang} 查询在 {depth} 档混入别语言源 {sorted(foreign)}：{combo}"
            )


class TestNoCrossLanguagePick:
    """反向门禁：别的语言的一手源不得被选中。"""

    def test_english_query_does_not_get_korean_source(self):
        combo = _combo("world news")
        assert not (set(combo) & WORLD_NEWS_ENGINES), (
            f"英文查询拿到了别国一手源：{combo}"
        )

    def test_english_query_routes_to_english_news_domain(self):
        r = route.route_query("world news", engine_override="auto",
                              mode="auto", depth="fast")
        assert r.get("domain") == "intl_news_flash", (
            f"英文国际新闻应走 intl_news_flash，实际 {r.get('domain')}"
        )

    def test_general_fallback_always_present(self):
        """任何语言下都必须有语言中立的兜底源，防语言判定失误变成零结果。"""
        for query, _, _ in LANG_CASES:
            combo = _combo(query)
            assert "anysearch" in combo, f"{query!r} 的 combo 缺通用兜底：{combo}"


class TestScriptFamilyExpansion:
    """书写系统标签 → 该语系下的具体语言源。"""

    @pytest.mark.parametrize("script,engine", [
        ("cyrillic", "tass"),
        ("arabic", "aljazeera"),
        ("hebrew", "ynet"),
        ("thai", "matichon"),
    ])
    def test_script_label_matches_language_source(self, script, engine):
        assert lang_allows(engine, script), (
            f"{script} 查询应认 {engine}（源声明的是具体语言码）"
        )

    def test_expansion_is_one_directional(self):
        """反向不展开：具体语言查询不认别语系源，防「zh 查询用日文源」串味。"""
        assert not lang_allows("tass", "zh")
        assert not lang_allows("aljazeera", "ja")
        assert not lang_allows("yna", "en")

    def test_script_table_covers_detector_labels(self):
        """lang_detect 会给出的语系标签都应在表里，否则那些语言静默失配。"""
        from lang_detect import detect_language

        for query, _, _ in LANG_CASES:
            label = detect_language(query)
            if label in _SCRIPT_FAMILY_LANGS:
                continue  # 已覆盖
            assert label not in ("cyrillic", "arabic", "hebrew", "thai"), (
                f"{query!r} 判成 {label}，但该语系不在展开表里"
            )


class TestLatinLanguageHint:
    """拉丁字母语言：lang_detect 只给 en/latin，用查询实词兜底。"""

    @pytest.mark.parametrize("query,expected", [
        ("noticias de hoy", "es"),
        ("notícias de hoje", "pt"),
        ("actualités du jour", "fr"),
        ("aktuelle Nachrichten", "de"),
        ("berita hari ini", "id"),
        ("tin tức hôm nay", "vi"),
        ("bugün haberler", "tr"),
        ("dnešní zprávy", "cs"),
    ])
    def test_hint_detects_latin_language(self, query, expected):
        assert lang_hint_from_query(query) == expected

    def test_hint_returns_empty_for_plain_english(self):
        assert lang_hint_from_query("world news") == ""
        assert lang_hint_from_query("python asyncio tutorial") == ""

    def test_hint_does_not_override_explicit_language(self):
        """具体语言判定（ko/ja/zh…）优先于实词兜底，不被 hint 覆盖。"""
        assert route._family_lang({"primary_lang": "ko"}, "오늘 뉴스") == "ko"
        assert route._family_lang({"lang_override": "ja"}, "AI ニュース") == "ja"

    def test_hint_words_are_lowercase_safe(self):
        """提示词按小写比较，大小写混写的查询同样命中。"""
        for words in _LANG_HINT_WORDS.values():
            for w in words:
                assert lang_hint_from_query(w.upper()) == \
                    lang_hint_from_query(w.lower())


class TestLangExclusiveSource:
    """语言独占源（如 cinii=ja）：只在该语言的查询里在场。

    与 world_news 族的区别是「摘除」的判据来源：族按 family，独占源按
    route._LANG_EXCLUSIVE_ENGINES 白名单（既有源的移尾语义是 2026-09-07
    review 的契约，不回溯改；新源从接入起就按摘除处理）。
    """

    def test_reachable_for_its_own_language(self):
        combo = _combo("人工知能 論文", depth="balanced")
        assert "cinii" in combo, f"日语学术查询未选中 cinii：{combo}"

    @pytest.mark.parametrize("query", ["machine learning survey", "机器学习 论文"])
    def test_dropped_for_other_languages(self, query):
        combo = _combo(query, depth="balanced")
        assert "cinii" not in combo, (
            f"{query!r} 拿到了日文专用源：{combo}——cinii 对该语言零召回"
        )

    def test_registered_in_static_lang_table(self):
        """语言独占源必须同时落在 ENGINE_LANGS：route._specs_snapshot() 在
        engines 未加载时返回空表，只写 spec YAML 会被路由层漏掉（实测漏登记时
        英文查询照样把它选进预算窗口）。"""
        from engine_families import ENGINE_LANGS

        for eng in route._LANG_EXCLUSIVE_ENGINES:
            assert ENGINE_LANGS.get(eng), f"{eng} 未在 ENGINE_LANGS 声明语言"


class TestFamilyMembershipDeclared:
    """族与语言声明必须同时落在静态表里。

    route._specs_snapshot() 在 engines 模块未加载时返回空表（性能设计），
    此时 family_of / lang_allows 走 engine_families 的静态表——新源只在
    spec YAML 里声明会被路由层漏掉（本次实现时实测踩到）。
    """

    @pytest.mark.parametrize("engine", sorted(WORLD_NEWS_ENGINES))
    def test_family_registered_statically(self, engine):
        assert family_of(engine, None) == "world_news"

    @pytest.mark.parametrize("engine", sorted(WORLD_NEWS_ENGINES))
    def test_langs_registered_statically(self, engine):
        from engine_families import ENGINE_LANGS

        assert ENGINE_LANGS.get(engine), f"{engine} 未在 ENGINE_LANGS 表声明语言"

    def test_family_not_refilled_into_generic_combo(self):
        """world_news 是实时流族，不得参与通用 combo 回填（同 hot_trending）。"""
        from engine_families import _REFILL_EXCLUDED_FAMILIES

        assert "world_news" in _REFILL_EXCLUDED_FAMILIES
