"""Argo 内容质量信号模块 — 内联移植自 Hound envelope.py。

4 个纯计算信号 + 1 个整合入口，全部基于 stdlib + re：
  classify_source         域名权威分类
  compute_freshness       时效性（年龄 + stale）
  detect_page_type        页面结构类型
  compute_content_quality 内容质量评分
  score_clickbait         标题党检测（2026-09-27 新增）
  score_title_body_consistency  标题-正文一致性（2026-09-27 新增）
  score_template_repetition     句法模板重复率（2026-09-27 新增）
主入口：analyze_fetch_result(url, html, content, metadata) -> dict。
零外部依赖，单次调用微秒级。
"""
from __future__ import annotations

import re
import functools
from datetime import datetime, date, timezone
from typing import Any
from urllib.parse import urlparse
from cli_io import dumps

# ── 全局常量 ────────────────────────────────────────────────────────

STALE_DAYS = 365

# 判定为 article 的最小正文字数（正文充分性阈值）。
# 供 detect_page_type 的 markdown-only 回退使用，避免与 fetch 判定漂移。
MIN_ARTICLE_CHARS = 200

_NEWS_DOMAINS = (
    "nytimes.com", "bbc.com", "bbc.co.uk", "reuters.com", "theguardian.com",
    "washingtonpost.com", "bloomberg.com", "apnews.com", "aljazeera.com",
    "cnbc.com", "ft.com", "economist.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "wired.com", "nature.com", "science.org",
)
_QA_DOMAINS = (
    "stackoverflow.com", "stackexchange.com", "serverfault.com",
    "superuser.com", "mathoverflow.com", "askubuntu.com",
)
_GITHUB_DOMAINS = (
    "github.com", "raw.githubusercontent.com", "gist.github.com",
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y",
    "%d %B %Y", "%d %b %Y",
)

_FORUM_MARKERS = (
    "phpbb", "discourse", 'class="forum', 'id="forum',
    'class="thread', 'class="post-body', 'class="message-body', "data-post-id",
)
_QA_MARKERS = (
    "stackoverflow", "stackexchange", 'class="question',
    'class="answer', "data-answerid", "data-questionid",
)
_DOCS_MARKERS = (
    "mkdocs", "docusaurus", "readthedocs", "sphinx-document",
    "algolia-docsearch", "md-nav", "theme-doc", 'class="rst-content"',
    "wy-nav-side",
)
_PAYWALL_MARKERS = (
    "subscribe to continue", "subscribe to read",
    "this article is for subscribers",
    "create a free account to continue", "sign in to continue reading",
    "you've reached your free article limit", "subscriber-only content",
    "premium content", "paywall",
)

