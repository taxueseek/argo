#!/usr/bin/env python3
"""serp_guard — SERP / 模糊搜索结果的垃圾结果守卫（判定规则单点）。

事故背景（dsh-free-search issue #38/#35 同款事故，argo 的 local_* 引擎同样
暴露）：html 引擎解析的是无 API 的网页 SERP，上游偶发把缓存 SERP / 与查询
完全无关的结果页当正常页返回；模糊搜索 API（people_daily 的 isFuzzy=True）
匹配不到时则回整页默认 feed。两种形态都是「答非所问但看起来正常」，解析层
照常抽出标题摘要，垃圾条目静默混进聚合，调用方把「答非所问」当成「网上
没有」——失败伪装成功是红线，这类混入是它的隐形变体。

裁决口径（全部量化，无模糊地带）：

  1. query token：CJK 连续段取相邻二元组、拉丁/数字连续段取小写词。
     token 少于 2 个时恒 False（无裁决权）——单 token 的巧合重叠率太高，
     拦了误杀大于漏放。单字 CJK 查询（如「猫」）只有 0 个二元组，双字
     CJK 查询（如「猫粮」）只有 1 个，都天然落在无裁决区。
  2. results 只取前 5 条；每条取 title+snippet。URL 不参与：缓存/跳转
     链接的 URL 常回显 query 参数（uddg=、q=），会正好把垃圾页放行，
     标题+摘要才是「这页在答什么」的信号。
  3. 前 5 条里没有任何一条覆盖 query token 的 20% → 整页判垃圾（True）。
     只判「有没有一个词命中」不够：实测「2026年新能源汽车销量数据」经
     local_bing 返回的五条全是 2026 年节假日/维基页面，只命中「2026」一个
     数字 token，旧判据（有交集即放行）把整页垃圾放了过去，而核心词
     「新能源汽车销量数据」全部缺席。覆盖率 1/9 远低于门槛。
     中文查询在存在 CJK 二元组时**不把纯数字 token 计入分母**：年份是最
     容易被无关页面顺带命中的词，不能拿它当相关性证据。

为什么调用方对判垃圾应整页丢弃而不是过滤后返回幸存者：前 5 条全无关说明
这一页整体答非所问（缓存页/错路由），幸存的一两条是同一页的噪声，过滤
返回等于给垃圾留门，宁缺勿假。调用方（engines_base）判垃圾后返回 [] 且
不记 note_failure——结果无关不是引擎故障，不该进熔断归因。

ARGO_SERP_GUARD=0 整体关闭守卫（逃生门：规则误杀时第一反应是关闸复现，
而不是删代码）。

不 import search_rank：本模块处在 engines_base 的冷启动热路径上，search_rank
较重且有环风险；分词口径按其 _tokens/_bigrams 的语义在本模块最小复刻
（CJK 二元组 + 小写拉丁词），不共享代码。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

# CJK 连续段 / 小写拉丁数字词（findall 前文本已 lower()）
_RUN_RE = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9]+")
_GUARD_WINDOW = 5      # 只看前 5 条：SERP 的「答非所问」看头部即可下结论
_MIN_QUERY_TOKENS = 2  # query token 少于此数 → 无裁决权，恒不拦
_MIN_COVERAGE = 0.2    # 单条结果至少要覆盖 20% 的 query token 才算「在答这题」
_SWITCH_ENV = "ARGO_SERP_GUARD"


def guard_enabled() -> bool:
    """逃生门：仅 ARGO_SERP_GUARD=0 关闭，其余取值（含未设置）均开启。"""
    return os.environ.get(_SWITCH_ENV, "") != "0"


def _extract_tokens(text: str) -> set[str]:
    """提取 token：CJK 相邻二元组 + 小写拉丁/数字词（与 search_rank 同口径）。"""
    tokens: set[str] = set()
    for run in _RUN_RE.findall((text or "").lower()):
        if "\u4e00" <= run[0] <= "\u9fff":
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
        else:
            tokens.add(run)
    return tokens


def _result_text(item: object) -> str:
    """单条结果的相关性文本 = title + snippet；非 dict 或字段缺失按空处理。"""
    if not isinstance(item, dict):
        return ""
    return f"{item.get('title') or ''} {item.get('snippet') or ''}"


def _query_tokens(query: str) -> set[str]:
    """query token；中文查询里纯数字不参与（年份不是相关性证据）。

    有 CJK 二元组说明查询主体是中文短语，此时「2026」这类数字是最容易被
    无关页面顺带命中的 token（节假日表、维基年表都带年份），留在分母里会
    把覆盖率算高、把垃圾页放行。纯拉丁查询（如「2026 GDP」）没有这个问题，
    数字是有信息的，照常保留。
    """
    tokens = _extract_tokens(query)
    if any("\u4e00" <= ch <= "\u9fff" for tok in tokens for ch in tok):
        tokens = {tok for tok in tokens if not tok.isdigit()}
    return tokens


def is_junk_serp(query: str, results: list[dict]) -> bool:
    """判一页 SERP 结果是否整体答非所问（口径见模块 docstring）。

    返回 True 表示整页判垃圾，调用方应丢弃全部条目；返回 False 表示不拦
    （含 query 无裁决权、results 为空、守卫被关闭——三种都不拦）。
    """
    if not guard_enabled():
        return False
    q_tokens = _query_tokens(query if isinstance(query, str) else "")
    if len(q_tokens) < _MIN_QUERY_TOKENS:
        return False
    if not isinstance(results, list):
        return False
    head = [r for r in results[:_GUARD_WINDOW] if r is not None]
    if not head:
        return False
    for item in head:
        hit = _extract_tokens(_result_text(item)) & q_tokens
        if len(hit) / len(q_tokens) >= _MIN_COVERAGE:
            return False
    return True


# ── SERP/跳转 URL 判定（自 evidence.py 迁入，2026-10-01）────────────────────
# 迁入原因：这组判定是纯 URL 规则，但原宿主 evidence.py 会把 content_signals
# 等证据质量栈一起拖进 import（实测 ~9 ms/进程），而它的调用方
# （evidence_loop.gate_results、search_pipeline 的 SERP 过滤）在**每次搜索
# 必经**的路径上——缓存命中也照付。本模块只依赖 os/re/json/urllib.parse，
# 处在 engines_base 冷启动热路径上，轻量有既有纪律保证（见模块 docstring）。
# evidence.py 保留同名转出，既有调用方（ab_eval_p0p1 等）不受影响。
_BACKENDS_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "backends"))
_CN_OVERRIDES: "dict[str, Any] | None" = None


def _load_cn_source_types() -> dict[str, Any]:
    global _CN_OVERRIDES
    if _CN_OVERRIDES is not None:
        return _CN_OVERRIDES
    path = os.path.join(_BACKENDS_DIR, "source_types_cn.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            _CN_OVERRIDES = json.load(f)
    except Exception:
        _CN_OVERRIDES = {}
    return _CN_OVERRIDES


def _normalize_domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _query_has_param(query: str, name: str) -> bool:
    """query 参数名完整匹配：只在开头或 & 后出现才算。

    子串匹配会把百度网盘分享的 ?pwd=x 连坐进 wd=（pwd 恰以 wd= 结尾，
    实测误杀）——参数名只可能出现在 query 开头或 & 之后。
    """
    return query.startswith(name + "=") or ("&" + name + "=") in query


def is_serp_or_jump_url(url: str) -> bool:
    """是否为搜索引擎结果页 / 跳转壳（不可当信源正文）。"""
    if not url:
        return True
    low = url.lower()
    host = _normalize_domain(url)

    # 白名单：百度子域（百家号、网盘等）不是 SERP，即使 path 为 /s 也不应误杀。
    # 必须最先检查，否则 serp_url_patterns 中的 baidu.com/s? 会误匹配
    # baijiahao.baidu.com/s?id= 百家号文章页。
    _serp_whitelist = ("baijiahao.baidu.com", "pan.baidu.com", "tieba.baidu.com",
                       "zhidao.baidu.com", "wenku.baidu.com")
    host_bare = host[4:] if host.startswith("www.") else host
    if host in _serp_whitelist or host_bare in _serp_whitelist:
        return False

    cfg = _load_cn_source_types()
    for pat in cfg.get("serp_url_patterns") or []:
        if pat.lower() in low:
            return True
    # 显式搜索/跳转模式
    if re.search(r"(?:^|\.)baidu\.com/s\?", low):
        return True
    if "baidu.com/link" in low or "baidu.com/baidu.php" in low:
        return True
    if "sogou.com/link" in low:
        return True
    if "google.com/url?" in low or "google.com.hk/url?" in low:
        return True
    if re.search(r"(bing|google|google\.com\.hk)\.com/search\?", low):
        return True
    if "weixin.sogou.com/weixin" in low:
        return True
    try:
        path = urlparse(url).path or ""
        query = urlparse(url).query or ""
    except Exception:
        path, query = "", ""
    # serp 域名只从 source_types_cn.json 的 serp_host_markers 读。此前这里
    # 还并列写了一份字面量集合，两处各写一份正是漏网的成因：google.co.jp /
    # yahoo.co.jp / duckduckgo.com 的结果页会被当成正文信源（authority 拿
    # 到正常分、并进入最终输出），而 google.com 被拦——同一个概念两种判定。
    #
    # 判定用**后缀匹配**而非等值：同一搜索引擎的入口域名很多（yahoo.co.jp /
    # search.yahoo.co.jp、duckduckgo.com / lite.duckduckgo.com、brave.com /
    # search.brave.com），而 _normalize_domain 只去 www.，子域会原样保留。
    # 等值比较下每上一个新入口就得往表里再补一条——那正是这份表原本在漏的。
    serp_hosts = tuple(cfg.get("serp_host_markers") or ())
    _hit = any(
        host_bare == m or host == m
        or host_bare.endswith("." + m) or host.endswith("." + m)
        for m in serp_hosts
    )
    if _hit:
        # path 只精确比较：此前 `path.startswith("/s")` 会把 pan.baidu.com/s/<id>
        # 网盘分享正文链连坐误杀（实测；中文资源类查询的常见结果）。SERP 的
        # path 恰为 /s，上方精确元组已覆盖，不需要前缀匹配。query 参数走
        # _query_has_param 完整匹配（?pwd= 不再喂进 wd=）。
        if path in ("", "/", "/s", "/web", "/search") \
                or "link" in path or "search" in path \
                or _query_has_param(query, "q") or _query_has_param(query, "wd"):
            return True
    return False


def drop_shell_urls(results: list) -> list:
    """过滤结果列表里的 SERP/跳转壳条目——壳判定的唯一过滤出口。

    为什么必须是本模块的共用函数而不是各处内联：壳判定有多个消费点
    （充分性判定 query_signals——壳不算「够数」；恢复链 L0——壳不算
    「交付」，否则误判恢复成功；L3 恢复结果收口——主链的壳过滤跑在
    恢复链之前，恢复产出是唯一可能把壳带回最终输出的入口）。判定规则
    演进过多次（百度子域白名单、网盘 /s/<id> 误杀），每处内联一份就多
    一处漏改点；2026-10-11 热榜壳链事故的根因之一正是充分性与漏斗
    两处口径漂移。无 url 的结构化快照（行情/天气）不受影响。
    """
    kept = []
    for r in results:
        if not isinstance(r, dict):
            continue
        u = r.get("url")
        if isinstance(u, str) and u and is_serp_or_jump_url(u):
            continue
        kept.append(r)
    return kept


# ── 内容农场与 AI 机翻垃圾站判定（2026-10-23）─────────────────────────────
#
# 依据 source_types_cn.json 的 content_farm_markers 与结构化特征模式，
# 识别并拦截低质搬运镜像、机翻爬虫站及流水线批量 AI 假稿。

_FARM_CONNECTORS = ("首先", "其次", "然后", "接下来", "最后", "综上所述")
_FARM_AI_PHRASES = ("业内人士表示", "专家分析", "消费者认为", "看完这篇你就懂了",
                    "看完你就懂了", "建议收藏", "综上所述")


def is_content_farm(url: str = "", title: str = "", snippet: str = "") -> bool:
    """判定某条目是否属于内容农场 / 机翻爬虫 / 批量流水线 AI 废话。

    2026-10-23 接线前用 5 条正例 + 4 条边界标定，修正两处：
      - 序数连接词单独出现**不判农场**：正常安装指南同样含「首先/其次/然后/
        接下来/最后」（实测 5 个），旧判据 `hit_conn >= 4` 把它误杀了。现在
        要求同时出现 ≥1 个 AI 套话（流水线稿的伴生特征）。
      - 「自费 N 元实测」不再要求标题带「最新」：实测真实的虚假实测标题是
        「自费5000元实测10款耳机」，不带「最新」，旧判据漏召。
    """
    if not guard_enabled():
        return False
    cfg = _load_cn_source_types()
    host = _normalize_domain(url)
    host_bare = host[4:] if host.startswith("www.") else host
    farm_markers = tuple(cfg.get("content_farm_markers") or ())
    if any(host_bare == m or host == m or host_bare.endswith("." + m) or host.endswith("." + m)
           for m in farm_markers):
        return True

    low_url = (url or "").lower()
    for pat in cfg.get("content_farm_url_patterns") or []:
        if pat.lower() in low_url:
            # 仅在非白名单顶级域下生效
            if any(host.endswith(tld) for tld in (".top", ".xyz", ".site", ".cc", ".icu", ".work")):
                return True

    # 标题模式：虚假实测 / 利益诱饵 + 年份刷新
    t = (title or "").strip()
    if t:
        if re.search(r"自费\s*\d+\s*元?实测", t):
            return True
        if re.search(r"\d{4}\s*最新推荐\s*\d+\s*款.*看完这篇你就懂了", t):
            return True

    # 摘要/正文模式：高度僵化的模板流水线
    body = f"{t} {snippet or ''}".strip()
    if body:
        hit_conn = sum(1 for w in _FARM_CONNECTORS if w in body)
        hit_ai = sum(1 for w in _FARM_AI_PHRASES if w in body)
        if hit_conn >= 4 and hit_ai >= 1:
            return True
        if hit_ai >= 2 and hit_conn >= 1:
            return True
        if hit_ai >= 3:
            return True

    return False


def is_unusable_url(url: str, title: str = "", snippet: str = "") -> bool:
    """是否为不可作为信源引用的条目（SERP壳 或 确凿内容农场）。"""
    if is_serp_or_jump_url(url):
        return True
    return is_content_farm(url, title, snippet)


def filter_content_farms(results: list) -> list:
    """过滤结果列表里的内容农场条目。"""
    if not results or not guard_enabled():
        return results or []
    kept = []
    for r in results:
        if not isinstance(r, dict):
            continue
        u = r.get("url") or ""
        t = r.get("title") or ""
        s = r.get("snippet") or ""
        if is_content_farm(u, t, s):
            continue
        kept.append(r)
    return kept

