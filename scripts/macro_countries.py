#!/usr/bin/env python3
"""macro_countries.py — 宏观域的「国家词」判定与国名/代码表。

## 为什么单独成模块

这张表 + 两个纯文本谓词原先住在 `engines_builders_data_macro.py` 里，而那个
模块会连带来整条 HTTP 栈（`engines_base` → `http_client` → urllib/ssl/cookiejar）。
`route.py` 为了调用 `is_foreign_macro_query` 就得在模块顶层 import 它——实测
**36 ms**，占 `import route` 的绝大部分（路由总耗时约 42 ms）。

路由是每次调用的必经路径（连缓存命中也要走一遍），而 HTTP 栈只有真正打网时
才需要。把纯逻辑与网络栈拆开，路由路径就再也不付这笔钱；顺带也不用为了省它
而引入「首次调用才 import」的惰性包装（那会带来隐藏状态与首调用毛刺）。

依赖只有 `re`。本模块必须保持零重依赖——这是它能被路由层直接引用的前提。
"""

from __future__ import annotations

import re

__all__ = ["WORLDBANK_COUNTRIES", "match_country", "is_foreign_macro_query"]

# 国家名/别名 → ISO 代码。worldbank 引擎解析国家、fred 引擎国家词守卫、
# route 层分流的唯一来源（此前注释里写着「共用」，但三处都在同一个模块里，
# 拆出来之后这句话才真正成立）。
WORLDBANK_COUNTRIES: dict[str, str] = {
    "中国": "CHN", "china": "CHN", "美国": "USA", "usa": "USA", "us": "USA",
    "united states": "USA", "america": "USA", "日本": "JPN", "japan": "JPN",
    "德国": "DEU", "germany": "DEU", "英国": "GBR", "uk": "GBR",
    "united kingdom": "GBR", "britain": "GBR", "法国": "FRA", "france": "FRA",
    "印度": "IND", "india": "IND", "巴西": "BRA", "brazil": "BRA",
    "俄罗斯": "RUS", "russia": "RUS", "韩国": "KOR", "korea": "KOR",
    "south korea": "KOR", "加拿大": "CAN", "canada": "CAN",
    "澳大利亚": "AUS", "australia": "AUS", "意大利": "ITA", "italy": "ITA",
    "西班牙": "ESP", "spain": "ESP", "世界": "WLD", "world": "WLD",
    "global": "WLD", "欧元区": "EMU", "eurozone": "EMU", "香港": "HKG",
    "hong kong": "HKG", "台湾": "TWN", "taiwan": "TWN", "新加坡": "SGP",
    "singapore": "SGP",
}


def match_country(query: str, mapping: dict[str, str]) -> str:
    """大小写不敏感国家匹配（中文原名 + 英文别名）。

    短别名（us/uk/eu 等 ≤3 字符）用词边界匹配，避免 'focus'/'consensus'
    之类包含子串的普通词误判为国家。
    """
    low = query.lower()
    for name, code in mapping.items():
        # 中文/长英文名直接子串匹配；短别名（≤3 字符）必须走词边界分支
        if name in query and (not name.isascii() or len(name) > 3):
            return code
        if name.isascii():
            if len(name) <= 3:
                if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
                    return code
            elif name in low:
                return code
    return ""


# 非美国国家/地区子集：FRED 是美国计算方式，只有「明确指向别国」时才让位。
# 从主表派生而非另抄一份，避免两张表漂移。
_NON_US_COUNTRIES: dict[str, str] = {
    name: code for name, code in WORLDBANK_COUNTRIES.items() if code != "USA"
}

# 美国子集：给「统计局/国内」这类中国语境别名做前置排除。
# 同一张主表派生，避免「美国 统计局」被当成中国口径。
_US_COUNTRIES: dict[str, str] = {
    name: code for name, code in WORLDBANK_COUNTRIES.items() if code == "USA"
}


def is_foreign_macro_query(query: str) -> bool:
    """查询是否明确指向非美国国家/地区。

    FRED 序列均为美国或全球计算方式，遇到「中国GDP」「日本通胀」这类查询时
    应放弃响应（返回 True），由 worldbank 按国家参数接管，避免美国数据冒充。

    判定复用 `match_country` 的词边界逻辑，而不是自己写一遍朴素子串匹配。
    此前这里是 `name in query`：`us` 这类 ≤3 字符别名会命中任何含该子串的
    英文词——实测 `focus on growth`、`consensus 预测`、`business survey`
    全被判成「外国宏观查询」，FRED 因而被无谓地排除。同一个概念（国家词识别）
    在同一模块里有两套行为不同的实现，正是这类漏判的来源。

    补充（2026-10-23）：「国家统计局」「国内」「我国」是明确的中国语境，
    但不在国家名表里。实测「国家统计局 一季度 GDP」因此没让位，FRED 领队
    返回美国 GDP。这几个词只指向中国，直接判为 foreign（非美国）。
    但「美国 统计局」「美国国内 CPI」同时含美国词，必须先排除美国口径。
    """
    q = query or ""
    if (not match_country(q, _US_COUNTRIES)
            and any(term in q for term in ("国家统计局", "统计局", "国内", "我国"))):
        return True
    return bool(match_country(query, _NON_US_COUNTRIES))
