#!/usr/bin/env python3
"""
query_understanding.py — 查询理解中间层（P0-001）

第一性原理：
  检索前先「读懂」查询，把用户的隐含约束（否定、地域、意图、多意图）
  显式化为结构化信号，供路由 / 检索 / 过滤 / 并行度决策统一消费。

职责（MECE 四个互不相干子任务）：
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

import copy
import functools
import re
from typing import Any
from cli_io import dumps


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class QueryUnderstanding:
    """查询理解结果（结构化信号容器）。

    用 __slots__ 类而不是 @dataclass：`from dataclasses import ...` 会把
    inspect/dis/ast 链拉进 import（实测 3.9ms，占本模块导入成本近四成），而本
    模块在每次查询里被无条件导入（query_rewriter / route.extract_features /
    execute_search 三个调用点）。字段与 to_dict() 契约保持不变——
    `_understand_cached` 依赖 `QueryUnderstanding(**to_dict())` 重建。
    """

    __slots__ = ("original", "clean_query", "exclude_terms", "geo",
                 "intents", "multi_intent_splits", "confidence")

    def __init__(self, original: str, clean_query: str,
                 exclude_terms: list[str] | None = None,
                 geo: dict[str, Any] | None = None,
                 intents: list[str] | None = None,
                 multi_intent_splits: list[str] | None = None,
                 confidence: float = 0.0):
        self.original = original
        self.clean_query = clean_query
        self.exclude_terms = list(exclude_terms) if exclude_terms else []
        self.geo = geo
        self.intents = list(intents) if intents else []
        self.multi_intent_splits = list(multi_intent_splits) if multi_intent_splits else []
        self.confidence = confidence

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict（与旧 asdict(self) 同形、同样不共享内部容器）。"""
        return {
            "original": self.original,
            "clean_query": self.clean_query,
            "exclude_terms": list(self.exclude_terms),
            "geo": copy.deepcopy(self.geo),
            "intents": list(self.intents),
            "multi_intent_splits": list(self.multi_intent_splits),
            "confidence": self.confidence,
        }

    def __repr__(self) -> str:  # 等价于 dataclass 的默认 repr，便于日志排查
        return (f"QueryUnderstanding(original={self.original!r}, "
                f"clean_query={self.clean_query!r}, "
                f"exclude_terms={self.exclude_terms!r}, geo={self.geo!r}, "
                f"intents={self.intents!r}, "
                f"multi_intent_splits={self.multi_intent_splits!r}, "
                f"confidence={self.confidence!r})")


# ── 否定解析 ──────────────────────────────────────────────────────────────────
# 每个模式捕获「被排除的实体」到 group(1)。实体非贪婪，在 的/，/空白/末尾 处收边，
# 避免把 "百度的搜索引擎" 整段吞进 exclude。

# 实体边界：的 / 逗号 / 空白 / 以外之外外 / 末尾
_ENT = r"([\u4e00-\u9fffA-Za-z0-9]{1,20}?)(?=的|[,，、\s]|以外|之外|外|$)"

# 否定模式的**单一来源**：一行 = 一条否定 = (提取正则, 剔除正则, 旗标, 触发必要条件)。
# 新增第 8 条否定只改这里一行，正则与触发条件不会再漂移（此前是两张 SRC 表 +
# 一份 _NEGATION_LITERALS 三处手工副本，加一条要散改三处）。
#
# 这 14 条正则**不在模块级编译**：它们含 [\u4e00-\u9fffA-Za-z0-9] 这类两万字符的
# 字符集，CPython 编译每条要在 _optimize_charset 上建一次位图，14 条合计实测
# 5.5 ms——而 import query_understanding 是**无条件**发生的（rewrite_query /
# route.extract_features / execute_search 都会拉它），纯缓存命中的那一档也要付。
# 绝大多数查询根本不含否定片段，为它们付这 5.5 ms 是纯固定税。
# 触发条件是**必要条件**（宁可多答 True 走慢路径，也不能漏答让否定实体污染
# 消歧信号与检索串）；"-" 是哨兵，表示按位置判定（见 _has_negation_trigger）。
_NEGATION_SPECS: list[tuple[str, str, int, tuple[str, ...]]] = [
    (r"除了" + _ENT,
     r"除了[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|以外|之外|外|$)(?:以外|之外|外)?",
     0, ("除了",)),
    (r"不想(?:要|用|看)?" + _ENT,
     r"不想(?:要|用|看)?[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|$)",
     0, ("不想",)),
    (r"不要" + _ENT,
     r"不要[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|$)",
     0, ("不要",)),
    (r"排除" + _ENT,
     r"排除[\u4e00-\u9fffA-Za-z0-9]{1,20}?(?=的|[,，、\s]|$)",
     0, ("排除",)),
    (r"(?<![A-Za-z0-9])-([A-Za-z0-9\u4e00-\u9fff]{1,20})",
     r"(?<![A-Za-z0-9])-[A-Za-z0-9\u4e00-\u9fff]{1,20}",
     0, ("-",)),
    (r"\bNOT\s+([A-Za-z0-9\u4e00-\u9fff]{1,20})",
     r"\bNOT\s+[A-Za-z0-9\u4e00-\u9fff]{1,20}",
     re.I, ("not",)),
    (r"\bwithout\s+([A-Za-z0-9\u4e00-\u9fff]{1,20})",
     r"\bwithout\s+[A-Za-z0-9\u4e00-\u9fff]{1,20}",
     re.I, ("without",)),
]


