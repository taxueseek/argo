#!/usr/bin/env python3
"""lang_detect.py — 查询语言检测（统一多语种判定）

覆盖全球主要语系：中文/日文/韩文/拉丁语系（英法德西等）/西里尔（俄语等）/
泰语/阿拉伯/希伯来/希腊/梵文天城体等。纯 stdlib、<1ms。

关键决策：
  - 汉字（CJK Unified）归 zh；但日文假名（\\u3040-\\u30ff）与韩文谚文
    （\\uac00-\\ud7af）是强语言信号，优先于汉字判定 —— 「東京の天気」虽是
    汉字+假名，应判 ja 而非 zh。
  - english_ratio 改为「拉丁字母占比」而非「1 - chinese_ratio」，
    修复日韩查询被误判为英文的老问题。
  - script 与 primary_lang 同源（由 detect_language 映射），避免
    「主语言 zh / script latin」这类混技术词查询上的矛盾。
  - 引擎语言参数（setlang/hl/lang/uselang）表只在此维护一处，
    engines_base / local-search/search_v3 共用。
"""

from __future__ import annotations

import re
from typing import Any

_RE_HAN = re.compile(r"[\u4e00-\u9fff]")
_RE_KANA = re.compile(r"[\u3040-\u30ff]")          # 平假名 + 片假名
_RE_HANGUL = re.compile(r"[\uac00-\ud7af]")        # 谚文音节
_RE_LATIN = re.compile(r"[A-Za-z]")
_RE_CYRILLIC = re.compile(r"[\u0400-\u04ff]")      # 西里尔（俄/乌/保等）
_RE_THAI = re.compile(r"[\u0e00-\u0e7f]")          # 泰文
_RE_ARABIC = re.compile(r"[\u0600-\u06ff]")        # 阿拉伯
_RE_HEBREW = re.compile(r"[\u0590-\u05ff]")        # 希伯来
_RE_GREEK = re.compile(r"[\u0370-\u03ff]")         # 希腊
_RE_DEVANAGARI = re.compile(r"[\u0900-\u097f]")    # 天城体（印地/梵）
_RE_LATIN_EXT = re.compile(r"[\u00c0-\u024f]")     # 拉丁扩展（é ü ñ 等）

LANG_LABELS = {
    "zh": "中文",
    "en": "英文",
    "ja": "日文",
    "ko": "韩文",
    "latin": "拉丁语系",
    "cyrillic": "西里尔语系",
    "thai": "泰语",
    "arabic": "阿拉伯语",
    "hebrew": "希伯来语",
    "greek": "希腊语",
    "devanagari": "印地语系",
    "mixed": "混合",
    "other": "其他",
}

# primary_lang → 书写系统类别（与 detect_language 同源，杜绝双真源）
_LANG_TO_SCRIPT: dict[str, str] = {
    "zh": "cjk",
    "ja": "kana",
    "ko": "hangul",
    "en": "latin",
    "latin": "latin",
    "cyrillic": "cyrillic",
    "thai": "thai",
    "arabic": "arabic",
    "hebrew": "hebrew",
    "greek": "greek",
    "devanagari": "devanagari",
    "mixed": "mixed",
    "other": "other",
}

# 非拉丁主语言：路由/跨语言回退共用
NON_LATIN_LANGS = frozenset({
    "zh", "ja", "ko", "cyrillic", "thai", "arabic",
    "hebrew", "greek", "devanagari",
})

# 引擎语言参数 → 各主语言取值（覆盖 config.yaml 静态值）
# 缺语言键时 engine_lang_param 返回空串，调用方保留静态默认。
ENGINE_LANG_PARAM_VALUES: dict[str, dict[str, str]] = {
    "setlang": {  # Bing
        "ja": "ja-JP", "ko": "ko-KR", "en": "en-US", "latin": "en-US",
        "zh": "zh-Hans", "cyrillic": "ru-RU", "thai": "th-TH",
        "arabic": "ar", "hebrew": "he", "greek": "el", "devanagari": "hi",
        "mixed": "en-US", "other": "en-US",
    },
    "hl": {  # Google
        "ja": "ja", "ko": "ko", "en": "en", "latin": "en",
        "zh": "zh-CN", "cyrillic": "ru", "thai": "th",
        "arabic": "ar", "hebrew": "iw", "greek": "el", "devanagari": "hi",
        "mixed": "en", "other": "en",
    },
    "lang": {  # Yandex
        "ja": "ja", "ko": "ko", "en": "en", "latin": "en", "zh": "zh",
        "cyrillic": "ru", "thai": "th",
        "arabic": "ar", "hebrew": "he", "greek": "el", "devanagari": "hi",
        "mixed": "en", "other": "en",
    },
    "uselang": {  # Wikipedia
        "ja": "ja", "ko": "ko", "en": "en", "latin": "en", "zh": "zh",
        "cyrillic": "ru", "thai": "th",
        "arabic": "ar", "hebrew": "he", "greek": "el", "devanagari": "hi",
        "mixed": "en", "other": "en",
    },
}

