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
from enum import Enum  # noqa: E402
from typing import Any, Callable, Optional  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache  # noqa: E402
try:
    from cache import query_similarity as _query_similarity
except ImportError:
    _query_similarity = None  # type: ignore
from route import route_query_cached  # noqa: E402  # 跨进程路由决策缓存（见 route 内说明）
from config import get_execution_config, get_cost_factor, get_engines  # noqa: E402

# ── 排序/融合层（search_rank）与输出层（search_output）按职责拆出，这里同名转出，
# 调用方与既有测试无需改。打桩点若落在这些符号上，必须打在**读取处**（实现模块）。
# **别按「谁引用了它」裁剪这份清单**：测试用字符串引用模块属性（patch.object
# (search, "_tokens")），AST 统计会漏——照它裁 24 个名字，全量测试红 83 条。
from search_rank import (  # noqa: E402
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
from search_output import (  # noqa: E402
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


class _QuotaBatch:
    """一次搜索的配额记账收集器（累积 → 一次性写入文件）。

    为什么不是每引擎各写一次：每次 record 都是「全量状态序列化 + rename」，
    一次 5 引擎搜索即 5 次全量写。合并后写盘次数从 N 降到 1，且整批在
    同一个跨进程文件锁内完成（`QuotaManager.record_many`）。

    失败静默：记账属于观测层，任何异常都不得拖累搜索主路径。
    """

    def __init__(self) -> None:
        self._entries: list[tuple[str, bool]] = []

    def add(self, engine: str, success: bool) -> None:
        self._entries.append((engine, success))

    def flush(self) -> None:
        entries, self._entries = self._entries, []
        if not entries:
            return
        try:
            from quota import get_quota_manager
            get_quota_manager().record_many(entries)
        except Exception:
            pass


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


# ── 进度阶段 ──────────────────────────────────────────────────────────────────

class Stage(str, Enum):
    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"
    ERROR = "error"


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

# 「bocha 没有产出排序」的唯一来源：落到本地五维保底的状态全集。
#
# 此前是内联在调用点的四元素元组，新增状态极易漏改，而漏改的后果是静默的——
# 既不精排也不保底，最终顺序退化成 RRF 原始序，没有任何信号。`rerank.py`
# 加一个状态就要回来补白名单，正是「同一事实两处定义」的典型形态。
_RERANK_DEGRADED_STATUSES = frozenset({
    "skipped_no_key",      # 未配置密钥
    "skipped_short",       # 结果太少，不值得精排
    "skipped_fast",        # fast 档不付远程精排
    "skipped_circuit_open",  # 端点熔断中（见 _RERANK_BREAKER_KEY）
    "fallback",            # 端点报错或返回不可用数据
})








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
    """执行搜索：缓存 → 熔断/负缓存 → 引擎 → 融合 → 精排 → 过滤 → 写缓存。"""
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    # 防御：过滤空引擎名（空串会在 registry 查无 → 空结果 → 熔断空键 ''）。
    # 全空时保底 anysearch，防止下方 engines[0] IndexError（route 层已保证
    # combo 非空，此处仅防畸形 decision 直接调用 execute_search）。
    engines = [e for e in engines_combo if e] or ["anysearch"]
    parallel = decision.get("parallel", False) and len(engines) > 1

    # P0-001：查询理解 — clean_query 用于检索，exclude_terms 用于融合后过滤
    exclude_terms: list[str] = []
    retrieval_query = query
    qu = None
    try:
        from query_understanding import _understand_cached as understand
        qu = understand(query)
        exclude_terms = qu.exclude_terms
        # 仅当去否定片段后仍有实义内容时才替换检索词，避免空检索
        if qu.clean_query and qu.clean_query.strip():
            retrieval_query = qu.clean_query
    except ImportError:
        pass  # query_understanding 不可用
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询理解跳过: {type(e).__name__}")

    # 词形规范化：全角→半角、拆斜杠、压多余空格（提升精确源命中，治型号/日期分隔符）
    try:
        from query_enhance import normalize_query
        retrieval_query = normalize_query(retrieval_query)
    except ImportError:
        pass

    if on_progress:
        on_progress(Stage.START, {"query": query})

    # 网络环境感知：慢网放大超时预算（避免误杀），快网收紧（更快响应）
    _eff_timeout = timeout
    try:
        from network_aware import adjusted_timeout, network_profile
        _eff_timeout = adjusted_timeout(timeout, engines)
        if _eff_timeout != timeout:
            import logging
            logging.getLogger("unified_search").debug(
                f"网络感知超时: {timeout}s → {_eff_timeout}s "
                f"({network_profile(engines).get('network')})",
            )
    except ImportError:
        pass

    # 时间窗归一化：下推/缓存键用归一化 ISO（相对值转绝对日期），
    # 后过滤用 epoch 秒；非法输入保持原样下推、不参与后过滤。
    since_iso, until_iso, since_ts, until_ts = _normalize_time_window(since, until)

    # 缓存键的引擎维度取**请求侧身份**（用户点名的引擎，或 auto），不取
    # 路由出来的 engines_combo。
    #
    # 为什么（2026-09-19 实测）：combo 是决策结果，会被 adaptive 学习器按上一次
    # 搜索的成败逐次改写。拿结果当键就是「用缓存让缓存失效」——同一查询连跑
    # 两次，进键的引擎串从 `anysearch+octen` 变成 `exa+octen`、从 `byted+...`
    # 变成 `local_bing+...`，20 条样本里 6 条重复查询（30%）因此白跑一遍网络，
    # 而查询、域、档位全都没变。
    #
    # 语义边界：用户点名 `--engine pypi` 时键里就是 pypi（显式约束必须隔离，
    # 这正是 v2.4.2 那条修复要保的东西）；`auto` 时键里是 auto——「用自动路由
    # 搜这个查询」本身就是请求，具体挑了哪几个引擎是实现细节，由 TTL 兜住
    # 时效，并原样保留在缓存载荷里供追溯。
    cache_engine_key = decision.get("engine_request") or "auto"
    # 时间窗并入缓存键：同一 query 不同 since/until 不串缓存；
    # 用归一化 ISO（7d 与等价绝对日期共享缓存；相对窗跨天自然过期不串旧数据）。
    # 仅当组合内含带时间能力引擎时隔离：无时间字段引擎忽略时间窗、结果相同，
    # 隔离只会降低命中率（7d/30d 查 octen/anysearch 命中同一缓存）。
    time_aware = any(_is_time_capable(e) for e in engines)
    if since_iso and time_aware:
        cache_engine_key += f"|since={since_iso}"
    if until_iso and time_aware:
        cache_engine_key += f"|until={until_iso}"

    if on_progress:
        on_progress(Stage.ROUTING, {"domain": domain, "engine": engine_label, "engines": engines})

    # combo 缓存命中（含 depth + 柔性命中）
    if not skip_cache:
        t_cache_start = time.time()
        _tk_cache = _tick(timing)
        hit = cache.get(query, cache_engine_key, max_results, domain=domain,
                        mode=mode, depth=depth)
        _tock(timing, "cache_lookup", _tk_cache)
        if hit:
            cache_elapsed = int((time.time() - t_cache_start) * 1000)
            if on_progress:
                on_progress(Stage.CACHE_HIT, {"cache_level": hit.get("_cache_level", "L?")})
            tfidf_scores = decision.get("tfidf_scores", [])
            if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
                tfidf_scores = []
            # 排序在缓存读出后、返回前：缓存内容保持 score 序，sort 只改展示顺序
            hit_results = _sort_results(hit.get("results", []), sort)
            # 命中时一律报**产出这批结果的那次运行**的组合与理由，而不是本次
            # 路由的。此前两者恒等（组合就在缓存键里），这条区分不存在；键改成
            # 请求身份后，同一查询两次运行可以路由到不同组合，而本次路由根本
            # 没执行——报它等于报一个没跑过的计划，还会与同样来自缓存的
            # engines_used / engine_outcomes 自相矛盾。
            cached_combo = (hit.get("engines_combo") or hit.get("engines")
                            or engines)
            _hit = {
                "query": query, "engine": (cached_combo or engines)[0],
                "engines": cached_combo,
                "engines_combo": cached_combo, "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain, "elapsed_ms": cache_elapsed,
                "tfidf_scores": tfidf_scores,
                "route_reason": hit.get("route_reason") or decision.get("reason"),
                "login_hint": decision.get("login_hint"),
                "results": hit_results,
                "count": len(hit_results),
                "engines_used": hit.get("engines_used") or engines,
                "mode": mode, "depth": depth,
                "reranker": "skipped_cache",
                "engine_outcomes": hit.get("engine_outcomes") or [],
                "time_filtered": 0,
            }
            # 软命中披露：L2 语义命中返回的是**另一条查询**的载荷，不标出来
            # 就与精确命中无法区分——调用方会以为这就是本查询的缓存。同样遵循
            # 「不适用就整个键缺席」（见下方漏斗注释）：精确命中下这三个键不
            # 存在，不写成 null。
            if hit.get("_semantic_hit"):
                _hit["semantic_hit"] = True
                _hit["semantic_query"] = hit.get("_semantic_query")
                _hit["semantic_similarity"] = hit.get("_semantic_similarity")
            # 缓存命中时漏斗记账沿用存档值（它描述的是上一次真实抓取）。
            # 存档里没有（该条写入于引入漏斗之前）就**整个键缺席**，不写成
            # null——null 会被读成「漏斗算出来是空」，而缺席只表示「这次没有
            # 这个数据」。两种档位（默认/agent）必须同一形态，否则同一件事
            # 有两种表述。
            if hit.get("funnel") is not None:
                _hit["funnel"] = hit["funnel"]
            if timing is not None:
                _hit["timing"] = timing.summary()
            return _hit

    if on_progress:
        on_progress(Stage.SEARCHING, {"engines": engines})

    try:
        from circuit_breaker import get_breaker
        breaker = get_breaker()
    except ImportError:
        breaker = None

    t0 = time.time()
    # 单调钟基准：预算窗不随 NTP 跳变失真（engine_dispatch 整套换钟，见其垫片注释）
    t0_mono = time.monotonic()
    _tk_dispatch = _tick(timing)
    # 配额批次在这里建、在融合后的 D6 补搜之后才 flush：中间所有 _ingest
    # （含补搜）都要记进同一批，提前 flush 会让补搜引擎的记账落不了盘。
    quota_batch = _QuotaBatch()
    # 引擎编排（并发/串行调度、重试、熔断、结局分类）整段在 engine_dispatch。
    # 入口与常量**按值传入**而非让那边直接 import search：测试靠
    # patch.object(search, "engine_search" / "get_engines" / "_missing_env_for" /
    # "_FAST_TOTAL_BUDGET_S" / "_PRIMARY_GRACE_S") 换掉它们，直连绑定会让补丁
    # 静默失效（假引擎不被调用、真网络被打开）。
    _dispatch = run_dispatch(
        query=query, retrieval_query=retrieval_query, engines=engines,
        decision=decision, parallel=parallel,
        domain=domain, mode=mode, depth=depth,
        max_results=max_results, timeout=timeout, net_timeout=_eff_timeout,
        skip_cache=skip_cache, cache=cache, breaker=breaker,
        since_iso=since_iso, until_iso=until_iso, t0=t0, t0_mono=t0_mono,
        engine_search=engine_search,
        get_engines_fn=get_engines,
        get_execution_config_fn=get_execution_config,
        missing_env_for=_missing_env_for,
        classify_outcome=_classify_engine_outcome,
        quota_batch=quota_batch,
        note_quota_exhausted=_note_remote_quota_exhausted,
        per_engine_budget_s=_PER_ENGINE_BUDGET_S,
        fast_budget_s=_FAST_TOTAL_BUDGET_S,
        auto_budget_s=_AUTO_TOTAL_BUDGET_S,
        primary_grace_s=_PRIMARY_GRACE_S,
        straggler_grace_s=_straggler_grace(),
        serial_stagger_s=_serial_stagger(),
    )
    raw_results = _dispatch.raw_results
    engine_outcomes = _dispatch.engine_outcomes
    engine_latency = _dispatch.engine_latency
    wasted_ms = _dispatch.wasted_ms
    useful_ms = _dispatch.useful_ms
    early_stopped = _dispatch.early_stopped
    budget_used_ms = _dispatch.budget_used_ms
    budget_total_ms = _dispatch.budget_total_ms
    # 融合后的 D6 补搜要用同一套「跑单引擎 + 入账」，钩子由此取回
    _run_one = _dispatch.run_one
    _ingest = _dispatch.ingest

    elapsed = int((time.time() - t0) * 1000)
    _tock(timing, "dispatch", _tk_dispatch)
    _tk_fusion = _tick(timing)

    # 融合
    valid_lists = [
        res for res in raw_results.values()
        if res and any(isinstance(r, dict) and "error" not in r for r in res)
    ]
    # 去掉 error-only 列表中的 error 条目
    clean_lists = []
    for res in valid_lists:
        clean = [r for r in res if isinstance(r, dict) and "error" not in r]
        if clean:
            clean_lists.append(clean)

    # 查询主语言：噪声门与语言能力加权共用同一个判定（唯一来源，
    # 避免两处各自算导致行为漂移）。
    _q_lang_for_fusion = (
        ((decision or {}).get("features") or {}).get("primary_lang") or None
    )

    # ── 多语言噪声门（result_lang）─────────────────────────────────────
    # 实测问题：引擎在非支持语言下会返回「成功但不相关」的结果——
    # juejin 在阿拉伯语查询下返回 10 条、相关度 0.00（全是通用热帖），
    # 却报告 status=ok。这类噪声混进 RRF 融合会污染最终结果。
    #
    # 判定不依赖「猜测查询语言」：用结果自身的书写系统（Unicode 码位，
    # 确定性）+ 查询词元命中率。语言不符且相关度低 → 判为噪声并剔除。
    #
    # 只在「查询语言可判定且非中英」时启用：中英是 argo 的主战场，
    # 现有引擎面足够宽，过早过滤会误伤（如英文查询命中中文优质内容）。
    _noise_dropped: list[dict[str, Any]] = []
    try:
        from result_lang import assess_results as _assess_lang
        _q_lang = _q_lang_for_fusion
        if _q_lang and _q_lang not in ("zh", "en", "mixed", "other", ""):
            _kept = []
            for _lst in clean_lists:
                if not _lst:
                    continue
                _eng = _lst[0].get("_engine") or _lst[0].get("source") or "?"
                _a = _assess_lang(query, _lst, expected_lang=_q_lang)
                if _a["verdict"] == "noise":
                    _noise_dropped.append({
                        "engine": _eng, "lang": _a["lang"],
                        "relevance": _a["relevance"],
                        "reason": (_a["reasons"][0] if _a.get("reasons")
                                   else "low relevance"),
                    })
                    continue
                _kept.append(_lst)
            clean_lists = _kept
    except Exception as _e:  # 噪声门是质量增强，失败不得拖垮主流程
        import logging as _lg
        _lg.getLogger("unified_search.search").debug(f"噪声门跳过: {_e}")

    if len(clean_lists) > 1:
        merged = rrf_merge(clean_lists, lang=_q_lang_for_fusion)
    elif clean_lists:
        merged = deduplicate_by_url(clean_lists[0])
        # 单引擎也补 consensus
        for r in merged:
            eng = r.get("_engine") or r.get("source") or ""
            if eng:
                r.setdefault("consensus_engines", [eng])
    else:
        merged = []

    # ── D6：macro_data 域证据下限（事实核查防单源）─────────────────────
    # deep 研究场景下结果 <2 条说明结构化源未覆盖该查询：追加通用保底引擎
    # 补证据，避免「单引擎单结果」被事实核查 / 融合阶段当作答案；补搜结果
    # 一并进 RRF，consensus 维度天然加权。
    if (domain == "macro_data" and merged and len(merged) < 2
            and depth in ("deep", "research")):
        _done = set(raw_results.keys())
        _cands = [
            e for e in ("anysearch", "duckduckgo", "local_bing")
            if e not in _done
            and e in set(available_engines())
            and (breaker is None or breaker.allow(e)[0])
        ]
        _extra_lists = []
        for _eng in _cands[:2]:
            _e, _res, _out, _lat = _run_one(_eng)
            _ingest(_e, _res, _out, _lat)
            _goods = [r for r in _res if isinstance(r, dict) and "error" not in r]
            if _goods:
                _extra_lists.append(_goods)
        if _extra_lists:
            merged = rrf_merge([merged] + _extra_lists)

    # 配额记账：整批一次写入文件（同一次搜索的 N 个引擎合并为一次写）。
    # 必须放在 D6 补搜之后：那是最后一个 _ingest 调用点，flush 提前会让
    # 补搜引擎的记账永远落不了盘（2026-09-13 审查实锤）。
    quota_batch.flush()
    _tock(timing, "fusion", _tk_fusion)
    _tk_dedupe = _tick(timing)

    # ── P0：过滤 SERP/跳转 URL（搜索结果页、baidu.com/link 等不可当信源正文）──
    if merged:
        try:
            from evidence import is_serp_or_jump_url as _is_serp
            merged = [r for r in merged if not _is_serp(r.get("url", ""))]
        except ImportError:
            pass  # evidence 不可用时跳过（本地五维 rerank 已对 SERP 降权）

    # ── minhash 近重复去重（结果级，RRF 后 / SERP 后）─────────────────────
    # max_keep 与下方放宽截断同源：去重只需要保证「前 N 条非重复」正确，
    # 因为多出来的条目下一行就被截掉了。不设上限时是 O(结果数²) 的两两比较。
    _pool_limit = _rerank_pool_limit(max_results)
    minhash_removed = 0
    if merged and len(merged) > 1:
        try:
            deduped, minhash_removed = minhash_dedupe(merged, max_keep=_pool_limit)
            merged = deduped
        except Exception as _e:
            import logging
            logging.getLogger("unified_search").debug(f"minhash 去重跳过: {type(_e).__name__}")
    _tock(timing, "dedupe", _tk_dedupe)
    # 漏斗第 4 格：跨引擎合并 + 近重复去重之后还剩多少（见 build_funnel）
    _funnel_deduped = len(merged)
    # 三个子段各自计时，**不能共用一个 tick**：
    # `filter`（否定词 + 时间窗过滤）是纯本地遍历；`recovery` 是**网络调用**
    # （域路由零结果时的救援链，见下）；`rerank` 是纯本地重排。此前三者共用
    # 一个 tick 且整个挂在 "rerank" 名下，实测「python 怎么读csv」的 rerank
    # 阶段报 706ms（占全查询 40%），而真正的重排只用 3.6ms——那 700ms 是恢复
    # 链打的一次 anysearch。`--explain-timing` 的全部价值就是回答「瓶颈在哪」，
    # 报错桶等于把人指向错误的优化对象。
    _tk_filter = _tick(timing)

    # ── P2：多语言语言偏好软排序（ja/ko 前置含目标语言字符结果，软排不删除）──
    try:
        _p_lang = (decision or {}).get("features", {}).get("primary_lang")
        if _p_lang in ("ja", "ko"):
            merged = _lang_prefer_rerank(merged, _p_lang)
    except Exception:
        pass

    # 放宽截断：rerank 阶段看到 _pool_limit 条，最终输出再截断 max_results
    merged = merged[:_pool_limit]

    # ── P0-001：按 exclude_terms 过滤（否定约束）──
    excluded_count = 0
    if merged and exclude_terms:
        kept = []
        low_terms = [t.lower() for t in exclude_terms if t]
        for r in merged:
            hay = f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}".lower()
            if any(t in hay for t in low_terms):
                excluded_count += 1
                continue
            kept.append(r)
        merged = kept

    # ── 时间窗结果后过滤保底 ──
    # 仅当组合内含带时间能力引擎时执行：无时间字段引擎的结果没有可滤对象，
    # 跳过遍历省开销；语义上与缓存键隔离保持一致（7d/30d 共享同一缓存内容）。
    time_filtered = 0
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        merged, time_filtered = _apply_time_window(merged, since_ts, until_ts)
        if time_filtered:
            import logging
            logging.getLogger("unified_search").debug(
                f"时间窗后过滤剔除 {time_filtered} 条（since={since_iso}, until={until_iso}）")
    # 漏斗第 5 格：否定词过滤 + 时间窗过滤之后（见 build_funnel）
    _funnel_filtered = len(merged)
    _tock(timing, "filter", _tk_filter)

    # D5：时间窗空操作告警——用户指定了时间窗，组合内含时间能力引擎，
    # 但结果没有任何 published_at（下推缺失/源端未返回）：
    # `--since/--until` 实际未生效（宽松策略保留无时间字段条目，
    # time_filtered 恒 0）。透传 warning 而非静默降级。
    # 组合内不含时间能力引擎时是已知常态，不重复告警。
    time_filter_warning: str | None = None
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        if not any(r.get("published_at") for r in merged):
            time_filter_warning = (
                f"引擎未返回 published_at，时间窗 {since_iso or '任意'} ~ "
                f"{until_iso or '任意'} 未实际过滤"
            )

    # ── P0-002：空结果错误恢复决策树 ──
    recovery_info: dict[str, Any] | None = None
    # 复杂度门控：低复杂度查询只允许低成本放宽（L1/L2），
    # 禁用高价多源/跨语言（L3/L4）——简单问题不搞多轮，省 token。
    _max_rec_level: str | None = None
    try:
        from query_enhance import complexity_gate
        if qu is not None and complexity_gate(query, qu) == "low":
            _max_rec_level = "L2"
    except Exception:
        pass
    _tk_recovery = _tick(timing)
    _recovery_engines: set[str] = set()
    if not merged:
        try:
            from recovery import run_recovery
            tried = list(raw_results.keys()) or list(engines)
            fallback_engines = decision.get("engines_fallback") or []
            # 域路由零结果：恢复链放行 L3 换引擎。复杂度门此前把简单查询
            # 压到 L2——L3 被禁 + 全域零结果 = 域命中查询无解（实测
            # macro_data「中国GDP」零结果、恢复链空转）。engines_fallback
            # 里是路由的定向保底声明（域未试成员优先），代价可控。
            rec_level = _max_rec_level
            if fallback_engines and decision.get("domain") not in (
                    None, "", "general", "general_search") \
                    and (rec_level is None or rec_level < "L3"):
                rec_level = "L3"
            try:
                enabled_set = set(available_engines())
            except Exception:
                enabled_set = None

            def _recovery_executor(rq: str, rengines: list[str]) -> list[dict[str, Any]]:
                """恢复执行器：串行跑候选引擎，取首个非空。跳过缓存避免污染。"""
                out: list[dict[str, Any]] = []
                for eng in rengines:
                    # 记下真正发出的调用：恢复段走的不是 dispatch，若不单独记账，
                    # 漏斗会算出「routed 2 → called 2 → returned 5」这种自相矛盾的
                    # 账（实测「python 怎么读csv」），而这正是最需要被看见的路径。
                    _recovery_engines.add(eng)
                    try:
                        # 恢复路径同样携带时间窗，避免恢复时丢弃用户约束；
                        # --no-cache 也要透传：本函数的契约是「跳过缓存避免污染」，
                        # 不透传会让恢复段既读到用户明确拒绝的旧缓存，又把自己的
                        # 临时查询写进缓存（dispatch 那条路径是透传的，两处不一致）。
                        res = engine_search(rq, eng, n=max_results,
                                            timeout=timeout, depth=depth, mode=mode,
                                            since=since_iso, until=until_iso,
                                            skip_cache=skip_cache)
                    except Exception:
                        res = []
                    goods = [r for r in (res or [])
                             if isinstance(r, dict) and "error" not in r]
                    if goods:
                        for r in goods:
                            r.setdefault("_engine", eng)
                            r.setdefault("_recovered", True)
                        out.extend(goods)
                        break
                return out

            rec_results, rec_result = run_recovery(
                query, tried, _recovery_executor,
                engines_fallback=fallback_engines, enabled=enabled_set, mode=mode,
                max_level=rec_level)
            recovery_info = rec_result.to_dict()
            # P2-6：恢复遥测——query 截断脱敏，只记概览不记明细
            if _emit_telemetry is not None:
                try:
                    _emit_telemetry("recovery", {
                        "query": (query[:60] if query else query),
                        "triggered": recovery_info.get("triggered"),
                        "recovered": recovery_info.get("recovered"),
                        "level_used": recovery_info.get("level_used"),
                        "strategy_used": recovery_info.get("strategy_used"),
                        "steps_tried": len(recovery_info.get("steps_tried") or []),
                        "final_query": (recovery_info.get("final_query") or "")[:60],
                        "note": recovery_info.get("note", ""),
                    })
                except Exception:
                    pass
            if rec_results:
                merged = deduplicate_by_url(rec_results)[:max_results]
                # 恢复引擎按引擎分组记回 raw_results：engines_used 此前不含
                # 救援引擎（provenance 断链，实测恢复成功后 engines_used 仍
                # 只列原 combo），自适应学习也看不到恢复成功信号。
                _rec_by_eng: dict[str, list] = {}
                for r in merged:
                    eng = r.get("_engine") or r.get("source") or ""
                    if eng:
                        r.setdefault("consensus_engines", [eng])
                        _rec_by_eng.setdefault(eng, []).append(r)
                for _eng, _lst in _rec_by_eng.items():
                    raw_results.setdefault(_eng, _lst)
        except ImportError:
            pass  # recovery 模块不可用
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"错误恢复跳过: {type(e).__name__}")

    # Reranker：ARGO_LOCAL_RERANK 开关（0 关闭本地五维保底；默认 1 开启）
    _tock(timing, "recovery", _tk_recovery)
    _tk_rerank = _tick(timing)
    local_rerank_on = env_flag("ARGO_LOCAL_RERANK")
    reranker_status = "skipped_short"
    rank_method = "none"
    if mode == "fast" or depth == "fast":
        reranker_status = "skipped_fast"
    elif merged and len(merged) > 1:
        # 全量重排（top_n=len），由最终输出统一截断 max_results
        merged, reranker_status = rerank_results(query, merged, top_n=len(merged))
        if reranker_status == "ok":
            rank_method = "bocha"

    # 本地五维 rerank 保底：受 ARGO_LOCAL_RERANK 开关控制（可观测 rank_method）。
    # 触发计算方式走 _RERANK_DEGRADED_STATUSES 唯一来源：新增「bocha 未出排序」的
    # 状态时只改那一处，不会漏掉这里的保底（漏了就是既不精排也不保底）。
    if local_rerank_on and merged and len(merged) > 1 and \
            reranker_status in _RERANK_DEGRADED_STATUSES:
        try:
            # top_n=max_results（而非 len(merged)）：下游 _apply_consensus_and_sort
            # 紧接着就按 max_results 截断，尾部不会有人用。传真实 K 才能启用
            # 函数内的 K 相关剪枝（可证明前 K 个选序不变），n=200 时把 O(n²)
            # 贪心从 ~33ms 压到毫秒级。
            merged = local_five_dim_rerank(query, merged, domain=domain,
                                           top_n=max_results)
            rank_method = "local_five_dim"
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"本地五维 rerank 跳过: {type(e).__name__}")
    elif not local_rerank_on and reranker_status in _RERANK_DEGRADED_STATUSES:
        rank_method = "none"

    _tock(timing, "rerank", _tk_rerank)
    _tk_signals = _tick(timing)

    if merged:
        merged = _apply_consensus_and_sort(merged, max_results)

    _attach_selection_signals(merged, mode, depth)

    # ── P0-004：关键事实交叉标记（仅 deep/auto 且结果 ≥3；fast 跳过）──
    fact_alignment: dict[str, Any] | None = _align_facts_safe(merged, mode, depth)

    _tock(timing, "signals", _tk_signals)
    _tk_cache_write = _tick(timing)

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    # 阶段漏斗账（见 build_funnel）。在写缓存之前算一次、两处消费：
    # 既进 result_payload（缓存命中时它随存档一起返回，否则命中路径的漏斗会
    # 缺席——而「缺席」比数字更容易被误读成「没数据」），也进本次输出。
    # 此处 len(merged) 已等于最终条数：_apply_consensus_and_sort 在上一步已按
    # max_results 截断，而 _sort_results 只重排、不改长度。
    funnel = build_funnel(
        len(engines),
        # 「实际发起调用」= 编排层跑过的引擎 ∪ 恢复链补调的引擎。
        # 此前只数 engine_outcomes/raw_results，恢复段走的不是 dispatch、
        # 两条都不进 → 实测「python 怎么读csv」报出 routed 2 → called 2 →
        # returned 5：两个引擎交出了 5 条结果，而真凶（恢复链打的 anysearch）
        # 在漏斗里根本不存在。漏斗的全部用途就是定位塌陷点，一个会漏掉真凶的
        # 漏斗比没有更危险。
        len({o.get("engine") for o in engine_outcomes if o.get("engine")}
            | set(raw_results) | _recovery_engines),
        sum(1 for items in raw_results.values() if isinstance(items, list)
            for it in items
            if isinstance(it, dict) and "error" not in it),
        _funnel_deduped, _funnel_filtered, len(merged),
    )

    result_payload = {
        "results": merged,
        "engines_used": list(raw_results.keys()),
        # 产出这批结果的**那次运行**的组合与理由，随载荷一起存。
        #
        # 缓存键改成请求身份后，同一查询的两次运行可以路由到不同组合，而命中
        # 时本次路由根本没执行。不存这两个字段的话，命中响应只能报本次路由，
        # 那是一个没跑过的计划，还会与同样来自缓存的 engines_used /
        # engine_outcomes 自相矛盾（2026-09-19）。约 100 字节/条，换来缓存
        # 条目自描述。旧条目没有这两个键，命中路径回退到本次路由（原行为）。
        "engines": list(engines),
        "engines_combo": list(engines_combo),
        "route_reason": decision.get("reason"),
        "domain": domain,
        "engine_outcomes": engine_outcomes,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "noise_dropped": _noise_dropped,
        "funnel": funnel,
    }

    # 写 combo 缓存：空结果短 TTL / 时效 cap 由 cache.set 处理
    if not skip_cache:
        effective_ttl = None
        if merged and elapsed > 2000:
            # 慢查询略延长缓存：省的是「同一查询再付一次慢网」的钱。
            effective_ttl = _slow_query_ttl(cache.resolve_ttl(domain, query=query),
                                            elapsed)
        cache.set(
            query, cache_engine_key, max_results, result_payload,
            domain=domain, ttl=effective_ttl, mode=mode, depth=depth,
        )
    _tock(timing, "cache_write", _tk_cache_write)

    # 自适应学习
    try:
        from adaptive import get_learner
        learner = get_learner()
        # 引擎结局映射：下面要区分「空结果」与「真失败」，需要它
        _status_of = {o.get("engine"): o.get("status")
                      for o in engine_outcomes if isinstance(o, dict)}
        for eng, res in raw_results.items():
            errors = [str(r.get("error", "")) for r in res if isinstance(r, dict) and "error" in r]
            # 配额/鉴权类是配置态故障，不是引擎质量信号：计入会把恢复后的
            # 引擎分数毒化在历史失败里（byted 配额期 38 连败 → 分数 0.072，
            # 配额自愈后无流量刷正分，死锁）。此类错误不计入，保持中性。
            # 配额关键词走 _QUOTA_ERROR_KEYWORDS 唯一来源；鉴权类仅此处有。
            if errors and all(
                any(k in msg.lower() for k in
                    (*_QUOTA_ERROR_KEYWORDS, "unauthorized", "api key",
                     "forbidden", "401", "403"))
                for msg in errors
            ):
                continue
            success = bool(res and any(isinstance(r, dict) and "error" not in r for r in res))
            # 「引擎正常、但这次查询它没有结果」与「引擎失败」是两回事：
            # 前者说明的是查询与源不匹配，不是源坏了。混在一起会误杀——实测
            # github 窗口内 63 次调用的失败归因全是 empty，它却在 local_code /
            # package_search 这些**声明要用它**的域里因分数低于 0.3 被整个剔除
            # （同类还有 wikipedia 0.17 / openalex 0.29 / hackernews 0.10 /
            # twitter 0.17 / open_library 0.17，共 16 个域受影响）。
            # 空结果只单独统计，不参与成功率计算（见 adaptive.get_score）。
            empty = (not success) and _status_of.get(eng) in (
                "no-results", "no-results-cached")
            latency = engine_latency.get(eng, elapsed / max(len(raw_results), 1))
            cost = get_cost_factor(eng)
            learner.record(eng, success=success, latency_ms=latency,
                           cost=0.0 if cost >= 0.85 else 0.001, empty=empty)
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"自适应学习记录跳过: {type(e).__name__}")

    # 语言偏好：记录本轮查询语 + 输出观测快照（默认中英 + 系统 + 习惯）
    lang_pref_info: dict[str, Any] | None = None
    try:
        from lang_pref import record_query_lang, lang_pref_snapshot
        feats = decision.get("features") or {}
        q_lang = feats.get("primary_lang") or ""
        if not q_lang:
            try:
                from lang_detect import detect_language
                q_lang = detect_language(query)
            except ImportError:
                q_lang = ""
        if q_lang:
            record_query_lang(q_lang)
        lang_pref_info = lang_pref_snapshot(query_lang=q_lang)
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"语言偏好记录跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    # 排序在返回前、写缓存后：缓存内容保持 score 序（缓存键/内容不受 sort 影响），
    # sort 只改变本次展示顺序；缓存命中路径在 return 前同样处理，两路径行为一致。
    out_results = _sort_results(merged, sort)

    out: dict[str, Any] = {
        "query": query, "engine": engine_label, "engines": engines,
        "engines_combo": engines_combo, "cached": False,
        "domain": domain, "elapsed_ms": elapsed,
        "tfidf_scores": tfidf_scores,
        "route_reason": decision.get("reason"),
        "results": out_results,
        "count": len(out_results), "engines_used": list(raw_results.keys()),
        "errors": _collect_errors(raw_results, engine_outcomes),
        "engine_outcomes": engine_outcomes,
        # 阶段漏斗账：0 结果时用它定位塌在哪一层（见 build_funnel）
        "funnel": funnel,
        # 多语言噪声门：被剔除的引擎及原因（可观测，便于定位「为什么少了几个源」）
        "noise_dropped": _noise_dropped,
        "wasted_engine_ms": wasted_ms,
        "early_stopped": early_stopped,
        "reranker": reranker_status,
        "rank_method": rank_method,
        "minhash_removed": minhash_removed,
        "local_rerank_on": local_rerank_on,
        "recovery": recovery_info,
        "fact_alignment": fact_alignment,
        "exclude_terms": exclude_terms,
        "excluded_count": excluded_count,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "mode": mode, "depth": depth,
        "login_hint": decision.get("login_hint"),
    }
    if lang_pref_info is not None:
        out["lang_pref"] = lang_pref_info
    if timing is not None:
        # 并发效率：dispatch 是墙钟，engine_latency 之和是各引擎各自耗时。
        # 比值远小于引擎数说明并发没排满（或某个慢源独占尾部），是判断
        # 「该加并发还是该摘慢源」的直接依据。
        eng_sum = sum(engine_latency.values())
        out["timing"] = timing.summary()
        out["timing"]["elapsed_ms"] = elapsed
        out["timing"]["dispatch"] = {
            # 用 dispatch 自己那口单调钟量出来的墙钟（budget_used_ms），不用外层
            # 这笔 time.time() 差值：useful/wasted 都是单调钟算的，混用两种钟会
            # 让「useful + wasted ≡ wall」只在毫秒取整恰好对齐时成立——而这条
            # 恒等式正是测试与文档承诺的「唯一自洽的墙钟分解」。
            "wall_ms": budget_used_ms,
            "engines_run": len(engine_latency),
            "engine_sum_ms": eng_sum,
            "parallel_efficiency": (round(eng_sum / budget_used_ms, 2)
                                    if budget_used_ms else None),
            # useful_ms + wasted_ms ≡ wall_ms（唯一自洽的墙钟分解）。
            # useful = 最后一个有效贡献引擎完成的时刻，wasted = 此后还在等。
            "useful_ms": useful_ms,
            "wasted_ms": wasted_ms,
            "early_stopped": early_stopped,
        }
        if budget_total_ms is not None:
            # 预算消耗额：fast/auto 有总预算，deep 无（键缺席即「无预算」）。
            # timing 在 agent 档保留，预算可见性随答案一起到达。
            out["timing"]["budget"] = {"used_ms": budget_used_ms,
                                       "total_ms": budget_total_ms}
    return out






