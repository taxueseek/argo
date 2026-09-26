#!/usr/bin/env python3
"""engine_status.py — 引擎状态聚合（list-engines --detail）

输出每引擎：
  enabled / type / cost_tier / env_ready / missing_env /
  allowed_by_env / blocked / admitted / routable / health summary
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config, get_cost_tiers  # noqa: E402
from engine_env import env_status_for, is_engine_allowed_by_env  # noqa: E402
from engine_requires import requires_status  # noqa: E402
from engine_admission import load_admission, is_blocked, is_admitted  # noqa: E402
from cli_io import dumps


def _cost_tier_of(engine_id: str, tiers: dict[str, list[str]]) -> str:
    for tier in ("paid", "api", "low", "free"):
        if engine_id in (tiers.get(tier) or []):
            return tier
    return "free"


def _all_engine_specs(cfg: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    cfg = cfg if cfg is not None else load_config()
    engines = cfg.get("engines") or {}
    return {k: v for k, v in engines.items() if isinstance(v, dict)}


def _adaptive_scores_snapshot() -> dict[str, float]:
    """一次取全部引擎学习分（get_ranking 单查询），避免 120+ 引擎逐个开 sqlite。"""
    try:
        from adaptive import get_learner
        return dict(get_learner().get_ranking())
    except Exception:
        return {}


def _runtime_status(engine_id: str,
                    adaptive_scores: dict[str, float] | None = None,
                    spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """运行时状态聚合（统一健康度视图）：熔断 + 学习分。

    被动读取、无网络副作用：breaker 状态读进程内存（构造时已 load 磁盘态）；
    adaptive 分数用调用方传入的全表快照，缺失时按中性分 0.5。
    与主动探针（health_check.check_engine）互补：本函数是「当前可用性」快照，
    探针是「立即连通性」验证，二者计算方式不同、各自保留。
    """
    out: dict[str, Any] = {"breaker": None, "adaptive_score": None,
                           "failure": None}
    try:
        from circuit_breaker import get_breaker
        st = get_breaker().status(engine_id)
        out["breaker"] = {
            "state": st.get("state", "closed"),
            "failures": st.get("failures", 0),
            "cooldown_remain": st.get("cooldown_remain", 0),
            "last_kind": st.get("last_kind"),
        }
        # 失败归因：熔断只回答「要不要继续用」，这里补「为什么坏」。
        # 仅在确有失败记录时输出，避免给健康引擎塞噪音字段。
        # 优先用失败现场记录的归因（真实 category/evidence）；旧数据没有该字段
        # 时才回落按 kind 文本归类（信息量有限，可能落 unknown）。
        if st.get("last_kind") and int(st.get("failures") or 0) > 0:
            persisted = st.get("last_attribution")
            if isinstance(persisted, dict) and persisted.get("category"):
                out["failure"] = persisted
            else:
                from engine_failure import explain as _explain_failure
                out["failure"] = _explain_failure(
                    engine_id, spec=spec, output=str(st.get("last_kind") or ""),
                )
    except Exception:
        pass
    if adaptive_scores is not None:
        out["adaptive_score"] = adaptive_scores.get(engine_id, 0.5)
    elif adaptive_scores is None:
        # 单引擎查询：只查一次 sqlite（批量场景必须传快照避免 N 次连接）
        try:
            from adaptive import get_learner
            out["adaptive_score"] = get_learner().get_score(engine_id)
        except Exception:
            pass
    return out


def _quota_exhausted_marks() -> dict[str, dict[str, Any]]:
    """一次取全部「远端配额耗尽」标记，避免逐引擎查询。失败返回空。"""
    try:
        from quota import get_quota_manager
        return get_quota_manager().remote_exhausted_marks()
    except Exception:
        return {}


def engine_detail(engine_id: str, spec: dict[str, Any] | None = None,
                  tiers: dict[str, list[str]] | None = None,
                  adaptive_scores: dict[str, float] | None = None,
                  quota_marks: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if spec is None:
        specs = _all_engine_specs()
        spec = specs.get(engine_id) or {}
    tiers = tiers if tiers is not None else get_cost_tiers()
    env = env_status_for(engine_id, spec)
    deps = requires_status(spec)
    adm = load_admission(engine_id)
    blocked = is_blocked(engine_id)
    admitted = is_admitted(engine_id)
    config_enabled = bool(spec.get("enabled", True))
    allowed = env["allowed_by_env"]
    env_ok = env["env_ready"]
    dep_ok = deps["dep_ready"]
    # 远端配额耗尽（如博查 AI Search 403 套餐额度不足）：这是源端的「现在别打」，
    # 与密钥/依赖无关，也不是引擎故障。此前这类引擎一律显示 ready、
    # routable=True，直到 lookup 才发现打不动——配额状态机已记下它，这里读出来。
    if quota_marks is None:
        quota_marks = _quota_exhausted_marks()
    quota_mark = quota_marks.get(engine_id)
    quota_exhausted = bool(quota_mark)
    # 自动路由可用条件（含后端依赖：缺 xhs/yt-dlp 这类工具时不该进路由）
    # explicit_only 排除在外：声明即「不进自动路由，只等显式 --engine」
    # （--routable-only 的帮助文案与 available_engines 同一口径）。此前漏判，
    # 旧显式源恰好被 env/dep 门挡住没暴露；toutiao（免 key 免依赖）把它炸出。
    routable = (
        config_enabled
        and allowed
        and env_ok
        and dep_ok
        and not blocked
        and not quota_exhausted
        and not bool(spec.get("explicit_only"))
    )
    status = "ready"
    if not config_enabled:
        status = "disabled"
    elif not allowed:
        status = "env_filtered"
    elif not env_ok:
        status = "missing_key"
    elif not dep_ok:
        # 后端工具缺失：密钥齐全、脚本存在，但真正干活的外部命令没有。
        # 此前这类引擎一律显示 ready，调用后静默返回空，无从定位。
        status = "missing_dep"
    elif quota_exhausted:
        status = "quota_exhausted"
    elif blocked:
        status = "blocked"
    elif admitted:
        status = "admitted"
    else:
        status = "ready"  # 未验证但可用（兼容）

    return {
        "engine_id": engine_id,
        "enabled": config_enabled,
        "type": spec.get("type", "cli"),
        "cost_tier": _cost_tier_of(engine_id, tiers),
        "status": status,
        # 设计上不进自动路由（需密钥的付费源 / 输入形态特殊），按 --engine 显式调用；
        # 可达性检查据此区分「有意显式」与「忘了接线」。
        "explicit_only": bool(spec.get("explicit_only")),
        "env_ready": env_ok,
        "required_env": env["required_env"],
        "missing_env": env["missing_env"],
        "allowed_by_env": allowed,
        "blocked": blocked,
        "admitted": admitted,
        "routable": routable,
        # 远端配额耗尽（源端明示）。配额问题既不是引擎健康问题、也不是密钥问题，
        # 单列出来，到周期边界惰性自愈。
        "quota_exhausted": quota_exhausted,
        "quota_exhausted_until": (quota_mark or {}).get("until"),
        "quota_exhausted_reason": (quota_mark or {}).get("reason") or "",
        # 后端依赖（requires 声明）：缺什么、怎么装。与 missing_env（密钥）互不相干。
        "dep_ready": dep_ok,
        "requires": deps["requires"],
        "missing_deps": deps["missing_deps"],
        "dep_fixes": deps["dep_fixes"],
        "admission": {
            "admitted_at": (adm or {}).get("admitted_at"),
            "stages_passed": (adm or {}).get("stages_passed") or [],
            "quality_score": (adm or {}).get("quality_score"),
            "avg_latency_ms": (adm or {}).get("avg_latency_ms"),
            "reason": (adm or {}).get("reason") or "",
        } if adm else None,
        # 统一健康度视图：熔断状态 + 学习分（被动快照，见 _runtime_status）
        "runtime": _runtime_status(engine_id, adaptive_scores, spec),
    }


def compact_engine_row(row: dict[str, Any]) -> dict[str, Any]:
    """全量清单的瘦身投影：剥掉嵌套转储（runtime/admission），留判定面。

    为什么要压缩：--list-engines --detail 不带过滤是 232 行 × 全字段的
    诊断转储，实测 151 KB（runtime 24% + admission 7% 是大头）——Agent
    一旦把它拉进上下文就是 ~50k token。而全量清单要回答的通常只有
    「哪些源可用/为什么不可用」，判定只需要标量旗标 + 条件性细节：
    failure/配额原因/缺 env 缺依赖在**非空时**才出现，健康源一行 ~150B。
    要单引擎的全量诊断（含 admission 里程碑、runtime 学习分），用
    `--engine <名>` 过滤后拿完整行——按需付全量的代价。

    降级契约：本函数只做「删键」，不改任何值；未来新增顶层字段默认
    不进瘦身面（显式登记才带），宁可瘦也不要悄悄膨胀。
    """
    slim: dict[str, Any] = {
        "engine_id": row.get("engine_id"),
        "enabled": row.get("enabled"),
        "type": row.get("type"),
        "cost_tier": row.get("cost_tier"),
        "status": row.get("status"),
        "explicit_only": row.get("explicit_only"),
        "env_ready": row.get("env_ready"),
        "routable": row.get("routable"),
        "quota_exhausted": row.get("quota_exhausted"),
        "dep_ready": row.get("dep_ready"),
    }
    # 条件性细节：非空才出现（健康源不带这些键，输出不随异常源膨胀）
    for key in ("missing_env", "missing_deps"):
        if row.get(key):
            slim[key] = row[key]
    if row.get("quota_exhausted") and row.get("quota_exhausted_reason"):
        slim["quota_exhausted_reason"] = row["quota_exhausted_reason"]
    failure = (row.get("runtime") or {}).get("failure") or {}
    if failure:
        slim["failure"] = {"category": failure.get("category"),
                           "evidence": failure.get("evidence")}
    return slim


def list_engines_detail(
    *,
    routable_only: bool = False,
    include_disabled: bool = True,
    engines: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """引擎详细状态行。

    engines：只保留这些 engine_id（None = 全量）。单引擎的详细行约 0.9 KB；
    全量诊断转储 2026-09-16 实测 151 KB（runtime 24% + admission 7% 是大头，
    此前误记 22 KB）——CLI 在不带 `--engine` 过滤时改出 compact_engine_row
    瘦身投影（约 1/4 体积），要全量就按引擎过滤，别把转储拉进上下文。
    """
    wanted = set(engines) if engines else None
    cfg = load_config()
    tiers = get_cost_tiers(cfg)
    specs = _all_engine_specs(cfg)
    adaptive_scores = _adaptive_scores_snapshot()
    quota_marks = _quota_exhausted_marks()
    rows = []
    for name in sorted(specs.keys()):
        if wanted is not None and name not in wanted:
            continue
        row = engine_detail(name, specs[name], tiers, adaptive_scores, quota_marks)
        if not include_disabled and not row["enabled"]:
            continue
        if routable_only and not row["routable"]:
            continue
        rows.append(row)
    return rows


def list_routable_engine_ids() -> list[str]:
    return [r["engine_id"] for r in list_engines_detail(routable_only=True, include_disabled=False)]


def format_engines_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{'ENGINE':<22} {'STATUS':<14} {'TIER':<6} {'TYPE':<14} {'ROUTABLE':<8} {'ENV':<28} FAILURE",
        "-" * 120,
    ]
    for r in rows:
        env_note = "ok" if r["env_ready"] else ",".join(r["missing_env"][:2]) or "missing"
        # 失败归因：熔断只回答「要不要继续用」，这一列补「为什么坏」。
        # 归因来自引擎内失败现场记录的寄存器（engines_base.http_open /
        # _http_get_raw → note_failure → 熔断持久化），此前只出现在 --json 里。
        failure = r.get("runtime", {}).get("failure") or {}
        fail_note = ""
        # 配额耗尽优先：它是由配额状态机确认过的「源端明示额度不足」，比熔断里
        # 可能陈旧的归因（旧版落的 unknown/empty）更可信，也更可行动。
        if r.get("quota_exhausted"):
            reason = (r.get("quota_exhausted_reason") or "").replace("\n", " ")
            fail_note = f"quota_exhausted: {reason[:40]}".strip(": ")
        elif failure:
            cat = failure.get("category") or ""
            ev = (failure.get("evidence") or "").replace("\n", " ")[:40]
            fail_note = f"{cat}: {ev}".strip(": ")
        lines.append(
            f"{r['engine_id']:<22} {r['status']:<14} {r['cost_tier']:<6} "
            f"{str(r['type']):<14} {str(r['routable']):<8} {env_note:<28} {fail_note}"
        )
    lines.append(f"\n总计 {len(rows)} · routable={sum(1 for r in rows if r['routable'])}")
    return "\n".join(lines)


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Argo 引擎状态")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--routable-only", action="store_true")
    parser.add_argument("--engine", help="只看单个引擎")
    args = parser.parse_args()
    if args.engine:
        rows = [engine_detail(args.engine)]
    else:
        rows = list_engines_detail(routable_only=args.routable_only)
    if args.json:
        print(dumps(rows if len(rows) != 1 else rows[0]))
    else:
        print(format_engines_table(rows))


if __name__ == "__main__":
    _cli()
