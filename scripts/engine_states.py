#!/usr/bin/env python3
"""engine_states.py — 引擎状态词表（单一真源）。

## 为什么需要它

Argo 的状态判定散落在 6 个模块里，同一状态名有多套写法（half_open 与
half-open 混用、status 与 verdict 两套词表、outcome 与 failure kind 各一份）。
后果不是「不好看」，而是**改状态名要改 N 处**：给熔断器加一个状态，要同时改
circuit_breaker 的转换逻辑、engine_health 的分诊、engine_status 的展示、
engine_dispatch 的探测收紧、route 的扶正判断、badcase_ledger 的过滤——漏一处
就是一个静默失配的状态（如 2026-11-08 的 auth 连击失配）。

## 设计原则

- 每个状态**只有一个名字**，所有模块从这里 import，不手写字面量。
- 状态词表按**机器（状态机）**组织，不按模块组织：一个状态机的全部状态与
  转换条件放在一起，读一遍就能理解它的生命周期。
- 只定义词表与纯判定函数（is_xxx / should_xxx），不碰 IO、不碰持久化。

## 覆盖的状态机

1. 熔断器（breaker）：closed → open → half_open → closed / disabled
2. 引擎配置（config）：enabled / disabled（声明层）
3. 环境白黑名单（env）：allowed / filtered
4. 准入（admission）：admitted / blocked
5. 路由可用性（routable）：可路由 / 不可路由（派生判定，非独立状态机）
6. 运行时健康（health verdict）：auth_down / suspect / auto_disabled /
   flapping / watch / unknown（分诊视图，派生自 breaker + adaptive）
7. 调用结局（outcome status）：ok / ok-cached / partial / no-results /
   timeout / error / auth-failed / quota-exhausted / rate-limited / blocked /
   skipped-* / cancelled（每次引擎调用的结局）
8. 失败归因（failure category）：dependency / auth / rate_limited /
   quota_exhausted / blocked / upstream / network / unknown
9. 抓取降级链（fetch method）：http_md / http / tls / jina / wayback /
   browser / chrome_cdp / parallel_mcp / jsrun_challenge / sogou_fallback /
   wechat / fulltext / blocked
10. 重试判据（retry）：retryable / non_retryable（派生自 outcome）
11. 引擎清单展示态（§2b，engine_detail.status）：ready / unknown / disabled /
    env_filtered / missing_key / missing_dep / quota_exhausted / blocked /
    admitted（CLI 与 ZCode 面板共用）
12. 平台/运行时状态（§10，dsh-plugin / daemon）：running / stopped / idle /
    busy（daemon ping/status 直接输出，ZCode 桥消费）；ready / loading /
    unavailable（JS 侧仍写死）
13. 精排器（reranker）：ok / fallback / skipped_no_key / skipped_short /
    skipped_fast / skipped_circuit_open / skipped_quota / skipped_cache
    （2026-11-09 收口：此前 8 个值散在 search_rank / search_pipeline /
    search_entry 三处，降级集合另有一份手抄字面量）
14. 搜索计划（plan）：ready / handoff_required / needs_authorization
    （plan.py 产出，search / search_output / search_cli 消费）
15. 批处理预检（batch probe）：条目 unsupported / needs_auth / already_have /
    not_found / unknown；结论 go / go_with_skips / stop
16. 数值印证（numeric verdict）：confirmed / partial / missing / unfetchable
17. 取证覆盖（coverage）：COVERED / PARTIAL / NOT_COVERED
18. 健康探针（probe status）：available / degraded / unavailable / unknown
    （health_probe 落库值；health_check 只写布尔 available）
19. 缓存层级（cache level）：L1 / L2 / L?（未知兜底）
20. 候选核验（verification）：candidate / opened
21. 观察模式变更（watch change）：none / noise / substantive / unknown
22. 候选信封路由（envelope route）：completed / partial

## 词表边界（不要把两套词表合并）

同一件事在不同层有不同名字，是**接口契约**，不是重复真源：

- outcome.status（连字符）：`quota-exhausted` / `auth-failed` / `rate-limited`
- engine_detail.status（下划线）：`quota_exhausted` / `missing_key` /
  `missing_dep` / `env_filtered` / `admitted` / `disabled`
- failure category（下划线）：`quota_exhausted` / `rate_limited`

engine_status.py 的 status 是 CLI 展示态，含 outcome 没有的 `missing_*` 值；
把 outcome 常量填进去会同时破坏既有展示口径与下游断言（2026-11-09 实测）。
各状态机只在自己的词表内收口，跨层映射用显式字典，不用「同一个名字」。
"""