# 不算失败的 outcome 状态：这些情况「引擎跑了、没问题」，不该出现在 errors[]
_NON_ERROR_OUTCOME = frozenset({
    "ok", "ok-cached", "partial", "no-results", "no-results-cached",
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
            import logging
            logging.getLogger("unified_search").debug(
                f"envelope 跳过: {type(e).__name__}")
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
        logging.getLogger("unified_search").debug(f"本地正文索引跳过: {type(_e).__name__}")

    # 证据完整链路 P0：回填已核验证据分 + 高后果门控（finance/health/legal）
    # 输出 fetch_required / evidence_loop 汇总，每条结果带 fetch_suggested
    # 与 has_fetched_evidence / post_fetch_absorption（若此前 fetch 过）。
    try:
        from evidence_loop import gate_results
        gate = gate_results(result.get("results") or [], result.get("domain"))
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
            import logging
            logging.getLogger("unified_search").debug(f"不可取源筛选跳过: {type(e).__name__}")
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"证据门控跳过: {type(e).__name__}")

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
            logging.getLogger("unified_search").debug(
                f"[domain-filter] {type(e).__name__}: {e}")

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
            logging.getLogger("unified_search").debug(
                f"[include-local] {type(e).__name__}: {e}")
        if local_hits:
            result.setdefault("results", []).extend(local_hits)
            result["local_results"] = local_hits
        result["include_local"] = True

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
