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

# primary_lang → 书写系统类别（与 detect_language 同源，杜绝双来源）
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
    "mkt": {  # Bing 市场码（比 setlang 更强制结果区域，修复 ja/ko 返回中文站问题）
        "ja": "ja-JP", "ko": "ko-KR", "en": "en-US", "latin": "en-US",
        "zh": "zh-CN", "cyrillic": "ru-RU", "thai": "th-TH",
        "arabic": "ar-SA", "hebrew": "he-IL", "greek": "el-GR", "devanagari": "hi-IN",
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
    "region": {  # ddgs -r region（{country}-{lang} 形态）
        "ja": "jp-ja", "ko": "kr-ko", "en": "us-en", "latin": "us-en",
        "zh": "cn-zh", "cyrillic": "ru-ru", "thai": "th-th",
        "arabic": "sa-ar", "hebrew": "il-he", "greek": "gr-el", "devanagari": "in-hi",
        "mixed": "us-en", "other": "us-en",
    },
}

_LANG_PARAM_KEYS = frozenset(ENGINE_LANG_PARAM_VALUES.keys())

# 纯汉字日文特征词：简体/繁体中文查询中均不出现（或极罕见）的日语词汇。
# 用于区分「无假名日文查询」与「中文查询」，避免被 han/total 分支误判为 zh。
_JA_KANJI_ONLY_SIGNALS = (
    "非同期", "処理", "予報", "検索", "天気", "株式", "映画", "配信",
    "報道", "速報", "転職", "面接", "給料", "応募", "卒業", "受験",
    "大学院", "授業", "講義", "宿題", "就活", "新卒", "口座", "振込",
    "残高", "通帳", "介護", "処方", "発熱", "予定", "営業", "退職",
    "支払", "申告", "円高", "円安", "株価", "参議院", "衆議院", "住民票",
    "印鑑", "免許", "交番", "弁護士", "捜査", "容疑者", "津波", "断水",
    "変電", "給湯", "残業", "有給", "退院", "受診", "処方箋", "薬局",
    "予約", "出張", "帰省", "新幹線", "改札", "切符", "乗車", "納期",
    "発注", "受注", "出荷", "納品", "売上", "仕入", "原価", "在庫",
    "領収書", "請求書", "見積",
    # AI / 技术高频（实测缺口：查询「人工知能 最新 動向」曾因不在表中被判成 zh）
    "人工知能", "機械学習", "深層学習", "情報処理", "開発環境", "実行",
    "変数", "関数", "引数", "配列", "例外", "継承", "抽象", "再帰",
)

# 日文新字体中「既不同于简体、也不同于繁体」的独有字形。
#
# 判定依据（逐个核对字形三态）：
#   観(ja) / 观(zh简) / 觀(zh繁)   → 仅日文用「観」
#   経(ja) / 经(zh简) / 經(zh繁)   → 仅日文用「経」
#   鉄(ja) / 铁(zh简) / 鐵(zh繁)   → 仅日文用「鉄」
# 这类字形出现即**可以确定**是日文，不会误伤简体或繁体中文。
#
# 反面教训（为什么不用「動/報/學」这类字形）：它们是日文新字体，
# 但**同时是繁体中文用字**（繁中亦作「動」「報」「學」），拿来判日文
# 会把繁体中文误判成日文。故只用「三态互异」的严格子集。
#
# 已知局限（本函数无法覆盖）：纯粹由中日共用汉字组成的短查询，
# 如「人工知能 最新 動向」——它不含任何日文独有字形，字符层面与中文
# 无从区分，只能靠上面的词汇表命中（该例已通过补词解决）。若两者
# 都不命中，会落到 zh 分支。这是一个**已知且有界的**限制，不做硬猜。
_JA_SHINJITAI_ONLY = frozenset(
    "亀亜伝価児円処剣剤単厳営団囲図圧塩壊売変奨実寛対専帯帰庁広弾徴応悪戦戸択拠挙挟摂斉斎桟検楽権歓歯気沢浄済渋滝犠獣産畳発糸経継続総縁縄縦繊聡聴臓芸荘薬蛍覚観証読譲賛転軽辺逓遅郷醸鉄鉱鋳関闘険雑顕駅騒験"
)


def _has_ja_only_kanji(text: str) -> bool:
    """文本是否含「仅日文使用」的新字体字形（三态互异）。"""
    return any(c in _JA_SHINJITAI_ONLY for c in text)


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
    # 纯汉字日文预检（两道，互为补充）：
    #   ① 日文独有字形（三态互异，如 観/経/鉄）——出现即可确定是日文，
    #      不会误伤简体或繁体中文，可靠性最高。
    #   ② 日语特征词表——覆盖不含独有字形的常见日文表达。
    #
    # 已知且**有界**的残余局限：纯粹由中日共用汉字组成的短查询
    # （如「人工知能 最新 動向」——「動」繁体中文亦用，不能作判据）
    # 在字符层面与中文无从区分。此类查询若两道路径都不命中，会落到 zh。
    # 不做硬猜：宁可保持现状（由调用方的语言覆盖/多语言回退保底），
    # 也不引入会把简体中文误判成日文的正则。
    if han and (_has_ja_only_kanji(query)
                or any(w in query for w in _JA_KANJI_ONLY_SIGNALS)):
        return "ja"
    # 汉字为主判 zh
    if han / total_chars >= 0.3:
        return "zh"
    # 汉字与拉丁并存的中文技术查询（「Transformer 模型 原理」「OpenAI o3 评测」
    # 「Python asyncio 教程」）判 mixed 而不是 en。
    #
    # 为什么这条放在拉丁判定**之前**：这些查询里拉丁词是技术专名（模型名、
    # 库名、产品名），汉字才是用户真正在问的内容。判 en 会让
    # route_domains 的中文域语言门把所有中文域整片跳过——实测「Transformer
    # 模型 原理」命中 chinese_tech_deep/chinese_general 后又被门清空，
    # 掉进 TF-IDF 分支落到 arxiv（返回 NLP 综述，而用户要的是原理）。
    # 判 mixed 与 en 都不在 _ZH_LANG_GATED_DOMAINS 的跳过条件里
    # （`primary_lang not in ("zh","mixed","other")`），中文域正常参与。
    if han and latin:
        return "mixed"
    # 拉丁字母（含扩展字符 é/ü/ñ 等）为主 → 拉丁语系
    # 扩展字符占比 ≥ 0.08 时判 latin（café / español / français 等）
    if (latin + latin_ext) / total_chars >= 0.5:
        if latin_ext / total_chars >= 0.08:
            return "latin"
        return "en" if latin else "latin"
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


def engine_lang_param(param: str, query: str, preferred_lang: str = "") -> str:
    """按查询主语言返回引擎语言参数取值；无法判定或无映射时返回空串。

    调用方约定：`v = engine_lang_param(k, query) or v`，保留 config 静态默认。

    preferred_lang：弱信号查询（mixed/other）时的回落语种，来自
    lang_pref.effective_engine_lang（习惯/系统/中英基线）。强查询信号始终优先。
    """
    table = ENGINE_LANG_PARAM_VALUES.get(param)
    if not table:
        return ""
    lang = detect_language(query)
    # 弱信号才采用偏好语；zh/ja/ko/非拉丁/en/latin 以查询为准
    if preferred_lang and lang in ("mixed", "other"):
        if preferred_lang in table:
            return table[preferred_lang]
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