from __future__ import annotations

# ── 1. 熔断器状态（breaker）────────────────────────────────────────────────
# 状态生命周期：
#   closed ──(failures≥threshold)──> open ──(冷却到期)──> half_open
#     ↑                                │                      │
#     │                                │                      ├─成功→ closed
#     │                                │                      └─失败→ open
#     │                                │
#     │                                └──(opens≥DISABLE_AFTER_OPENS)──> disabled
#     │                                                                 │
#     └──────────────────(冷却到期/换 key/手动 revive)────────────────────┘
#
# 状态名**只用下划线**（half_open）：与持久化字段一致，避免 half-open 与
# half_open 两套写法（历史上 engine_dispatch 写 half_open、文档写 half-open）。

BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"
BREAKER_HALF_OPEN = "half_open"
BREAKER_DISABLED = "disabled"

BREAKER_STATES = frozenset({
    BREAKER_CLOSED, BREAKER_OPEN, BREAKER_HALF_OPEN, BREAKER_DISABLED,
})

# 允许被路由的熔断状态（半开探测与关闭态都放行；open/disabled 由 allow() 拒绝）
BREAKER_ROUTABLE_STATES = frozenset({BREAKER_CLOSED, BREAKER_HALF_OPEN})


def is_breaker_routable(state: str) -> bool:
    """该熔断状态是否允许调用（不含冷却期等动态条件）。"""
    return state in BREAKER_ROUTABLE_STATES


# ── 2. 引擎配置状态（config）───────────────────────────────────────────────
# 声明层：enabled 是 spec 的字段，disabled 是它的反面。
# 与 breaker 的 disabled 区分：config disabled 是人工停用（disabled_reason），
# breaker disabled 是自动禁用（opens 达阈值）。

CONFIG_ENABLED = "enabled"
CONFIG_DISABLED = "disabled"


# ── 2b. 引擎清单展示态（engine_detail.status）─────────────────────────────
# `scripts/engine_status.py` 的 engine_detail() 输出这一列；CLI 表格、
# `--list-engines --json` 与 ZCode 面板都读它。它回答「现在能不能自动用、
# 不能用的原因是什么」，与调用结局 outcome.status 不是同一个问题：
# 这里有 missing_* 这类诊断态，outcome 里没有；outcome 里有 ok/partial
# 这类交付态，这里没有。故单列词表，禁止把 outcome 常量填进来（见文件头
# 「词表边界」）。
#
# 判定顺序即优先级（engine_detail 的 if/elif 链），改顺序要同时改这里。

ENGINE_STATUS_READY = "ready"
ENGINE_STATUS_UNKNOWN = "unknown"
ENGINE_STATUS_DISABLED = "disabled"
ENGINE_STATUS_ENV_FILTERED = "env_filtered"
ENGINE_STATUS_MISSING_KEY = "missing_key"
ENGINE_STATUS_MISSING_DEP = "missing_dep"
ENGINE_STATUS_QUOTA_EXHAUSTED = "quota_exhausted"
ENGINE_STATUS_BLOCKED = "blocked"
ENGINE_STATUS_ADMITTED = "admitted"

ENGINE_DETAIL_STATUSES = frozenset({
    ENGINE_STATUS_READY, ENGINE_STATUS_UNKNOWN, ENGINE_STATUS_DISABLED,
    ENGINE_STATUS_ENV_FILTERED, ENGINE_STATUS_MISSING_KEY,
    ENGINE_STATUS_MISSING_DEP, ENGINE_STATUS_QUOTA_EXHAUSTED,
    ENGINE_STATUS_BLOCKED, ENGINE_STATUS_ADMITTED,
})

