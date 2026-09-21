#!/usr/bin/env python3
"""
search.py — Unified Search v2 CLI 主入口 & 执行调度

职责：
  - 解析命令行参数
  - 通过 route.py 做路由决策（含预算模式）
  - 通过 cache.py 做双层缓存
  - 通过 engines.py 执行引擎搜索
  - RRF 融合 + Bocha Reranker 精排
  - 通过 adaptive.py 记录引擎表现
  - 输出统一 JSON / 文本格式
"""

from __future__ import annotations

import time

# 尽早取时点：**放在重导入之前**，整条 import 链才算得进「固定开销」。
# （放在导入之后测出来的是 0——模块体跑完时导入早已结束。）
# 解释器自身的启动（本机 ~15 ms）到这里仍然测不到；那部分只能由外部
# `time` 命令给，所以不在这儿谎报成已测。
_MODULE_T0 = time.perf_counter()


from engine_env import env_flag  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
from typing import Any, Callable, Optional  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache  # noqa: E402
try:
    from cache import query_similarity as _query_similarity
except ImportError:
    _query_similarity = None  # type: ignore
from route import route_query_cached  # noqa: E402  # 跨进程路由决策缓存（见 route 内说明）
from config import get_cost_factor, get_execution_config, get_engines  # noqa: E402

# ── 排序/融合层（search_rank）与输出层（search_output）按职责拆出，这里同名转出，
# 调用方与既有测试无需改。打桩点若落在这些符号上，必须打在**读取处**（实现模块）。
# **别按「谁引用了它」裁剪这份清单**：测试用字符串引用模块属性（patch.object
# (search, "_tokens")），AST 统计会漏——照它裁 24 个名字，全量测试红 83 条。
from search_rank import (  # noqa: E402
    _RERANK_DEGRADED_STATUSES,
    _CJK_OR_WORD,
    _ENGINE_FUSION_WEIGHTS,
    _REL_FACTOR_TTL,
    _RERANK_BREAKER_KEY,
    _RERANK_POOL_FACTOR,
    _RERANK_POOL_MIN,
    _align_facts_safe,
    _apply_consensus_and_sort,
    _attach_selection_signals,
    _bigrams,
    _canonical_url,
    _consensus_prior,
    _content_sig,
    _content_similarity,
    _distinct_data_rows,
    _domain_matches,
    _domain_score_floors,
    _engine_weight,
    _jaccard,
    _lang_prefer_rerank,
    _note_rerank_failure,
    _rel_factor_cache,
    _rerank_breaker,
    _rerank_pool_limit,
    _rrf_weighted_default,
    _score_completeness,
    _score_relevance,
    _single_reliability,
    _tokens,
    _weight_cache,
    deduplicate_by_url,
    filter_results_by_domains,
    invalidate_engine_weight_cache,
    local_five_dim_rerank,
    minhash_dedupe,
    rerank_results,
    rrf_merge,
)
from quota import _QuotaBatch  # noqa: E402  # 配额记账收集器（规范层）
from search_entry import _SearchHooks, dispatch, prepare  # noqa: E402
from search_pipeline import (  # noqa: E402
    _SearchRequest,
    _SearchRun,
    finalize,
    postprocess,
)
from search_output import (  # noqa: E402
    _ShapeContext,
    shape_response,
    _collect_errors,
    FUNNEL_STAGES,
    _AGENT_RESULT_FIELDS,
    _slow_query_ttl,
    _strip_for_agent,
    build_funnel,
    build_sources,
    describe_funnel,
    format_text_output,
    format_timing,
    funnel_collapse,
)
try:
    from telemetry import emit as _emit_telemetry
except ImportError:
    _emit_telemetry = None  # type: ignore

# import 链结束的时点。固定开销 = 导入 + argparse + 收尾；这里把导入那段
# 单独报出来，因为它的可控性最好（延迟导入 / 拆模块就是冲它去的）。
_IMPORTS_DONE = time.perf_counter()


