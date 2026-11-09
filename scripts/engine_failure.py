#!/usr/bin/env python3
"""engine_failure.py — 引擎失败归因（依赖缺失 / 认证失效 / 上游改版 / 网络）。

背景（v2.4.2）：熔断器此前只回答「要不要继续用」（open/closed/disabled），
不回答「为什么坏」。用户看到 `--list-engines --detail` 里某引擎 blocked 或
连续失败，只能自己猜是没登录、没装工具、还是站点改版。

本模块把最后一次失败归类为七类，并给出对应动作：

  dependency  后端命令/工具不存在或不可执行（如 xhs / tw / rdt 未安装）
              → 动作：按 requires.fix 安装
  quota_exhausted 余额/套餐/免费额度已耗尽（402/403 + 「余额不足」「insufficient
              balance」）——充值或到下一计费周期前重试都不会成功
              → 动作：充值或等周期边界自愈；期间路由自动跳过该引擎
              与 rate_limited 的区别：后者是「慢点来」，冷却几十秒后值得再试；
              前者重试只烧时间，必须驱动停用而不是短冷却。
  auth        认证/授权失效（401/403、login required、cookie 过期）
              → 动作：重新登录（如 `xhs login`），并注意结果应进登录态分区
  rate_limited 限流（429/Retry-After）——源端在说「慢点来」，引擎本身是好的
              → 动作：等待后重试；不触发降权，不应累计进引擎故障
  blocked     拿到内容之前被拦截（反爬页面 / TLS 指纹识别 / WAF 挑战页）。
              与 auth 的区分：auth 是「内容存在但你没资格」，blocked 是
              「请求在到达内容前就被机器判定挡下」——重登录无用，
              换客户端形态（请求头完整度 / TLS 指纹 / 浏览器通道）才有救
              → 动作：升级请求形态或切换浏览器态通道；不该当作引擎故障降权
  upstream    上游改版（404 找不到接口、5xx 持续、HTML 结构不再匹配）
              → 动作：引擎实现需跟进（解析器/接口路径过期）
  network     网络层问题（超时、DNS、连接重置、SSL）
              → 动作：重试或换网络；通常不需改代码

设计原则：
  - 分类是**纯函数**：输入 (异常/状态码/输出文本/依赖状态)，输出分类 + 依据。
    不产生副作用，便于单测与回溯。
  - 只做「归类 + 给动作」，不自动改变熔断状态：熔断策略仍由 circuit_breaker
    掌管，避免两套逻辑互相打架。
  - 归因置信度不足时返回 unknown 而不是硬猜——错的归因比没有归因更坏。
  - 判定顺序即裁决：更具体的信号先于更泛的信号。带 auth 语义的 403
    （members-only / login required）不得被误判成 blocked，反之纯 403
    叠加反爬页面特征时必须判 blocked——把「需要登录」和「被封锁」混为一谈
    会把用户支去错误的修复方向，比不给建议更糟。
"""

from __future__ import annotations

import engine_states  # 状态词表唯一真源（2026-11-09 收口）

import re
from typing import Any

# 分类常量
DEPENDENCY = engine_states.FAILURE_DEPENDENCY
AUTH = engine_states.FAILURE_AUTH
RATE_LIMITED = engine_states.FAILURE_RATE_LIMITED
QUOTA_EXHAUSTED = engine_states.FAILURE_QUOTA_EXHAUSTED
BLOCKED = engine_states.FAILURE_BLOCKED
UPSTREAM = engine_states.FAILURE_UPSTREAM
NETWORK = engine_states.FAILURE_NETWORK
UNKNOWN = engine_states.FAILURE_UNKNOWN

# 归因类别 → 单引擎 outcome.status（唯一来源）。
# engine_dispatch 用它把「为什么坏」落成可观测状态；retry_policy 反过来用
# 它派生「哪些类别不重试」。两处各抄一份会在同一次失败上给出两种判断
# （2026-10-26 审查发现的重复真源）。
# 归因类别 → outcome status：从 engine_states.CATEGORY_TO_OUTCOME 派生
# （2026-11-09 收口）。此前手写字面量，engine_dispatch 的 _BREAKER_FAIL_KIND
# 也手抄一份，两处各改一套词表时同一次失败会给出两种判断。
NOTE_STATUS: dict[str, str] = dict(engine_states.CATEGORY_TO_OUTCOME)