# 引擎不可自动路由的归因（search_cli._ENGINE_BLOCK_REASONS 与
# py_bridge._engine_row 共用）。与展示态的区别：这里是「为什么被拦」，
# 六态里不含 ready/admitted。以本表为唯一来源，两侧不再各写一份字面量。
ENGINE_BLOCK_DISABLED = "disabled"
ENGINE_BLOCK_MISSING_ENV = "missing_env"
ENGINE_BLOCK_MISSING_DEPS = "missing_deps"
ENGINE_BLOCK_QUOTA_EXHAUSTED = "quota_exhausted"
ENGINE_BLOCK_NOT_ROUTABLE = "not_routable"

ENGINE_BLOCK_REASONS = frozenset({
    ENGINE_BLOCK_DISABLED, ENGINE_BLOCK_MISSING_ENV,
    ENGINE_BLOCK_MISSING_DEPS, ENGINE_BLOCK_QUOTA_EXHAUSTED,
    ENGINE_BLOCK_NOT_ROUTABLE,
})


# ── 3. 环境白黑名单（env）─────────────────────────────────────────────────
ENV_ALLOWED = "allowed"
ENV_FILTERED = "env_filtered"


# ── 4. 准入（admission）───────────────────────────────────────────────────
ADMISSION_ADMITTED = "admitted"
ADMISSION_BLOCKED = "blocked"
ADMISSION_UNVERIFIED = "unverified"  # 未做准入验证（兼容旧引擎）


# ── 5. 运行时健康分诊（health verdict）─────────────────────────────────────
# 派生视图：由 breaker 状态 + adaptive 分数 + 归因综合判定，不是独立状态机。
# 严重度顺序即排查顺序。

HEALTH_AUTH_DOWN = "auth_down"          # 结构性失败（auth/quota）已停用
HEALTH_SUSPECT = "suspect"              # 空结果基线破位（疑似失效）
HEALTH_AUTO_DISABLED = "auto_disabled"  # 连续失败自动禁用
HEALTH_FLAPPING = "flapping"            # 处于 open 冷却
HEALTH_WATCH = "watch"                  # 近期有失败但未熔断
HEALTH_UNKNOWN = "unknown"              # 无证据（低流量或一直健康）

HEALTH_VERDICTS = frozenset({
    HEALTH_AUTH_DOWN, HEALTH_SUSPECT, HEALTH_AUTO_DISABLED,
    HEALTH_FLAPPING, HEALTH_WATCH, HEALTH_UNKNOWN,
})

# 严重度排序（越靠前越需要人看）；不在表内的 verdict 排末尾
HEALTH_SEVERITY_ORDER = (
    HEALTH_AUTH_DOWN, HEALTH_SUSPECT, HEALTH_AUTO_DISABLED,
    HEALTH_FLAPPING, HEALTH_WATCH,
)


def health_severity_index(verdict: str) -> int:
    """verdict 的严重度序号（越小越严重）；未知 verdict 排末尾。"""
    try:
        return HEALTH_SEVERITY_ORDER.index(verdict)
    except ValueError:
        return len(HEALTH_SEVERITY_ORDER)


# ── 6. 调用结局（outcome status）───────────────────────────────────────────
# 每次引擎调用的结局。引擎级粒度，进 engine_outcomes / 漏斗 / 熔断记账。

OUTCOME_OK = "ok"
OUTCOME_OK_CACHED = "ok-cached"
OUTCOME_PARTIAL = "partial"
OUTCOME_NO_RESULTS = "no-results"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_AUTH_FAILED = "auth-failed"
OUTCOME_QUOTA_EXHAUSTED = "quota-exhausted"
OUTCOME_RATE_LIMITED = "rate-limited"
OUTCOME_BLOCKED = "blocked"
OUTCOME_SKIPPED_PREFIX = "skipped-"
OUTCOME_SKIPPED_MISSING_ENV = "skipped-missing-env"
OUTCOME_SKIPPED_QUOTA = "skipped-quota-exhausted"
OUTCOME_SKIPPED_CIRCUIT_OPEN = "skipped-circuit-open"
OUTCOME_SKIPPED_BUDGET = "skipped-budget"
OUTCOME_CANCELLED = "cancelled"

