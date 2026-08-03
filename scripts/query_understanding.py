#!/usr/bin/env python3
"""
query_understanding.py — 查询理解中间层（P0-001）

第一性原理：
  检索前先「读懂」查询，把用户的隐含约束（否定、地域、意图、多意图）
  显式化为结构化信号，供路由 / 检索 / 过滤 / 并行度决策统一消费。

职责（MECE 四个正交子任务）：
  1. 否定解析：除了X / 不想X / 不要X / 排除X / -X / NOT X / without X
     → exclude_terms（供融合后过滤）
  2. 地域解析：附近|本地|同城|周边 触发词 + 城市词典
     → geo（供 route.py 追加 local_openstreetmap）
  3. 意图分类：compare / definition / news / fact / social
     → intents（供 route.py 动态并行度 P0-005）
  4. 多意图拆分：显式并列词（和/与/以及/、）且两段均 ≥2 有效 token
     → multi_intent_splits（最多 2 子查询）

纯本地、零依赖（仅 stdlib），典型延迟 <1ms。

用法：
  from query_understanding import understand
  qu = understand("除了百度的搜索引擎")
  qu.exclude_terms  # ["百度"]
  qu.clean_query    # "的搜索引擎"（去掉否定片段）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class QueryUnderstanding:
    """查询理解结果（结构化信号容器）。"""

    original: str
    clean_query: str
    exclude_terms: list[str] = field(default_factory=list)
    geo: dict[str, Any] | None = None
    intents: list[str] = field(default_factory=list)
    multi_intent_splits: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict。"""
        return asdict(self)


# ── 否定解析 ──────────────────────────────────────────────────────────────────
# 每个模式捕获「被排除的实体」到 group(1)。实体非贪婪，在 的/，/空白/末尾 处收边，
# 避免把 "百度的搜索引擎" 整段吞进 exclude。

# 实体边界：的 / 逗号 / 空白 / 以外之外外 / 末尾
_ENT = r"([\u4e00-\u9fffA-Za-z0-9]{1,20}?)(?=的|[,，、\s]|以外|之外|外|$)"

_NEGATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"除了" + _ENT),
    re.compile(r"不想(?:要|用|看)?" + _ENT),
    re.compile(r"不要" + _ENT),
    re.compile(r"排除" + _ENT),
    re.compile(r"(?<![A-Za-z0-9])-([A-Za-z0-9\u4e00-\u9fff]{1,20})"),
    re.compile(r"\bNOT\s+([A-Za-z0-9\u4e00-\u9fff]{1,20})", re.I),
    re.compile(r"\bwithout\s+([A-Za-z0-9\u4e00-\u9fff]{1,20})", re.I),
]

# 否定片段本身（用于从 clean_query 中剔除触发词 + 实体，保留其后正文）
_NEGATION_SPANS: list[re.Pattern] = [
    re.compile(r"除了[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|以外|之外|外|$)(?:以外|之外|外)?"),
    re.compile(r"不想(?:要|用|看)?[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|$)"),
    re.compile(r"不要[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|$)"),
    re.compile(r"排除[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|$)"),
    re.compile(r"(?<![A-Za-z0-9])-[A-Za-z0-9\u4e00-\u9fff]{1,20}"),
    re.compile(r"\bNOT\s+[A-Za-z0-9\u4e00-\u9fff]{1,20}", re.I),
    re.compile(r"\bwithout\s+[A-Za-z0-9\u4e00-\u9fff]{1,20}", re.I),
]


def parse_negation(query: str) -> tuple[list[str], str]:
    """解析否定约束。

    Returns:
        (exclude_terms, clean_query)：被排除词列表 + 去掉否定片段后的查询。
    """
    exclude: list[str] = []
    for pat in _NEGATION_PATTERNS:
        for m in pat.finditer(query):
            term = m.group(1).strip()
            if term and term not in exclude:
                exclude.append(term)

    clean = query
    for pat in _NEGATION_SPANS:
        clean = pat.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    # 去掉否定片段剔除后残留的前导虚词（如 "的搜索引擎" → "搜索引擎"）
    clean = re.sub(r"^[的了，,、\s]+", "", clean).strip()
    return exclude, clean