# 各类对应的建议动作
_ACTIONS = {
    DEPENDENCY: "按 requires.fix 安装缺失的后端命令，确保其在 PATH 上",
    AUTH: "重新登录/刷新凭证；登录态结果应标记 cache_eligible=false 进独立分区",
    RATE_LIMITED: "源端限流，等待后重试（尊重 Retry-After）；不降权、不改代码",
    QUOTA_EXHAUSTED: "配额/余额已耗尽：充值或等下一计费周期自动恢复；"
                     "期间自动跳过该引擎（quota.py reset 可提前恢复）",
    BLOCKED: "请求在到达内容前被拦截（反爬/TLS 指纹）。升级请求形态"
             "（完整浏览器请求头 / TLS 指纹 / 浏览器态通道），重登录与重试均无效",
    UPSTREAM: "上游接口或页面结构可能已改版，需更新该引擎的解析实现",
    NETWORK: "属网络层瞬时问题，优先重试或切换网络，通常无需改代码",
    UNKNOWN: "信息不足，建议手动复现一次以定位（不要据此改代码）",
}

# 认证类信号
_AUTH_PATTERNS = (
    r"\b401\b", r"unauthor", r"forbidden", r"login required",
    r"not logged in", r"please log ?in", r"cookie.*(expired|invalid)",
    r"token.*(expired|invalid|revoked)", r"需要登录", r"登录已过期",
    r"请先登录", r"认证失败", r"无权限", r"permission denied.*auth",
)

# 封锁类信号：请求在到达内容前被机器判定挡下（反爬挑战页 / TLS 指纹识别）。
# 特征取自真实拦截页面的自述文案与握手失败字样；与 auth 信号几乎不重叠，
# 重叠处（403）由判定顺序仲裁：先 auth 语义、后封锁。
_BLOCKED_PATTERNS = (
    r"just a moment", r"enable javascript and cookies", r"checking your browser",
    r"cf-browser-verification", r"cf_chl", r"challenge-platform",
    r"ddos-guard", r"perimeterx", r"px-captcha", r"captcha-delivery",
    r"unable to handshake", r"handshake failure", r"sslv3_alert",
    r"access denied", r"request blocked", r"banned", r"防火墙", r"访问被拒",
    r"验证码", r"安全验证", r"滑动验证",
)

# 限流类信号（含「额度/套餐」——源端说「现在不能用」，动作同为止损等待/升配额，
# 不是改代码、也不是重新登录）
_RATE_LIMITED_PATTERNS = (
    r"\b429\b", r"too many requests", r"rate limit", r"ratelimit",
    r"quota.*(exceed|exhausted| depleted)", r"请求过于频繁", r"限流", r"访问频率",
    r"not enough money", r"package quota", r"insufficient (funds|balance|credit|quota)",
    r"(余额|额度|配额).{0,4}(不足|不够|耗尽)", r"(套餐|package).{0,12}(到期|用尽|不足|quota)",
)

# 硬配额耗尽信号（余额/套餐/免费额度）。与限流分开判定：这类失败重试不会
# 成功，必须让配额状态机停用到下一周期，而不是冷却几十秒后再撞一次。
# 只收「账户/套餐层面已没有额度」的表述；泛化的 quota exceeded 仍按限流处理
# （它常与 429 同现，且可能几小时后恢复）。
_QUOTA_EXHAUSTED_PATTERNS = (
    r"insufficient (funds|balance|credit|quota)",
    r"not enough (money|credit|balance)",
    r"(do not|don't|does not) have enough (money|credit|balance)",
    r"package quota",
    r"out of credits",
    r"no (remaining|available) quota",
    r"quota.{0,20}(exhausted|depleted|used up)",
    r"(余额|额度|配额).{0,4}(不足|不够|耗尽)",
    r"(套餐|package).{0,12}(到期|用尽|不足)",
)

