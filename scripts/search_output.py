#!/usr/bin/env python3
"""search_output.py — 输出成形（信源标准化、漏斗、人读格式）。

「结果 → 给谁看」的全部转换都在这里：Agent 档字段裁剪、sources 标准化、
六格漏斗与塌陷归因、阶段耗时表、终端文本格式。与排序层分开的理由：输出契约
有独立门禁（tests/test_output_contract.py），改动它不该牵动排序。
"""

from __future__ import annotations

from typing import Any


_AGENT_RESULT_FIELDS = (
    "title", "url", "snippet", "source", "score", "ref",
    "published_at", "fetch_suggested", "full_text_url",
    "image_url", "image_license",
    "episode_count", "duration_minutes",
    # 本地已有正文：是可操作提示而非遥测——告诉 Agent 这条不必重新联网，
    # 以及全文在哪（被截断时还给出 full_text_path）。剥掉它等于把「随时
    # 核对原文」这条路径从 Agent 视野里藏起来，而它正是 Agent 最需要的。
    # 只在确有本地正文时出现，单条约 60 字节。
    "local_body",
    # 取数可用性：取不到（系统类）/ 取到了但没用（内容类）。与 local_body 同源，
    # 都是可操作提示。剥掉等于让调用方抓一次才知道结果，正是它要避免的事。
    "retrieval", "fetch_blocked",
    # 可验证出处：技能目录源给的是市场页，upstream 才是能核对/能安装的地址
    # （上游仓目录或 owner/slug 安装引用）。剥掉等于把「搜到的这条到底是什么」
    # 重新变成一次额外的浏览器往返。
    "upstream",
)


def _strip_for_agent(payload: dict[str, Any]) -> dict[str, Any]:
    """--fields agent：输出只留答案内容（P2-2，2026-09-13）。

    在默认精简档之上再剥遥测标量（tfidf_scores/lang_pref/engine_outcomes 等）
    与 null/空键。fetch_required 必须保留——SKILL.md 的高后果门控纪律依赖它，
    不能被瘦身掉；funnel 同理保留，它是 agent 判「0 结果卡在哪一层」的唯一依据。
    """
    keep_top = (
        "query", "engine", "engines", "engines_used", "domain", "count",
        "mode", "depth", "status", "fetch_required", "evidence_loop",
        "errors", "login_hint",
        # 阶段漏斗账：约 70 字节，换来「这次为什么只有这么几条」的可归因性。
        # 它是 agent 档里唯一能回答「0 结果卡在哪一层」的东西，不剥。
        "funnel",
        # 质量信号：局限声明与告警必须随答案一起到达。此前 agent 档把
        # limitations/recovery/time_filter_warning 一并剥掉，agent 无从判断
        # 「这批结果能用到什么程度」（2026-09-15 输出契约审查）。
        "limitations", "recovery", "time_filter_warning",
        # 阶段耗时：默认就带（--no-timing 才没有）。剥掉会让使用者看不到
        # 「这次慢在哪」，也拿不到自己动手优化所需的依据。
        "timing",
    )
    out: dict[str, Any] = {k: payload[k] for k in keep_top
                           if payload.get(k) is not None}
    slim_results = []
    for r in payload.get("results") or []:
        if not isinstance(r, dict):
            continue
        slim = {k: r[k] for k in _AGENT_RESULT_FIELDS if r.get(k) is not None}
        slim_results.append(slim)
    out["results"] = slim_results
    out["count"] = len(slim_results)
    return out


def build_funnel(routed: int, called: int, returned: int,
                 deduped: int, filtered: int, kept: int) -> dict[str, int]:
    """阶段漏斗账：一次调用在管线每一层还剩多少条。

    why（GLM 密集反馈那篇的核心）：端到端指标只能说明「变差了」，说明不了
    **在哪一层**变差。同一句「0 结果」背后至少有四种病——路由没选到能答的引擎、
    引擎返回了但去重削没了、否定词/时间窗过滤压到 0、精排阶段全被剔掉。
    这些数字此前散在 minhash_removed / excluded_count / time_filtered / count
    里，读者得自己拿管线知识把它们按顺序拼起来，拼错就会误判。

    六格按管线顺序（routed → called → returned → deduped → filtered → kept），
    相邻两格的差值就是该层的损耗，「哪一格塌了」一眼可见。

    routed   = 路由选出的引擎数（engines_combo）
    called   = 实际发起调用的引擎数（早停时小于 routed）
    returned = 引擎返回的原始条数（跨引擎去重前）
    deduped  = 跨引擎合并 + 近重复去重后
    filtered = 否定词过滤 + 时间窗过滤后
    kept     = 最终输出条数
    """
    return {"routed": routed, "called": called, "returned": returned,
            "deduped": deduped, "filtered": filtered, "kept": kept}