# 对最终答案有贡献的结局（RRF 融合与 wasted 记账共用这张表）
CONTRIBUTING_OUTCOMES = frozenset({
    OUTCOME_OK, OUTCOME_OK_CACHED, OUTCOME_PARTIAL,
})

# 熔断器 failure kind：熔断策略用的粗粒度标签，与 outcome 不是一个词表
# （outcome 有 12 值，kind 只有 5 值）。此前只在 OUTCOME_TO_BREAKER_KIND
# 的 value 里出现，circuit_breaker.record_failure 仍裸比字面量，改一个值
# 漏一处即静默改变开闸行为（2026-11-09 审计）。
BREAKER_KIND_EMPTY = "empty"
BREAKER_KIND_TIMEOUT = "timeout"
BREAKER_KIND_BLOCKED = "blocked"
BREAKER_KIND_RATE_LIMITED = "rate-limited"
BREAKER_KIND_ERROR = "error"

BREAKER_KINDS = frozenset({
    BREAKER_KIND_EMPTY, BREAKER_KIND_TIMEOUT, BREAKER_KIND_BLOCKED,
    BREAKER_KIND_RATE_LIMITED, BREAKER_KIND_ERROR,
})

# 不累计 opens 的 kind：查询级/源站级信号，不是引擎持续故障。
# circuit_breaker.record_failure 的 drives_opens 判定与此处同源。
NON_DRIVING_BREAKER_KINDS = frozenset({
    BREAKER_KIND_EMPTY, BREAKER_KIND_BLOCKED, BREAKER_KIND_RATE_LIMITED,
})

# 非贡献结局 → 熔断器 failure kind（唯一来源）
OUTCOME_TO_BREAKER_KIND = {
    OUTCOME_NO_RESULTS: BREAKER_KIND_EMPTY,
    OUTCOME_TIMEOUT: BREAKER_KIND_TIMEOUT,
    OUTCOME_BLOCKED: BREAKER_KIND_BLOCKED,
    OUTCOME_RATE_LIMITED: BREAKER_KIND_RATE_LIMITED,
    OUTCOME_AUTH_FAILED: BREAKER_KIND_ERROR,
    OUTCOME_QUOTA_EXHAUSTED: BREAKER_KIND_ERROR,
    OUTCOME_ERROR: BREAKER_KIND_ERROR,
}

# 缓存命中的负结果：outcome.status 值，但不属于引擎调用结局的常规枚举
# （engine_dispatch 在缓存路径单独写入）。登记在此避免各处手拼字符串。
OUTCOME_NO_RESULTS_CACHED = "no-results-cached"


def is_contributing_outcome(status: str) -> bool:
    """该结局是否对最终答案有贡献。"""
    return status in CONTRIBUTING_OUTCOMES


# 注：`is_state_failure_outcome` 的**唯一实现**在 cache_guard.py，入参是
# outcome dict，判据除状态名外还看 detail 里的缺密钥线索，且刻意不收
# `blocked`（那是源站侧事实，换环境不一定能好）。本模块曾有一个同名纯函数
# 只收状态名、把 blocked 也算进去，两处同名单义反——统一按 dict 版走，
# 需要判定请 import cache_guard.is_state_failure_outcome，勿在此处再长一份。


# ── 7. 失败归因（failure category）─────────────────────────────────────────
# 失败原因分类，进归因寄存器与 --list-engines 展示。与 outcome 的区别：
# outcome 是「这次调用怎么了」（引擎级），category 是「为什么坏」（归因级）。

FAILURE_DEPENDENCY = "dependency"
FAILURE_AUTH = "auth"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_QUOTA_EXHAUSTED = "quota_exhausted"
FAILURE_BLOCKED = "blocked"
FAILURE_UPSTREAM = "upstream"
FAILURE_NETWORK = "network"
FAILURE_UNKNOWN = "unknown"

FAILURE_CATEGORIES = frozenset({
    FAILURE_DEPENDENCY, FAILURE_AUTH, FAILURE_RATE_LIMITED,
    FAILURE_QUOTA_EXHAUSTED, FAILURE_BLOCKED, FAILURE_UPSTREAM,
    FAILURE_NETWORK, FAILURE_UNKNOWN,
})

