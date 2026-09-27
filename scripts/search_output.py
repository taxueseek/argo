#!/usr/bin/env python3
"""search_output.py — 输出成形（信源标准化、漏斗、人读格式）。

「结果 → 给谁看」的全部转换都在这里：Agent 档字段裁剪、sources 标准化、
六格漏斗与塌陷归因、阶段耗时表、终端文本格式。与排序层分开的理由：输出契约
有独立门禁（tests/test_output_contract.py），改动它不该牵动排序。
"""

from __future__ import annotations

from dataclasses import dataclass

from search_rank import filter_results_by_domains
from typing import Any


# 本模块的 6 处日志都写在 **except 处理器**里，且分散在 shape_response 的
# 不同阶段。此前其中三处各写了一次裸 `import logging`——CPython 在**编译期**
# 就把 `logging` 判成 shape_response 的局部变量（与那行是否执行到无关），
# 于是其余三处 `logging.getLogger(...)` 全部编译成 LOAD_FAST，走到即
# UnboundLocalError；而它们位于 except 内部，处理器再抛异常会**顶替掉**原本
# 被 fail-open 吞掉的错误——「增强失败不得让搜索失败」变成整次搜索崩溃。
#
# 修法是**一处按需取 logger 的小函数**，全模块统一走它：只在真的要记日志时
# 才 import logging，模块级不留任何绑定。这样既没有局部遮蔽，也不会把
# traceback → dataclasses → inspect → _colorize（CPython 3.13+ 实测 21 ms）
# 拖进 import search 的必经之路——本模块的日志全是 debug，而全仓没有把
# `unified_search` 的 level 调离默认 WARNING，也就是说它们默认永不产生输出。
def _log(message: str) -> None:
    """默认静默的调试出口；仅在真正需要记录时才引入 logging。"""
    import logging
    logging.getLogger("unified_search").debug(message)


