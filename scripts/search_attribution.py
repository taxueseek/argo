"""search_attribution.py — 引擎级耗时归因（可观测性，不参与检索决策）。

为什么独立成模块：主管线 search_pipeline.py 已到模块大小门禁的祖父上限
（1030 行，见 tests/test_module_size_gate.py），归因逻辑塞进它会让
`test_grandfathered_files_do_not_grow` 变红——门禁要的是「拆」，不是
「把数字往上抬」。抽出来同时得到一件好事：提取与聚合都是纯函数，可以
脱离编排单测，不必构造 _SearchRun 才能验证。

它解决的问题（2026-10-15 实录）：usage_log 的 query 流此前只记
engines_used（谁参与了），不记各自花了多久，于是「哪个源吃掉了这次查询」
只能靠人离线重算日志——1059 条真实查询的瓶颈（local_search 独跑 109 次、
占全部墙钟 369s）就是这样翻出来的。engine_latency 本来就在编排里，
把它按「谁最慢」取前 N 写进日志，瓶颈就该由系统自己报出来。

口径说明：engine_latency[eng] 是**单个引擎自身的耗时**，并发时它可能大于
整次查询的墙钟（elapsed_ms）——这不是错误，是「并行等待」的正常形态，
排序与归因只看相对大小，不与之做减法。
"""

from __future__ import annotations

from typing import Any, Iterable

import engine_states  # 状态词表唯一真源（2026-11-09 收口）

# 写进日志的慢源条数：日志是 append-only JSONL，超限会回缩，
# 慢源恒在前排，Top-3 足够定位瓶颈而不必留全量底。
DEFAULT_TOP_N = 3


def top_engine_latency(engine_latency: dict[str, Any] | None,
                       top_n: int = DEFAULT_TOP_N) -> list[list[Any]]:
    """把 {引擎: 耗时ms} 取最慢 top_n，输出 [[引擎, 耗时ms], ...]（降序）。

    紧凑成二元组而非对象：日志每行只多 ~60B，且读侧一眼可读。
    非数值耗时（None / 字符串 / NaN 形态）一律跳过——归因字段不该因为
    一个畸形引擎把整条日志写崩（写入侧还有 SHAPE_BENIGN 兜底，双保险）。
    稳定排序键含引擎名，保证同耗时引擎的次序在重复运行间确定。
    """
    if not engine_latency:
        return []
    pairs = [(e, round(v)) for e, v in engine_latency.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)]
    pairs.sort(key=lambda kv: (-kv[1], str(kv[0])))
    return [[e, v] for e, v in pairs[:max(0, int(top_n))]]


def usage_log_attribution(engine_latency: dict[str, Any] | None,
                          top_n: int = DEFAULT_TOP_N,
                          engine_outcomes: Iterable[dict[str, Any]] | None = None
                          ) -> dict[str, Any]:
    """usage_log query 流的归因字段（慢源 Top-N + 各引擎交付条数 + 失败类别）。

    返回 dict 以便调用方 **展开进日志记录**，未来新增归因维度（如
    「等待预算被谁耗掉」「缓存未命中的原因分布」）时只扩这里，
    主管线那一行 `**usage_log_attribution(...)` 不动——归因是会长大的，
    写入点不该跟着长（主管线有模块大小门禁，见模块 docstring）。

    失败类别只记「哪类」不记条数：errors 已经记了条数，两者互补。
    """
    out: dict[str, Any] = {
        "engine_latency_top": top_engine_latency(engine_latency, top_n),
    }
    counts = engine_result_counts(engine_outcomes)
    if counts:
        out["engine_results"] = counts
    kinds = failure_kinds(engine_outcomes)
    if kinds:
        out["failure_kinds"] = kinds
    return out


# 合成引擎名（只存在于基准/混沌测试的 fake combo）。它们出现在 engines_used
# 里就说明这一跑不是真实检索；写进生产 usage_log 会污染慢源排行与总量统计
# （2026-10-21 实测 "hedge bench" 32 条，dead_primary/fast_backup 进了慢源榜）。
_SYNTHETIC_ENGINES: frozenset[str] = frozenset({
    "dead_primary", "dead_backup", "fast_backup", "slow_backup",
    "slow_primary", "quick_primary", "fast_primary",
})


def is_synthetic_run(engines: Iterable[str] | None) -> bool:
    """这组引擎是否来自基准/测试的合成组合（有一个即判合成）。"""
    return any(str(e) in _SYNTHETIC_ENGINES for e in (engines or []))


# 「源被跳过/没资格调用」的状态：空结果时要把它们与「查询没有召回」分开。
# 只收持续性状态（配额/缺 env/鉴权），不含 timeout/error——后者是本次
# 真的失败了，原表述已够用。
SKIPPED_SOURCE_STATUSES: frozenset[str] = frozenset({
    engine_states.OUTCOME_SKIPPED_QUOTA,
    engine_states.OUTCOME_SKIPPED_MISSING_ENV,
    engine_states.OUTCOME_QUOTA_EXHAUSTED,
    engine_states.OUTCOME_AUTH_FAILED,
})