_META_REFRESH_RE = re.compile(
    r'<meta\b[^>]*?http-equiv=["\']refresh["\'][^>]*?content=["\'][^"\']*url=',
    re.IGNORECASE,
)
_JS_REDIRECT_RE = re.compile(
    r'(?:location\.href\s*=|location\.replace|window\.location\s*=)',
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\']', re.IGNORECASE)
_STRIP_BLOCK_RE = re.compile(
    r'<(nav|header|footer|aside|script|style|noscript)\b[^>]*>.*?</\1>',
    re.IGNORECASE | re.DOTALL,
)
_ARTICLE_TAG_RE = re.compile(r'<article\b', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


# ── 1. 域名权威分类 ─────────────────────────────────────────────────

def classify_source(url: str) -> dict:
    """URL 域名权威分类。

    返回 {"source_type", "is_official"}。
    source_type: gov/edu/github/news/blog/forum/qa/docs-site/ecommerce/unknown
    is_official: 仅对 .gov/.edu/github/厂商 docs 判定 True。
    """
    if not url:
        return {"source_type": "unknown", "is_official": False}
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return {"source_type": "unknown", "is_official": False}
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if not host:
        return {"source_type": "unknown", "is_official": False}

    if host.endswith(".gov") or host == "gov" or ".gov." in host:
        return {"source_type": "gov", "is_official": True}
    if host.endswith(".edu") or host.endswith(".ac.uk") or re.search(r"\.ac\.[a-z]{2}$", host):
        return {"source_type": "edu", "is_official": True}
    if host in _GITHUB_DOMAINS or host.endswith(".github.io"):
        return {"source_type": "github", "is_official": True}
    if host.startswith(("docs.", "developer.", "developers.")):
        return {"source_type": "docs-site", "is_official": True}
    if host in _QA_DOMAINS or host.endswith(".stackexchange.com") or host.endswith(".stackoverflow.com"):
        return {"source_type": "qa", "is_official": False}
    if any(m in host for m in ("forum", "forums", "community", "discourse", "board")):
        return {"source_type": "forum", "is_official": False}
    if host in ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com") or host.endswith(".reddit.com"):
        return {"source_type": "forum", "is_official": False}
    if (
        host.startswith("blog.")
        or host in ("medium.com", "wordpress.com", "substack.com")
        or host.endswith((".substack.com", ".medium.com"))
    ):
        return {"source_type": "blog", "is_official": False}
    if host.startswith(("shop.", "store.")) or host in ("amazon.com", "ebay.com") or host.endswith(".shop"):
        return {"source_type": "ecommerce", "is_official": False}
    if any(host == d or host.endswith("." + d) for d in _NEWS_DOMAINS):
        return {"source_type": "news", "is_official": False}
    return {"source_type": "unknown", "is_official": False}


# ── 2. 时效性 ────────────────────────────────────────────────────────

def _parse_date(s: str) -> date | None:
    """解析日期：ISO 8601 (含 Z/offset)、压缩 YYYYMMDD、英文格式。"""
    if not s or not s.strip():
        return None
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    for cand in (s, s[:10]):
        try:
            return datetime.fromisoformat(cand.replace("Z", "+00:00")).date()
        except ValueError:
            continue
    for fmt in _DATE_FORMATS:
        for cand in (s, s[:32]):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def compute_freshness(meta_dates: dict, fetched_at: str = None) -> dict:
    """计算内容年龄。

    输入 {"published_time", "modified_time", "date"}；
    偏好 modified > published > date；
    返回 {"content_age_days", "is_stale"}，无日期返回 (-1, False)。
    """
    if not meta_dates:
        return {"content_age_days": -1, "is_stale": False}
    date_str = (
        meta_dates.get("modified_time")
        or meta_dates.get("published_time")
        or meta_dates.get("date")
        or ""
    )
    content_date = _parse_date(date_str) if date_str else None
    if content_date is None:
        return {"content_age_days": -1, "is_stale": False}
    fetched_date = _parse_date(fetched_at) if fetched_at else datetime.now(timezone.utc).date()
    delta = (fetched_date - content_date).days
    if delta < 0:
        return {"content_age_days": -1, "is_stale": False}
    return {"content_age_days": delta, "is_stale": delta > STALE_DAYS}


# ── 3. 页面结构类型检测 ─────────────────────────────────────────────

def _count_content_links(html: str, host: str) -> int:
    """统计主内容区同域链接数（剥除 nav/header/footer 等 chrome）。"""
    stripped = _STRIP_BLOCK_RE.sub("", html)
    count = 0
    for m in _ANCHOR_RE.finditer(stripped):
        href = (m.group(1) or "").strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if href.startswith(("/", "?")):
            count += 1
            continue
        try:
            h = urlparse(href).netloc.lower()
        except Exception:
            continue
        if h and (h == host or h.endswith("." + host)):
            count += 1
    return count


def detect_page_type(html: str, url: str = "", content: str = "") -> dict:
    """从 HTML 判断页面结构类型。

    返回 {"page_type", "confidence"}。
    page_type: article/list/forum/qa/docs/paywall/redirect/unknown。
    错误推导信号（js_shell/auth_wall）不在此处检测——由上层错误回调 override。

    content 参数用于 markdown-only 源（.md 变体 / tinyfish 渲染层）回退：
    这类源没有原始 HTML，docs / list / forum 等结构信号全部缺失，只能按
    正文字数判「正文充分」，confidence 相应压低（0.4）以反映判定降级。
    """
    if not html or not html.strip():
        if content and len(content) > MIN_ARTICLE_CHARS:
            return {"page_type": "article", "confidence": 0.4}
        return {"page_type": "unknown", "confidence": 0.0}
    low = html.lower()
    # 跳转
    if _META_REFRESH_RE.search(low):
        return {"page_type": "redirect", "confidence": 0.9}
    text_len_approx = len(re.sub(r"<[^>]+>", "", low))
    if _JS_REDIRECT_RE.search(low) and text_len_approx < 500:
        return {"page_type": "redirect", "confidence": 0.85}
    # paywall
    if any(m in low for m in _PAYWALL_MARKERS):
        return {"page_type": "paywall", "confidence": 0.9}
    # 结构化标记
    if any(m in low for m in _QA_MARKERS):
        return {"page_type": "qa", "confidence": 0.85}
    if any(m in low for m in _FORUM_MARKERS):
        return {"page_type": "forum", "confidence": 0.8}
    if any(m in low for m in _DOCS_MARKERS):
        return {"page_type": "docs", "confidence": 0.85}
    # list 页：多同域链接 + 文本少
    host = ""
    if url:
        try:
            host = urlparse(url).netloc.lower().split(":", 1)[0]
        except Exception:
            host = ""
    if host and not _ARTICLE_TAG_RE.search(low):
        n_links = _count_content_links(html, host)
        if n_links >= 20 and (text_len_approx < 1500 or text_len_approx / n_links < 200):
            return {"page_type": "list", "confidence": 0.75}
    # article
    if _ARTICLE_TAG_RE.search(low):
        return {"page_type": "article", "confidence": 0.8}
    return {"page_type": "unknown", "confidence": 0.4}


# ── 4. 内容质量评分 ─────────────────────────────────────────────────

# GEO 实证：数字/定义/对比/how-to 与吸收深度正相关；纯 Q&A 格式无优势（甚至略负）
_NUM_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:%|％|亿|万|万亿|pct|bp|元|美元|吨|倍)"
    r"|(?:环比|同比|较上[季年]度?)[^\n]{0,12}[+\-＋－]?\d"
    r"|Q[1-4]\b|20\d{2}\s*年",
    re.I,
)
_DEF_RE = re.compile(
    r"(?:是指|定义为|所谓|即指|指的是|是一种|可定义为"
    r"|definition of|is defined as|refers to)",
    re.I,
)
_CMP_RE = re.compile(
    r"(?:对比|比较|相较|相比|环比|同比|分别|vs\.?|versus|versus|"
    r"增持|减持|上升|下降|提升|回落|高于|低于|超过|不及)",
    re.I,
)
_HOWTO_RE = re.compile(
    r"(?:步骤|如何|怎么做|操作建议|方法如下|第[一二三四五六七八九十1-9][步、.]"
    r"|step\s*\d|how to|tutorial)",
    re.I,
)
_QA_FMT_RE = re.compile(
    r"(?:^|\n)\s*(?:Q\s*[:：]|A\s*[:：]|问\s*[:：]|答\s*[:：])"
    r"|class=[\"']question[\"']|class=[\"']answer[\"']"
    r"|(?:怎么样|好不好|靠谱吗)\s*$",
    re.I | re.M,
)
_DISCLOSE_RE = re.compile(
    r"(?:截至|根据|数据显示|研究报告|披露|公告|季报|年报|来源[：:])",
    re.I,
)


