#!/usr/bin/env python3
"""extend_route_labels_by_language.py — 按语言能力扩展路由标注集。

## 为什么需要它

既有语料 745 条里中文 504（67.7%）、英文 239（32.1%）、韩文 2（0.3%），
**其余 13 种语言 0 条**。而 config 里 273 个引擎声明了 16 种语言能力
（ja 4 个、ko 2 个、ar/id/cs/es/de/pt/tr/fr/th/ru/vi/he 各 1 个），
`world_news` 域的 patterns 更是直接写了 11 种语言的「新闻」。

语言覆盖的缺失意味着：**非中英查询的路由对不对，没有任何门在守**。

## MECE 拆解：按字符系统分组

语言按「字符系统 + 有无专属引擎」正交拆分，七组互不重叠：

| 组 | 语言 | 字符系统 | 专属引擎 |
|---|---|---|---|
| CJK 扩展 | ja / ko | 假名 / 谚文 | qiita/hatena/cinii/nhk、kor_law/yna |
| 西里尔 | ru | 西里尔 | tass |
| 阿拉伯 | ar | 阿拉伯 | aljazeera |
| 东南亚 | th / vi | 泰文 / 拉丁+变音 | matichon、vnpress |
| 欧洲 | de / fr / es / pt / cs / tr | 拉丁 | faz/lefigaro/elpais/folha/ct24/hurriyet |
| 希伯来 | he | 希伯来 | ynet |
| 拉丁通用 | id | 拉丁 | antara |

**穷尽性**：16 种语言里 en/zh 已在语料里，其余 14 种按上表七组覆盖。
`id`（印尼）虽用拉丁字母，但有专属引擎（antara），单独成组避免被
「拉丁=英文」的直觉漏掉。

## 第一性原理：判据是「该语言有专属能力」

不是「随便抽几条非中英查询」——而是**每种语言都要测它的专属引擎/域能不能
被路由命中**。例：日语测 `world_news` 的 `ニュース` pattern 与 qiita 的
技术查询；俄语测 `world_news` 的 `новости` 与 tass。

## 用法

    python3 scripts/extend_route_labels_by_language.py            # 扩展并写回
    python3 scripts/extend_route_labels_by_language.py --dry-run  # 只统计
    python3 scripts/extend_route_labels_by_language.py --report   # 打印命中/失配
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

LABELS = ROOT / "tests" / "golden" / "route_labeled.jsonl"


def _lang_of(query: str) -> str:
    """判定查询的**文字系统**（复用系统 lang_detect，不自造判据）。

    系统 `detect_language` 返回的是文字系统而非具体语言：ja/ko/zh 各自可辨，
    但拉丁语系（越/德/法/西/葡/捷/土/印尼）统一返回 `latin`。这是**正确的
    抽象**——路由层按文字系统选源（`engine_families.ENGINE_LANGS` 的
    `lang_allows`），不区分具体拉丁语言。

    具体语言由 `_LANGUAGE_GROUPS` 的组声明给出（`langs` 字段），不靠字符判定
    ——靠字符判定会把越南语误判成英文（都是拉丁字母）。
    """
    try:
        from lang_detect import detect_language
        return detect_language(query)
    except Exception:
        return "unknown"


# ── 语言能力组（MECE）────────────────────────────────────────────────────────
# 每组：(组名, [语言...], [(查询, 期望域, 说明), ...])
# 查询构造自该语言**真实会被问到的形式**，且尽量命中该语言的专属 pattern。

_LANGUAGE_GROUPS: list[tuple[str, list[str], list[tuple[str, str, str]]]] = [
    # ── CJK 扩展：日语 ──
    ("ja", ["ja"], [
        # world_news 的 patterns 直接写了 ニュース
        ("ニュース 今日 速報", "world_news", "日语新闻"),
        ("天気 東京 予報", "weather_query", "日语天气（域 patterns 含 天気/予報）"),
        ("Qiita python 記事", "cn_tech_community", "日语技术社区（qiita 引擎）"),
        ("学术论文 博士論文 探す", "scholar_search", "日语学术（cinii 引擎）"),
    ]),
    # ── CJK 扩展：韩语 ──
    ("ko", ["ko"], [
        ("세계 뉴스 today", "world_news", "韩语新闻（域 patterns 含 뉴스）"),
        ("서울 날씨 예보", "weather_query", "韩语天气"),
        ("대한민국 형법 조문", "kor_law", "韩语法律（kor_law 引擎）"),
    ]),
    # ── 西里尔：俄语 ──
    ("ru", ["ru"], [
        ("мировые новости сегодня", "world_news", "俄语新闻（域 patterns 含 новости）"),
        ("погода Москва прогноз", "weather_query", "俄语天气"),
        ("геополитика конфликт санкции", "global_event", "俄语地缘政治"),
    ]),
    # ── 阿拉伯：阿拉伯语 ──
    ("ar", ["ar"], [
        ("أخبار العالم اليوم", "world_news", "阿拉伯语新闻（域 patterns 含 أخبار）"),
        ("الطقس الرياض توقعات", "weather_query", "阿拉伯语天气"),
    ]),
    # ── 东南亚：泰语 / 越南语 ──
    ("sea", ["th", "vi"], [
        ("ข่าวโลก วันนี้", "world_news", "泰语新闻（域 patterns 含 ข่าว）"),
        ("อากาศ กรุงเทพ พยากรณ์", "weather_query", "泰语天气"),
        ("tin thế giới hôm nay", "world_news", "越南语新闻"),
        ("thời tiết Hà Nội dự báo", "weather_query", "越南语天气"),
    ]),
    # ── 欧洲：德/法/西/葡/捷/土 ──
    ("eu", ["de", "fr", "es", "pt", "cs", "tr"], [
        ("Weltnachrichten heute", "world_news", "德语新闻（域 patterns 含 Nachrichten）"),
        ("Wetter Berlin Vorhersage", "weather_query", "德语天气"),
        ("nouvelles du monde aujourd'hui", "world_news", "法语新闻（含 nouvelles/actualités）"),
        ("météo Paris prévisions", "weather_query", "法语天气"),
        ("noticias del mundo hoy", "world_news", "西班牙语新闻（含 noticias）"),
        ("tiempo Madrid pronóstico", "weather_query", "西班牙语天气"),
        ("notícias do mundo hoje", "world_news", "葡萄牙语新闻（含 notícias）"),
        ("previsão do tempo São Paulo", "weather_query", "葡萄牙语天气"),
        ("světové zprávy dnes", "world_news", "捷克语新闻"),
        ("počasí Praha předpověď", "weather_query", "捷克语天气"),
        ("dünya haberleri bugün", "world_news", "土耳其语新闻"),
        ("hava durumu İstanbul tahmin", "weather_query", "土耳其语天气"),
    ]),
    # ── 希伯来：以色列 ──
    ("he", ["he"], [
        ("חדשות העולם היום", "world_news", "希伯来语新闻"),
        ("מזג אוויר תל אביב תחזית", "weather_query", "希伯来语天气"),
    ]),
    # ── 拉丁通用：印尼 ──
    ("id", ["id"], [
        ("berita dunia hari ini", "world_news", "印尼语新闻"),
        ("prakiraan cuaca Jakarta", "weather_query", "印尼语天气"),
    ]),
]


def _guess_latin_lang(query: str, candidates: list[str]) -> str:
    """拉丁语系内的具体语言猜测（只在组内候选里选）。

    判据是各语言的**特征词/特征字符**，不是通用字符集——系统
    `detect_language` 对拉丁语系只返回 `latin`，区分具体语言需要词级判据。
    命中不了时返回 `latin`（诚实标注「未区分」，不硬猜）。
    """
    q = query.lower()
    # 特征词/字符 → 语言（按候选集裁剪，避免把「德」的特征词用在中式英文上）
    markers: list[tuple[str, list[str]]] = [
        ("vi", ["thời tiết", "dự báo", "tin thế giới", "việt"]),
        ("de", ["wetter", "vorhersage", "nachrichten", "weltnachrichten", "temperatur"]),
        ("fr", ["météo", "prévision", "nouvelles", "aujourd'hui", "prévisions"]),
        ("es", ["tiempo", "pronóstico", "noticias", "del mundo", "previsión"]),
        ("pt", ["previsão", "notícias", "do tempo", "do mundo", "são paulo"]),
        ("cs", ["počasí", "předpověď", "světové", "zprávy", "teplota"]),
        ("tr", ["hava durumu", "tahmin", "dünya haberleri", "yağış", "sıcaklık"]),
        ("id", ["prakiraan cuaca", "berita dunia", "hari ini", "jakarta"]),
    ]
    for lang, words in markers:
        if lang in candidates and any(w in q for w in words):
            return lang
    return "latin"


# 组名 → 该组声明的语言清单（供修正既有条目时确定具体语言）
_GROUP_LANGS: dict[str, list[str]] = {
    g: list(langs) for g, langs, _ in _LANGUAGE_GROUPS
}


def build(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """为每种语言生成用例，实测路由后写入（命中的才写）。"""
    from config import get_domains
    from route import route_query
    from route_domains import match_domains

    doms = get_domains()
    dmap = {d["name"]: d for d in doms}
    existing = {e["query"] for e in entries}

    out = list(entries)
    added = 0
    hit_count = 0
    miss_count = 0
    misses: list[str] = []
    langs_covered: set[str] = set()

    for group, langs, cases in _LANGUAGE_GROUPS:
        for query, want_domain, why in cases:
            if query in existing:
                continue
            if want_domain not in dmap:
                continue
            try:
                r = route_query(query, mode="auto", depth="fast")
            except Exception:
                continue
            hit_names = {h.get("name") for h in match_domains(query, doms, max_n=5)}
            if want_domain in hit_names:
                hit_count += 1
                # 具体语言由**组声明**给出，不由字符判定——拉丁语系都是
                # latin，字符判定会把越南语误判成英文。组内有多语言时取
                # 第一个（一条用例只测一种语言）。
                specific_lang = langs[0] if len(langs) == 1 else _guess_latin_lang(query, langs)
                langs_covered.add(specific_lang)
                out.append({
                    "query": query,
                    "expect_domain": want_domain,
                    "expect_family": None,
                    "evidence": "language",
                    "quality": {"errors": False, "recovered": False,
                                "count": 0, "occurrences": 1},
                    "source": f"language:{group}",
                    "lang": specific_lang,
                    "script": _lang_of(query),
                    "intent": None,
                })
                added += 1
            else:
                miss_count += 1
                if len(misses) < 40:
                    misses.append(
                        f"  {query[:34]!r:38} want={want_domain:20} "
                        f"got={r.get('domain'):20} [{why}]")

    # 修正既有 language 条目的 lang/script 字段——它们是上一版用字符判定
    # 写入的（把越南语判成 en、日语判成 zh），必须重算，否则错标签会留在
    # 语料里被当成事实。
    fixed = 0
    for e in out:
        if e.get("evidence") != "language":
            continue
        script = _lang_of(e["query"])
        # 具体语言：优先用组声明的候选集做词级猜测
        group_langs = _GROUP_LANGS.get(
            str(e.get("source", "")).replace("language:", ""), [])
        if len(group_langs) == 1:
            lang = group_langs[0]
        elif script == "latin":
            lang = _guess_latin_lang(e["query"], group_langs) if group_langs else "latin"
        else:
            lang = script
        if e.get("lang") != lang or e.get("script") != script:
            fixed += 1
        e["lang"] = lang
        e["script"] = script

    # 补 intent 标签
    try:
        from extend_route_labels import classify_intent
        for e in out:
            if not e.get("intent"):
                e["intent"] = classify_intent(e["query"], e.get("expect_domain"))
    except Exception:
        pass

    stats = {
        "total": len(out),
        "added": added,
        "hit": hit_count,
        "miss": miss_count,
        "langs_covered": sorted(langs_covered),
        "lang_fields_fixed": fixed,
        "misses": misses,
    }
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="按语言能力扩展路由标注集")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if not LABELS.is_file():
        print(f"[lang-extend] 语料不存在：{LABELS}", file=sys.stderr)
        return 1
    entries = [json.loads(l) for l in
               LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]

    extended, stats = build(entries)
    print(f"[lang-extend] 语料 {len(entries)} -> {stats['total']} 条"
          f"（新增 {stats['added']}，命中 {stats['hit']}，失配 {stats['miss']}）")
    print(f"[lang-extend] 覆盖语言: {stats['langs_covered']}")

    if args.report and stats["misses"]:
        print("失配样例：")
        for m in stats["misses"]:
            print(m)

    if args.dry_run:
        return 0

    with LABELS.open("w", encoding="utf-8") as f:
        for e in extended:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[lang-extend] 已写回 {LABELS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