def skipped_sources(engine_outcomes: Iterable[dict[str, Any]] | None,
                    limit: int = 4) -> list[str]:
    """空结果时被跳过/鉴权失败的引擎名（去重、保序、限量）。"""
    out: list[str] = []
    seen: set[str] = set()
    for o in (engine_outcomes or []):
        if not isinstance(o, dict):
            continue
        if str(o.get("status") or "") not in SKIPPED_SOURCE_STATUSES:
            continue
        name = str(o.get("engine") or "?")
        if name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= max(0, int(limit)):
            break
    return out


def zero_result_recovery_note(recovery: Any,
                              engine_outcomes: Iterable[dict[str, Any]] | None
                              ) -> str | None:
    """恢复链触发但没救回来时的局限声明；不需要声明时返回 None。

    「没有召回」与「源被跳过/鉴权失败」处置完全不同：前者改写查询，
    后者看 failures 或补凭证。此前一律写成 no recall，实测 bocha 配额
    耗尽（403 not enough money）被报成「查询无召回」，调用方白跑一轮。
    """
    if not isinstance(recovery, dict) or not recovery.get("triggered") \
            or recovery.get("recovered"):
        return None
    note = recovery.get("note") or "no note"
    skipped = skipped_sources(engine_outcomes)
    if skipped:
        return (
            "recovery triggered but no usable strategy ("
            f"{note}); empty results came from skipped/failed sources "
            f"({', '.join(skipped)}) — see failures; this is not necessarily "
            "a no-recall query"
        )
    return (
        "recovery triggered but no usable strategy ("
        f"{note}); empty results likely mean the query had no recall — "
        "try rephrasing"
    )


def engine_result_counts(engine_outcomes: Iterable[dict[str, Any]] | None,
                         limit: int = 8) -> dict[str, int]:
    """每个引擎交付的有效结果条数 {引擎: 条数}，与 engine_latency_top 配对读。

    只有耗时回答不了「慢源是在拖尾，还是唯一出货的那个」（2026-11-08 实测：
    wechat_sogou 580ms 返回 0 条、sogou_weixin_browser 2342ms 交付全部 10 条，
    仅凭耗时会被误判成「白等慢源」）。条数取自 outcome.results_count（与
    agent 档 engine_outcomes 同一口径）；被跳过、取消、失败的引擎记 0。
    同名引擎多次出现（如补搜）取最大值；畸形条目跳过；上限与 engines_used 一致。
    """
    out: dict[str, int] = {}
    for o in (engine_outcomes or []):
        if not isinstance(o, dict) or not o.get("engine"):
            continue
        n = o.get("results_count")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
        name = str(o["engine"])
        if name in out:
            out[name] = max(out[name], n)
        elif len(out) < max(0, int(limit)):
            out[name] = n
    return out


def failure_kinds(engine_outcomes: Iterable[dict[str, Any]] | None,
                  limit: int = 6) -> list[str]:
    """本次调用的失败类别（去重、稳定排序），写进 usage_log 供长期归因。

    errors 只记「几条」，回答不了「哪类失败在反复出现」；类别来自
    output_transparency.build_failures（与 agent 档 failures 同一口径），
    不在日志侧另写一份分类。
    """
    try:
        from output_transparency import build_failures
    except ImportError:  # pragma: no cover - 单文件分发时的降级
        return []
    rows = build_failures({"engine_outcomes": list(engine_outcomes or [])},
                          limit=limit)
    return sorted({str(r.get("category") or "unknown") for r in rows})


def aggregate_slow_engines(rows: Iterable[dict[str, Any]],
                           top_n: int = 5) -> list[dict[str, Any]]:
    """聚合多行日志的 engine_latency_top → 按累计耗时的慢源排行。

    `argo stats` 的读出口：单条日志能说「这次谁慢」，聚合才能说
    「长期是谁在拖」。畸形条目（长度不对 / 非数值耗时 / 非序列）整体跳过，
    读侧面对历史日志里可能存在的旧格式必须 fail-soft。
    """
    total: dict[str, int] = {}
    peak: dict[str, int] = {}
    hits: dict[str, int] = {}
    for row in rows:
        entries = row.get("engine_latency_top")
        # 标量/None 都不是可迭代的条目表：历史日志或未来换格式都必须 fail-soft，
        # 不能因为一行脏数据让整个 `argo stats` 崩掉（契约见模块 docstring）。
        if not isinstance(entries, (list, tuple)):
            continue
        for item in entries:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            eng, ms = item
            if not isinstance(eng, str) or not isinstance(ms, (int, float)) \
                    or isinstance(ms, bool):
                continue
            total[eng] = total.get(eng, 0) + int(ms)
            peak[eng] = max(peak.get(eng, 0), int(ms))
            hits[eng] = hits.get(eng, 0) + 1
    ranked = sorted(total.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"engine": e, "total_ms": t, "max_ms": peak.get(e, 0),
             "queries": hits.get(e, 0)}
            for e, t in ranked[:max(0, int(top_n))]]