@functools.lru_cache(maxsize=512)
def _score_evidence_density_cached(text: str, title: str = "") -> tuple:
    """score_evidence_density 的缓存版本：纯函数（仅 regex + 长度判定，
    无时间/全局状态），返回元组以避免 lru_cache 缓存可变 dict 被调用侧
    意外修改——与 score_authority 的缓存形态统一。"""
    body = f"{title or ''}\n{text or ''}"
    has_numbers = bool(_NUM_RE.search(body))
    has_definition = bool(_DEF_RE.search(body))
    has_comparison = bool(_CMP_RE.search(body))
    has_howto = bool(_HOWTO_RE.search(body))
    has_disclose = bool(_DISCLOSE_RE.search(body))
    is_qa_format = bool(_QA_FMT_RE.search(body)) or bool(
        re.search(r"[?？]\s*$", (title or "").strip())
    )

    score = 0.15
    if has_numbers:
        score += 0.22
    if has_definition:
        score += 0.18
    if has_comparison:
        score += 0.16
    if has_howto:
        score += 0.12
    if has_disclose:
        score += 0.08
    if len(body) >= 80:
        score += 0.05
    if is_qa_format:
        score -= 0.08  # GEO: 纯 Q&A 格式平均吸收略负

    absorption = round(min(max(score, 0.0), 1.0), 3)
    return (has_numbers, has_definition, has_comparison, has_howto,
            has_disclose, is_qa_format, absorption)


