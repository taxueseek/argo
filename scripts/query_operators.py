#!/usr/bin/env python3
"""query_operators.py — 查询级操作符契约层（2026-10-17，借鉴 DeepSeek Search）。

## 解决什么问题

Google 风格查询里的操作符（site:/-site:/-词/-"短语"/lang:/after:/before:）
此前散落四处：引号短语走 phrase_filter、-词走 query_understanding 的否定、
after/before 只认 CLI flag、lang 靠自动检测、site: 更是只以「域正则触发词」
的形态存在——结果是一条 `DeepSeek site:github.com` 把路由锁死进 code_search，
而引擎侧拿到的还是含字面 site: 的脏查询（2026-10-17 实测：github 引擎返回
GitHub Pages 通用页）。同一能力五种形态、没有一处是契约。

## 本层的契约（对照 DeepSeek Search 的透传语义）

Argo 侧选择**解析而非透传**：DeepSeek 把操作符原样甩给上游搜索服务，Argo 的
上游是 260 个异构引擎（多数不认识这些语法），原样透传=把字面 site: 喂给
不认识它的引擎。故本层抽取为结构化 operators，路由/缓存/查询理解只认
clean_query（剥掉指令后的意图核心），operators 走三个既有消费点：

  - langs     → route 的语言偏好覆盖（替代自动检测的选择）
  - after/before → 时间窗归一化（--since/--until 的同一判据；CLI flag 优先）
  - exclude_terms → query_understanding 的排除项汇合（融合后过滤）
  - sites/exclude_sites/phrases → v1 只记录可观测（results 的 limitations 提示
    未下推），不污染检索词；按引擎能力下推是后续增量（extra_params 的查询级
    覆盖机制已就位）

## 语法（与 DeepSeek 对齐的子集）

  site:github.com / -site:x.com   站点限定/排除（多个空格分隔按 OR 语义记录）
  -词 / -"短语"                   排除关键词/短语
  "短语"                          精确短语（保留在 clean_query——它是意图
                                  本体不是指令，phrase_filter 照常提取）
  lang:zh,en                      语言限定（逗号分隔）
  after:2026 / after:2026-01-01   发布时间下界
  before:2026 / before:2026-06-01 发布时间上界

纯函数、零依赖、无全局状态。用法：
  from query_operators import parse_query_operators
  clean, ops = parse_query_operators('DeepSeek site:github.com lang:zh after:2026')
  # clean == 'DeepSeek'；ops['sites'] == ['github.com']；ops['langs'] == ['zh']
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

# site: 的值：域名形态（字母数字点连字符），到空白/引号/结尾收边
# site: 值；尾部 OR 连接词仅在「后面还跟 site:」时消费（DeepSeek 文档的
# 多站点 OR 语义），否则查询里的裸 OR 原样保留。
_SITE_RE = re.compile(r"(?<![\w-])(-?)site:([a-zA-Z0-9.\-]+)(?:\s+OR(?=\s+site:))?")
# lang: 的值：逗号分隔的语言代码
_LANG_RE = re.compile(r"(?<![\w-])lang:([a-zA-Z,]+)")
# after:/before: 的值：年份或 YYYY-MM-DD
_DATE_RE = re.compile(r"(?<![\w-])(after|before):(\d{4}(?:-\d{2}-\d{2})?)")
# country:/region: 的值：ISO-2 代码或单/双词英文国名（如 country:de / region:united states）
_COUNTRY_RE = re.compile(r"(?<![\w-])(?:country|region)[:=]([A-Za-z][A-Za-z\- ]{0,32})")
# 排除短语：-"..."（先于单词排除处理）
_EXCL_PHRASE_RE = re.compile(r'-\s*"([^"]+)"')
# 排除单词：-词（字母数字或 CJK 串；连字符后紧跟非空白非连字符）
_EXCL_TERM_RE = re.compile(r"(?<![\w\"])-(\S+)")

# 常用 ISO 3166-1 alpha-2 代码与名称映射（零外部依赖，覆盖主流区域检索）
_COUNTRY_ALIASES: dict[str, str] = {
    "us": "US", "usa": "US", "united states": "US", "america": "US",
    "cn": "CN", "china": "CN",
    "gb": "GB", "uk": "GB", "united kingdom": "GB", "britain": "GB",
    "de": "DE", "germany": "DE",
    "fr": "FR", "france": "FR",
    "jp": "JP", "japan": "JP",
    "kr": "KR", "south korea": "KR", "korea": "KR",
    "sg": "SG", "singapore": "SG",
    "ca": "CA", "canada": "CA",
    "au": "AU", "australia": "AU",
    "in": "IN", "india": "IN",
    "hk": "HK", "hong kong": "HK",
    "tw": "TW", "taiwan": "TW",
    "nl": "NL", "netherlands": "NL",
    "ch": "CH", "switzerland": "CH",
    "se": "SE", "sweden": "SE",
    "es": "ES", "spain": "ES",
    "it": "IT", "italy": "IT",
    "br": "BR", "brazil": "BR",
    "ru": "RU", "russia": "RU",
}
_TAVILY_COUNTRY_NAME: dict[str, str] = {
    "US": "united states", "CN": "china", "GB": "united kingdom",
    "DE": "germany", "FR": "france", "JP": "japan", "KR": "south korea",
    "SG": "singapore", "CA": "canada", "AU": "australia", "IN": "india",
    "HK": "hong kong", "TW": "taiwan", "NL": "netherlands",
    "CH": "switzerland", "SE": "sweden", "ES": "spain", "IT": "italy",
    "BR": "brazil", "RU": "russia",
}
_CODE_TOPIC_RE = re.compile(
    r"\b(github|gitlab|npm|pnpm|cargo|pip|pypi|stackoverflow|python|rust|"
    r"typescript|javascript|golang|react|vue|docker|kubernetes|k8s|linux|"
    r"api|sdk|graphql|grpc|bug|error|traceback|compiler)\b"
    r"|(?:代码|函数|接口|报错|崩溃|源码|组件|版本号|仓库|异常|编译)",
    re.I,
)
_RESEARCH_TOPIC_RE = re.compile(
    r"\b(arxiv|paper|research|benchmark|ablation|dataset|neurips|icml|iclr|cvpr|acl|ieee)\b"
    r"|(?:论文|学术|评测|基准|消融实验|数据集|研报|文献)",
    re.I,
)
_FINANCE_TOPIC_RE = re.compile(
    r"\b(stock|shares|nasdaq|nyse|sec|10-k|10-q|earnings|revenue|market cap|valuation|ipo|dividend|ebitda)\b"
    r"|(?:财报|股价|营收|市值|估值|财季|分红|股票|证券|上市|净利润)",
    re.I,
)
_NEWS_TOPIC_RE = re.compile(
    r"\b(news|breaking|announced|announcement|press release|headline|coverage)\b"
    r"|(?:新闻|快讯|发布会|报道|最新消息|突发|头条)",
    re.I,
)


def resolve_country_code(raw: str) -> str | None:
    """将 ISO-2 代码或常见英文国名归一化为大写 alpha-2，无法识别返回 None。"""
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    return _COUNTRY_ALIASES.get(key)


def _strip_country_directive(text: str, ops: dict[str, Any]) -> str:
    """抽取首个 country:/region: 指令（最长合法前缀优先；无效值也剥离防污染）。"""
    m = _COUNTRY_RE.search(text)
    if not m:
        return text
    tokens = m.group(1).strip().split()[:3]
    matched_val = ""
    resolved: str | None = None
    for n in range(len(tokens), 0, -1):
        cand = " ".join(tokens[:n])
        code = resolve_country_code(cand)
        if code:
            matched_val = cand
            resolved = code
            break
    if not matched_val:
        matched_val = tokens[0] if tokens else ""
    prefix_end = m.start(1)
    cut_end = prefix_end + len(matched_val)
    raw_directive = text[m.start():cut_end].strip()
    ops["dropped"].append(raw_directive)
    if resolved:
        ops["country"] = resolved
    return text[:m.start()] + " " + text[cut_end:]


def parse_query_operators(query: str) -> tuple[str, dict[str, Any]]:
    """抽取查询操作符 → (clean_query, operators)。

    clean_query 剥掉全部指令（site/-site/-词/-"短语"/lang/after/before），
    保留精确短语 "..."（它是意图本体）。operators 字段：
      sites / exclude_sites: list[str]
      exclude_terms: list[str]（-词 与 -"短语" 合并，供排除过滤）
      langs: list[str]
      after / before: str | None
      dropped: list[str]（原操作符文本，可观测/limitations 提示用）
    """
    if not query or not isinstance(query, str):
        return (query or ""), _empty_ops()

    ops = _empty_ops()
    text = query

    def _take(match: re.Match) -> str:
        ops["dropped"].append(match.group(0).strip())
        return " "

    # 1) 站点限定/排除
    def _site_sub(m: re.Match) -> str:
        (ops["exclude_sites"] if m.group(1) else ops["sites"]).append(m.group(2))
        return _take(m)
    text = _SITE_RE.sub(_site_sub, text)

    # 2) 语言
    def _lang_sub(m: re.Match) -> str:
        langs = [x.strip() for x in m.group(1).split(",") if x.strip()]
        ops["langs"].extend(langs)
        return _take(m)
    text = _LANG_RE.sub(_lang_sub, text)

    # 3) 时间窗
    def _date_sub(m: re.Match) -> str:
        ops[m.group(1)] = m.group(2)
        return _take(m)
    text = _DATE_RE.sub(_date_sub, text)

    # 3.5) 国家/区域指令（country:de / region:united states）
    text = _strip_country_directive(text, ops)

    # 4) 排除短语（先于 -词，避免 " 被当词的一部分）
    def _excl_phrase_sub(m: re.Match) -> str:
        ops["exclude_terms"].append(m.group(1).strip())
        return _take(m)
    text = _EXCL_PHRASE_RE.sub(_excl_phrase_sub, text)

    # 5) 排除单词
    def _excl_term_sub(m: re.Match) -> str:
        term = m.group(1).strip()
        if term:
            ops["exclude_terms"].append(term)
        return _take(m)
    text = _EXCL_TERM_RE.sub(_excl_term_sub, text)

    clean = re.sub(r"\s+", " ", text).strip()
    return clean, ops


def _host_matches(host: str, site: str) -> bool:
    """host 是否属于 site（含子域）。site 为空或非法时返回 False。"""
    host = (host or "").lower().strip(".")
    site = (site or "").lower().strip().strip(".")
    if not host or not site:
        return False
    return host == site or host.endswith("." + site)


def operator_cache_suffix(operators: dict[str, Any] | None) -> str:
    """把影响结果集的操作符拼成缓存键后缀；无操作符返回空串。

    缓存键的主体是剥掉指令后的 clean_query，若不把指令本身并入键，
    `python asyncio` 与 `python asyncio -site:realpython.com` 会命中同一条
    缓存：要么把受限结果回给无约束查询，要么相反——后者更隐蔽，
    用户写了约束却拿到未过滤的结果。langs 同理（它改变路由与引擎选择）。
    after/before 已归一化为 since/until，由调用方单独并入，不在这重复。
    """
    ops = operators or {}
    parts: list[str] = []
    for key, tag in (("sites", "site"), ("exclude_sites", "xsite"),
                     ("exclude_terms", "not"), ("langs", "lang")):
        vals = [str(v).strip() for v in (ops.get(key) or [])
                if isinstance(v, (str, int, float)) and str(v).strip()]
        if vals:
            parts.append(f"{tag}={','.join(vals)}")
    cc = str(ops.get("country") or "").strip().upper()
    if cc:
        parts.append(f"cc={cc}")
    return "".join(f"|{p}" for p in parts)


def filter_results_by_sites(
    results: list[dict[str, Any]],
    sites: list[str] | None,
    exclude_sites: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """按 site:/-site: 后置过滤结果，返回 (保留项, 剔除数)。

    为什么是后置过滤而不是下推检索词：argo 的上游是 260 个异构引擎，多数
    不认识 site: 语法；把它留在检索串里只会降低召回。解析层已经把它从
    clean_query 剥掉，若不再执行就等于「宣称支持但静默忽略」——实测
    `DeepSeek site:github.com` 会返回 models.dev 的模型页（2026-10-17）。

    子域算匹配：site:github.com 命中 gist.github.com。多个 site 之间是 OR。
    无 url 的模态卡不参与站点判定（不因无法判定而被误删）。
    """
    include = [s for s in (sites or []) if isinstance(s, str) and s.strip()]
    exclude = [s for s in (exclude_sites or []) if isinstance(s, str) and s.strip()]
    if not include and not exclude:
        return results, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for r in results:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        url = str(r.get("url") or "")
        if not url:
            kept.append(r)
            continue
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except ValueError:
            host = ""
        if include and not any(_host_matches(host, s) for s in include):
            dropped += 1
            continue
        if exclude and any(_host_matches(host, s) for s in exclude):
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


def _empty_ops() -> dict[str, Any]:
    return {
        "sites": [], "exclude_sites": [], "exclude_terms": [],
        "langs": [], "country": None, "after": None, "before": None, "dropped": [],
    }


def classify_hint_topic(query: str, sites: list[str] | None = None) -> str:
    """零 LLM 轻量主题判定：code / research / finance / news / general。"""
    site_set = [str(s).lower() for s in (sites or [])]
    if any(any(d in s for d in ("github.com", "gitlab.com", "stackoverflow.com")) for s in site_set):
        return "code"
    if _CODE_TOPIC_RE.search(query or ""):
        return "code"
    if any("arxiv.org" in s for s in site_set) or _RESEARCH_TOPIC_RE.search(query or ""):
        return "research"
    if _FINANCE_TOPIC_RE.search(query or ""):
        return "finance"
    if _NEWS_TOPIC_RE.search(query or ""):
        return "news"
    return "general"


def is_evergreen_tech_query(query: str, sites: list[str] | None = None) -> bool:
    """常青技术/学术查询保护：非新闻场景不因 latest/recent 等词被误加短时效截断。"""
    topic = classify_hint_topic(query, sites)
    return topic in ("code", "research")


def _norm_ymd(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{4}", s):
        return f"{s}-01-01"
    return None


def compile_provider_params(
    engine: str,
    query: str,
    *,
    operators: dict[str, Any] | None = None,
    since: str | None = None,
    until: str | None = None,
    depth: str = "fast",
) -> dict[str, Any]:
    """将统一操作符与意图提示编译为目标引擎的原生请求参数（零 LLM、纯函数）。"""
    ops = operators if isinstance(operators, dict) else {}
    sites = [str(s).strip().lower() for s in (ops.get("sites") or []) if str(s).strip()]
    excl_sites = [str(s).strip().lower() for s in (ops.get("exclude_sites") or []) if str(s).strip()]
    langs = [str(x).strip().lower() for x in (ops.get("langs") or []) if str(x).strip()]
    country = str(ops.get("country") or "").strip().upper() or None
    after = _norm_ymd(since or ops.get("after"))
    before = _norm_ymd(until or ops.get("before"))
    topic = classify_hint_topic(query, sites)
    eng = (engine or "").strip().lower()
    out: dict[str, Any] = {}

    if eng == "exa":
        cat_map = {"research": "publication", "news": "news", "finance": "financial report"}
        if topic in cat_map:
            out["category"] = cat_map[topic]
        if sites:
            out["includeDomains"] = sites
        if excl_sites:
            out["excludeDomains"] = excl_sites
        if after:
            out["startPublishedDate"] = f"{after}T00:00:00.000Z"
        if before:
            out["endPublishedDate"] = f"{before}T23:59:59.999Z"
        if country:
            out["userLocation"] = country
        return out

    if eng == "tavily":
        if topic in ("news", "finance"):
            out["topic"] = topic
        if sites:
            out["include_domains"] = sites
        if excl_sites:
            out["exclude_domains"] = excl_sites
        if after:
            out["start_date"] = after
        if before:
            out["end_date"] = before
        if country and country in _TAVILY_COUNTRY_NAME:
            out["country"] = _TAVILY_COUNTRY_NAME[country]
        if langs:
            # 官方 /search 有原生 language 参数（ISO 639-1 或英文名，boost 语义）；
            # 旧版剥离 lang 却不下发，语言约束静默丢失（2026-11-21 审计）。
            # exa 经官方文档逐参数核对无语言参数，维持剥给路由不改（勿编造契约）。
            out["language"] = langs[0]
        return out

    if eng == "parallel":
        policy: dict[str, Any] = {}
        if sites:
            policy["include_domains"] = sites
        if excl_sites:
            policy["exclude_domains"] = excl_sites
        if after:
            policy["after_date"] = after
        if before:
            policy["before_date"] = before
        if policy:
            out["source_policy"] = policy
        return out

    if eng == "you":
        if sites:
            out["boost_domains"] = sites
        if country:
            out["country"] = country
        if langs:
            out["language"] = langs[0]
        return out

    return out


def format_exa_notices(notices: Any) -> str | None:
    """将 Exa 3.0 响应中的 notices 扁平化为单行模型可读提示（去重、容错）。"""
    if not isinstance(notices, list) or not notices:
        return None
    lines: list[str] = []
    for n in notices:
        line = ""
        if isinstance(n, str):
            line = n.strip()
        elif isinstance(n, dict):
            for key in ("message", "description", "text", "notice"):
                val = n.get(key)
                if isinstance(val, str) and val.strip():
                    line = val.strip()
                    break
        if line and line not in lines:
            lines.append(line)
    return f"Exa: {' | '.join(lines)}" if lines else None
