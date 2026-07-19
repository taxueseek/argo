#!/usr/bin/env python3
"""smart_router.py — local-search 查询智能路由

根据查询特征将请求路由到最优本地引擎组合，覆盖：
- 中文通用 / 中文新闻 / 代码 / 学术 / 参考百科 / 事实问答 / 垂直实体
- 与 unified-search 的 route.py 解耦，local-search 内部使用
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from engine_registry import EngineRegistry, get_registry

logger = logging.getLogger("local_search.smart_router")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler())

# 查询特征正则
_RE_CHINESE = re.compile(r"[一-鿿]")
_RE_ACADEMIC = re.compile(
    r"\b(paper|arxiv|preprint|doi|citation|abstract|journal|conference|"
    r"peer[-\s]?review|research|survey|review|thesis|dissertation)\b|"
    r"(论文|学术|arxiv|预印本|引用|摘要|期刊|会议|综述|研究)", re.I,
)
_RE_CODE = re.compile(
    r"\b(github|stackoverflow|gitlab|npm|pypi|cargo|maven|gradle|package|"
    r"repo|repository|source\s*code|function|class|api|sdk|library|framework|"
    r"python|javascript|typescript|java|go|golang|rust|c\+\+|sql|regex|docker|"
    r"kubernetes|linux|git|error|exception|bug|debug)\b|"
    r"(代码|源码|开源|仓库|函数|类|库|框架|报错|调试|编程|算法|实现)", re.I,
)
_RE_NEWS = re.compile(
    r"\b(news|breaking|latest|today|yesterday|headline|event|development|"
    r"update|report)\b|"
    r"(新闻|最新|热点|时事|突发|报道|消息|进展|动态)", re.I,
)
_RE_REFERENCE = re.compile(
    r"\b(wiki|wikipedia|wiktionary|encyclopedia|definition|meaning|"
    r"biography|history|geography|film|movie|book|novel|author|imdb|goodreads)\b|"
    r"(百科|词典|定义|含义|是什么|是谁|什么时候|在哪里|历史人物|电影|书籍)", re.I,
)
_RE_FACT = re.compile(
    r"\b(what\s+is|who\s+is|when\s+did|where\s+is|how\s+(many|much|old|long)|"
    r"define|meaning\s+of)\b|"
    r"(是什么|是谁|什么时候|在哪里|多少|多少钱|几岁|多大|定义)", re.I,
)
_RE_LOCATION = re.compile(
    r"\b(map|maps|location|address|nearby|distance|route|nominatim|"
    r"openstreetmap|osm)\b|"
    r"(地图|地址|附近|路线|导航|位置)", re.I,
)

# 分类 → 默认引擎优先级映射
CATEGORY_PRIORITY = {
    "academic": ["local_arxiv", "local_semantic_scholar", "local_crossref", "local_pubmed"],
    "code": ["local_github", "local_stackoverflow", "local_gitlab", "local_npm"],
    "news": ["local_bing_news", "local_google_news", "local_duckduckgo_news"],
    "chinese": ["local_baidu", "local_sogou", "local_bing", "local_duckduckgo"],
    "reference": ["local_wikipedia", "local_wiktionary", "local_wikiquote"],
    "vertical": ["local_openstreetmap", "local_imdb", "local_goodreads"],
    "web_general": ["local_bing", "local_duckduckgo", "local_mojeek", "local_startpage"],
}


def extract_features(query: str) -> dict[str, Any]:
    """提取查询特征向量。"""
    total = max(len(query), 1)
    chinese = len(_RE_CHINESE.findall(query))
    return {
        "chinese_ratio": chinese / total,
        "is_chinese": chinese / total > 0.3,
        "is_academic": bool(_RE_ACADEMIC.search(query)),
        "is_code": bool(_RE_CODE.search(query)),
        "is_news": bool(_RE_NEWS.search(query)),
        "is_reference": bool(_RE_REFERENCE.search(query)),
        "is_fact": bool(_RE_FACT.search(query)),
        "is_location": bool(_RE_LOCATION.search(query)),
    }


def route_query(
    query: str,
    registry: EngineRegistry | None = None,
    preferred_engines: list[str] | None = None,
    max_engines: int = 3,
    require_available: bool = True,
) -> dict[str, Any]:
    """根据查询特征选择最优本地引擎组合。

    Returns:
        {
          "engines": [...],
          "reason": "...",
          "features": {...},
          "domain": "...",
        }
    """
    reg = registry or get_registry()
    features = extract_features(query)

    # 用户指定引擎优先
    if preferred_engines:
        engines = [e for e in preferred_engines if reg.get_engine(e)]
        if engines:
            return {
                "engines": engines[:max_engines],
                "reason": f"用户指定: {', '.join(engines[:max_engines])}",
                "features": features,
                "domain": "custom",
            }

    # 特征 → 候选分类
    candidates: list[tuple[str, float]] = []
    if features["is_academic"]:
        candidates.append(("academic", 1.0))
    if features["is_code"]:
        candidates.append(("code", 1.0))
    if features["is_news"]:
        candidates.append(("news", 0.95))
    if features["is_location"]:
        candidates.append(("vertical", 0.9))
    if features["is_reference"]:
        candidates.append(("reference", 0.9))
    if features["is_fact"]:
        # 事实查询优先百科与通用搜索
        candidates.append(("reference", 0.85))
        candidates.append(("web_general", 0.7))
    if features["is_chinese"]:
        candidates.append(("chinese", 0.85))
        # 中文新闻类查询增强
        if features["is_news"]:
            candidates.append(("news", 0.9))

    # 兜底
    if not candidates:
        candidates.append(("web_general", 0.5))

    # 去重并排序
    seen: set[str] = set()
    ordered: list[tuple[str, float]] = []
    for cat, score in sorted(candidates, key=lambda x: -x[1]):
        if cat not in seen:
            seen.add(cat)
            ordered.append((cat, score))

    # 按分类取引擎，再合并去重
    selected: list[str] = []
    reasons: list[str] = []
    for cat, score in ordered:
        for eng in CATEGORY_PRIORITY.get(cat, []):
            if len(selected) >= max_engines:
                break
            spec = reg.get_engine(eng)
            if not spec:
                continue
            if require_available and not spec.get("available", True):
                continue
            if not spec.get("enabled", True):
                continue
            if eng not in selected:
                selected.append(eng)
                reasons.append(f"{eng}({cat})")
        if len(selected) >= max_engines:
            break

    # 兜底：如果都没选到，返回前几个启用的可用引擎
    if not selected:
        selected = reg.list_engines(available_only=require_available, enabled_only=True)[:max_engines]
        reasons = [f"{e}(fallback)" for e in selected]

    domain = ordered[0][0] if ordered else "web_general"

    return {
        "engines": selected,
        "reason": f"特征路由 → {' + '.join(reasons)}",
        "features": features,
        "domain": domain,
    }


def pick_engines(
    query: str,
    registry: EngineRegistry | None = None,
    preferred: list[str] | None = None,
    max_engines: int = 3,
    require_available: bool = True,
) -> list[str]:
    """便捷函数：直接返回引擎名列表。"""
    decision = route_query(query, registry, preferred, max_engines, require_available)
    return decision["engines"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="local-search 智能路由调试")
    parser.add_argument("query", help="搜索查询")
    parser.add_argument("--engine", default="", help="指定引擎，逗号分隔")
    parser.add_argument("--max-engines", type=int, default=3)
    parser.add_argument("--ignore-availability", action="store_true")
    args = parser.parse_args()

    preferred = [e.strip() for e in args.engine.split(",") if e.strip()] if args.engine else None
    decision = route_query(
        args.query,
        preferred_engines=preferred,
        max_engines=args.max_engines,
        require_available=not args.ignore_availability,
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2))