def score_evidence_density(text: str, title: str = "") -> dict:
    """证据密度评分（snippet 或正文均可）。

    第一性：Agent 需要的不是「被检索到」，而是「可抽取、可核对的证据块」。
    返回布尔特征 + absorption_score ∈ [0,1]。

    结果按 (text, title) 做进程内 lru_cache：rerank / selection / evidence
    打分 / compute_content_quality 间共享，避免同一 snippet 反复跑六次
    正则提取。调用契约不变（返回新 dict）。
    """
    has_numbers, has_definition, has_comparison, has_howto, \
        has_disclose, is_qa_format, absorption = _score_evidence_density_cached(
            text, title)
    return {
        "has_numbers": has_numbers,
        "has_definition": has_definition,
        "has_comparison": has_comparison,
        "has_howto": has_howto,
        "has_disclose": has_disclose,
        "is_qa_format": is_qa_format,
        "absorption_score": absorption,
    }


def compute_content_quality(content: str, title: str = "") -> dict:
    """去 HTML 后文本质量评分。

    返回 quality_score / content_ok / word_count / text_density / has_structure
    + 证据密度字段（GEO 吸收信号）。
    content_ok = quality_score > 0.3 且 word_count > 50。
    """
    clean = _TAG_RE.sub(" ", content or "")
    clean = _WS_RE.sub(" ", clean).strip()
    word_count = len(clean)
    text_len = len(clean.replace(" ", ""))
    raw_len = max(len(content or ""), 1)
    text_density = text_len / raw_len
    has_structure = bool(
        re.search(r"</?(p|li|h[1-6]|pre|blockquote|section|div)\b", content or "", re.IGNORECASE)
    )
    # 长度分：50-1500 字线性增长（降权，避免 SEO 水文仅靠长度胜出）
    length_score = min(max((word_count - 50) / 1000, 0.0), 1.0)
    density_score = min(text_density / 0.5, 1.0)
    structure_score = 0.2 if has_structure else 0.0
    title_bonus = 0.0
    if title:
        title_words = [w for w in _WS_RE.split(title.lower()) if len(w) > 1]
        if title_words:
            hits = sum(1 for w in title_words if w in clean.lower())
            title_bonus = min(hits / len(title_words), 1.0) * 0.1

    evidence = score_evidence_density(clean, title)
    # 权重：长度 0.2 + 密度 0.2 + 结构 0.2 + 证据 0.3 + 标题保持一致 0.1
    quality_score = min(
        length_score * 0.2
        + density_score * 0.2
        + structure_score
        + evidence["absorption_score"] * 0.3
        + title_bonus,
        1.0,
    )
    return {
        "quality_score": round(quality_score, 3),
        "content_ok": quality_score > 0.3 and word_count > 50,
        "word_count": word_count,
        "text_density": round(text_density, 3),
        "has_structure": has_structure,
        **evidence,
    }