# ── 地域解析 ──────────────────────────────────────────────────────────────────

_GEO_TRIGGERS = re.compile(r"(附近|本地|同城|周边)")

# 省级市 + 地级市（前 100，覆盖高频城市）
_CITY_DICT: tuple[str, ...] = (
    # 直辖市 + 省会 + 副省级
    "北京", "上海", "天津", "重庆", "广州", "深圳", "成都", "杭州", "武汉",
    "西安", "南京", "郑州", "长沙", "沈阳", "青岛", "宁波", "东莞", "无锡",
    "昆明", "大连", "厦门", "苏州", "合肥", "佛山", "福州", "哈尔滨", "济南",
    "温州", "长春", "石家庄", "常州", "泉州", "南宁", "贵阳", "南昌", "南通",
    "金华", "徐州", "太原", "嘉兴", "烟台", "惠州", "保定", "台州", "中山",
    "绍兴", "乌鲁木齐", "潍坊", "兰州", "珠海", "扬州", "邯郸", "海口", "洛阳",
    "临沂", "唐山", "汕头", "湖州", "盐城", "泰州", "镇江", "赣州", "廊坊",
    "呼和浩特", "银川", "西宁", "拉萨", "三亚", "威海", "泰安", "淄博", "德州",
    "岳阳", "衡阳", "襄阳", "宜昌", "荆州", "株洲", "湘潭", "常德", "桂林",
    "柳州", "北海", "秦皇岛", "包头", "鞍山", "吉林", "大庆", "connecticut",
    "绵阳", "南充", "宜宾", "遵义", "大理", "丽江", "咸阳", "宝鸡", "榆林",
    "十堰", "九江", "上饶", "抚州", "漳州", "莆田", "龙岩", "宁德",
)
_CITY_SET = frozenset(c for c in _CITY_DICT if any("\u4e00" <= ch <= "\u9fff" for ch in c))


def parse_geo(query: str) -> dict[str, Any] | None:
    """解析地域意图。

    Returns:
        {"has_geo": True, "trigger": str|None, "city": str|None} 或 None（无地域信号）。
    """
    trigger_m = _GEO_TRIGGERS.search(query)
    city = None
    for c in _CITY_SET:
        if c in query:
            city = c
            break
    if not trigger_m and not city:
        return None
    return {
        "has_geo": True,
        "trigger": trigger_m.group(1) if trigger_m else None,
        "city": city,
    }


# ── 意图分类 ──────────────────────────────────────────────────────────────────

_INTENT_PATTERNS: dict[str, re.Pattern] = {
    "compare": re.compile(
        r"\b(vs|versus)\b|(对比|比较|区别|相比|哪个好|哪个更|谁更|优缺点|pk)", re.I),
    "definition": re.compile(
        r"\b(what is|what are|define|definition)\b|(是什么|什么是|定义|含义|概念|指的是)", re.I),
    "news": re.compile(
        r"\b(news|latest|breaking)\b|(最新|新闻|快讯|突发|今天|近期|最近|进展|动态)", re.I),
    "fact": re.compile(
        r"\b(how many|how much|when did|where is|who is)\b|"
        r"(多少|几个|几号|什么时候|哪一年|哪里|谁是|是谁)", re.I),
    "social": re.compile(
        r"(小红书|抖音|推特|twitter|reddit|b站|bilibili|微博|舆情|舆论|"
        r"网友|口碑|种草|拔草|评价|讨论|热议)", re.I),
}


def classify_intents(query: str) -> list[str]:
    """多标签意图分类，按固定优先级返回命中的意图列表。"""
    intents: list[str] = []
    # compare / social / news 优先于 fact / definition（更具体）
    for name in ("compare", "social", "news", "definition", "fact"):
        if _INTENT_PATTERNS[name].search(query):
            intents.append(name)
    return intents