FUNNEL_STAGES = ("routed", "called", "returned", "deduped", "filtered", "kept")


def describe_funnel(funnel: dict[str, Any] | None) -> str:
    """把漏斗压成一行 `routed 2→called 2→returned 0→…`（给局限声明用）。"""
    if not isinstance(funnel, dict):
        return ""
    return "→".join(f"{k} {funnel.get(k)}" for k in FUNNEL_STAGES
                    if k in funnel)


def funnel_collapse(funnel: dict[str, Any] | None) -> str | None:
    """返回漏斗里第一个被打到 0 的层名；没有 0 就返回 None。

    这是「0 结果」的归因答案：结果在 `returned` 归零 = 引擎没抓到；
    在 `deduped` 归零 = 抓到了但被当重复削掉；在 `kept` 归零 = 被过滤/截断压没。
    三者的处置完全不同，混成一句「没有结果」就没法据此行动。
    """
    if not isinstance(funnel, dict):
        return None
    for name in FUNNEL_STAGES:
        if funnel.get(name) == 0:
            return name
    return None


def _slow_query_ttl(base_ttl: int, elapsed_ms: int) -> int:
    """慢查询的缓存 TTL：按耗时延长，上限一律 2× base。

    慢查询值得多缓存一会儿——省的是「同一查询再付一次慢网」的钱。但上限
    必须存在：此前这条逻辑写成 if/else 两支，`base_ttl > 900` 的 else 支
    **没有上限**，而 multiplier 最大 8。实测后果：evergreen 档
    （image_search / geo_places / book_search，base=86400s）的慢查询拿到
    691200s＝**8 天** TTL，慢查询结果以「新鲜」的样子交付一整周；注释一直
    写的是「时效域最多 2×」，代码只有一半兑现。

    顺带修掉的冗余：原 ≤900 支写 `min(b * min(m, 2), b * 2)`，而它对任意
    b、m 恒等于 `b * min(m, 2)`（外层 min 永远取不到第二个参数），两支本就
    等价。合并后 TTL 延长规则只剩这一个定义点，也可被测试直接打到。

    单独成函数是为了让回归测试调用**真实实现**——把公式抄进测试的写法，
    实现回退时测试照样绿，等于没锁。
    """
    multiplier = min(2 ** (max(0, elapsed_ms) // 2000), 8)
    return base_ttl * min(multiplier, 2)


def build_sources(results: list[Any] | None) -> list[dict[str, Any]]:
    """将 results 投影为编号信源列表（传统搜索引擎底部「相关链接」形态）。

    规则：
      - ref 与列表序号一致，从 1 起
      - 无 URL 的条目跳过（不占号？——保留占位会错位；跳过并重编号）
      - 字段齐全便于 Agent/归档复用，不伪造 metrics
    """
    sources: list[dict[str, Any]] = []
    ref = 0
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url:
            continue
        ref += 1
        sources.append({
            "ref": ref,
            "title": (r.get("title") or "")[:160],
            "url": url,
            "engine": r.get("source") or r.get("_engine") or r.get("engine"),
            "score": r.get("score"),
            "snippet": ((r.get("snippet") or "")[:160] or None),
        })
    return sources


def format_timing(t: dict[str, Any]) -> str:
    """人读格式的阶段耗时表（`--explain-timing`，非 JSON 分支）。"""
    lines = ["", "=== 阶段耗时 ==="]
    for row in t.get("stages") or []:
        lines.append(f"  {row['stage']:<14}{row['ms']:>9.1f} ms  {row['pct']:>5.1f}%")
    d = t.get("dispatch") or {}
    if d:
        lines.append(
            f"  引擎并发        墙钟 {d.get('wall_ms')} ms / 引擎合计 "
            f"{d.get('engine_sum_ms')} ms → 并发效率 "
            f"{d.get('parallel_efficiency')}（跑了 {d.get('engines_run')} 个，"
            f"早停={d.get('early_stopped')}，浪费 {d.get('wasted_ms')} ms）")
    if "overhead_ms" in t:
        lines.append(
            f"  固定开销        {t['overhead_ms']} ms"
            f"（其中 import {t.get('import_ms')} ms；不含解释器自身启动）")
        lines.append(f"  进程总计        {t.get('process_ms')} ms")
    return "\n".join(lines)


def format_text_output(results: dict[str, Any]) -> str:
    """日常搜索人读格式：条目正文 + 底部「相关信源」链接（类传统 SERP）。"""
    lines = []
    if results.get("status") == "handoff_required":
        ho = results.get("handoff") or {}
        lines.append("=== HANDOFF (known-url, search skipped) ===")
        lines.append(f"  url: {ho.get('url')}")
        lines.append(f"  suggest: {', '.join(ho.get('suggested_tools') or [])}")
        for lim in (results.get("limitations") or [])[:4]:
            lines.append(f"  ! {lim}")
        return "\n".join(lines)
    if results.get("status") in ("ready",) and results.get("steps") and not results.get("results"):
        # plan-only
        lines.append(f"=== PLAN {results.get('status')} kind={results.get('input_kind')} ===")
        route = results.get("route") or {}
        lines.append(f"  engine={route.get('backend')} domain={route.get('domain')} combo={route.get('engines_combo')}")
        for lim in (results.get("limitations") or [])[:5]:
            lines.append(f"  ! {lim}")
        return "\n".join(lines)

    count = results.get("count", 0)
    elapsed = results.get("elapsed_ms", 0)
    engine = results.get("engine", "?")
    cached = results.get("cached", False)
    cache_level = results.get("cache_level", "")
    domain = results.get("domain", "")
    mode = results.get("mode", "auto")

    header = f"=== {count} results ({elapsed}ms via {engine})"
    if cached:
        header += f" [CACHE {cache_level}]"
    elif domain:
        header += f" [domain:{domain}]"
    if mode != "auto":
        header += f" [mode:{mode}]"
    if results.get("input_kind"):
        header += f" [kind:{results.get('input_kind')}]"
    lines.append(header)

    for err in results.get("errors", [])[:3]:
        lines.append(f"  [ERROR] {err}")

    # 正文区：编号 + 标题 + 摘要（链接沉底，避免噪声）
    sources = results.get("sources")
    if not isinstance(sources, list) or not sources:
        sources = build_sources(results.get("results") or [])

    # 用 URL 保持一致 ref
    url_to_ref = {s.get("url"): s.get("ref") for s in sources if isinstance(s, dict)}
    body_items = [r for r in (results.get("results") or []) if isinstance(r, dict)]
    for r in body_items:
        url = (r.get("url") or "").strip()
        ref = url_to_ref.get(url)
        if ref is None:
            # 未进 sources 时临时编号。分两种：有 URL 但没被收进 sources
            # （编号未知，用 ?）；以及**本就没有 URL** 的条目——天气/行情/
            # 宏观这类结构化快照走的就是这条。此前两种情况共用一个分支，
            # 且分支条件是 `and url`，于是无 URL 的条目 ref 保持 None，
            # 正文里直接打成 `[None]`（2026-09-19 实测「北京天气」复现）。
            ref = "?" if url else "—"
        score = r.get("score", 0)
        title = (r.get("title") or "?")[:80]
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) and score else "—"
        lines.append(f"  [{ref}] {title}")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            lines.append(f"      {snippet[:140]}")
        elif score:
            lines.append(f"      (score={score_s})")

    # 底部相关信源（传统搜索引擎形态）
    if sources:
        lines.append("")
        lines.append("── 相关信源 ──")
        for s in sources:
            if not isinstance(s, dict):
                continue
            ref = s.get("ref", "?")
            eng = s.get("engine") or ""
            title = (s.get("title") or "")[:60]
            url = s.get("url") or ""
            eng_s = f" · {eng}" if eng else ""
            if title:
                lines.append(f"  [{ref}] {title}{eng_s}")
                if url:
                    lines.append(f"      {url}")
            elif url:
                lines.append(f"  [{ref}] {url}{eng_s}")

    if results.get("limitations"):
        lines.append("")
        lines.append("── limitations ──")
        for lim in results["limitations"][:4]:
            lines.append(f"  ! {lim}")

    return "\n".join(lines)