# 上游改版类信号
_UPSTREAM_PATTERNS = (
    r"\b404\b", r"\b410\b", r"\b50[0-9]\b", r"not found", r"object not found",
    r"no such (endpoint|api|route)", r"deprecated", r"api.*(changed|removed)",
    r"unexpected (html|response|structure)", r"parse (error|failed)",
    r"schema.*(mismatch|changed)", r"接口.*(变更|不可用|不存在)", r"页面结构",
)

# 网络类信号
_NETWORK_PATTERNS = (
    r"timed? ?out", r"timeout", r"connection (reset|refused|aborted|error)",
    r"name or service not known", r"temporary failure in name resolution",
    r"ssl", r"tls", r"\bdns\b", r"network is unreachable", r"connectionpool",
    r"超时", r"连接.*(失败|重置|被拒)", r"网络",
)

# 依赖类信号（异常类型 + 文本）
_DEPENDENCY_TYPE_NAMES = ("FileNotFoundError", "PermissionError",
                          "NotADirectoryError", "ImportError",
                          "ModuleNotFoundError")
_DEPENDENCY_PATTERNS = (
    r"no such file or directory", r"command not found", r"not found in \$?PATH",
    r"executable file not found", r"is not recognized as an internal",
    r"找不到.*(命令|文件)", r"未安装",
)