# 归因类别 → outcome status（唯一来源）
CATEGORY_TO_OUTCOME = {
    FAILURE_AUTH: OUTCOME_AUTH_FAILED,
    FAILURE_RATE_LIMITED: OUTCOME_RATE_LIMITED,
    FAILURE_QUOTA_EXHAUSTED: OUTCOME_QUOTA_EXHAUSTED,
    FAILURE_BLOCKED: OUTCOME_BLOCKED,
    FAILURE_DEPENDENCY: OUTCOME_ERROR,
    FAILURE_UPSTREAM: OUTCOME_ERROR,
    FAILURE_NETWORK: OUTCOME_ERROR,
    FAILURE_UNKNOWN: OUTCOME_ERROR,
}

# 驱动熔断开闸的归因类别（其余类别只做短冷却，不累计 opens）
DRIVES_OPENS_CATEGORIES = frozenset({
    FAILURE_DEPENDENCY, FAILURE_UPSTREAM, FAILURE_NETWORK, FAILURE_UNKNOWN,
})


# ── 8. 重试判据（retry）───────────────────────────────────────────────────
# 派生自 outcome/category：确定性失败不重试（重试只是多烧一次调用）。

RETRYABLE = "retryable"
NON_RETRYABLE = "non_retryable"

NON_RETRYABLE_OUTCOMES = frozenset({
    OUTCOME_QUOTA_EXHAUSTED, OUTCOME_AUTH_FAILED, OUTCOME_BLOCKED,
})


def is_retryable_outcome(status: str) -> bool:
    """该结局是否值得重试（纯函数，门禁可直接打到）。"""
    return status not in NON_RETRYABLE_OUTCOMES


# ── 9. 抓取降级链（fetch method）───────────────────────────────────────────
# 抓取降级链的每一级产出不同的 fetch_method，用于「这条正文是怎么拿到的」。
# 值即 fetch 结果里的 fetch_method 字段。

FETCH_HTTP_MD = "http_md"              # HTTP + 内容协商拿到 Markdown
FETCH_HTTP = "http"                    # 普通 HTTP
FETCH_TLS = "tls"                      # TLS 指纹（curl_cffi）
FETCH_JINA = "jina"                    # jina/Parallel 免费云渲染
FETCH_WAYBACK = "wayback"              # Wayback 快照回退
FETCH_BROWSER = "browser"              # 反检测浏览器
FETCH_CHROME_CDP = "chrome_cdp"        # Chrome CDP 通道
FETCH_PARALLEL_MCP = "parallel_mcp"    # Parallel MCP 渲染
FETCH_JSRUN_CHALLENGE = "jsrun_challenge"  # js-run 算车道跑挑战
FETCH_SOGOU_FALLBACK = "sogou_fallback"    # 搜狗微信兜底
FETCH_WECHAT = "wechat:"               # 公众号前置分支（带冒号，拼子方法名）
FETCH_WECHAT_FAILED = "wechat_failed"  # 公众号分支失败
FETCH_FULLTEXT = "fulltext"            # 从本地 fulltext 存档读全文
FETCH_X_ARTICLE = "x_article"          # X 文章打包
FETCH_BLOCKED = "blocked"              # SSRF/安全拦截

FETCH_METHODS = frozenset({
    FETCH_HTTP_MD, FETCH_HTTP, FETCH_TLS, FETCH_JINA, FETCH_WAYBACK,
    FETCH_BROWSER, FETCH_CHROME_CDP, FETCH_PARALLEL_MCP, FETCH_JSRUN_CHALLENGE,
    FETCH_SOGOU_FALLBACK, FETCH_WECHAT, FETCH_WECHAT_FAILED, FETCH_FULLTEXT,
    FETCH_X_ARTICLE, FETCH_BLOCKED,
})

# 已产出 Markdown 的抓取方式：内容已由站点或阅读器转好，没有 HTML 壳可言。
# 降级链级别（http_md / jina / parallel_mcp）之外的渲染层细分值
# （tinyfish / md_variant / llms_txt / jina_reader / parallel）也收在这里，
# 因为它们回答的是同一个问题：「这次正文拿到的是不是已经转好的 Markdown」。
MARKDOWN_FETCH_METHODS = frozenset({
    FETCH_HTTP_MD, FETCH_JINA, FETCH_PARALLEL_MCP,
    "md_variant", "llms_txt", "jina_reader", "parallel", "tinyfish",
})