# ── 5. SEO/低质内容信号（2026-09-27 新增）───────────────────────────
#
# 三个互补信号，全部纯规则、零依赖，服务于「主题相关但质量低」的内容——
# 这类内容能骗过 serp_guard（token 交集高）也能骗过长度型完整性分。
#
# 文献依据（详见 research/argo-低质内容检测调研.md）：
#   - Chang & Huang, PACLIC 2024：中文标题党的四类语言特征，并明确警告
#     「单纯数标点会误杀」——故本实现要求悬念/夸张词与标点**共现**才算
#   - Google Search Quality Rater Guidelines 4.0/5.0：把「title extremely
#     misleading, shocking, or exaggerated」列为独立的最低质量判据
#   - 百度《违规低质页面问题说明》：「资源内容不符」（内容与标题不一致）
#   - Shaib et al., EMNLP 2024：模板重复率（模型 76% vs 人类 35%）

# 中文停用字：高频但无区分度。与 search_rank._CJK_STOPCHARS 同表——
# 两处独立维护是为了避开反向依赖（search_rank 在 import 链上游，
# content_signals 不能被它反向依赖），分叉由测试的一致性用例发现。
_ZH_STOPCHARS: frozenset[str] = frozenset(
    "的了是和与或在有为被把对从到就都也还很更最之其此这那一个"
    "个们我你他她它上下中里外前后时和及等所可能够会要"
    "怎幺么样如选哪个多少何呢吗吧啊哦呀嘛"
)

# PACLIC 2024 的悬念词/夸张词表（原文四类里可直接规则化的两类）。
# 刻意保持小规模（<20 词）：词表越长越容易误伤正常标题。
_ZH_SUSPENSE_WORDS = ("疑", "曝", "露", "公開", "揭秘", "内幕", "真相", "原来")
_ZH_EXAGGERATED_WORDS = ("震惊", "驚", "轟", "必看", "绝对", "史上最", "第一", "唯一",
                         "彻底", "惊呆", "炸了", "翻车", "逆天")
# 前指代词/列表数字：制造 curiosity gap 与 listicle 结构
_ZH_FORWARD_REFS = ("这", "那", "他", "她", "它", "此", "该")