# ── engines 惰性代理 ──────────────────────────────────────────────────────────
# engines 是 import 链里最重的一块：它连带 engines_builders 的 7 个模块与
# urllib.request 的 http/ssl/email 链，实测占 `import search` 的 39%（交错
# A/B 中位 47.5ms → 29.2ms，即 18.3ms）。而**缓存命中**这一跳对它零依赖——
# 实测缓存命中时 engine_search 与 available_engines 调用次数都是 0，两者只在
# 派发、清单与错误路径上用到。改成惰性代理后：日常热路径省下整棵子树，冷路径
# 在首次派发时照常导入（总量不变，而冷路径本来就被网络耗时主导）。
#
# 必须是**模块级函数**而不是 import 内联：网络出口靠属性替换被顶替——
# search_benchmark 直接 `search.engine_search = fake`，
# tests/test_budget_observability 用 `patch("search.engine_search")`。属性被换掉
# 后代理自然让位，测试与基准的既有语义逐位不变。

def engine_search(*args, **kwargs):
    """惰性包装 engines.search（见上方说明）。"""
    from engines import search as _engine_search
    return _engine_search(*args, **kwargs)


def available_engines(*args, **kwargs):
    """惰性包装 engines.available_engines（见上方说明）。"""
    from engines import available_engines as _available_engines
    return _available_engines(*args, **kwargs)


# ── 时间辅助（时间窗归一化 / published_at 解析 / 后过滤 / 排序）──────────────
#
# 时间窗三层语义：
#   1. 下推（since/until → 引擎）：入口统一归一化为绝对 ISO，引擎收到确定值
#   2. 后过滤（结果层保底）：引擎不带时间窗能力时，按 published_at 剔除超窗
#   3. 排序（--sort）：仅展示顺序，不影响召回
#
# 归一化规则：相对量（Nd/Nh/Nw）→ 绝对日期；绝对时间无时区按本地时区解释
# （与 published_ts 一致）；非法输入保持原样下推、不参与后过滤，不阻断搜索。

from time_utils import (  # noqa: E402
    published_ts as _published_ts,
    normalize_time_window as _normalize_time_window,
    apply_time_window as _apply_time_window,
    sort_results_by_time as _sort_results,
    is_time_capable as _is_time_capable,
)


# ── 查询改写辅助 ────────────────────────────────────────────────────────────────
# 实现在 query_signals（纯本地启发式，无网络、无编排状态），此处只做别名导入，
# 保持既有调用点与测试的 `search._query_coverage_ok` 等名字不变。

from query_signals import (  # noqa: E402
    apply_query_rewrite as _apply_query_rewrite,
    results_sufficient as _results_sufficient,
    cumulative_sufficient as _cumulative_sufficient,
    query_coverage_ok as _query_coverage_ok,
)


def _missing_env_for(eng: str) -> list[str]:
    """返回引擎缺失的环境变量名列表；检测不可用时返回空（不阻断搜索）。

    与路由层 env_ready(spec) 同计算方式：查当前注册表拿 spec，否则
    声明里自定义 required_env 的引擎在此拦截不到（仅 KNOWN_ENV_ALIASES
    成员能命中）。注册表值是 callable（spec 在闭包里）时退化为原名检测。
    """
    try:
        from engine_env import missing_env_for as _missing
        spec = None
        try:
            from engines import get_engine_spec
            spec = get_engine_spec(eng)
        except Exception:
            spec = None
        return _missing(eng, spec)
    except Exception:
        return []




def _record_quota(engine: str, success: bool) -> None:
    """单条配额记账（真实打网后写）；失败静默。

    批量路径请用 `_QuotaBatch`——它把同一次搜索的 N 条合成一次写入文件。
    """
    try:
        from quota import get_quota_manager
        get_quota_manager().record(engine, success=success)
    except Exception:
        pass


# 结局分类（状态码表 / 配额与拦截关键词 / classify_engine_outcome）已随编排段
# 搬到 engine_dispatch；这里只导入仍需在 search 内部用到的两个名字：
# classify_engine_outcome 供下面的 run_dispatch 注入，配额关键词供自适应
# 学习跳过判定。

from engine_dispatch import (  # noqa: E402
    _QUOTA_ERROR_KEYWORDS,
    classify_engine_outcome as _classify_engine_outcome,
    run_dispatch,
)


