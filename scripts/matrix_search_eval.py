#!/usr/bin/env python3
"""matrix_search_eval.py — 多语言 × 引擎族 × 场景 全面测试 harness

沿 loho / regression_p0p1 思路：
  - offline：语言检测、路由矩阵、recovery 门禁、引擎族、lang_pref（无网络）
  - live：多语金标 E2E（domain / primary / selection_hit / 污染）

用法：
  python3 scripts/matrix_search_eval.py --offline
  python3 scripts/matrix_search_eval.py --live
  python3 scripts/matrix_search_eval.py --all
  python3 scripts/matrix_search_eval.py --all --report tests/matrix_search_report.json

退出码：有 hard FAIL 则为 1（warn 不计入）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# 中文专用源：非中文主语言不得入 combo
_CN_ONLY = frozenset({
    "bocha", "byted", "wechat_sogou", "zhihu", "zhihu_hot",
    "baidu_hot", "toutiao_hot", "weibo", "xiaohongshu",
    "sina_quote", "tencent_quote", "em_flow", "eastmoney",
    "cls_telegraph", "jin10", "em_miaoxiang", "em_global_news",
    "baidu_baike", "zh_wikipedia", "local_baidu", "local_sogou",
    "weread", "douban_book", "juejin", "v2ex", "wenshu",
})

# recovery L3 禁止的无关垂直（与 recovery 门禁对齐）
_RECOVERY_POLLUTE = frozenset({
    "pypi", "npm", "crates", "jin10", "cls_telegraph",
    "sina_quote", "finviz", "pubchem", "steam",
})

# 日韩等语言引擎候选
_LANG_ENGINE_CANDIDATES = {
    "ja": ("local_yandex", "local_bing", "local_duckduckgo", "local_google"),
    "ko": ("local_google", "local_bing", "local_duckduckgo"),
    "cyrillic": ("local_bing", "anysearch", "duckduckgo", "local_yandex"),
    "thai": ("local_bing", "anysearch", "duckduckgo"),
    "arabic": ("local_bing", "anysearch", "duckduckgo"),
    "hebrew": ("local_bing", "anysearch", "duckduckgo"),
    "greek": ("local_bing", "anysearch", "duckduckgo"),
    "devanagari": ("local_bing", "anysearch", "duckduckgo"),
    "en": ("anysearch", "duckduckgo", "local_bing", "octen", "exa"),
    "zh": ("byted", "bocha", "local_bing", "anysearch", "zhihu_global", "duckduckgo"),
}


class Checker:
    def __init__(self) -> None:
        self.ok = 0
        self.fail = 0
        self.warn = 0
        self.rows: list[dict[str, Any]] = []

    def check(
        self,
        name: str,
        cond: bool,
        detail: str = "",
        *,
        soft: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        if cond:
            self.ok += 1
            status = "PASS"
        elif soft:
            self.warn += 1
            status = "WARN"
        else:
            self.fail += 1
            status = "FAIL"
        row: dict[str, Any] = {
            "name": name,
            "status": status,
            "detail": detail,
        }
        if meta:
            row["meta"] = meta
        self.rows.append(row)
        mark = {"PASS": "✓", "FAIL": "✗", "WARN": "~"}[status]
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        return cond


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE A — 语言检测
# ═══════════════════════════════════════════════════════════════════════════

LANG_DETECT_CASES: list[tuple[str, str, str]] = [
    # (id, query, expect_lang)
    ("ld_zh", "苹果股价最新行情", "zh"),
    ("ld_zh_mix", "Python 异步编程", "zh"),
    ("ld_en", "React hooks tutorial", "en"),
    ("ld_en2", "hello world", "en"),
    ("ld_ja_kana", "アニメ おすすめ", "ja"),
    ("ld_ja_mix", "東京の天気予報", "ja"),
    ("ld_ja_kata", "トヨタ 株価", "ja"),
    ("ld_ko", "한국 영화 추천", "ko"),
    ("ld_ko2", "삼성전자 주가", "ko"),
    ("ld_latin", "café français", "latin"),
    ("ld_cyr", "Как написать скрипт", "cyrillic"),
    ("ld_cyr_mix", "Python курс", "cyrillic"),
    ("ld_thai", "สวัสดีครับ", "thai"),
    ("ld_ar", "مرحبا بالعالم", "arabic"),
    ("ld_he", "שלום עולם", "hebrew"),
    ("ld_el", "Καλημέρα", "greek"),
    ("ld_hi", "नमस्ते दुनिया", "devanagari"),
    # 纯汉字日文地名：当前可能判 zh（假名缺席）— soft 期望仍记为 ja 缺口
    ("ld_ja_kanji_only", "宮崎駿", "ja"),
]


def run_offline_lang(c: Checker) -> None:
    print("\n== offline: language detection ==")
    from lang_detect import detect_language, detect_script, engine_lang_param, language_features

    for cid, q, expect in LANG_DETECT_CASES:
        got = detect_language(q)
        soft = cid == "ld_ja_kanji_only" and got == "zh"
        c.check(
            f"lang:{cid}",
            got == expect,
            detail=f"got={got} expect={expect} q={q!r}",
            soft=soft,
            meta={"query": q, "lang": got, "expect": expect},
        )

    # script 与 primary 同源
    for q, lang, script in [
        ("苹果股价", "zh", "cjk"),
        ("アニメ", "ja", "kana"),
        ("한국", "ko", "hangul"),
        ("hello", "en", "latin"),
        ("Как", "cyrillic", "cyrillic"),
    ]:
        f = language_features(q)
        c.check(
            f"script_align:{lang}",
            f["primary_lang"] == lang and f["script"] == script,
            detail=str(f),
        )

    # 引擎语言参数覆盖非 CJK
    for q, param, expect_sub in [
        ("アニメ", "setlang", "ja"),
        ("한국", "hl", "ko"),
        ("hello world", "setlang", "en"),
        ("苹果", "setlang", "zh"),
        ("Как дела", "setlang", "ru"),
        ("مرحبا", "setlang", "ar"),
        ("שלום", "lang", "he"),
    ]:
        v = engine_lang_param(param, q)
        c.check(
            f"lang_param:{param}:{q[:6]}",
            expect_sub.lower() in (v or "").lower(),
            detail=f"got={v}",
        )


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE B — 路由矩阵（语言 × 场景）
# ═══════════════════════════════════════════════════════════════════════════

# soft=True：已知能力缺口（日韩垂直域、纯汉字日文），记 WARN 不阻断
ROUTE_MATRIX: list[dict[str, Any]] = [
    # ── 金融 / 行情 ──
    {"id": "R_zh_stock", "q": "贵州茅台股价", "lang": "zh", "domain": "stock_query",
     "primary": "sina_quote", "scenario": "finance"},
    {"id": "R_en_usstock", "q": "AAPL stock price", "lang": "en", "domain": "us_stock",
     "primary": "finviz", "scenario": "finance"},
    {"id": "R_zh_macro", "q": "中国 GDP", "lang": "zh", "domain": "macro_data",
     "primary_any": ["worldbank", "nbs_stats", "fred"], "scenario": "macro"},
    {"id": "R_en_macro", "q": "US CPI", "lang": "en", "domain": "macro_data",
     "primary": "fred", "scenario": "macro"},
    {"id": "R_ja_stock", "q": "トヨタ 株価", "lang": "ja", "domain": None,
     "need_lang_engine": True, "forbid_cn": True, "scenario": "finance", "soft": True},
    {"id": "R_ko_stock", "q": "삼성전자 주가", "lang": "ko", "domain": None,
     "need_lang_engine": True, "forbid_cn": True, "scenario": "finance", "soft": True},

    # ── 影视 ──
    {"id": "R_en_film", "q": "Inception movie director", "lang": "en",
     "domain": "film_search", "primary": "imdb", "scenario": "film"},
    {"id": "R_zh_film", "q": "肖申克的救赎 主演", "lang": "zh",
     "domain": "film_search", "primary": "imdb", "scenario": "film"},
    {"id": "R_en_film2", "q": "Interstellar film cast", "lang": "en",
     "domain": "film_search", "primary": "imdb", "scenario": "film"},
    {"id": "R_ja_film_kana", "q": "宮崎駿 アニメ 映画", "lang": "ja",
     "domain": "film_search", "primary": "imdb", "scenario": "film", "soft": True},
    {"id": "R_ko_film", "q": "기생충 영화 감독", "lang": "ko",
     "domain": "film_search", "primary": "imdb", "scenario": "film", "soft": True},

    # ── 体育 ──
    {"id": "R_zh_sports", "q": "梅西 俱乐部", "lang": "zh",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_en_sports", "q": "LeBron James team", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_en_sports2", "q": "Cristiano Ronaldo club", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_zh_sports2", "q": "库里 球队", "lang": "zh",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_en_wc", "q": "World Cup 2022 winner", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_ja_sports", "q": "メッシ クラブ", "lang": "ja",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports", "soft": True},
    {"id": "R_ko_sports", "q": "손흥민 팀", "lang": "ko",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports", "soft": True},

    # ── 地理 ──
    {"id": "R_zh_geo", "q": "埃菲尔铁塔在哪", "lang": "zh",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo"},
    {"id": "R_en_geo", "q": "where is Eiffel Tower", "lang": "en",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo"},
    {"id": "R_zh_geo2", "q": "黄河 流经省份", "lang": "zh",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo"},
    {"id": "R_ja_geo", "q": "エッフェル塔 どこ", "lang": "ja",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo", "soft": True},

    # ── 组织 ──
    {"id": "R_zh_org", "q": "国务院职能", "lang": "zh",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    {"id": "R_en_org", "q": "NASA founding year", "lang": "en",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    {"id": "R_en_org2", "q": "Apple Inc headquarters", "lang": "en",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    {"id": "R_zh_org2", "q": "清华大学 创办年份", "lang": "zh",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},

    # ── 媒体/音乐 ──
    {"id": "R_en_media", "q": "Taylor Swift album", "lang": "en",
     "domain": "media_search", "primary": "itunes", "scenario": "media"},
    {"id": "R_zh_media", "q": "周杰伦 专辑", "lang": "zh",
     "domain": "media_search", "primary": "itunes", "scenario": "media"},

    # ── 天气 ──
    {"id": "R_zh_weather", "q": "北京 天气", "lang": "zh",
     "domain": "weather_query", "scenario": "weather"},
    {"id": "R_en_weather", "q": "weather Tokyo", "lang": "en",
     "domain": "weather_query", "scenario": "weather"},
    {"id": "R_ja_weather", "q": "東京の天気", "lang": "ja",
     "domain": "weather_query", "scenario": "weather", "soft": True},

    # ── 学术 / 化学 / RFC / 代码 ──
    {"id": "R_en_academic", "q": "transformer attention paper", "lang": "en",
     "domain": "academic", "primary_any": ["arxiv", "crossref", "semantic_scholar"],
     "scenario": "academic"},
    {"id": "R_zh_chem", "q": "阿司匹林 分子式", "lang": "zh",
     "domain": "chem_search", "primary": "pubchem", "scenario": "chem"},
    # RFC 9110 字母过少，lang 可能判 other；只硬卡 domain/primary
    {"id": "R_en_rfc", "q": "RFC 9110", "lang": None,
     "domain": "rfc_search", "primary": "rfc_editor", "scenario": "rfc"},
    {"id": "R_en_tech", "q": "React vs Vue", "lang": "en",
     "domain": None,  # english_tech / general
     "forbid": list(_CN_ONLY - {"local_baidu", "local_sogou"}),  # 放宽 local
     "forbid_cn": True, "scenario": "tech"},
    {"id": "R_zh_package", "q": "requests pypi", "lang": "en",
     "domain": "package_search", "primary_any": ["pypi", "npm"], "scenario": "code",
     "soft": True},

    # ── 通用多语 ──
    {"id": "R_ja_gen", "q": "アニメ おすすめ", "lang": "ja",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_ko_gen", "q": "한국 영화 추천", "lang": "ko",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_cyr_gen", "q": "Как написать скрипт", "lang": "cyrillic",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_ar_gen", "q": "أخبار التكنولوجيا", "lang": "arabic",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_th_gen", "q": "อากาศ กรุงเทพ", "lang": "thai",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_en_gen", "q": "hello world", "lang": "en",
     "domain": None, "forbid_cn": True, "scenario": "general"},
    {"id": "R_zh_gen", "q": "如何学习机器学习", "lang": "zh",
     "domain": None, "scenario": "general"},
]


def _route_ok(case: dict[str, Any], d: dict[str, Any]) -> tuple[bool, str]:
    combo = list(d.get("engines_combo") or [])
    got_d = d.get("domain")
    got_e = d.get("engine")
    feat = d.get("features") or {}
    got_lang = feat.get("primary_lang")
    problems: list[str] = []

    if case.get("lang") is not None and got_lang != case["lang"]:
        # 纯汉字等已知误判允许 soft 外层处理；这里仍记问题
        problems.append(f"lang={got_lang}!={case['lang']}")

    if case.get("domain") is not None and got_d != case["domain"]:
        problems.append(f"domain={got_d}!={case['domain']}")

    primary = case.get("primary")
    if primary and not (got_e == primary or primary in combo[:2]):
        problems.append(f"primary missing: eng={got_e} combo={combo}")

    primary_any = case.get("primary_any")
    if primary_any and not any(p == got_e or p in combo[:3] for p in primary_any):
        problems.append(f"primary_any miss: eng={got_e} combo={combo}")

    if case.get("need_lang_engine"):
        cands = _LANG_ENGINE_CANDIDATES.get(case.get("lang") or "", ())
        if cands and not any(e in combo for e in cands):
            problems.append(f"no lang engine in {combo}")

    forbid = set(case.get("forbid") or [])
    if case.get("forbid_cn"):
        forbid |= _CN_ONLY
        # 中文主查询允许中文源
        if case.get("lang") == "zh":
            forbid -= _CN_ONLY
    hit_forbid = [e for e in combo if e in forbid]
    if hit_forbid:
        problems.append(f"forbid={hit_forbid}")

    ok = not problems
    detail = (
        f"domain={got_d} eng={got_e} combo={combo} lang={got_lang}"
        + (f" | {'; '.join(problems)}" if problems else "")
    )
    return ok, detail


def run_offline_route(c: Checker) -> None:
    print("\n== offline: route matrix (lang × scenario) ==")
    from route import route_query

    by_scenario: dict[str, list[bool]] = {}
    by_lang: dict[str, list[bool]] = {}

    for case in ROUTE_MATRIX:
        d = route_query(case["q"], mode="auto", depth="fast", context="search")
        ok, detail = _route_ok(case, d)
        soft = bool(case.get("soft"))
        c.check(
            f"route:{case['id']}",
            ok,
            detail=detail,
            soft=soft,
            meta={
                "id": case["id"],
                "q": case["q"],
                "scenario": case.get("scenario"),
                "lang": case.get("lang"),
                "domain": d.get("domain"),
                "engine": d.get("engine"),
                "combo": d.get("engines_combo"),
                "primary_lang": (d.get("features") or {}).get("primary_lang"),
            },
        )
        sc = case.get("scenario") or "other"
        lg = case.get("lang") or "other"
        by_scenario.setdefault(sc, []).append(ok)
        by_lang.setdefault(lg, []).append(ok)

    print("\n  -- route coverage by scenario --")
    for sc, vals in sorted(by_scenario.items()):
        rate = sum(vals) / len(vals) if vals else 0
        print(f"     {sc:12} {sum(vals)}/{len(vals)} ({rate:.0%})")
    print("  -- route coverage by lang --")
    for lg, vals in sorted(by_lang.items()):
        rate = sum(vals) / len(vals) if vals else 0
        print(f"     {lg:12} {sum(vals)}/{len(vals)} ({rate:.0%})")


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE C — recovery 门禁
# ═══════════════════════════════════════════════════════════════════════════

def run_offline_recovery(c: Checker) -> None:
    print("\n== offline: recovery gates ==")
    from recovery import (
        build_recovery_plan,
        cross_lang_query,
        pick_alternative_engines,
        baseline_cross_query,
    )

    # L3：空 tried 时不得选污染垂直源
    picks = pick_alternative_engines(
        tried=["imdb"],
        engines_fallback=["pypi", "npm", "jin10", "anysearch", "duckduckgo"],
    )
    c.check(
        "rec_l3_no_pollute_film",
        not any(e in _RECOVERY_POLLUTE for e in picks) and bool(picks),
        detail=str(picks),
    )
    picks2 = pick_alternative_engines(
        tried=["thesportsdb"],
        engines_fallback=["crates", "pypi", "wikipedia", "anysearch"],
    )
    c.check(
        "rec_l3_no_pollute_sports",
        not any(e in _RECOVERY_POLLUTE for e in picks2) and bool(picks2),
        detail=str(picks2),
    )

    # 同族可保留（knowledge）
    picks3 = pick_alternative_engines(
        tried=["wikipedia"],
        engines_fallback=["wikidata", "pypi"],
        max_n=2,
    )
    c.check(
        "rec_l3_same_family_ok",
        "pypi" not in picks3,
        detail=str(picks3),
    )

    # 跨语言
    for q, expect_cross in [
        ("アニメ おすすめ", True),
        ("한국 영화", True),
        ("如何写爬虫", True),
        ("Как дела", True),
        ("hello world", False),
        ("React vs Vue", False),
    ]:
        cq, ce = cross_lang_query(q)
        has = bool(ce)
        c.check(
            f"rec_cross:{q[:12]}",
            has is expect_cross,
            detail=f"engines={ce!r}",
        )

    # plan 含 L4 cross_lang（非 fast）
    plan = build_recovery_plan(
        "アニメ おすすめ", ["anysearch"], ["duckduckgo", "wikipedia"], mode="auto",
    )
    strategies = [s.strategy for s in plan]
    c.check("rec_plan_cross_lang", "cross_lang" in strategies, detail=str(strategies))

    plan_fast = build_recovery_plan(
        "アニメ おすすめ", ["anysearch"], ["duckduckgo"], mode="fast",
    )
    c.check(
        "rec_fast_no_cross",
        "cross_lang" not in [s.strategy for s in plan_fast],
        detail=str([s.strategy for s in plan_fast]),
    )

    # baseline zh
    cq, ce = baseline_cross_query("hello world", primary_lang="en", prefer=["zh", "en"])
    c.check("rec_baseline_zh", bool(ce), detail=str(ce))
    cq2, ce2 = baseline_cross_query("hello world", primary_lang="en", prefer=["en"])
    c.check("rec_baseline_skip", not ce2, detail=str(ce2))


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE D — 引擎族 + 注册完整性
# ═══════════════════════════════════════════════════════════════════════════

FAMILY_EXPECT: dict[str, str] = {
    "anysearch": "web_general",
    "duckduckgo": "web_general",
    "local_bing": "web_general",
    "arxiv": "academic",
    "github": "code",
    "pypi": "code",
    "sina_quote": "finance_market",
    "finviz": "finance_market",
    "fred": "finance_macro",
    "worldbank": "finance_macro",
    "jin10": "news_flash",
    "zhihu": "social",
    "wikipedia": "knowledge",
    "wikidata": "knowledge",
    "baidu_baike": "knowledge",
    "pubchem": "science_chem",
    "imdb": "media_book",
    "itunes": "media_book",
    "thesportsdb": "sports",
    "local_openstreetmap": "science_geo",
}


def run_offline_families(c: Checker) -> None:
    print("\n== offline: engine families ==")
    from engine_families import family_of, DEFAULT_FAMILY

    for eng, expect in FAMILY_EXPECT.items():
        got = family_of(eng)
        soft = eng == "local_openstreetmap" and got != expect
        c.check(
            f"family:{eng}",
            got == expect,
            detail=f"got={got}",
            soft=soft or (got == DEFAULT_FAMILY and expect != DEFAULT_FAMILY),
        )

    # 关键垂直源不得被误标 web_general（污染风险）
    for eng in ("pypi", "npm", "jin10", "sina_quote", "imdb", "thesportsdb"):
        got = family_of(eng)
        c.check(
            f"family_not_general:{eng}",
            got != "web_general",
            detail=f"got={got}",
        )


def run_offline_lang_pref(c: Checker) -> None:
    print("\n== offline: lang_pref ==")
    from lang_pref import (
        prefer_langs, system_lang, dominant_habit,
        effective_engine_lang, record_query_lang, reset_habit_for_tests,
        BASELINE_LANGS,
    )

    reset_habit_for_tests()
    try:
        c.check("pref_sys_zh", system_lang("zh_CN.UTF-8") == "zh")
        c.check("pref_sys_ja", system_lang("ja_JP") == "ja")
        c.check("pref_sys_ko", system_lang("ko_KR") == "ko")
        c.check("pref_sys_ru", system_lang("ru_RU") == "cyrillic")

        prefs = prefer_langs(
            query_lang="ko", system="zh", habit="ja", use_live_habit=False,
        )
        c.check("pref_query_first", prefs[0] == "ko", detail=str(prefs))
        for b in BASELINE_LANGS:
            c.check(f"pref_baseline:{b}", b in prefs, detail=str(prefs))

        c.check(
            "pref_effective_strong",
            effective_engine_lang("ja", ["zh", "en"]) == "ja",
        )
        c.check(
            "pref_effective_weak",
            effective_engine_lang("mixed", ["ja", "zh", "en"]) == "ja",
        )

        for _ in range(6):
            record_query_lang("ja")
        c.check("pref_habit", dominant_habit() == "ja")
    finally:
        reset_habit_for_tests()


def run_offline(c: Checker) -> None:
    run_offline_lang(c)
    run_offline_route(c)
    run_offline_recovery(c)
    run_offline_families(c)
    run_offline_lang_pref(c)


# ═══════════════════════════════════════════════════════════════════════════
# LIVE — 多语 × 场景 E2E
# ═══════════════════════════════════════════════════════════════════════════

# gold: 标题/摘要命中任一即可；pollute: source/title 命中则污染
LIVE_CASES: list[dict[str, Any]] = [
    # film
    {"id": "L_en_film", "q": "Inception movie director", "lang": "en",
     "domain": "film_search", "primary": "imdb", "scenario": "film",
     "gold": ["nolan", "inception", "christopher"], "pollute": ["pypi", "npm", "jin10"]},
    {"id": "L_zh_film", "q": "肖申克的救赎 主演", "lang": "zh",
     "domain": "film_search", "primary": "imdb", "scenario": "film",
     "gold": ["shawshank", "redemption", "肖申克", "freeman", "robbins"],
     "pollute": ["pypi", "npm"]},
    # sports
    {"id": "L_en_sports", "q": "Cristiano Ronaldo club", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports",
     "gold": ["ronaldo", "cristiano", "al nassr", "portugal"],
     "pollute": ["pypi", "npm", "jin10"]},
    {"id": "L_zh_sports", "q": "库里 球队", "lang": "zh",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports",
     "gold": ["curry", "stephen", "warrior", "库里"],
     "pollute": ["pypi", "npm"]},
    # media
    {"id": "L_zh_media", "q": "周杰伦 专辑", "lang": "zh",
     "domain": "media_search", "primary": "itunes", "scenario": "media",
     "gold": ["jay", "chou", "周杰伦", "album", "叶惠美", "范特西"],
     "pollute": ["pypi", "crates"]},
    {"id": "L_en_media", "q": "Taylor Swift album", "lang": "en",
     "domain": "media_search", "primary": "itunes", "scenario": "media",
     "gold": ["taylor", "swift", "album", "midnights", "1989"],
     "pollute": ["pypi"]},
    # org / geo
    {"id": "L_en_org", "q": "NASA founding year", "lang": "en",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org",
     "gold": ["nasa", "1958", "space", "aeronautics"],
     "pollute": ["pypi", "npm"]},
    {"id": "L_en_geo", "q": "where is Eiffel Tower", "lang": "en",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo",
     "gold": ["eiffel", "paris", "france", "tower"],
     "pollute": ["pypi"]},
    # finance
    {"id": "L_zh_stock", "q": "贵州茅台股价", "lang": "zh",
     "domain": "stock_query", "primary": "sina_quote", "scenario": "finance",
     "gold": ["茅台", "600519", "股价", "行情", "元"],
     "pollute": ["pypi", "imdb"]},
    # chem / rfc
    {"id": "L_zh_chem", "q": "阿司匹林 分子式", "lang": "zh",
     "domain": "chem_search", "primary": "pubchem", "scenario": "chem",
     "gold": ["aspirin", "c9h8o4", "阿司匹林", "acetylsalicylic", "pubchem"],
     "pollute": ["jin10", "imdb"]},
    # multi-lang general（允许 recovery / 通用源，关注污染与语言）
    {"id": "L_ja_gen", "q": "アニメ おすすめ", "lang": "ja",
     "domain": None, "scenario": "general",
     "gold": ["アニメ", "anime", "おすすめ", "漫画", "作品"],
     "pollute": ["pypi", "npm", "sina_quote", "jin10"],
     "timeout": 25},
    {"id": "L_ko_gen", "q": "한국 영화 추천", "lang": "ko",
     "domain": None, "scenario": "general",
     "gold": ["영화", "movie", "korea", "한국", "film"],
     "pollute": ["pypi", "npm", "sina_quote"],
     "timeout": 25},
    {"id": "L_cyr_gen", "q": "Как написать скрипт python", "lang": "cyrillic",
     "domain": None, "scenario": "general",
     "gold": ["python", "скрипт", "script", "программ"],
     "pollute": ["sina_quote", "jin10", "imdb"],
     "timeout": 25},
]


def _blob_from_results(results: list[dict], used: list, eng: str) -> str:
    parts: list[str] = []
    for x in results:
        for k in ("title", "snippet", "content", "url", "source", "engine"):
            v = x.get(k)
            if v:
                parts.append(str(v))
    parts.extend(str(u) for u in used)
    parts.append(eng or "")
    return " ".join(parts).lower()


def run_live(c: Checker) -> None:
    print("\n== live: multilingual × scenario E2E ==")
    from search import super_search

    live_rows: list[dict[str, Any]] = []
    for case in LIVE_CASES:
        q = case["q"]
        timeout = int(case.get("timeout") or 20)
        t0 = time.perf_counter()
        try:
            r = super_search(
                q, n=5, mode="auto", depth="fast",
                skip_cache=True, timeout=timeout, envelope=False,
            )
        except Exception as exc:  # noqa: BLE001
            ms = int((time.perf_counter() - t0) * 1000)
            c.check(f"live:{case['id']}", False, detail=f"exc={exc} ms={ms}")
            live_rows.append({"id": case["id"], "q": q, "error": str(exc), "ms": ms})
            continue

        ms = int((time.perf_counter() - t0) * 1000)
        results = r.get("results") or []
        used = list(r.get("engines_used") or [])
        eng = (r.get("engine") or "").lower()
        domain_got = r.get("domain")
        combo = r.get("engines_combo") or r.get("engines") or []
        recovery = r.get("recovery")
        blob = _blob_from_results(results, used, eng)

        dom_ok = case.get("domain") is None or domain_got == case["domain"]
        primary = case.get("primary")
        eng_ok = True
        if primary:
            eng_ok = (
                eng == primary
                or primary in used
                or primary in (combo or [])
                or any(
                    primary in str(x.get("source") or x.get("engine") or "").lower()
                    for x in results
                )
            )

        gold = [g.lower() for g in case.get("gold") or []]
        gold_matched = next((g for g in gold if g in blob), "")
        hit = bool(gold_matched) if gold else len(results) >= 1

        pollute_keys = [p.lower() for p in case.get("pollute") or []]
        # 污染：结果 source/engine 命中垂直脏源
        pollute_hit: list[str] = []
        for x in results:
            src = f"{x.get('source') or ''} {x.get('engine') or ''}".lower()
            for p in pollute_keys:
                if p in src and p not in pollute_hit:
                    pollute_hit.append(p)
        for u in used:
            ul = str(u).lower()
            for p in pollute_keys:
                if p == ul and p not in pollute_hit:
                    # 若仅作为 recovery 未进 results 可不算污染；仅 results 源算
                    pass

        # 严格：top results 的 source 不得为 pollute
        pollute_ok = not pollute_hit

        soft = case.get("scenario") == "general" and case.get("lang") in (
            "ja", "ko", "cyrillic",
        )
        overall = dom_ok and eng_ok and hit and pollute_ok and len(results) >= 1
        # general 多语：domain 不强求；eng_ok 默认 true
        if case.get("domain") is None:
            overall = hit and pollute_ok and len(results) >= 1

        detail = (
            f"dom={domain_got} eng={eng} n={len(results)} ms={ms} "
            f"hit={hit} gold={gold_matched!r} pollute={pollute_hit} "
            f"used={used} rec={recovery}"
        )
        c.check(
            f"live:{case['id']}",
            overall,
            detail=detail,
            soft=soft and not pollute_ok is False and not overall and hit is False,
            meta={
                "id": case["id"],
                "q": q,
                "scenario": case.get("scenario"),
                "lang": case.get("lang"),
                "dom_ok": dom_ok,
                "eng_ok": eng_ok,
                "hit": hit,
                "pollute": pollute_hit,
                "domain": domain_got,
                "engine": eng,
                "n": len(results),
                "ms": ms,
                "gold_matched": gold_matched,
                "top1": (results[0].get("title") if results else None),
                "sources": [
                    str(x.get("source") or x.get("engine") or "") for x in results[:5]
                ],
                "recovery": recovery,
            },
        )
        live_rows.append({
            "id": case["id"],
            "q": q,
            "dom_ok": dom_ok,
            "eng_ok": eng_ok,
            "hit": hit,
            "pollute": pollute_hit,
            "domain": domain_got,
            "engine": eng,
            "n": len(results),
            "ms": ms,
            "gold_matched": gold_matched,
            "top1": (results[0].get("title") if results else None),
            "recovery": recovery,
        })

    # live 汇总
    if live_rows:
        n = len(live_rows)
        with_dom = [r for r in live_rows if "dom_ok" in r]
        if with_dom:
            print(
                f"\n  live summary: n={n} "
                f"domain_acc={sum(1 for r in with_dom if r.get('dom_ok'))/len(with_dom):.0%} "
                f"hit={sum(1 for r in with_dom if r.get('hit'))/len(with_dom):.0%} "
                f"pollute_n={sum(1 for r in with_dom if r.get('pollute'))}"
            )
    c._live_rows = live_rows  # type: ignore[attr-defined]


def _summarize(c: Checker) -> dict[str, Any]:
    by_status: dict[str, int] = {"PASS": 0, "FAIL": 0, "WARN": 0}
    by_section: dict[str, dict[str, int]] = {}
    for row in c.rows:
        st = row["status"]
        by_status[st] = by_status.get(st, 0) + 1
        sec = row["name"].split(":")[0]
        by_section.setdefault(sec, {"PASS": 0, "FAIL": 0, "WARN": 0})
        by_section[sec][st] = by_section[sec].get(st, 0) + 1

    route_meta = [
        r.get("meta") for r in c.rows
        if r["name"].startswith("route:") and r.get("meta")
    ]
    scenario_stats: dict[str, dict[str, int]] = {}
    lang_stats: dict[str, dict[str, int]] = {}
    for m in route_meta:
        if not m:
            continue
        sc = m.get("scenario") or "other"
        lg = m.get("lang") or "other"
        # find status from parent
        scenario_stats.setdefault(sc, {"ok": 0, "n": 0})
        lang_stats.setdefault(lg, {"ok": 0, "n": 0})

    for r in c.rows:
        if not r["name"].startswith("route:"):
            continue
        m = r.get("meta") or {}
        sc = m.get("scenario") or "other"
        lg = m.get("lang") or "other"
        scenario_stats.setdefault(sc, {"ok": 0, "n": 0})
        lang_stats.setdefault(lg, {"ok": 0, "n": 0})
        scenario_stats[sc]["n"] += 1
        lang_stats[lg]["n"] += 1
        if r["status"] == "PASS":
            scenario_stats[sc]["ok"] += 1
            lang_stats[lg]["ok"] += 1

    live_rows = getattr(c, "_live_rows", [])
    live_summary = None
    if live_rows:
        valid = [r for r in live_rows if "dom_ok" in r]
        n = len(valid) or 1
        live_summary = {
            "n": len(valid),
            "domain_acc": sum(1 for r in valid if r.get("dom_ok")) / n,
            "engine_acc": sum(1 for r in valid if r.get("eng_ok")) / n,
            "selection_hit": sum(1 for r in valid if r.get("hit")) / n,
            "domain_and_hit": sum(
                1 for r in valid if r.get("dom_ok") and r.get("hit")
            ) / n,
            "pollute_n": sum(1 for r in valid if r.get("pollute")),
        }

    return {
        "ok": c.ok,
        "fail": c.fail,
        "warn": c.warn,
        "by_status": by_status,
        "by_section": by_section,
        "route_by_scenario": scenario_stats,
        "route_by_lang": lang_stats,
        "live_summary": live_summary,
        "live_rows": live_rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="多语言×引擎×场景 矩阵测试")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument(
        "--report",
        default=str(_ROOT / "tests" / "matrix_search_report.json"),
        help="JSON 报告路径",
    )
    ap.add_argument("--json", action="store_true", help="stdout 打印摘要 JSON")
    args = ap.parse_args()
    if not (args.offline or args.live or args.all):
        args.offline = True

    c = Checker()
    if args.offline or args.all:
        run_offline(c)
    if args.live or args.all:
        run_live(c)

    summary = _summarize(c)
    print(f"\n== summary: {c.ok} PASS / {c.fail} FAIL / {c.warn} WARN ==")
    if summary.get("route_by_scenario"):
        print("  route by scenario:")
        for sc, v in sorted(summary["route_by_scenario"].items()):
            print(f"    {sc:12} {v['ok']}/{v['n']}")
    if summary.get("route_by_lang"):
        print("  route by lang:")
        for lg, v in sorted(summary["route_by_lang"].items()):
            print(f"    {lg:12} {v['ok']}/{v['n']}")
    if summary.get("live_summary"):
        ls = summary["live_summary"]
        print(
            f"  live: n={ls['n']} domain_acc={ls['domain_acc']:.0%} "
            f"hit={ls['selection_hit']:.0%} pollute_n={ls['pollute_n']}"
        )

    report = {
        "summary": summary,
        "rows": c.rows,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  report → {report_path}")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    return 1 if c.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