def score_clickbait(title: str) -> dict:
    """标题党打分（0-1，越高越像标题党）。

    2026-09-27 标定（tests/golden/lowquality_calibration.json，14 条好/坏对照）
    量化出两件事，直接决定了这个函数的形态：

    1. **纯词表对现代农场标题近乎无效**。标定集 7 条低质样本里 6 条得 0.00，
       只有 2020 年代的老式标题党（震惊！…99% 的人都不知道）被抓到。2026 年
       的 SEO 写法是「2026最新推荐10款，看完这篇你就懂了」——一个词表都不沾。
    2. **单纯换结构模式也不够**。试过疑问尾钩/数字开头/年份/比较声明/利益承诺/
       最高级/破折号/副标题八种模式，各自的区分度都在噪声附近，且
       「疑问尾钩」把正常的技术文标题（"Python 性能优化从哪开始？"）误伤。

    故现在的形态是**词汇与结构取并集、且两者都弱**：词表命中给 0.30/0.35，
    结构模式每个只给 0.08-0.12。理由见下——

    这个信号在标定集上 AUC 仅 0.643，**它本就该是弱信号**。它的价值不在
    独立判别（做不到），而在当它与 title_body / 来源侧证据**同时**命中时提高
    置信度。因此宁可漏放也不误杀：单信号误伤的代价（正常长文被判农场）
    高于漏放（反正还有别的信号兜着）。真要提升整体判别力，该加的是来源侧
    证据（见 rank_signals.cross_domain_homogeneity_penalty），不是在这里
    堆词表。

    PACLIC 2024 的教训仍然生效：标点**不单独计分**，只作已有命中的放大器
    （原文发现模型把「含感叹/疑问号的中文非标题党」过度泛化为标题党）。
    """
    t = (title or "").strip()
    if not t:
        return {"score": 0.0, "hits": [], "is_clickbait": False}

    hits: list[str] = []
    score = 0.0

    # ── 词汇层：老式标题党仍能抓到，权重维持较高 ──
    sus = [w for w in _ZH_SUSPENSE_WORDS if w in t]
    exa = [w for w in _ZH_EXAGGERATED_WORDS if w in t]
    if sus:
        score += 0.30
        hits.extend(sus)
    if exa:
        score += 0.35
        hits.extend(exa)

    # ── 结构层：现代农场写法，每个模式只给弱权重 ──
    # 数字 listicle：「10款」「3款」等。不要求数字前有分隔符——「震惊！这3款…」
    # 这种形态里数字紧跟量词，漏掉它就丢掉了最典型的一类现代标题党。
    if re.search(r"\d+\s*(?:个|款|种|条|大|招|步|类)", t):
        score += 0.12
        hits.append("listicle")
    # 分隔式堆砌：三段以上短语（内容农场标题的标准形态）
    if len([s for s in re.split(r"[，,、|｜;；【】]", t) if s.strip()]) >= 3:
        score += 0.12
        hits.append("segmented")
    # 伪新鲜：标题里的年份几乎从不参与正文判断，是农场刷新页的标志
    if re.search(r"(?:19|20)\d{2}\s*年", t):
        score += 0.08
        hits.append("year_prefix")
    # 利益承诺：「看完…你就会」「建议收藏」「一篇搞懂」
    if re.search(r"(看完|建议收藏|建议收藏|一篇(?:讲|说|搞懂)|全解析|必看|建议收藏)", t):
        score += 0.10
        hits.append("benefit_promise")

    # 标点：仅在已有命中时作**放大器**（PACLIC 的误杀教训）
    if score > 0:
        if "！" in t or "!" in t:
            score += 0.12
            hits.append("!")
        if "？" in t or "?" in t:
            score += 0.08
            hits.append("?")
    # 前指代词开头制造悬念
    if t[0] in _ZH_FORWARD_REFS:
        score += 0.10
        hits.append("fwd_ref")

    score = round(min(score, 1.0), 3)
    return {"score": score, "hits": hits, "is_clickbait": score >= 0.5}


def score_title_body_consistency(title: str, content: str) -> dict:
    """标题-正文一致性（0-1，越高越一致）。

    百度官方低质判据里的「资源内容不符」——标题承诺的主题在正文里找不到。
    纯规则实现：标题的信息单元（CJK bigram + 拉丁词）在正文里的覆盖率。
    标题党与洗稿站常在这里露馅：标题堆满热词，正文却是通用模板。
    """
    t = (title or "").strip()
    if not t or not content:
        return {"score": 0.5, "coverage": 0.0, "unmatched": [], "mismatch": False}
    # 复用与 search_rank 同口径的单元划分（此处独立实现以避开循环导入：
    # search_rank 已经在 import 链的上游，content_signals 不能被它反向依赖）
    def _units(text: str) -> list[str]:
        low = text.lower()
        out: list[str] = []
        for run in re.findall(r"[\u4e00-\u9fff]+", low):
            kept = [ch for ch in run if ch not in _ZH_STOPCHARS]
            if len(kept) == 1:
                out.append(kept[0])
            else:
                out.extend(kept[i] + kept[i + 1] for i in range(len(kept) - 1))
        out.extend(re.findall(r"[a-zA-Z0-9]+", low))
        return out

    tu = _units(t)
    if not tu:
        return {"score": 0.5, "coverage": 0.0, "unmatched": [], "mismatch": False}
    # 误伤防护（2026-09-27）：本信号只对**中文标题**判定。
    # 理由：英文标题常是单个词或短短语（"Gold" / "Test" / "Pricing"），
    # 与正文的 bigram 交集天然稀疏，覆盖率恒低会误判为「文不对题」——
    # 实测 tests/test_evidence_loop.py 的 mock（title="Test"，正文为黄金行情
    # 英文段落）覆盖率 0.0，直接把正常抓取压成 mismatch。
    # 中文标题的信息单元密度高，覆盖率才有判别力；英文交给 clickbait 与
    # template 两个信号处理。
    if not re.search(r"[\u4e00-\u9fff]", t):
        return {"score": 0.5, "coverage": 0.0, "unmatched": [], "mismatch": False,
                "skipped": "non_cjk_title"}
    # 标题过短（单元数 <3）时覆盖率噪声大，不判定
    uniq_cjk = [u for u in dict.fromkeys(tu) if re.search(r"[\u4e00-\u9fff]", u)]
    if len(uniq_cjk) < 3:
        return {"score": 0.5, "coverage": 0.0, "unmatched": [], "mismatch": False,
                "skipped": "title_too_short"}
    body = set(_units(content))
    uniq = list(dict.fromkeys(tu))          # 去重保序，避免长标题压倒短标题
    hit = [u for u in uniq if u in body]
    coverage = len(hit) / len(uniq)
    unmatched = [u for u in uniq if u not in body][:8]
    return {
        "score": round(coverage, 3),
        "coverage": round(coverage, 3),
        "unmatched": unmatched,
        # 阈值 0.5：过半数标题单元在正文里找不到 → 文不对题
        "mismatch": coverage < 0.5,
    }


