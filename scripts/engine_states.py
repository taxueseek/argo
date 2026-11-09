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
    busy；ready / loading / unavailable

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


def is_state_failure_outcome(status: str) -> bool:
    """该结局是否为配置/状态类失败（缺密钥/熔断/配额/准入）。

    这类失败不该被当成引擎故障记进熔断器——它们消耗了时间但原因不在引擎实现。
    """
    if status.startswith(OUTCOME_SKIPPED_PREFIX):
        return True
    return status in {
        OUTCOME_AUTH_FAILED, OUTCOME_QUOTA_EXHAUSTED, OUTCOME_BLOCKED,
    }


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
# 仅登记 Python 侧观测到的状态名，**尚未收口**：
# - DSH 插件 `packages/dsh-plugin/dsh/client.js` 把 ready/loading/unavailable
#   写成字面量；ZCode 桥 `packages/argo-zcode/server/py_bridge.py` 的探活是
#   中文文案 + daemon_running 布尔 + pid 正则，面板 `ui/panel.html` 再写一份。
# - `scripts/gen_native_tools.py` 只从 mcp_tools.py 生成 NATIVE_TOOLS 工具
#   schema，产物 `native-tools.mjs` 不含任何状态常量；旧注释声称「JS 侧通过
#   该生成器引用本词表」与实现不符，已按实测更正。
# - `tests/test_native_tools_sync.py` 只比对工具 schema，不会发现状态词表漂移。
# 跨侧收口须另建生成器与对拍门禁，本文件当前不是 JS 侧真源。

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
