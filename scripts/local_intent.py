#!/usr/bin/env python3
"""local_intent.py — 查询是否值得检索本机文件的单点判据。

为什么独立成模块（奥卡姆剃刀）：判据只有两条，但被两处消费——
提交前的「跳过判定」（search.py）与提交后的「是否记账」（同处）。
塞进任一处都会让另一处变成「读了对方代码的隐式耦合」，独立成模块后
两处共用同一份口径，改判据只改这里。

判据（两条都满足才跳过，缺一搜）：
  1. 查询命中明确的公域域（天气/行情/汇率/宏观/新闻等答案型域）；
  2. 查询不含本地工程/文件意图信号。

反向保护：纯英文泛词查询（"latest news"）不命中域时**不跳过**，
宁可多搜一次本地也不误杀；只有「明确公域 + 无本地信号」才跳。
"""

from __future__ import annotations

import re

# 本地工程/文件意图信号：代码词、扩展名、工程文件、中文本地说法。
# 与 local_seek._LOCAL_INTENT_PATTERN 同口径，但**不 import 它**：
# local_seek 会拉起 seek_locator/缓存/执行器等一整条链，而这里只需要
# 一个正则。两处判据必须一致——见下方 test_local_intent_parity。
_LOCAL_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:def|class|function|fn|import|export|const|var|struct|enum)\b|"
    r"\.(?:py|js|ts|json|yaml|yml|md|rs|go|c|cpp|h|sh|sql)\b|"
    r"(?:readme|config|package\.json|cargo\.toml|dockerfile|makefile)|"
    r"(?:本工程|本项目|代码库|本地文件|本地脚本|脚本文件|架构实现|函数|方法|源码|接口定义))"
)

# 公域答案型域：这些域的答案只可能来自公网（天气/行情/汇率/宏观/
# 新闻/法律/学术），本机文件命中对它们是纯噪声。
# 名单与 config.yaml 的 domains 段对齐，但**只收「答案型」域**——
# skill_search/wechat_search 这类「本机也可能有」的域不在内。
_PUBLIC_ONLY_DOMAINS = frozenset({
    "weather_query", "aviation_weather",
    "stock_query", "us_stock", "hk_filings", "sec_filings",
    "fund_query", "us_fundamentals", "futures_market",
    "fx_query", "macro_data", "crypto_search",
    "financial_news", "cls_telegraph_search", "em_news_search",
    "jin10_flash", "zhihu_hot_list", "hot_trending",
    "thsl_hot_search", "ths_hot_search",
    "law_text", "legal", "us_legal", "japan_law", "kor_law",
    "wenshu_query", "patent_search",
    "scholar_search", "medical", "outbreak_health",
    "astro_space", "earth_science", "energy_grid", "transport_rt",
    "trade_stats", "vehicle_data", "soil_agri", "standards",
    "dataset_search", "chem_search", "species_search", "protein_search",
    "rfc_search", "package_intel", "package_search",
})


def has_local_signal(query: str) -> bool:
    """查询是否带有本地工程/文件意图信号。"""
    if not query:
        return False
    return bool(_LOCAL_SIGNAL_RE.search(query))


def should_skip_local(domain: str | None, query: str) -> bool:
    """是否跳过本机检索。

    跳过条件（两条都满足）：
      - domain 命中公域答案型名单；
      - query 不含本地工程/文件意图信号。

    显式 `--include-local` / MCP include_local=True 不受此判定影响
    （调用方已经点名要搜，判据不该推翻用户意图）。
    """
    if not domain or domain not in _PUBLIC_ONLY_DOMAINS:
        return False
    return not has_local_signal(query)