def score_template_repetition(content: str) -> dict:
    """句法模板重复率（0-1，越高越像批量生成/模板化内容）。

    依据 Shaib et al. (EMNLP 2024)：LLM 生成文本的句法模板重复率显著高于
    人类（76% vs 35%），且微调后不被覆盖。此处用**粗粒度近似**（不做 POS
    标注，避免引入 NLP 工具）：每句取「首二字 + 末二字 + 长度档位」作模板
    指纹，统计同一指纹在文内的重复率。

    ⚠️ **2026-09-27 标定结论：这个近似在本仓的口径下没有判别力，勿依赖它。**

    标定集（14 条好/坏对照）实测 AUC = **0.500**——恰好等于随机猜测。逐条
    看：正常技术长文得 0.833，LLM 批量生成文本得 0.875，两者几乎不可分。
    原因不难理解：中文技术写作本身句式就高度平行（「X 通过 Y 实现 Z」、
    「需要注意的一点是」），首二字+末二字这个指纹在**人类**文本上同样高频
    重复。论文测的是英文 POS 模板，被搬到中文后失去了分辨力。

    又试了三个更贴近论文的廉价代理，全部落在噪声附近：
      标点序列模板 AUC=0.551 / 句首二字集中度 AUC=0.592 / 句长自相关 AUC=0.531

    结论：要做这件事需要真正的句法分析（POS 标注或依存句法），那会引入
    重依赖，与本仓「零外部依赖、单次调用微秒级」的纪律冲突。故**保留字段
    但降权到几乎不参与决策**（见 evidence_loop 的折扣系数），并在标定脚本
    里持续显示 AUC=0.5 作为「已知无效」的显式记录。将来若接入句法分析，
    这里是接入点，且标定集能立刻给出 before/after。
    """
    text = (content or "").strip()
    if len(text) < 120:
        return {"score": 0.0, "templates": 0, "sentences": 0, "repetition": 0.0}
    # 分句：中英标点 + 换行
    sents = [s.strip() for s in re.split(r"[。！？!?；;\n]+", text) if len(s.strip()) >= 6]
    if len(sents) < 4:
        return {"score": 0.0, "templates": len(sents), "sentences": len(sents),
                "repetition": 0.0}
    sigs: list[str] = []
    for s in sents:
        head = s[:2]
        tail = s[-2:]
        # 长度档位：粗化到 3 档，避免「模板相同但字数微差」被判为不同
        bucket = "S" if len(s) < 25 else ("M" if len(s) < 60 else "L")
        sigs.append(f"{head}|{tail}|{bucket}")
    uniq = len(set(sigs))
    repetition = 1.0 - uniq / len(sigs)
    return {
        "score": round(min(repetition, 1.0), 3),
        "templates": uniq,
        "sentences": len(sigs),
        "repetition": round(repetition, 3),
    }