def _note_remote_quota_exhausted(engine: str, detail: str) -> None:
    """远端明示配额耗尽（如 byted 10406）→ 标记到周期边界自动恢复。

    标记后路由组合层全模式排除该引擎、备用源自然接管；恢复无需人工干预。
    """
    try:
        from quota import get_quota_manager
        get_quota_manager().mark_remote_exhausted(engine, reason=detail)
    except Exception:
        pass


# 进度阶段枚举住在 search_types（执行层与加工层都要发进度事件，见那里的注释）。
from search_types import Stage  # noqa: E402


# ── RRF 融合 ───────────────────────────────────────────────────────────────────

# URL 归一化的唯一来源在 url_canon：本仓曾有四份各自实现的「URL 归一」
# （search / plan / candidate_envelope / research_dossier，追踪参数表与
# 大小写规则各不相同），导致同一链接在融合层与 dossier 层归一成不同键。
# 此处只做薄转发，规则改动一律进 url_canon。
from url_canon import canonical_url as _canonical_url_impl  # noqa: E402




# 引擎融合权重（WG-RRF：按来源质量加权，权威源提权、社交/低质源降权）


# 动态可靠性因子（weakest-link，论文 arxiv 2508.01405）：熔断/高错误引擎降权，
# 避免「弱检索路径」在融合时拖垮整体精度。带 30s TTL 缓存，避免热路径重复查询。

# _engine_weight 的结果缓存：{(source, lang): (weight, expires_at)}。
# 见 _engine_weight 文档串——rrf_merge 逐条调用而取值空间极小，缓存后 300 条
# 结果由 0.54ms 降到常数级；TTL 与上面的可靠性窗口对齐，不额外冻结熔断状态。


















# 精排池容量：放宽截断让 rerank 看到 max_results 的 3 倍（下限 15 条），
# 最终输出再截断到 max_results。去重提前停与放宽截断共用这一个计算方式——
# 此前它是散在截断点上的字面量，而「去重该停在哪」需要知道同一个数。








# ── 多语言结果语言偏好软排序（P2-覆盖，2026-08 新增）────────────────────────
# ja/ko 明确主语言查询：把含目标语言字符（假名/谚文）的结果前移，纯相反语言
# 结果后移。**软排序不删除**（避免误删混合/技术结果），其余语言零开销返回。


# ── Bocha Reranker ──────────────────────────────────────────────────────────────

# 精排端点在熔断器里的键。用 `rerank:` 前缀与可路由引擎区分——它不是一个能
# 出现在 combo 里的引擎，但**复用同一套熔断语义**（失败计数 → 冷却 → 半开探测
# → 自动禁用后周期复探），这样就不必另造一套「端点退避」机制。









# ── P0-003：本地五维 Rerank 保底 ──────────────────────────────────────────────





















# ── 融合后段（execute_search 的可独立测试单元）────────────────────────────────







# ── 执行层 ─────────────────────────────────────────────────────────────────────

# fast 单发总墙钟预算（秒）。引擎级超时收紧（≥8s 源 cap 6s、half_open 2s）之外
# 的整条路径保底：实测引擎 P95 跨度 1.5-8.5s，6s 预算覆盖绝大多数快引擎，
# 只砍 github 类拖尾——「等待剩余时间」与「是否再起新引擎」都受它约束。
_FAST_TOTAL_BUDGET_S = 6.0

# auto/budget 的总墙钟预算（秒）。auto 是默认模式，此前**没有**预算
# （deadline=inf），于是 race 窗口退化成 `_eff_timeout + 2`：timeout 默认 10s
# → 单查询最坏 grace + 12s(race) = 14s。2026-09-15 实测 "claude 5 release
# date" 冷查询 13.4s，期间无任何信号说明在等什么，用户体感就是「卡住了」。
#
# 取 10.0 = 用户明确的体感阈值，也 ≥ execution.default_timeout(8s)，不截断
# 正常查询（实测中位 4s、p75 8s，绝大多数早已 early-stop 完成），只砍极端尾部。
# deep 仍不设预算：研究场景宁可等待，截断会丢证据（与 fast 的成本优先相反）。
_AUTO_TOTAL_BUDGET_S = 10.0