def _matches(patterns: tuple[str, ...], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


def _looks_blocked(text: str) -> bool:
    """文本里是否出现封锁页特征（供状态码分支复核用）。"""
    return _matches(_BLOCKED_PATTERNS, text) is not None


def classify(*,
             error: BaseException | None = None,
             status_code: int | None = None,
             output: str | None = None,
             missing_deps: list[dict[str, Any]] | None = None,
             missing_env: list[str] | None = None,
             anti_bot: bool | None = None) -> dict[str, Any]:
    """把一次失败归类。

    判定顺序（强信号优先，避免误判）：
      1. 已声明的依赖缺失（missing_deps）→ dependency（最可信，声明即证据）
      2. 已声明的密钥缺失（missing_env）→ auth
      3. 反爬页面检测显式命中（anti_bot=True）→ blocked
         （调用方在拿到 HTML 时已做页面级检测，这是强证据，先于状态码——
          拦截页也常披着 200/403 的皮）
      4. 异常类型（FileNotFoundError 等）→ dependency
      5. HTTP 状态码（429 → rate_limited；402/403 带额度不足文案 → quota_exhausted；
         403 带封锁特征 → blocked，纯 403 → auth；404/410/5xx → upstream）
      6. 文本模式匹配（依次 auth → blocked → quota_exhausted → rate_limited →
         dependency → upstream → network）
      7. 保底 unknown

    返回 {category, reason, evidence, action, confidence}
    """
    missing_deps = missing_deps or []
    missing_env = missing_env or []
    blob_probe = " ".join(p for p in (str(error) if error is not None else "",
                                      output or "") if p)

    # 1. 依赖声明先行：这是环境已知事实，比任何运行时猜测都可靠
    if missing_deps:
        bins = ", ".join(d.get("bin", "?") for d in missing_deps)
        return {
            "category": DEPENDENCY,
            "reason": "declared-missing-deps",
            "evidence": f"requires 声明缺失: {bins}",
            "action": _ACTIONS[DEPENDENCY],
            "confidence": "high",
        }

    # 2. 密钥缺失 → 认证类
    if missing_env:
        return {
            "category": AUTH,
            "reason": "declared-missing-env",
            "evidence": f"缺少环境变量: {', '.join(missing_env)}",
            "action": _ACTIONS[AUTH],
            "confidence": "high",
        }

    # 3. 反爬页面显式命中：调用方做过页面级检测，强于状态码推断
    if anti_bot:
        return {
            "category": BLOCKED,
            "reason": "anti-bot-page",
            "evidence": blob_probe[:200] or "拦截页特征命中",
            "action": _ACTIONS[BLOCKED],
            "confidence": "high",
        }

    # 4. 异常类型
    if error is not None:
        tname = type(error).__name__
        if tname in _DEPENDENCY_TYPE_NAMES:
            return {
                "category": DEPENDENCY,
                "reason": f"exception:{tname}",
                "evidence": str(error)[:200],
                "action": _ACTIONS[DEPENDENCY],
                "confidence": "high",
            }

    # 5. HTTP 状态码
    if status_code is not None:
        # 0 = 连接层就没成（DNS 失败/拒绝/重置），没有 HTTP 响应可解释
        if status_code == 0:
            return {
                "category": NETWORK,
                "reason": "connection-failed",
                "evidence": "未收到 HTTP 响应",
                "action": _ACTIONS[NETWORK],
                "confidence": "high",
            }
        # 429 与 503 同族：源站「现在不受理，稍后再说」。503 在 HTTP 层已按
        # 合规等待信号处理（带 Retry-After 则等待，见 http_client 的 stop-signal
        # 契约与 test_stop_signal.py）；归因层若判 upstream，会把人支去「改解析
        # 代码」，方向错误。故二者合并，动作同为止损等待。
        if status_code in (429, 503):
            return {
                "category": RATE_LIMITED,
                "reason": f"http-{status_code}",
                "evidence": f"HTTP {status_code}",
                "action": _ACTIONS[RATE_LIMITED],
                "confidence": "high",
            }
        if status_code == 408:
            return {
                "category": NETWORK,
                "reason": "http-408",
                "evidence": "HTTP 408 请求超时",
                "action": _ACTIONS[NETWORK],
                "confidence": "high",
            }
        # 额度/套餐文案优先于鉴权仲裁：403 里写着「套餐额度不足」时，
        # 让人「重新登录」是错误方向（登录改不了套餐）。search.py 的
        # _QUOTA_ERROR_KEYWORDS 早就把 quota 排在 auth 之前，这里保持一致。
        #
        # 硬配额耗尽与限流再分一层（2026-10-18）：octen 的
        # `403 Insufficient balance in account` 此前落进 rate_limited，
        # 只换来 60s 冷却、永不自动停用，后备链第二顺位长期是个死引擎。
        # 这不是「慢点来」，重试不会成功——必须交给配额状态机停用。
        if status_code in (401, 402, 403) and blob_probe:
            if _matches(_QUOTA_EXHAUSTED_PATTERNS, blob_probe):
                return {
                    "category": QUOTA_EXHAUSTED,
                    "reason": f"http-{status_code}+quota-exhausted",
                    "evidence": blob_probe[:200],
                    "action": _ACTIONS[QUOTA_EXHAUSTED],
                    "confidence": "high",
                }
            hit = _matches(_RATE_LIMITED_PATTERNS, blob_probe)
            if hit:
                return {
                    "category": RATE_LIMITED,
                    "reason": f"http-{status_code}+quota-sign",
                    "evidence": blob_probe[:200],
                    "action": _ACTIONS[RATE_LIMITED],
                    "confidence": "high",
                }
        if status_code == 401:
            return {
                "category": AUTH,
                "reason": "http-401",
                "evidence": "HTTP 401",
                "action": _ACTIONS[AUTH],
                "confidence": "high",
            }
        if status_code == 403:
            # 403 有歧义：带封锁特征 → 被拦截；纯 403 → 权限/认证。
            # 顺序错了会把「需要登录」的人支去装反爬对抗，反之亦然。
            if _looks_blocked(blob_probe):
                return {
                    "category": BLOCKED,
                    "reason": "http-403+block-sign",
                    "evidence": blob_probe[:200],
                    "action": _ACTIONS[BLOCKED],
                    "confidence": "high",
                }
            return {
                "category": AUTH,
                "reason": "http-403",
                "evidence": "HTTP 403",
                "action": _ACTIONS[AUTH],
                "confidence": "high",
            }
        if status_code in (404, 410) or 500 <= status_code < 600:
            return {
                "category": UPSTREAM,
                "reason": f"http-{status_code}",
                "evidence": f"HTTP {status_code}",
                "action": _ACTIONS[UPSTREAM],
                "confidence": "high",
            }
        # 其余 4xx（400/402/405/406/409/413…）请求被源站拒绝，但理由无法从
        # 状态码还原（参数不符？欠费？方法不对？）。按本模块纪律不硬猜：
        # 继续走第 6 步找文本证据，找不到就是 unknown。

    # 6. 文本模式：auth 最具体先判，封锁次之（封锁特征与 auth 语义几乎不重叠），
    #    限流再后，依赖、上游、网络殿后
    if blob_probe:
        for patterns, category in (
            (_AUTH_PATTERNS, AUTH),
            (_BLOCKED_PATTERNS, BLOCKED),
            # 配额耗尽先于限流：文本里同时出现「quota exhausted」与
            # 「rate limit」时，前者是更具体、更可行动的结论
            (_QUOTA_EXHAUSTED_PATTERNS, QUOTA_EXHAUSTED),
            (_RATE_LIMITED_PATTERNS, RATE_LIMITED),
            (_DEPENDENCY_PATTERNS, DEPENDENCY),
            (_UPSTREAM_PATTERNS, UPSTREAM),
            (_NETWORK_PATTERNS, NETWORK),
        ):
            hit = _matches(patterns, blob_probe)
            if hit:
                return {
                    "category": category, "reason": f"text:{hit}",
                    "evidence": blob_probe[:200], "action": _ACTIONS[category],
                    "confidence": "medium",
                }

    return {
        "category": UNKNOWN,
        "reason": "insufficient-signal",
        "evidence": blob_probe[:200],
        "action": _ACTIONS[UNKNOWN],
        "confidence": "low",
    }


def explain(engine_id: str, *, spec: dict[str, Any] | None = None,
            error: BaseException | None = None,
            status_code: int | None = None,
            output: str | None = None,
            anti_bot: bool | None = None) -> dict[str, Any]:
    """引擎级便捷入口：自动带上该引擎已声明的依赖/密钥缺失事实再归类。"""
    spec = spec or {}
    missing_deps: list[dict[str, Any]] = []
    missing_env: list[str] = []
    try:
        from engine_requires import requires_status
        missing_deps = requires_status(spec).get("missing_deps") or []
    except Exception:
        pass
    try:
        from engine_env import missing_env_for
        missing_env = missing_env_for(engine_id, spec) or []
    except Exception:
        pass
    res = classify(error=error, status_code=status_code, output=output,
                   missing_deps=missing_deps, missing_env=missing_env,
                   anti_bot=anti_bot)
    res["engine_id"] = engine_id
    return res


def from_note(note: dict[str, Any] | None,
              engine_id: str = "") -> dict[str, Any]:
    """把归因寄存器里的一条记录规整成与 classify 同构的归因契约。

    寄存器（engines_base.note_failure）存的是 {category, reason, detail, ts}，
    而 classify/explain 输出 {category, reason, evidence, action, confidence}。
    熔断持久化与 --list-engines 展示都用后一形态，这里做一次转换，避免每个
    消费者各自拼字段（字段名漂移过一次就会让「为什么坏」显示不出东西）。
    """
    note = note or {}
    category = str(note.get("category") or UNKNOWN)
    reason = str(note.get("reason") or "")
    # confidence 不newly invent：按 reason 前缀还原现场判定的把握度
    # （http-<code> 来自状态码→high；text: 来自文本模式→medium；
    #   insufficient-signal/空→low）。一律报 high 会把 unknown 也标成高置信，
    # 违背本模块「信息不足不硬猜」的纪律。
    if not note.get("category"):
        confidence = "low"
    elif reason.startswith("http-"):
        confidence = "high"
    elif reason.startswith("text:") or reason == "insufficient-signal":
        confidence = "medium" if reason.startswith("text:") else "low"
    else:
        confidence = "medium"
    return {
        "category": category,
        "reason": reason,
        "evidence": str(note.get("detail") or "")[:200],
        "action": _ACTIONS.get(category, _ACTIONS[UNKNOWN]),
        "confidence": confidence,
        "engine_id": engine_id,
    }