def is_degraded_fetch_method(method: str) -> bool:
    """该 fetch_method 是否为降级路径（非首级 HTTP）。

    降级路径拿到的正文质量通常低于首级 HTTP（云渲染可能丢结构、快照可能过期），
    调用方据此决定是否值得再抓一次。
    """
    return method in {
        FETCH_JINA, FETCH_WAYBACK, FETCH_BROWSER, FETCH_CHROME_CDP,
        FETCH_PARALLEL_MCP, FETCH_SOGOU_FALLBACK, FETCH_FULLTEXT,
    }


# ── 10. 平台/运行时状态（dsh-plugin / daemon）──────────────────────────────
# 生产端已收口：`scripts/daemon.py` 的 ping 响应与 `status` 子命令都带
# machine-readable 的 `state` 字段，值取自本表；进程停止时 status 报
# DAEMON_STOPPED（此时没有 ping 可回）。`packages/argo-zcode/server/py_bridge.py`
# 直接读这个字段，不再从中文文案反推 pid、也不再用布尔冒充三态。
#
# 未收口的部分（JS 侧，本文件当前不是 JS 真源）：
# - DSH 插件 `packages/dsh-plugin/dsh/client.js` 的 ready/loading/unavailable
#   仍是字面量；面板的连接池卡片已改为按配置值渲染，不再写死「正常」。
# - `scripts/gen_native_tools.py` 只从 mcp_tools.py 生成 NATIVE_TOOLS 工具
#   schema，产物 `native-tools.mjs` 不含任何状态常量；旧注释声称「JS 侧通过
#   该生成器引用本词表」与实现不符，已按实测更正。
# - `tests/test_native_tools_sync.py` 只比对工具 schema，不会发现状态词表漂移。
# 跨侧收口须另建生成器与对拍门禁。

DAEMON_RUNNING = "running"
DAEMON_STOPPED = "stopped"
DAEMON_IDLE = "idle"
DAEMON_BUSY = "busy"

DAEMON_STATES = frozenset({
    DAEMON_RUNNING, DAEMON_STOPPED, DAEMON_IDLE, DAEMON_BUSY,
})

PLUGIN_READY = "ready"
PLUGIN_LOADING = "loading"
PLUGIN_UNAVAILABLE = "unavailable"

PLUGIN_STATES = frozenset({
    PLUGIN_READY, PLUGIN_LOADING, PLUGIN_UNAVAILABLE,
})


# ── 11. 精排器（reranker）──────────────────────────────────────────────────
# 一次精排调用的结局。`ok` 是唯一「远程精排真出了排序」的值；其余全部
# 落到降级集合 RERANK_DEGRADED_STATUSES，由本地五维 / TF-IDF 保底接管。
#
# 为什么降级集合必须是 frozenset 常量而不是各调用点手抄元组：2026-11-09
# 审计发现 search_pipeline 的触发条件、postprocess_signals 的抛光条件、
# ab_eval 的断言三处各写一份，新增一个状态时漏改任何一处都是**静默退化**
# （既不精排也不保底，顺序退回 RRF 原始序且无信号）。

RERANK_OK = "ok"
RERANK_FALLBACK = "fallback"
RERANK_SKIPPED_NO_KEY = "skipped_no_key"
RERANK_SKIPPED_SHORT = "skipped_short"
RERANK_SKIPPED_FAST = "skipped_fast"
RERANK_SKIPPED_CIRCUIT_OPEN = "skipped_circuit_open"
RERANK_SKIPPED_QUOTA = "skipped_quota"
# 缓存命中：本次根本没进加工段（search_entry 在命中路径直接写这个值）。
RERANK_SKIPPED_CACHE = "skipped_cache"

RERANK_STATUSES = frozenset({
    RERANK_OK, RERANK_FALLBACK, RERANK_SKIPPED_NO_KEY, RERANK_SKIPPED_SHORT,
    RERANK_SKIPPED_FAST, RERANK_SKIPPED_CIRCUIT_OPEN, RERANK_SKIPPED_QUOTA,
    RERANK_SKIPPED_CACHE,
})