# 首选引擎的独占宽限窗（秒）：在这个时间内完成且合格就免掉 hedge、只付 1 次
# 调用；窗口内没完成就补发下一个引擎并行赛跑。
#
# **两条路径共用同一个值**，因为它们是同一个语义：「我们最信任的那个引擎，
# 在放弃它之前先给它多久」。串行垂直域（`parallel=False`）此前根本没有这道窗，
# 一个死源要耗满自己的超时上限（5-8s）才轮到备选；对冲改造后它与 wave-1 用
# 同一个数。两个名字、两个值会让「到底等多久」没有单一答案。
#
# 取值 0.8s（2026-09-17 实测，本机真实网络，8 条查询 ×5 次、逐条交替执行）：
#
#   - 主引擎在 0.8s 内交付时（网络正常时的大多数），两者完全一致——都是 1 次
#     调用、墙钟无差别（合计 7388ms → 6085ms 那一轮里 7/8 条查询就是这种情况）。
#   - 主引擎慢于 2.0s 时，旧值要多等 1.2s 才肯补发备选，这段直接进墙钟：
#     实测慢网那一轮 8 条查询合计 20988ms → 13426ms（**−36%**），其中
#     「2026年 AI Agent 进展」2856→1228ms、「量子计算 突破 2026」2846→1310ms。
#   - 代价是中间地带（主引擎落在 0.8~2.0s）偶尔多发一次调用：实测 8 条查询
#     合计调用 8→9 条（+12%），而备选源都是免密钥源。
#
# 关键判断：旧值 2.0s 恰好在最差的位置——主引擎（anysearch）实测均值 1.9s，
# 落在窗前窗后都说不准，于是「省下一次调用」这个目的基本没兑现，却把上界
# 1.2s 的等待稳定地写进了每一次慢查询。**用一个免费备用源的调用换掉 1.2s
# 用户可见延迟，是一笔划算的交换。**
_PRIMARY_GRACE_S = 0.8

# 质量守卫拒绝早停后的有界宽限窗（秒）。对冲的备份引擎先回来、但结果被判不充分
# （计数不足，或与查询零词面交集）时，主引擎还能再跑这么久，之后不再等它。
# 实测「上海 地铁 线路图」：备份给出 10 条纽约共享单车结果（零词面交集，守卫
# 正确地拒绝早停），主引擎 Nominatim 对这类查询本就答不了、1-3s 后返回 0 条，
# 却把墙钟拖到 4.4s。只影响「还等多久」，不改变任何结果的取舍。
# 0 关闭（回到「等到 race 窗口耗尽」的旧行为，供消融对照）。
_STRAGGLER_GRACE_S = 1.5


def _straggler_grace() -> float:
    """宽限窗取值（ARGO_STRAGGLER_GRACE_S 覆盖，供消融对照）。"""
    try:
        raw = os.environ.get("ARGO_STRAGGLER_GRACE_S")
        if raw is not None and raw.strip() != "":
            return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    return _STRAGGLER_GRACE_S


def _serial_stagger() -> float:
    """串行路径的起步间隔：`ARGO_SERIAL_STAGGER_S` > `_PRIMARY_GRACE_S`。

    值与 wave-1 的宽限窗同源（同一语义，见 `_PRIMARY_GRACE_S` 注释）：首个
    引擎跑过这么久还没回来，就补发备选源并行跑，而不是等它耗满自己的超时上限。

    单调语义：0 = 不节流（备选源与首引擎同时起跑，最激进）；值越大越接近严格
    串行（取一个大于引擎超时上限的值即可完全退回改造前的行为）。

    环境变量这一层是给消融对照用的（取大于超时上限的值 = 退回严格串行）；
    与隔壁 `_STRAGGLER_GRACE_S` 同一种取法：常量默认 + 环境变量覆盖、**按值
    传进 engine_dispatch**，不在调度模块里重新解析配置。三种取法（常量 /
    config / env）混用会让「这个值到底是多少」没有单一答案，也容易让测试里
    patch 的常量被 config 静默顶掉。
    """
    try:
        raw = os.environ.get("ARGO_SERIAL_STAGGER_S")
        if raw is not None and raw.strip() != "":
            return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    return _PRIMARY_GRACE_S