# 中文停用字（与 search_rank._CJK_STOPCHARS 同表；此处独立维护以避开
# 反向依赖，两处若分叉由 tests/test_relevance_cjk.py 的一致性用例发现）


# ── 6. 整合入口 ─────────────────────────────────────────────────────

def analyze_fetch_result(url: str, html: str, content: str, metadata: dict | None = None) -> dict:
    """综合所有信号返回完整质量信封。

    2026-09-29 新增：集成证据分层（evidence_tier）和来源账本（source_ledger），
    用于量化内容来源的可信度和新鲜度风险。
    """
    metadata = metadata or {}
    title = metadata.get("title", "")

    # 基础信号
    source_info = classify_source(url)
    quality_info = compute_content_quality(content, title)

    # 证据分层
    try:
        from evidence_tier import assess_evidence_tier
        has_date = bool(metadata.get("published_time") or metadata.get("modified_time"))
        has_author = bool(metadata.get("author"))
        has_citation = bool(re.search(r"(https?://|参考文献|references|资料来源)", content or "", re.IGNORECASE))
        evidence = assess_evidence_tier(
            url=url,
            source_type=source_info.get("source_type", "unknown"),
            is_official=source_info.get("is_official", False),
            has_date=has_date,
            has_author=has_author,
            has_citation=has_citation,
            content=content,
        )
        evidence_result = {
            "tier": evidence.tier,
            "tier_weight": evidence.tier_weight,
            "notes": evidence.notes,
        }
    except ImportError:
        evidence_result = {"tier": "unknown", "tier_weight": 0.0, "notes": []}

    # 来源账本
    try:
        from source_ledger import SourceLedger, create_source_record
        ledger = SourceLedger()
        record = create_source_record(
            url=url,
            publisher=source_info.get("source_type", ""),
            fetch_success=True,
            http_status=200,
            title=title,
            content=content,
            confidence_tier=evidence_result.get("tier", "D"),
        )
        ledger.add_source(record)
        ledger_result = {
            "freshness_risk": ledger.get_freshness_risk(url),
            "access_risk": ledger.get_access_risk(url),
            "overall_risk": ledger.get_overall_risk(url),
        }
    except ImportError:
        ledger_result = {"freshness_risk": 0.0, "access_risk": 0.0, "overall_risk": 0.0}

    # 文体特征检测（2026-09-29）
    try:
        from stylometry_detector import StylometryDetector
        stylometry = StylometryDetector().score(content)
    except ImportError:
        stylometry = {"score": 0.0, "features": {}, "is_suspicious": False}

    # 突发性检测（2026-09-29）
    try:
        from burstiness_detector import BurstinessDetector
        burstiness = BurstinessDetector().score(content)
    except ImportError:
        burstiness = {"score": 0.0, "burstiness": {}, "is_suspicious": False}

    return {
        "source": source_info,
        "freshness": compute_freshness(metadata),
        "page_type": detect_page_type(html, url),
        "quality": quality_info,
        # SEO/低质信号（2026-09-27）：只在有正文时才有意义，故随主信封一起算
        "clickbait": score_clickbait(title),
        "title_body": score_title_body_consistency(title, content),
        "template": score_template_repetition(content),
        # 证据分层 + 来源账本（2026-09-29）
        "evidence_tier": evidence_result,
        "source_ledger": ledger_result,
        # 文体特征 + 突发性检测（2026-09-29）
        "stylometry": stylometry,
        "burstiness": burstiness,
    }


if __name__ == "__main__":
    test_url = "https://docs.python.org/3/library/asyncio.html"
    body_text = "word " * 200
    test_html = (
        '<html><head><title>asyncio</title></head><body><article><h1>asyncio</h1>'
        "<p>" + body_text + "</p>"
        + '<a href="/library/a.html">a</a>' * 5
        + '<nav><a href="/">home</a></nav>'
        + "</article></body></html>"
    )
    test_content = body_text
    test_meta = {"title": "asyncio", "published_time": "2024-01-15", "modified_time": "2025-06-01"}
    print(dumps(analyze_fetch_result(test_url, test_html, test_content, test_meta)))
