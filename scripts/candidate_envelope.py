#!/usr/bin/env python3
"""
candidate_envelope.py — 候选交接包（统一候选交接 schema）

在保留 Argo 原有 results[] 的前提下，附加：
  - candidates[]  统一字段 + verification/provenance
  - coverage[]    每后端返回/截断/局限
  - limitations[] 全局局限
  - input_kind / schema_version

设计原则：
  - 纯后处理，不改检索结果排序
  - metrics 缺失写 null，不用 0 伪造
  - snippet 仅作线索（verification.status 默认 candidate）
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse

_TZ_CN = timezone(timedelta(hours=8))

# 技能目录源（engines/specs/*.yaml 里声明 `coverage: skill` 的那些，加上走
# 自定义引擎的 redskill）。这里用常量而非运行时读 spec：本函数在输出路径上，
# config.yaml 有 130KB+，为一句局限声明去解析它是拿延迟换措辞。
# 与 spec 的一致性由 tests/test_skill_registry_contract.py 断言，不靠人记。
SKILL_REGISTRY_ENGINES = frozenset({"redskill", "skillsmp", "clawhub"})

SKILL_REGISTRY_LIMITATION = (
    "Skill directory hits are marketplace listings, not verified packages; "
    "check the upstream reference before installing."
)


def canonicalize_url(url: str) -> str:
    """URL 归一化（薄转发到 url_canon 唯一来源）。

    本函数曾自带一份较短的追踪参数表（缺 share_token/spm 族等），与
    search/plan/research_dossier 的实现不一致；现统一到 url_canon。
    """
    from url_canon import canonical_url as _impl
    return _impl(url)


def _candidate_id(platform: str, url: str, source_id: str | None = None) -> str:
    if source_id:
        return f"{platform}:{source_id}"
    raw = canonicalize_url(url) or url or ""
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{platform}:{h}"


def _platform_of(url: str, source: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "github.com" in host:
        return "github"
    if "zhihu.com" in host:
        return "zhihu"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xiaohongshu"
    if "bilibili.com" in host:
        return "bilibili"
    if "weibo.com" in host:
        return "weibo"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if host in {"x.com", "twitter.com"} or host.endswith(".x.com"):
        return "x"
    if "mp.weixin.qq.com" in host:
        return "wechat"
    if source and source.startswith("local_"):
        return "web"
    return "web"


def _login_state_of(item: dict[str, Any], source: str = "") -> bool:
    """结果是否使用了登录态（ego-browser / 显式 provenance）。"""
    if item.get("login_state_used") is True:
        return True
    if item.get("cache_eligible") is False:
        return True
    auth = item.get("auth_partition")
    if isinstance(auth, str) and auth.lower().startswith("login"):
        return True
    blob = " ".join(
        str(x) for x in (
            source,
            item.get("source"),
            item.get("_engine"),
            item.get("engine"),
            item.get("backend"),
            item.get("fetch_method"),
        )
        if x
    ).lower()
    return "ego-browser" in blob or "ego_browser" in blob or "browser_api" in blob


def verification_of(item: dict[str, Any]) -> dict[str, Any]:
    """核验状态：这条候选是「只有 snippet 线索」还是「原文已取到」。

    历史 bug（2026-09-19）：这里硬编码 `opened_original: False`（注释还写着
    「snippet 线索，未打开原文」），于是**同一份载荷自相矛盾**——已抓过正文的
    结果行上明明带着 `local_body`（search 的本地正文回填）或
    `has_fetched_evidence` / `post_fetch_absorption`（evidence_loop 的证据
    回填），candidate 投影却声明「未打开原文」。下游按 verification 判断
    「能不能把这条当事实」时，拿到的答案与载荷本身相反。

    判据是「这条 URL 的原文已经被取到过」，三个等价证据任一成立即可：
    `local_body`（本机正文在手）、`has_fetched_evidence`（已核验证据）、
    `post_fetch_absorption`（正文级吸收分）。三者都由取数链路写在同一行上，
    不额外联网、不额外读盘。
    """
    opened = bool(
        item.get("local_body")
        or item.get("has_fetched_evidence")
        or item.get("post_fetch_absorption") is not None
    )
    return {
        "status": "opened" if opened else "candidate",
        "opened_original": opened,
        "checked_at": None,
    }


def result_to_candidate(
    item: dict[str, Any],
    query: str,
    rank: int,
    route_reason: str | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    url = item.get("url") or ""
    source = item.get("source") or item.get("_engine") or ""
    platform = _platform_of(url, source)
    canon = canonicalize_url(url) or url
    social = item.get("social_meta") if isinstance(item.get("social_meta"), dict) else {}
    metrics = {
        "likes": social.get("likes") if social else None,
        "comments": social.get("comments") if social else None,
        "collects": social.get("collects") if social else None,
        "shares": social.get("shares") or social.get("retweets"),
        "views": social.get("views") or social.get("play"),
    }
    # 明确后端给了 0 才保留 0；否则 null
    for k, v in list(metrics.items()):
        if v is None:
            metrics[k] = None
        elif v == "":
            metrics[k] = None

    retrieved = retrieved_at or datetime.now(_TZ_CN).isoformat()
    login_used = _login_state_of(item, source)
    limitations = ["snippet is a discovery clue, not verified body text"]
    if login_used:
        limitations.append("login_state_used: not eligible for public SearchCache")
    return {
        "candidate_id": _candidate_id(platform, url),
        "query": query,
        "platform": platform,
        "backend": source or "unknown",
        "rank": rank,
        "title": item.get("title") or "",
        "url": url,
        "canonical_url": canon,
        "snippet": (item.get("snippet") or "")[:300],
        "author": social.get("author") if social else None,
        "published_at": item.get("published_at") or social.get("published_at"),
        "content_type": "social_post" if social else "web_page",
        "language": None,
        "metrics": metrics,
        "access": {
            "visibility": "authenticated" if login_used else "public",
            "login_state_used": login_used,
        },
        "verification": verification_of(item),
        "provenance": {
            "source_id": social.get("id") if social else None,
            "retrieved_at": retrieved,
            "route_reason": route_reason,
            "score": item.get("score"),
            "credibility_fast": item.get("credibility_fast"),
            "selection": item.get("selection"),
            "absorption": item.get("absorption"),
            "consensus_engines": item.get("consensus_engines"),
        },
        "limitations": limitations,
    }


def build_coverage(engine_outcomes: list[dict[str, Any]] | None, max_results: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in engine_outcomes or []:
        status = o.get("status") or "unknown"
        n = int(o.get("results_count") or 0)
        eng = o.get("engine") or ""
        login_used = bool(o.get("login_state_used")) or _login_state_of(
            o if isinstance(o, dict) else {}, str(eng)
        )
        out.append({
            "backend": o.get("engine"),
            "status": status,
            "returned": n,
            "truncated": n >= max_results if n else False,
            "latency_ms": o.get("latency_ms"),
            "login_state_used": login_used,
            "detail": o.get("detail"),
            "limitations": (
                ["empty or failed backend"]
                if status not in ("ok", "ok-cached", "partial")
                else []
            ),
        })
    return out


def _route_login_used(
    search_result: dict[str, Any],
    candidates: list[dict[str, Any]] | None = None,
) -> bool:
    """本次结果是否用到登录态（决定能否写入公共缓存）。

    candidates 只在 envelope 路径可得；精简路径传 None，其余判据同样成立。
    """
    return bool(
        search_result.get("login_state_used") is True
        or search_result.get("cache_eligible") is False
        or _login_state_of(search_result, str(search_result.get("engine") or ""))
        or any(
            (c.get("access") or {}).get("login_state_used")
            for c in (candidates or [])
        )
    )


def build_limitations(
    search_result: dict[str, Any],
    extra_limitations: list[str] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> list[str]:
    """结果局限声明——agent 判断「这批结果能用到什么程度」的依据。

    这是**质量信号，与归档开关无关**：envelope 模式与精简模式共用本实现，
    避免两处各写一份导致计算方式漂移（本仓对「同一件事写两遍」的一贯态度）。

    2026-09-15 输出契约审查发现：精简档（--no-envelope，文档推荐给 agent
    的档位）此前整块拿不到局限声明，于是 agent 无从知晓自己拿到的是
    「相关发现而非正文」「降级路由结果」「未预确认的 daily 档」——把单次
    归档开关的副作用，变成了日常路径的质量损失。
    """
    limitations = list(extra_limitations or [])
    limitations.append("Do not treat engagement metrics as factual correctness.")
    # 有 funnel（阶段漏斗账）时，早停说明由 search 侧给出带 called/routed 数字的
    # 版本，此处不再说一遍——同一件事两条表述会互相削弱（读者该信哪条？）。
    # 无 funnel 的情况（引入漏斗之前写入的缓存条目）仍由这里兜底。
    if search_result.get("early_stopped") \
            and not isinstance(search_result.get("funnel"), dict):
        limitations.append("early_stopped: later engines in combo may not have run.")
    if search_result.get("recovery"):
        limitations.append("recovery path used; results may come from fallback engines.")
    if search_result.get("cached"):
        limitations.append(
            f"served from cache level={search_result.get('cache_level')}")
    # 软命中必须单独说一句：它复用的是**另一条查询**的载荷，不是本查询的结果。
    # 只说 cache level=L2 不足以让 agent 判断这一点，会把别人的结果当自己的。
    if search_result.get("semantic_hit"):
        limitations.append(
            "semantic cache hit: results are reused from a similar query "
            f"({search_result.get('semantic_query')!r}, "
            f"similarity={search_result.get('semantic_similarity')}), "
            "not from this exact query.")
    if _skill_registry_used(search_result):
        limitations.append(SKILL_REGISTRY_LIMITATION)
    if _route_login_used(search_result, candidates):
        limitations.append("login_state_used: do not write to public SearchCache")
    return limitations


def _skill_registry_used(search_result: dict[str, Any]) -> bool:
    """本次结果里有没有技能目录源的贡献。

    只看真正跑出结果的引擎（engines_used），不看 combo——combo 里挂了但
    失败/未运行的源不该让「这条结果是市场页」的提示出现。
    """
    names: set[str] = set()
    used = search_result.get("engines_used")
    if isinstance(used, list):
        names.update(str(x) for x in used)
    for key in ("engine", "engines"):
        val = search_result.get(key)
        if isinstance(val, str) and val:
            names.add(val)
        elif isinstance(val, list):
            names.update(str(x) for x in val)
    return bool(names & SKILL_REGISTRY_ENGINES)


def attach_envelope(
    search_result: dict[str, Any],
    *,
    query: str | None = None,
    input_kind: str = "keyword",
    route_reason: str | None = None,
    extra_limitations: list[str] | None = None,
) -> dict[str, Any]:
    """在原 search 结果上附加 envelope 字段（原地扩展并返回）。"""
    q = query or search_result.get("query") or ""
    results = search_result.get("results") or []
    retrieved = datetime.now(_TZ_CN).isoformat()
    candidates = [
        result_to_candidate(
            r, q, rank=i + 1,
            route_reason=route_reason or search_result.get("domain"),
            retrieved_at=retrieved,
        )
        for i, r in enumerate(results)
        if isinstance(r, dict) and "error" not in r
    ]
    max_results = int(search_result.get("count") or len(results) or 5)
    coverage = build_coverage(search_result.get("engine_outcomes"), max_results=max_results)

    route_login = _route_login_used(search_result, candidates)
    limitations = build_limitations(search_result, extra_limitations, candidates)

    # 去重：canonical_url
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for c in candidates:
        key = c.get("canonical_url") or c.get("url") or c.get("candidate_id")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    search_result["schema_version"] = "1.0"
    search_result["input_kind"] = input_kind
    search_result["candidates"] = deduped
    search_result["coverage"] = coverage
    search_result["limitations"] = limitations
    # 兼容候选交接最小交付
    search_result.setdefault("routes", [{
        "platform": "web",
        "backend": search_result.get("engine"),
        "engines": search_result.get("engines_combo") or search_result.get("engines"),
        "mode": search_result.get("mode"),
        "login_state_used": bool(route_login),
        "status": "completed" if not search_result.get("errors") else "partial",
        "limitations": limitations[:3],
    }])
    return search_result