# 不是 ok 即降级：需要本地保底接管的状态全集。
RERANK_DEGRADED_STATUSES = RERANK_STATUSES - {RERANK_OK}


def is_rerank_degraded(status: str) -> bool:
    """该精排结局是否意味着「远程精排没出排序」（需要本地保底）。"""
    return status in RERANK_DEGRADED_STATUSES


# ── 12. 搜索计划（plan）─────────────────────────────────────────────────────
# plan.build_plan 的三态：可直接搜（ready）、known-url 交接给 fetch
# （handoff_required）、站内登录需要显式授权（needs_authorization）。
# 消费面：search.super_search 的短路分支、search_output 的人读格式、
# search_cli 的归档开关、plan.main 的退出码。

PLAN_READY = "ready"
PLAN_HANDOFF_REQUIRED = "handoff_required"
PLAN_NEEDS_AUTHORIZATION = "needs_authorization"

PLAN_STATUSES = frozenset({
    PLAN_READY, PLAN_HANDOFF_REQUIRED, PLAN_NEEDS_AUTHORIZATION,
})


# ── 13. 批处理预检（batch probe）───────────────────────────────────────────
# 条目级分类 + 批次级结论。条目 UNKNOWN = 合法 URL 但本地无证据（可能
# 需要网络探测）；它不直接导致 stop——探测不到不等于不存在。

PROBE_UNSUPPORTED = "unsupported"
PROBE_NEEDS_AUTH = "needs_auth"
PROBE_ALREADY_HAVE = "already_have"
PROBE_NOT_FOUND = "not_found"
PROBE_UNKNOWN = "unknown"

PROBE_ITEM_STATES = frozenset({
    PROBE_UNSUPPORTED, PROBE_NEEDS_AUTH, PROBE_ALREADY_HAVE,
    PROBE_NOT_FOUND, PROBE_UNKNOWN,
})

PROBE_GO = "go"
PROBE_GO_WITH_SKIPS = "go_with_skips"
PROBE_STOP = "stop"

PROBE_VERDICTS = frozenset({PROBE_GO, PROBE_GO_WITH_SKIPS, PROBE_STOP})


# ── 14. 数值印证（numeric verdict）─────────────────────────────────────────
# 摘要里的关键数字与原文是否对得上。只有 missing / partial 触发降权
# （NUMERIC_PENALIZED），confirmed 与 unfetchable 都不动排序——拿不到正文
# 不是「数字错了」的证据。

NUMERIC_CONFIRMED = "confirmed"
NUMERIC_PARTIAL = "partial"
NUMERIC_MISSING = "missing"
NUMERIC_UNFETCHABLE = "unfetchable"

NUMERIC_VERDICTS = frozenset({
    NUMERIC_CONFIRMED, NUMERIC_PARTIAL, NUMERIC_MISSING, NUMERIC_UNFETCHABLE,
})

# 触发降权的 verdict（reorder_by_numeric 的唯一判据）
NUMERIC_PENALIZED = frozenset({NUMERIC_MISSING, NUMERIC_PARTIAL})


# ── 15. 取证覆盖（coverage）────────────────────────────────────────────────
# 单个研究维度被多少个结果覆盖。research_dossier 产出，research_gates 与
# blind_spots 消费；值域大写是既有输出契约（dossier JSON 里就这样出现）。

COVERAGE_COVERED = "COVERED"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_NOT_COVERED = "NOT_COVERED"

COVERAGE_STATUSES = frozenset({
    COVERAGE_COVERED, COVERAGE_PARTIAL, COVERAGE_NOT_COVERED,
})


# ── 16. 健康探针（probe status）────────────────────────────────────────────
# health_probe 的 engine_health.status 列：连续失败未达阈值记 degraded，
# 达阈值记 unavailable，无记录记 unknown。health_check 另一条路径只写布尔
# available，不写这一列——两套视图并存是接口事实，不要合并。

PROBE_STATUS_AVAILABLE = "available"
PROBE_STATUS_DEGRADED = "degraded"
PROBE_STATUS_UNAVAILABLE = "unavailable"
PROBE_STATUS_UNKNOWN = "unknown"