# 单引擎墙钟硬预算（秒）：含该引擎的**全部**重试尝试，超预算即停、不再发起
# 新尝试，由调度层切备选源。
#
# 为什么要这个上界（2026-09-10 实测）：重试会叠乘。anysearch 曾同时具备
#   引擎级 retry_count=1（2 次）× HTTP 级 max_retries=1（2 次）× 8s
#   = 最坏 32s（实测 31.3s）
# 用户侧表现是「一个查询卡半分钟」，而这期间既没切备选源、也没有任何
# 信号说明在等什么。逐处调小超时不是好解法——那会误杀慢网下正常的源；
# 给**单引擎总墙钟**设上界才是根本的，且对未来新增的重试层同样生效。
#
# 取值 10s：用户明确的体感阈值（「10 秒以内也应该能够解决」），
# 也 ≥ execution.default_timeout(8s)，保证单次正常尝试不被截断。
_PER_ENGINE_BUDGET_S = 10.0


# ── 阶段耗时（--explain-timing）─────────────────────────────────────────────
# 实现在 stage_timing（纯数据结构），此处只做别名导入，保持既有调用点与
# 测试的 `search.StageTiming` 名字不变。

from stage_timing import (  # noqa: E402
    StageTiming,
    tick as _tick,
    tock as _tock,
)


def execute_search(query: str, decision: dict[str, Any], max_results: int,
                   timeout: int, depth: str, cache: SearchCache, skip_cache: bool,
                   mode: str = "auto",
                   since: str | None = None, until: str | None = None,
                   sort: str = "relevance",
                   on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
                   timing: StageTiming | None = None) -> dict[str, Any]:
    hooks = _SearchHooks(
        engine_search=engine_search, available_engines=available_engines,
        get_cost_factor=get_cost_factor,
        get_engines=get_engines, get_execution_config=get_execution_config,
        missing_env_for=_missing_env_for,
        classify_outcome=_classify_engine_outcome,
        note_quota_exhausted=_note_remote_quota_exhausted,
        per_engine_budget_s=_PER_ENGINE_BUDGET_S,
        fast_budget_s=_FAST_TOTAL_BUDGET_S,
        auto_budget_s=_AUTO_TOTAL_BUDGET_S,
        primary_grace_s=_PRIMARY_GRACE_S,
        straggler_grace_s=_straggler_grace(),
        serial_stagger_s=_serial_stagger(),
    )
    prepared = prepare(query, decision, max_results, timeout, depth, cache,
                       skip_cache, mode=mode, since=since, until=until,
                       sort=sort, on_progress=on_progress, timing=timing,
                       hooks=hooks)
    if prepared.cached is not None:
        return prepared.cached
    run = dispatch(prepared.req, prepared.run, hooks)
    run = postprocess(prepared.req, run, hooks)
    return finalize(prepared.req, run, hooks)











# ── 统一入口 ──────────────────────────────────────────────────────────────────