_LANG_PARAM_KEYS = frozenset(ENGINE_LANG_PARAM_VALUES.keys())


def detect_language(query: str) -> str:
    """返回主语言标签：zh / en / ja / ko / latin / cyrillic / thai / arabic /
    hebrew / greek / devanagari / mixed / other。"""
    if not query or not query.strip():
        return "other"
    total_chars = len([c for c in query if not c.isspace()])
    if total_chars == 0:
        return "other"
    han = len(_RE_HAN.findall(query))
    kana = len(_RE_KANA.findall(query))
    hangul = len(_RE_HANGUL.findall(query))
    latin = len(_RE_LATIN.findall(query))
    latin_ext = len(_RE_LATIN_EXT.findall(query))
    cyrillic = len(_RE_CYRILLIC.findall(query))
    thai = len(_RE_THAI.findall(query))
    arabic = len(_RE_ARABIC.findall(query))
    hebrew = len(_RE_HEBREW.findall(query))
    greek = len(_RE_GREEK.findall(query))
    devanagari = len(_RE_DEVANAGARI.findall(query))

    # 非拉丁书写系统强信号优先
    if kana / total_chars >= 0.15:
        return "ja"
    if hangul / total_chars >= 0.15:
        return "ko"
    if kana and han and han / total_chars >= 0.3:
        return "ja"  # 汉字 + 少量假名（如「東京の天気」）
    if thai / total_chars >= 0.2:
        return "thai"
    if arabic / total_chars >= 0.2:
        return "arabic"
    if hebrew / total_chars >= 0.2:
        return "hebrew"
    if greek / total_chars >= 0.2:
        return "greek"
    if devanagari / total_chars >= 0.2:
        return "devanagari"
    if cyrillic / total_chars >= 0.2:
        return "cyrillic"
    # 汉字为主判 zh
    if han / total_chars >= 0.3:
        return "zh"
    # 拉丁字母（含扩展字符 é/ü/ñ 等）为主 → 拉丁语系
    # 扩展字符占比 ≥ 0.08 时判 latin（café / español / français 等）
    if (latin + latin_ext) / total_chars >= 0.5:
        if latin_ext / total_chars >= 0.08:
            return "latin"
        return "en" if latin else "latin"
    # 有汉字也有大量拉丁 → 中英混合；其余情况判混合
    if han and latin:
        return "mixed"
    return "other"


def detect_script(query: str) -> str:
    """返回书写系统类别，与 detect_language 同源映射。

    不再独立计分：避免「Python 异步编程」主语言 zh 却 script=latin 的矛盾。
    供路由做 is_latin / 跨语言回退判断。
    """
    return _LANG_TO_SCRIPT.get(detect_language(query), "other")


def is_non_latin_lang(lang: str) -> bool:
    """主语言是否为非拉丁书写系统（需要跨语言回退/语言引擎补充）。"""
    return lang in NON_LATIN_LANGS


def engine_lang_param(param: str, query: str) -> str:
    """按查询主语言返回引擎语言参数取值；无法判定或无映射时返回空串。

    调用方约定：`v = engine_lang_param(k, query) or v`，保留 config 静态默认。
    """
    table = ENGINE_LANG_PARAM_VALUES.get(param)
    if not table:
        return ""
    lang = detect_language(query)
    return table.get(lang, "")


def is_lang_param(key: str) -> bool:
    """extra_params 键是否为可动态覆盖的语言参数。"""
    return key in _LANG_PARAM_KEYS


def language_features(query: str) -> dict[str, Any]:
    """返回与 route.extract_features 兼容的语言特征字段。

    新增 primary_lang / script / is_latin，并保留 chinese_ratio /
    english_ratio 的既有语义（chinese_ratio 仍为汉字占比，english_ratio
    为拉丁占比），兼容现有调用方（route / smart_router 的 is_chinese
    判断不破坏）。
    """
    if not query:
        query = ""
    total = max(len(query), 1)
    chinese = len(_RE_HAN.findall(query))
    latin = len(_RE_LATIN.findall(query)) + len(_RE_LATIN_EXT.findall(query))
    primary_lang = detect_language(query)
    script = _LANG_TO_SCRIPT.get(primary_lang, "other")
    return {
        "primary_lang": primary_lang,
        "script": script,
        "is_latin": primary_lang in ("en", "latin"),
        "chinese_ratio": round(chinese / total, 4),
        "english_ratio": round(latin / total, 4),
        "length": total,
    }