PROBE_HEALTH_STATUSES = frozenset({
    PROBE_STATUS_AVAILABLE, PROBE_STATUS_DEGRADED,
    PROBE_STATUS_UNAVAILABLE, PROBE_STATUS_UNKNOWN,
})

# 连续失败达到该次数即判 unavailable（状态机转换条件，与状态名同源）。
# 此前 health_probe 与 health_check 各写一个 MAX_FAILURES = 2，且 health_check
# 那份没有任何读取点——改名也照样跑，等于两处「看起来有阈值」。
PROBE_MAX_FAILURES = 2

# 视为「可路由」的探针状态：只有 unavailable 是硬失败；degraded 是「连续
# 失败但未达阈值」，unknown 是「没探过」。后两者都放行——与
# health_probe.get_engine_status 的 available 判据（`!= "unavailable"`）逐位
# 一致，包括对未登记状态值的 fail-open（宁可放行，不让词表演进把源静默过滤）。
PROBE_ROUTABLE_STATUSES = frozenset({
    PROBE_STATUS_AVAILABLE, PROBE_STATUS_DEGRADED, PROBE_STATUS_UNKNOWN,
})


def is_probe_available(status: str) -> bool:
    """探针状态是否允许参与路由（唯一判据：不是 unavailable）。

    刻意不看 PROBE_ROUTABLE_STATUSES 白名单——历史行为就是「只认
    unavailable 一票否决」，改成白名单会让未登记值（旧库/新版本回滚）从
    「可用」静默变成「不可用」，把源过滤掉。
    """
    return str(status or "") != PROBE_STATUS_UNAVAILABLE


# ── 17. 缓存层级（cache level）─────────────────────────────────────────────
CACHE_LEVEL_L1 = "L1"
CACHE_LEVEL_L2 = "L2"
# 未知兜底：缓存命中但载荷没带 _cache_level（旧存档/替身缓存）。
CACHE_LEVEL_UNKNOWN = "L?"

CACHE_LEVELS = frozenset({
    CACHE_LEVEL_L1, CACHE_LEVEL_L2, CACHE_LEVEL_UNKNOWN,
})


# ── 18. 候选核验（verification）────────────────────────────────────────────
# candidate = 只有摘要线索、未打开原文；opened = 已 fetch 过原文。
# snippet 永远不得被标成 opened（candidate_envelope 的注释契约）。

VERIFICATION_CANDIDATE = "candidate"
VERIFICATION_OPENED = "opened"

VERIFICATION_STATUSES = frozenset({
    VERIFICATION_CANDIDATE, VERIFICATION_OPENED,
})


# ── 19. 观察模式变更（watch change）────────────────────────────────────────
# 两帧正文比对的三态 + 旧存档（只有指纹无正文）的 unknown 兜底。
# 哈希只做快速路径：diff 说没变就是没变（noise 也算没变）。

WATCH_CHANGE_NONE = "none"
WATCH_CHANGE_NOISE = "noise"
WATCH_CHANGE_SUBSTANTIVE = "substantive"
WATCH_CHANGE_UNKNOWN = "unknown"

WATCH_CHANGE_TYPES = frozenset({
    WATCH_CHANGE_NONE, WATCH_CHANGE_NOISE, WATCH_CHANGE_SUBSTANTIVE,
    WATCH_CHANGE_UNKNOWN,
})

# 视为「内容真的变了」的集合（watch.cmd_check 的 changed 判据）。
WATCH_SUBSTANTIVE_TYPES = frozenset({WATCH_CHANGE_SUBSTANTIVE})


# ── 20. 候选信封路由（envelope route）──────────────────────────────────────
# attach_envelope 给每个 route 写的交付态：无 errors 记 completed，有记 partial。
# 与 outcome.status 的 partial 同名不同层：那个是单个引擎的结局，
# 这个是整次检索的交付结论。

ENVELOPE_ROUTE_COMPLETED = "completed"
ENVELOPE_ROUTE_PARTIAL = "partial"

ENVELOPE_ROUTE_STATUSES = frozenset({
    ENVELOPE_ROUTE_COMPLETED, ENVELOPE_ROUTE_PARTIAL,
})
