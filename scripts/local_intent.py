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

# 本地工程/文件意图信号：
# 1. 显式工程信号：代码关键字、扩展名、工程配置文件、中文本地说法
# 2. 隐式工程信号：代码异常类（ZeroDivisionError 等）、内部私有标识符（_foo_bar）、函数调用特征（foo()）
# 与 local_seek._LOCAL_INTENT_PATTERN 同口径，两处必须逐字一致——见下方 test_local_intent_parity。
_LOCAL_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:def|class|function|fn|import|export|const|var|struct|enum)\b|"
    r"\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|md|rs|go|c|cpp|h|hpp|sh|sql|conf|cfg|ini|log)\b|"
    r"(?:readme|config|package\.json|cargo\.toml|dockerfile|makefile|pyproject\.toml|tsconfig\.json|go\.mod|requirements\.txt|Cargo\.lock|\.gitignore|\.env)|"
    r"\b[A-Za-z0-9_]+(?:Error|Exception|Fault|Warning)\b|"
    r"\b_[a-zA-Z0-9_]{2,}\b|"
    r"\b[a-zA-Z0-9_]+\([^\)]*\)|"
    r"(?:本工程|本项目|代码库|本地文件|本地脚本|脚本文件|架构实现|函数|方法|源码|接口定义|"
    # 2026-11-10 P0-1 补：实测「我的 .agents/skills 有哪些」「我的文件」
    # 「仓库里」「本机」这类说法在默认路径下被误判为公网查询，漏搜本机。
    # 补的都是「本机/我的/仓库」语义，不会误伤公网查询（"我的" 单独出现
    # 不算，必须带 文件/笔记/电脑/代码 等宾语）。
    r"本机|本地代码|本地笔记|我的文件|我的笔记|我的电脑|我的代码|"
    r"仓库里|仓库中|工程里|项目里|当前目录|工作目录|\.agents/skills|\.obsidian))"
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


# ── 信号驱动判据（2026-11-10 P0-1）─────────────────────────────────────────
# 为什么需要：默认双轨下，本机轨对纯公网查询不是「空跑」——实测
# 「attention mechanism paper」「红烧肉 做法」等查询本机轨同样返回 3 条命中，
# 经 shape_response 并入总结果（总条数 8 条里 3 条是本地噪声），还白付 100-160ms。
# 原判据只覆盖「公域答案型域」（实测只跳过 36% 的样本），泛查询仍默认双轨。
#
# 新判据把默认翻转为信号驱动：**只有查询明确带本地意图才跑本机轨**。
# 判据失败 fail-open（宁可多搜也不误杀）——见 should_run_local_by_signal。

# 明确的本地意图信号：路径前缀、文件扩展名、工程文件、中文本地说法、
# 本机能力目录（.agents/skills、.obsidian 等）。
# 与 _LOCAL_SIGNAL_RE 的区别：这个正则**只收「强本地意图」**，不收
# `def`/`class` 这类可能只是英文技术词的弱信号（"def" 出现在 "default" 里）。
_STRONG_LOCAL_SIGNAL_RE = re.compile(
    r"(?i)(?:"
    # 路径前缀（最明确）
    r"(?:^|[\s\"'`(])(?:~/|/Users/|/home/|file://|C:\\\\|\./|\.\./)|"
    # 文件扩展名
    r"\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|md|rs|go|c|cpp|h|hpp|sh|"
    r"sql|docx|pptx|xlsx|pdf|csv|log|cfg|ini|conf)\b|"
    # 工程/配置文件名
    r"(?:readme|package\.json|cargo\.toml|dockerfile|makefile|"
    r"\.gitignore|\.env|pyproject\.toml|tsconfig\.json|"
    r"go\.mod|requirements\.txt|Cargo\.lock)|"
    # 本机目录与说法
    r"(?:\.agents/skills|\.obsidian|\.config/argo|本机|本地文件|本地脚本|"
    r"本地代码|我的文件|我的笔记|我的电脑|代码库|本工程|本项目|"
    r"这个仓库|这个工程|当前目录|工作目录|源码里|仓库里)"
    r")"
)


def has_strong_local_signal(query: str) -> bool:
    """查询是否带本地意图信号（复用 _LOCAL_SIGNAL_RE，含词边界保护）。

    为什么复用而不是新写正则：_LOCAL_SIGNAL_RE 已用 `\\b` 词边界处理了
    `def`/`class` 与 `default`/`classic` 的歧义（"default" 不匹配 `def`），
    且覆盖扩展名、工程文件、中文本地说法三类信号。重写一份必然引入漂移。

    实测（2026-11-10）：
      「python asyncio error handling」→ False（纯英文技术问题，跳）
      「def cache_key 怎么实现的」→ True（代码关键词 + 中文，跑）
      「config.yaml 里的 engines 字段」→ True（扩展名，跑）
      「红烧肉 做法」→ False（菜谱，跳）
    """
    if not query:
        return False
    return bool(_STRONG_LOCAL_SIGNAL_RE.search(query))


def should_run_local_by_signal(query: str, domain: str | None = None) -> bool:
    """信号驱动：查询带本地意图信号时才跑本机轨。

    这是默认路径（include_local=None）的主判据。与 should_skip_local 的分工：
      - should_skip_local：公域答案型域的补充跳过（仍保留，覆盖
        「域已确定是天气但查询写法不含强信号」的漏网情形）；
      - should_run_local_by_signal：默认路径的主判据（无域信息时靠它）。

    若命中公域答案型白名单（天气、汇率等）且无强信号，坚决不跑（零公域污染）；
    否则带本地意图信号则跑。
    """
    if domain and domain in _PUBLIC_ONLY_DOMAINS:
        return False
    return has_strong_local_signal(query)