def _run_local_seek(query: str, max_n: int = 5) -> list[dict[str, Any]]:
    """本机文件命中（--include-local 用）：调 local-seek 子技能，JSON 并入。

    仅在显式开启时调用（默认零开销）；结果不参与融合评分，
    仅作尾部来源（source=local_files）。
    """
    import subprocess as _sp

    # 安装感知 + 唯一来源：委托 seek_locator 统一发现 local-seek/scripts/seek.py
    # （打包子技能优先，ARGO_LOCAL_SEEK_PATH / ARGO_LOCAL_SEEK_ROOTS 承载自定义/遗留）。
    # 不硬编码 ~/.agents/skills|~/.claude/skills 主机路径（SKILL.md 明令禁止）。
    from seek_locator import resolve_seek_py
    seek_py = resolve_seek_py()
    if not seek_py or not os.path.isfile(seek_py):
        return []
    r = _sp.run(
        [sys.executable, seek_py, query, "--json", "--max", str(max(max_n, 1))],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
        env={**os.environ, "PYTHONUTF8": "1"},  # 子进程是自家 seek.py，双向显式 UTF-8
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        return []
    hits = payload.get("results") or payload.get("files") or []
    out = []
    for h in hits[:max_n]:
        if not isinstance(h, dict):
            continue
        path = h.get("path") or h.get("file") or ""
        line = h.get("line") or h.get("lineno") or 1
        url = f"file://{path}" + (f"#{line}" if str(line).isdigit() else "")
        out.append({
            "title": path,
            "url": url,
            "snippet": (h.get("snippet") or h.get("text") or h.get("line_text") or "")[:160],
            "source": "local_files",
            "score": 0.0,
            "kind": "local",
        })
    return out


def super_search(query: str, engine: str = "auto", n: int = 5, explain: bool = False,
                 skip_cache: bool = False, timeout: int = 10,
                 depth: str = "fast", mode: str = "auto", local_first: bool = False,
                 rewrite: bool = True, cache: Any = None,
                 on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
                 input_kind: str = "auto",
                 plan_only: bool = False,
                 force_search: bool = False,
                 envelope: bool = True,
                 context: str = "search",
                 engines_boost: list[str] | None = None,
                 since: str | None = None,
                 until: str | None = None,
                 sort: str = "relevance",
                 include_local: bool = False,
                 include_domains: list[str] | None = None,
                 exclude_domains: list[str] | None = None,
                 timing: StageTiming | None = None) -> dict[str, Any]:
    """统一搜索便捷入口。

    执行分层（不阻塞日常）：
      - daily（默认 auto/fast）：直搜，不挂 plan，不要求用户确认
      - professional（mode=deep 或 depth=deep）：直搜 + 附加 plan 元数据
      - plan_only：仅离线计划（显式开关，不进热路径默认）
      - known-url：工具分流 handoff（不是「请确认后再搜」）

    Args:
        query: 搜索查询词
        engine: 指定引擎（默认 auto）
        n: 最大结果数
        explain: 是否输出路由解释
        skip_cache: 是否跳过缓存
        timeout: 超时
        depth: 搜索深度
        mode: 预算模式
        local_first: 强制本地优先
        rewrite: 是否自动改写查询（默认 True）
        on_progress: 可选进度回调 (stage, data)
        input_kind: auto|keyword|url-seed|known-url
        plan_only: 仅离线计划，不联网
        force_search: 即使判定 known-url 也强制多引擎搜索
        envelope: 附加 candidates/coverage/limitations
        context: search | research
        engines_boost: 垂直引擎前置列表（研究路径 boost，不锁死单引擎）
        since/until: 发布时间时间窗（如 7d / 2026-08-01），下推到支持时间窗的引擎

    注意：路由永远基于原始 query。改写词只用于引擎检索，避免
    「Python → 追加 pip/库」之类改写污染 package_search 等域规则。
    """
    cache = cache if cache is not None else SearchCache()
    original_query = query

    # 查询改写：仅影响检索串，不影响路由（在执行引擎前应用）
    rewrite_result = None
    search_query = original_query

    # ── 离线计划 / URL 分流（离线计划 / 输入分流）──
    # 纪律：build_plan 无网络、不回调本函数 → 无 plan↔search 死循环
    kind = "keyword"
    tier = "daily"
    plan_info: dict[str, Any] | None = None
    try:
        from plan import (
            build_plan, classify_input_kind, execution_tier, should_attach_plan,
        )
        kind = classify_input_kind(query, input_kind)
        tier = execution_tier(mode, depth, context)
        if plan_only:
            return build_plan(
                query, mode=mode, depth=depth, max_results=n,
                engine=engine if not local_first else "local_search",
                input_kind=input_kind,
                context=context,
            )
        if kind == "known-url" and not force_search:
            plan_info = build_plan(
                query, mode=mode, depth=depth, max_results=n,
                engine=engine, input_kind="known-url", context=context,
            )
            # 不发起多引擎搜索；返回 handoff 形态，避免把读链接当热搜
            out = {
                "query": query,
                "engine": None,
                "engines": [],
                "engines_combo": [],
                "cached": False,
                "domain": None,
                "elapsed_ms": 0,
                "results": [],
                "count": 0,
                "errors": [],
                "engine_outcomes": [],
                "wasted_engine_ms": 0,
                "early_stopped": False,
                "mode": mode,
                "depth": depth,
                "status": "handoff_required",
                "input_kind": "known-url",
                "execution_tier": tier,
                "requires_confirmation": False,
                "plan": plan_info,
                "handoff": plan_info.get("handoff"),
                "limitations": plan_info.get("limitations") or [],
                "schema_version": "1.0",
                "candidates": [],
                "coverage": [],
            }
            return out
    except ImportError:
        kind = "keyword"
        tier = "daily"
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"plan 分流跳过: {type(e).__name__}")

    # 无词元查询短路：整条查询连一个字母/数字/汉字都没有（纯符号、纯标点、
    # 纯 emoji），任何文本引擎都不可能召回——实测放行只会白烧引擎调用与
    # 3 秒级时延（"!!!" 实测 dispatch 3.1 s 且 anysearch 超时、缓存里多一条
    # 垃圾空结果）。只拦 auto 档：显式指定引擎或 local_first 是用户在点名
    # 「就要拿这个串去搜」，计划分流的 plan_only 也已在上方返回，均不受影响。
    if (engine == "auto" and not local_first
            and not re.search(r"\w", original_query)):
        out = {
            "query": original_query,
            "engine": "none", "engines": [], "engines_used": [],
            "domain": "general", "count": 0, "mode": mode, "depth": depth,
            "status": "completed", "fetch_required": False,
            "evidence_loop": {"high_consequence_domain": None,
                              "suggested": [], "verified_count": 0,
                              "pending_count": 0},
            "errors": [], "login_hint": {"needs_login": False, "reason": ""},
            "funnel": {"routed": 0, "called": 0, "returned": 0,
                       "deduped": 0, "filtered": 0, "kept": 0},
            "limitations": ["query has no word tokens (letters/digits/CJK); "
                            "no engine can recall it, network dispatch skipped"],
            "results": [],
            "schema_version": "1.0",
            "input_kind": kind, "execution_tier": tier,
        }
        if timing is not None:
            out["timing"] = timing.summary()
        return out

    # 查询改写：追加领域关键词提升搜索质量
    # local_first 路径跳过改写：改写词面向 web 引擎召回设计，套到本地
    # 聚合（search_v3 智能路由）上会稀释查询、收窄引擎选择、扩大失败面
    #（实测改写词把「Python 异步编程」扩为 5 词长句后本地聚合返回空）。
    rewrite_result = None
    original_query = query
    if rewrite and not local_first:
        rewritten, rewrite_result = _apply_query_rewrite(original_query)
        if rewrite_result and rewrite_result.get("rewritten"):
            search_query = rewritten

    if local_first:
        _tk_route = _tick(timing)
        decision = route_query_cached(
            original_query, engine_override="local_search", mode=mode,
            depth=depth, context=context, engines_boost=engines_boost,
        )
        _tock(timing, "route", _tk_route)
    else:
        _tk_route = _tick(timing)
        decision = route_query_cached(
            original_query, engine_override=engine, mode=mode,
            depth=depth, context=context, engines_boost=engines_boost,
        )
        _tock(timing, "route", _tk_route)
    if context == "research":
        # 研究子查询禁早停：第一个「有结果」的垂直目录（如 models_dev 的
        # 模型规格页）不等于研究证据齐了，跑满 combo 再 RRF 融合。
        # 复用 no_early_stop 通道，串行/并行两条执行路径均已消费该标志。
        decision["no_early_stop"] = True
    if explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={mode} kind={kind} tier={tier}",
            file=sys.stderr,
        )
        if search_query != original_query:
            print(f"[改写] {original_query} → {search_query}", file=sys.stderr)
    result = execute_search(
        query=search_query, decision=decision, max_results=n,
        timeout=timeout, depth=depth, cache=cache,
        skip_cache=skip_cache, mode=mode, on_progress=on_progress,
        since=since, until=until, sort=sort, timing=timing,
    )
    # 对外仍报告用户原始 query
    result["query"] = original_query
    if since:
        result["since"] = since
    if until:
        result["until"] = until
    if sort and sort != "relevance":
        result["sort"] = sort
    if rewrite_result and rewrite_result.get("rewritten"):
        result["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }
    result["input_kind"] = kind
    result["status"] = "completed"
    result["execution_tier"] = tier
    result["requires_confirmation"] = False  # 日常/专业热路径永不阻塞等确认
    # query_original 仅当改写改变检索词时才携带原始词，供存档/MCP 回退。
    # 此前条件 `original_query != query` 恒为 False（原查询词从未被重赋），
    # 导致该字段永远未写出——任何依赖它的下游都拿不到「是否被改写」的信号。
    if search_query != original_query:
        result["query_original"] = original_query

    # professional：附加离线 plan 元数据（不阻断、不二次搜索）
    try:
        from plan import build_plan, should_attach_plan
        if should_attach_plan(mode, depth, context, plan_only=False):
            result["plan"] = build_plan(
                original_query, mode=mode, depth=depth, max_results=n,
                engine=engine if not local_first else "local_search",
                input_kind=kind if kind != "auto" else "auto",
                context=context,
            )
    except Exception:
        pass

    # 结果局限声明：**质量信号，与归档开关无关**，两条路径共用同一计算方式。
    # 此前只在 envelope 分支内计算，于是 --no-envelope（文档推荐给 agent 的
    # 档位）整块拿不到它——agent 无从知晓拿到的是「相关发现而非正文」
    # 「降级路由结果」「未预确认的 daily 档」（2026-09-15 输出契约审查）。
    extra_lim: list[str] = []
    if kind == "url-seed":
        extra_lim.append(
            "url-seed: seed URL was not fetched; results are related discovery only"
        )
    if result.get("recovery"):
        extra_lim.append("recovery used; engine fallback may differ from primary route")
    # 执行归因：把「为什么只有这么几条 / 为什么只跑了一个源」写进随答案一起
    # 到达的局限声明。此前这类问题只能靠翻 engine_outcomes 与 early_stopped
    # 反推，而局限声明本身就是给消费者看的「这批结果能用到什么程度」，
    # 成因属于同一类信息（GLM 密集反馈：反馈要能追到具体原因）。
    _fn = result.get("funnel")
    if isinstance(_fn, dict):
        _routed, _called = _fn.get("routed"), _fn.get("called")
        if isinstance(_routed, int) and isinstance(_called, int) \
                and _called < _routed:
            # 有漏斗时由这里给带数字的版本；candidate_envelope.build_limitations
            # 见到 funnel 就不再出泛化表述——同一件事只留一句。
            extra_lim.append(
                f"early_stopped: only {_called} of {_routed} routed engines "
                "were queried; coverage may be narrower than the route implies"
            )
        if result.get("count") == 0:
            _stage = funnel_collapse(_fn)
            if _stage:
                extra_lim.append(
                    f"no results: pipeline emptied at '{_stage}' "
                    f"[{describe_funnel(_fn)}]"
                )
    # 输出成形（七个阶段：档位局限 → envelope/局限 → 本地正文索引 → 证据门控
    # → 域过滤 → 信源标准化 → 本地命中并入）住在 search_output.shape_response：
    # 那是**响应契约**，与执行/调度无关。
    result = shape_response(_ShapeContext(
        query=query, kind=kind, tier=tier, envelope=envelope, decision=decision,
        extra_lim=extra_lim, cache=cache, include_domains=include_domains,
        exclude_domains=exclude_domains, include_local=include_local, n=n,
        run_local_seek=_run_local_seek,
    ), result)


    return result


# ── 信源标准化 ─────────────────────────────────────────────────────────────────

# --fields agent：每条 result 保留的答案字段（P2-2）。
# 媒体专属字段按需登记——非该媒体的结果此键为 None，_strip_for_agent 会自动
# 丢弃，其他查询不付代价：image_* 来自图源，episode_count/duration_minutes
# 来自 itunes 的播客结果。
















# ── 输出格式化 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    from search_cli import main  # 反向导入放这里：CLI 依赖本模块，模块级会成环
    main()