@functools.lru_cache(maxsize=1)
def _negation_patterns() -> tuple[list[re.Pattern], list[re.Pattern]]:
    """首次真正需要否定解析时才编译这些正则（进程内只编一次）。

    触发条件（第四列）在模块级是纯数据，不触发编译，所以「不含否定片段的
    查询」这一档可以完全不付编译成本。
    """
    return (
        [re.compile(extract, flags) for extract, _span, flags, _trig in _NEGATION_SPECS],
        [re.compile(span, flags) for _extract, span, flags, _trig in _NEGATION_SPECS],
    )


_RE_COLLAPSE_WS = re.compile(r"\s+")
_RE_LEAD_PARTICLES = re.compile(r"^[的了，,、\s]+")


def _has_negation_trigger(query: str) -> bool:
    """否定解析的廉价前置条件：**必要条件**，不是判据。

    只回答「有没有可能命中」，答 True 就走完整解析。宁可多答 True（多编译一次
    正则），也不能漏答——漏答会让否定实体重新污染消歧信号与检索串。

    触发条件与正则同表（_NEGATION_SPECS 第四列），所以两者不会漂移；覆盖关系：
      - 除了 / 不想 / 不要 / 排除：正则要求字面量本身，直接子串判定；
      - NOT / without：正则带 re.I，故按小写子串判定；
      - `-`（哨兵）：正则要求 (?<![A-Za-z0-9])，即连字符前一位不是 ASCII 字母数字。
        逐个位置判而不是「含 - 就走慢路径」——GPT-4o / 2026-09-19 / SWE-bench
        这类查询的连字符前是字母数字，本来就不构成否定，不该因此丢掉快路径。
    """
    lowered = query.lower()
    for _extract, _span, _flags, triggers in _NEGATION_SPECS:
        for trigger in triggers:
            if trigger != "-":
                if trigger in lowered:
                    return True
                continue
            idx = query.find("-")
            while idx != -1:
                if idx == 0 or not (query[idx - 1].isascii()
                                    and query[idx - 1].isalnum()):
                    return True
                idx = query.find("-", idx + 1)
    return False


def _normalize_clean(text: str) -> str:
    """无否定片段时的 clean_query——与走完整 sub 链后完全同路。

    两条 sub 在零命中时是恒等操作，所以「跳过解析」与「解析但没命中」必须给出
    同一个 clean_query，否则跳过就成了行为变更（clean_query 会喂给消歧）。
    """
    return _RE_LEAD_PARTICLES.sub("", _RE_COLLAPSE_WS.sub(" ", text).strip()).strip()


def parse_negation(query: str) -> tuple[list[str], str]:
    """解析否定约束。

    Returns:
        (exclude_terms, clean_query)：被排除词列表 + 去掉否定片段后的查询。
    """
    if not _has_negation_trigger(query):
        return [], _normalize_clean(query)

    patterns, spans = _negation_patterns()
    exclude: list[str] = []
    for pat in patterns:
        for m in pat.finditer(query):
            term = m.group(1).strip()
            if term and term not in exclude:
                exclude.append(term)

    clean = query
    for pat in spans:
        clean = pat.sub(" ", clean)
    return exclude, _normalize_clean(clean)


# ── 地域解析 ──────────────────────────────────────────────────────────────────

# 本地生活 + 地理/地点实体（LoHo Geography & Places 保持一致）
_GEO_TRIGGERS = re.compile(
    r"(附近|本地|同城|周边|"
    r"在哪里|在哪儿|在哪|位于|坐落|坐标|经纬度|海拔|发源地|地标|名胜|景点|"
    r"流经|途经|贯穿|横跨|流域|水系|汇入|注入|源头|入海|省份|省区|"
    r"山脉|河流|湖泊|岛屿|沙漠|高原|峡谷|海峡|首都|省会|"
    r"where\s+is|located\s+in|capital\s+of|latitude|longitude|coordinates?|"
    r"flows?\s+through|passes\s+through|drainage|basin|"
    r"openstreetmap|nominatim|geonames)",
    re.I,
)

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
# 一次 understand()，同一查询重复计算纯属浪费。query 即 key，用 lru_cache 做
# LRU 淘汰（上限 256 条，热查询复用），比满则清空更平滑。缓存 to_dict() 后再
# 重建 dataclass，避免不可哈希对象的 lru_cache 限制；纯本地正则，不设 TTL。

@functools.lru_cache(maxsize=256)
def _understand_cached_dict(query: str) -> dict[str, Any]:
    """understand(query).to_dict() 的 lru_cache 包装（key 为原始查询词）。"""
    return understand(query).to_dict()


def _understand_cached(query: str) -> QueryUnderstanding:
    """understand 的进程内缓存包装：同一查询只解析一次。"""
    return QueryUnderstanding(**_understand_cached_dict(query))


# ── CLI 测试 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
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
        print(dumps(qu.to_dict()))
        print("-" * 60)
