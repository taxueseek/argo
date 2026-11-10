#!/usr/bin/env python3
"""badcase_causes.py — 坏例根因词表（第 27 台状态机，单一真源）。

## 为什么单独成文件

2026-11-10 首次落地时它写在 `engine_states.py` 里，把那个文件从 983 行推到
1030 行，撞上 `tests/test_module_size_gate.py` 的 1000 行硬上限。拆分不是被
门禁逼的——**它本来就不属于「引擎状态」这一族**：engine_states 的 26 台
状态机描述的都是「一次调用/一个引擎现在处于什么状态」，而坏例根因描述的是
「这条失败该由谁修」，是项目级的归因维度。两者混在一张表里，读的人会以为
改根因分类等于改引擎状态。

## 与 failure category 的分工

`engine_states.py` §7 的 failure category 有 8 个值（dependency / auth /
rate_limited / quota_exhausted / blocked / upstream / network / unknown），
那是**引擎级**的「这次调用为什么坏」，由 `engine_failure.py` 的纯函数判定。

本模块的 root_cause 是**项目级**的「这条坏例该由谁修」，取值刻意不同名：
把 failure 的 8 个值直接塞进账本，会把「上游改版」与「活网排名漂移」混成
一类，而这两件事的处置完全不同（一个要改代码，一个只需重新录制基线）。

## 谁消费它

`scripts/badcase_ledger.py` 入账时判 root_cause，`check --promote-days` 按
它做 Seen N× 升级判定。`external_ranking` 明确排除在升级判定外。

词表同时登记进 `engine_states.STATE_REGISTRY`（见该文件 §26 的注册表），
`tests/test_state_registry.py` 会对拍投影完整性。
"""

from __future__ import annotations

ROOT_CAUSE_ROUTE = "route_misdispatch"        # 路由选错源（域/语言/引擎组合）
ROOT_CAUSE_PARSE_EMPTY = "parse_empty"        # 解析层零交付（空结果/字段缺失）
ROOT_CAUSE_AUTH = "auth_expired"              # 认证/授权失效
ROOT_CAUSE_QUOTA = "quota_exhausted"          # 配额耗尽
ROOT_CAUSE_RATE_LIMITED = "rate_limited"      # 限流
ROOT_CAUSE_BLOCKED = "source_blocked"         # 源站拦截（反爬/TLS/WAF）
ROOT_CAUSE_UPSTREAM = "upstream_change"       # 上游改版（404/结构失配）
ROOT_CAUSE_NETWORK = "network"                # 网络层（超时/DNS/重置）
ROOT_CAUSE_RECOVERY = "recovery_missed"       # 恢复链未兜住
ROOT_CAUSE_BREAKER = "engine_disabled"        # 引擎被停用（opens 达阈值）
ROOT_CAUSE_EXTERNAL = "external_ranking"      # 外部排名漂移（非本项目问题）
ROOT_CAUSE_UNKNOWN = "unattributed"           # 未归因（证据不足，不硬猜）

ROOT_CAUSES = frozenset({
    ROOT_CAUSE_ROUTE, ROOT_CAUSE_PARSE_EMPTY, ROOT_CAUSE_AUTH,
    ROOT_CAUSE_QUOTA, ROOT_CAUSE_RATE_LIMITED, ROOT_CAUSE_BLOCKED,
    ROOT_CAUSE_UPSTREAM, ROOT_CAUSE_NETWORK, ROOT_CAUSE_RECOVERY,
    ROOT_CAUSE_BREAKER, ROOT_CAUSE_EXTERNAL, ROOT_CAUSE_UNKNOWN,
})

# 「这是 argo 自己的问题」的根因子集——这些才允许触发 Seen N× 升级判定。
# external_ranking 明确排除：活网换血是常态，把它算成复发会制造长期噪音
# （2026-10-28 issue #35 的误报根因，见 scripts/ci_drift_gate.py 文件头）。
OWN_ROOT_CAUSES = frozenset(ROOT_CAUSES - {ROOT_CAUSE_EXTERNAL})


# ── 接入状态机注册表 ────────────────────────────────────────────────────────
# 把词表投影进 engine_states.STATE_REGISTRY，让 test_state_registry.py 能
# 机械对拍「登记了但没注册」与「注册了但词表没有」。延迟 import 避免循环：
# engine_states 不 import 本模块，本模块 import engine_states。

def register() -> None:
    """把 root_cause 登记进 engine_states 的状态机注册表（幂等）。"""
    import engine_states

    if "root_cause" in engine_states.list_state_machines():
        return
    engine_states.register_state_machine(
        "root_cause",
        states=ROOT_CAUSES,
        description="坏例根因：route_misdispatch / parse_empty / auth_expired / "
                    "quota_exhausted / rate_limited / source_blocked / "
                    "upstream_change / network / recovery_missed / "
                    "engine_disabled / external_ranking / unattributed",
    )


register()