# ── 多意图拆分 ────────────────────────────────────────────────────────────────

_CONJUNCTIONS = re.compile(r"(以及|和|与|、)")
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")


def _effective_tokens(text: str) -> int:
    """统计有效 token 数（中文单字 + 英文单词）。"""
    return len(_TOKEN_RE.findall(text))


def split_multi_intent(query: str) -> list[str]:
    """基于显式并列词拆分多意图查询（最多 2 子查询）。

    条件：存在并列词 且 拆分后两段均 ≥2 有效 token。
    """
    parts = _CONJUNCTIONS.split(query)
    if len(parts) < 3:
        return []
    # split 会保留分隔符：[left, conj, right, conj, right2, ...]
    segments = [p.strip() for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]
    valid = [s for s in segments if _effective_tokens(s) >= 2]
    if len(valid) >= 2:
        return valid[:2]
    return []


# ── 主入口 ────────────────────────────────────────────────────────────────────

def understand(query: str) -> QueryUnderstanding:
    """对查询做完整语义理解，返回结构化信号。

    Args:
        query: 原始查询词。

    Returns:
        QueryUnderstanding：含否定、地域、意图、多意图拆分及总置信度。
    """
    if not isinstance(query, str):
        raise TypeError(f"query 必须为 str，实际 {type(query).__name__}")

    original = query
    exclude_terms, clean_query = parse_negation(query)
    if not clean_query:
        clean_query = original  # 全被否定片段吃掉时回退原查询，避免空检索

    geo = parse_geo(query)
    intents = classify_intents(query)
    multi_splits = split_multi_intent(clean_query)

    # 置信度：命中的信号越多、越明确，置信度越高（上限 0.95）
    conf = 0.0
    if exclude_terms:
        conf += 0.3
    if geo:
        conf += 0.25
    if intents:
        conf += 0.2 + 0.05 * min(len(intents), 2)
    if multi_splits:
        conf += 0.2
    confidence = round(min(conf, 0.95), 2)

    return QueryUnderstanding(
        original=original,
        clean_query=clean_query,
        exclude_terms=exclude_terms,
        geo=geo,
        intents=intents,
        multi_intent_splits=multi_splits,
        confidence=confidence,
    )


# ── 进程内 memoize ────────────────────────────────────────────────────────────
# 同一次搜索中 query_rewriter / route.extract_features / execute_search 会各调
# 一次 understand()，同一查询重复计算纯属浪费。缓存 to_dict() 后再重建
# dataclass，避免不可哈希对象的 lru_cache 限制。query 即 key，上限 256 条
# 覆盖热查询复用，不设长 TTL（纯本地正则，成本 <1ms）。

_cache_dict: dict[str, dict[str, Any]] = {}
_CACHE_MAX = 256


def _understand_cached(query: str) -> QueryUnderstanding:
    """understand 的进程内缓存包装：同一查询只解析一次。"""
    cached = _cache_dict.get(query)
    if cached is not None:
        return QueryUnderstanding(**cached)
    if len(_cache_dict) >= _CACHE_MAX:
        _cache_dict.clear()  # 简单清空，避免复杂淘汰逻辑
    qu = understand(query)
    _cache_dict[query] = qu.to_dict()
    return qu


# ── CLI 测试 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    tests = sys.argv[1:] if len(sys.argv) > 1 else [
        "除了百度的搜索引擎",
        "附近医院",
        "北京附近的川菜馆",
        "Python 和 Rust 哪个好",
        "什么是 Transformer",
        "英伟达最新财报进展",
        "小米 SU7 车主口碑 -广告",
        "React vs Vue without jQuery",
        "上海周边亲子游 以及 露营地推荐",
    ]
    for q in tests:
        qu = understand(q)
        print(json.dumps(qu.to_dict(), ensure_ascii=False, indent=2))
        print("-" * 60)