_AGENT_RESULT_FIELDS = (
    "title", "url", "snippet", "source", "score", "ref",
    "published_at", "fetch_suggested", "full_text_url",
    "image_url", "image_license",
    "episode_count", "duration_minutes",
    # 本地已有正文：是可操作提示而非观测数据——告诉 Agent 这条不必重新联网，
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

    在默认精简档之上再剥观测标量（tfidf_scores/lang_pref/engine_outcomes 等）
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
    deduped  = 跨引擎合并 + 近重复去重后（SERP/跳转页过滤的损耗已计入本格
               之差，取值点在 SERP 过滤之前、minhash 之后；此前取值点在
               minhash 之后，把 SERP 丢弃也算成「去重削掉了」，会让 0 结果
               的首要诊断入口给出错误归因）
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


# 不算失败的 outcome 状态：这些情况「引擎跑了、没问题」，不该出现在 errors[]
# cancelled = 早停收工时被主动弃置（结果已够，引擎没失败也没超窗）
_NON_ERROR_OUTCOME = frozenset({
    "ok", "ok-cached", "partial", "no-results", "no-results-cached",
    "cancelled",
})


def _collect_errors(raw_results: dict[str, list[dict[str, Any]]],
                    engine_outcomes: list[dict[str, Any]] | None = None
                    ) -> list[str]:
    """收集失败文本，两个来源缺一不可。

    1. raw_results 里的 error 条目——引擎把失败**当成结果**返回（异常被
       `_exec_engine` 捕获后塞进列表）。
    2. engine_outcomes 里带 detail 的失败 outcome——引擎内部**吞掉**异常，
       只把失败原因写进记录（engines_base.note_failure），列表是空的。

    只收第 1 类时，`--engine you` 的 SSL 超时会上报成
    `status=completed, count=0, errors=[]`：调用方（Agent）据此判定「网上
    没有这个信息」并停止追问，而真相是引擎连不上（2026-09-15 实测）。
    去重按整行，避免同一失败既来自 error 条目又来自 outcome detail。
    """
    errors: list[str] = []
    seen: set[str] = set()

    def _add(line: str) -> None:
        if line and line not in seen:
            seen.add(line)
            errors.append(line)

    for eng, res in raw_results.items():
        for r in res:
            if isinstance(r, dict) and "error" in r:
                _add(f"{eng}: {r['error']}")
    for o in (engine_outcomes or []):
        if not isinstance(o, dict):
            continue
        detail = str(o.get("detail") or "").strip()
        if not detail or str(o.get("status") or "") in _NON_ERROR_OUTCOME:
            continue
        _add(f"{o.get('engine')}: {detail}")
    return errors

@dataclass(frozen=True)
class _ShapeContext:
    """输出成形的全部选项（请求侧只读 + 一个本地检索钩子）。"""

    query: str
    kind: str
    tier: str
    envelope: bool
    decision: dict[str, Any]
    extra_lim: list[str]
    cache: Any
    include_domains: list[str]
    exclude_domains: list[str]
    include_local: bool
    n: int
    run_local_seek: Any


def shape_response(ctx: _ShapeContext, result: dict[str, Any]) -> dict[str, Any]:
    """把执行层载荷整理成对外响应（就地改 result 并返回它）。

    七个阶段，顺序即契约：档位局限 → envelope/局限声明 → 本地正文索引 →
    证据门控（fetch_required / evidence_loop）→ 域过滤 → 信源标准化 → 本地命中并入。
    每一步都 fail-open（增强失败不得让一次搜索失败），因此每段自带 try。

    住在 search_output 而不是 search.py：这是**响应契约**（字段形态、什么时候
    生成 sources、envelope 与精简档的差异），与执行/调度无关。
    """
    query = ctx.query
    kind, tier = ctx.kind, ctx.tier
    envelope = ctx.envelope
    decision = ctx.decision
    extra_lim = ctx.extra_lim
    cache = ctx.cache
    include_domains, exclude_domains = ctx.include_domains, ctx.exclude_domains
    include_local, n = ctx.include_local, ctx.n
    _run_local_seek = ctx.run_local_seek
    if tier == "daily":
        extra_lim.append(
            "daily tier: direct search; no pre-confirm gate"
        )
    elif tier == "professional":
        extra_lim.append(
            "professional tier: plan metadata attached; verify top-k before hard claims"
        )

    # 候选交接包（附加字段，不改 results 排序）
    if envelope:
        try:
            from candidate_envelope import attach_envelope
            attach_envelope(
                result,
                query=query,
                input_kind=kind,
                route_reason=decision.get("reason"),
                extra_limitations=extra_lim,
            )
        except Exception as e:
            _log(f"envelope 跳过: {type(e).__name__}")
            result.setdefault("schema_version", "1.0")
            result.setdefault("limitations", [])
    else:
        # 精简档：attach_envelope 不跑，局限声明仍须上报（同一个 build_limitations
        # 实现，避免两处各写一份导致计算方式漂移）
        try:
            from candidate_envelope import build_limitations
            result["limitations"] = build_limitations(result, extra_lim)
        except Exception:
            result.setdefault("limitations", list(extra_lim))

    # 本地正文索引：给每条结果标出「这篇的正文我已经取回过」，并给出全文位置。
    # 结果原本只有 300 字摘要与核验分，调用方看不出本地已有正文——想核对原文
    # 只能重新 fetch，或者压根不知道能回看。这里是纯本地查询（冷 L1 下 20 条
    # 约 1.7 ms），不联网、不额外请求，故无条件附加。
    #
    # `_local` 在 try 外初始化：cache.local_status() 抛异常（SQLite 锁/库损坏）
    # 时下面 :469 的 blocked 筛选仍要读它——try 内首次赋值会让那条路径吃
    # NameError，被外层 except 吞掉后「不可取源不再建议核验」这段增强静默
    # 永不执行，日志还把原因误报成「不可取源筛选跳过: NameError」。
    _local: dict[str, Any] = {}
    try:
        _urls = [r.get("url") for r in (result.get("results") or [])
                 if isinstance(r, dict) and r.get("url")]
        _local = cache.local_status(_urls) if _urls else {}
        for _r in (result.get("results") or []):
            if not isinstance(_r, dict):
                continue
            _st = _local.get(_r.get("url") or "")
            if not _st:
                continue
            if _st.get("body"):
                _r["local_body"] = _st["body"]
            if _st.get("retrieval"):
                # 取数可用性：把「取不到」（系统类）与「取到了但没用」（内容类）
                # 分开报，两者都不等于「还没试过」。搜索阶段就能判定的事，不该
                # 留给调用方抓一次才知道。
                _r["retrieval"] = _st["retrieval"]
    except Exception as _e:
        _log(f"本地正文索引跳过: {type(_e).__name__}")

    # 证据完整链路 P0：回填已核验证据分 + 高后果门控（finance/health/legal）
    # 输出 fetch_required / evidence_loop 汇总，每条结果带 fetch_suggested
    # 与 has_fetched_evidence / post_fetch_absorption（若此前 fetch 过）。
    try:
        from evidence_loop import gate_results
        # cache 必须传下去：gate_results → backfill_results → lookup_fetch_evidence
        # 对每条结果 URL 查证据缓存。不传时每条结果各新建一个 SearchCache——
        # 每次 _init_db（connect + 3 PRAGMA + executescript + 2×table_info）
        # 再加一次整页 fetch 条目的 gunzip+json.loads，只为取其中一个子键。
        # 默认 n=5 即 5–20 ms/次搜索纯浪费（缓存命中的整次搜索只要 ~26 ms）。
        # ctx.cache 就在同函数上方 :431 刚用过，递一下参数即可。
        gate = gate_results(result.get("results") or [], result.get("domain"),
                            cache=cache)
        result["fetch_required"] = gate["fetch_required"]
        result["evidence_loop"] = {
            "high_consequence_domain": gate["high_consequence_domain"],
            "suggested": gate["suggested"],
            "verified_count": gate["verified_count"],
            "pending_count": gate["pending_count"],
        }
        # 已知取不到的源不再「建议核验」——建议了也只会白跑一趟。
        # 必须放在 gate_results 之后：它才是 fetch_suggested 的产出方。
        # 不改「该不该核验」的判断，只去掉注定徒劳的那部分，并把原因写清楚，
        # 否则调用方会把「未建议」误读成「不必核验」。
        try:
            _blocked = []
            _keep = []
            for _u in (result["evidence_loop"].get("suggested") or []):
                _hit = _local.get(_u) or {}
                if (_hit.get("retrieval") or {}).get("status") == "blocked":
                    _blocked.append(_u)
                else:
                    _keep.append(_u)
            if _blocked:
                result["evidence_loop"]["suggested"] = _keep
                result["evidence_loop"]["unretrievable"] = _blocked
                result["evidence_loop"]["pending_count"] = max(
                    0, int(result["evidence_loop"].get("pending_count") or 0)
                    - len(_blocked))
                for _r in (result.get("results") or []):
                    if isinstance(_r, dict) and _r.get("url") in _blocked:
                        _r["fetch_suggested"] = False
                        _r["fetch_blocked"] = (_local[_r["url"]]
                                               .get("retrieval") or {}).get("reason")
        except Exception as e:
            _log(f"不可取源筛选跳过: {type(e).__name__}")
    except Exception as e:
        _log(f"证据门控跳过: {type(e).__name__}")

    # 域过滤（后置，引擎无关）：融合排序之后裁剪，sources 与 results 保持一致。
    # 裁剪导致不足 n 条是调用方过滤条件的诚实结果，不回填。
    if include_domains or exclude_domains:
        try:
            kept, note = filter_results_by_domains(
                result.get("results"), include_domains, exclude_domains)
            result["results"] = kept
            if note:
                result["domain_filter"] = note
        except Exception as e:
            _log(f"[domain-filter] {type(e).__name__}: {e}")

    # 相关信源标准化（日常搜索底部引用列表；与 results 顺序一致）。
    # sources 是 results 的降级投影（URL 100% 重叠，实测零信息增量），
    # --no-envelope（Agent 默认输出）下不再生成：每次调用省 ~0.9KB，
    # 要 provenance 时用 envelope 模式或 --archive（2026-09-13 审查 P2-1）。
    if envelope:
        result["sources"] = build_sources(result.get("results") or [])

    # 本地命中并入（默认关）：seek 结果尾部拼入，来源 local_files，
    # 不参与融合评分。仅显式开启（--include-local / MCP include_local）才触发。
    if include_local:
        try:
            local_hits = _run_local_seek(query, n)
        except Exception as e:
            local_hits = []
            _log(f"[include-local] {type(e).__name__}: {e}")
        if local_hits:
            result.setdefault("results", []).extend(local_hits)
            result["local_results"] = local_hits
        result["include_local"] = True
    # ── 收口：重算一切「由 results 派生」的字段 ────────────────────────────────
    #
    # why：本函数**后置**改 results 的地方不止一处（域过滤裁剪、本地命中并入），
    # 而 count / evidence_loop 的更早版本是按改之前的 results 算的。实测
    # `--include-domains example.com` 得到 `count=4` 而 `results=[]`——JSON 消费
    # 者会以为拿到了 4 条答案，而下游按 count 循环就会取到空气。
    #
    # 这类「改了 results、忘了改派生字段」的缺陷根治不了，只能靠位置根治：把
    # 重算放在**唯一出口**，任何后置改写都自动被覆盖，新增后置阶段也不必记得
    # 来这里补一行。判定以 results 为唯一真值来源，而不是把旧值加减修正——
    # 后者在多次改写叠加时必然漂移。
    result["count"] = len(result.get("results") or [])
    _el = result.get("evidence_loop")
    if isinstance(_el, dict):
        _live = {r.get("url") for r in (result.get("results") or [])
                 if isinstance(r, dict)}
        _sug = [u for u in (_el.get("suggested") or []) if u in _live]
        if len(_sug) != len(_el.get("suggested") or []):
            # suggested 是「建议核验的 URL」，被域过滤裁掉的 URL 不该再出现在
            # 这里；pending_count 的语义就是 len(suggested)（见 evidence_loop.
            # gate_results 的返回），两者必须同步，否则会出现「建议 4 条、
            # 待核验 0 条」这类自相矛盾的口径。
            _el["suggested"] = _sug
            _el["pending_count"] = len(_sug)
    return result
