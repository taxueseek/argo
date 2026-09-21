#!/usr/bin/env python3
"""replay_eval.py — 把真实搜索的返回录下来，以后不上网也能重跑整条处理流程。

## 解决什么问题

原来这套工具能回答「这次改动有没有弄坏东西」，回答不了「这次改动是不是变好了」。

已有的几项离线检查各只管一段：matrix_search_eval 只判断路由选得对不对（是或否）；
ranking_golden 能算排序质量分，但它用的引擎返回是手写的假数据；relevance_golden
存的是真实返回，却只核对相关性判断，不跑后面的流程。结果是：去重、合并、排序、
输出这几处的改动，效果只能等一周一次的联网检查，或者靠人读代码。

这个脚本补上中间那一段——把一次真实搜索里每个引擎返回的内容存下来，以后不上网
也能用同一批数据重跑整条流程，并给出两次运行之间的差异。

## 三个关键决定

1. **不改产品代码，只换掉一个函数。** search.py 顶部写着 engine_search 必须是模块级
   函数、网络出口靠替换属性来顶替（search_benchmark 和 test_budget_observability
   都这么做）。本脚本照同样的做法，只替换 search.engine_search 与
   search._missing_env_for，跑完立刻还原。其余三个（get_engines /
   get_execution_config / get_cost_factor）保持原样——它们读的是仓库里的
   config.yaml，本来就是确定的。

2. **路由结果沿用录制时的那一份，不重新计算。** 路由会受熔断状态、配额耗尽、
   自适应学习影响，而录制是在真实状态目录下跑的、重跑是在临时空目录下跑的，
   两边选出的引擎组合可能不一样。不沿用就会变成「在测状态差异」而不是「在测代码
   改动」（实测：不沿用的时候，40 次里有 1 次走了完全不同的引擎序列）。
   路由本身对不对，由已有的 matrix_search_eval --offline 和路由金标负责。

3. **记下「第几次调用返回了什么」。** 重跑时按顺序发放，超过记录次数就重复最后一次
   并记一笔。重试次数这类变化因此看得见，不会被悄悄吞掉。

4. **路由选中的引擎如果在记录里找不到，单独记一笔，不当成空结果。** 「这个源没录到」
   和「这个源返回了空」是两回事，混在一起会让对比工具自己变成新的误报来源。

## 使用注意（重要）

录下来的引擎返回**只属于录制那一天**。它保证的是「我们的处理逻辑没有退步」，
不是「现在网上就是这个样子」。网页结构会变、配额会变、不同地区的可达性也不同，
重跑不会反映这些。

## 用法

  python3 scripts/replay_eval.py                         # 重跑并打印结果
  python3 scripts/replay_eval.py --json                  # 输出 JSON
  python3 scripts/replay_eval.py --check                 # 检查是否低于下限
  python3 scripts/replay_eval.py --case <id>             # 看单条明细
  python3 scripts/replay_eval.py --save-baseline P.json   # 把当前结果存为对照基准
  python3 scripts/replay_eval.py --compare P.json        # 与基准对比，列出差异
  python3 scripts/replay_eval.py --record                # 联网录制（唯一联网模式）
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from cli_io import dumps, dumps_pretty

# 状态目录隔离必须放在 import 任何 argo 模块之前：熔断、配额、自适应学习都读本机
# 状态文件，重跑要跨机器得到同样结果，所以一律落到一个空的临时目录。同 ranking_eval.py。
os.environ.setdefault("ARGO_STATE_DIR",
                      tempfile.mkdtemp(prefix="argo-replay-eval-"))

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

ROOT = SCRIPT_DIR.parent
DEFAULT_DATA_PATH = ROOT / "tests" / "golden" / "pipeline_golden.json"

# 要替换掉的两个函数。名字的唯一来源是 search.py 顶部注释（那里说明了为什么它们
# 必须是模块级函数）；这里只引用，不另立一套。
_REPLACED_NAMES = ("engine_search", "_missing_env_for")

# 录制时默认用的查询词：跨几个不同的领域，让重跑能碰到不同的引擎组合和判断分支。
# 这只是「录什么」的建议，实际以数据文件内容为准。
_DEFAULT_QUERIES = [
    "Python asyncio 事件循环 最佳实践",
    "2026 中国 GDP 增速 目标",
    "OpenAI MCP specification",
    "量子计算 最新进展",
    "React Server Components production",
    "上海天气 未来一周",
]

# 下划线开头的字段不进录制文件：它们是流程中途补上的标记，存下来只会让下一次重跑
# 看到上一次的残留。
_DROP_PREFIX = "_"


# ── 小工具 ────────────────────────────────────────────────────────────────────

def _jsonable(obj: Any) -> Any:
    """只保留能写成 JSON 的值，保证录制文件存得下、换台机器也读得回来。"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.startswith(_DROP_PREFIX))
                and _writable(v)}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj if _writable(v)]
    return obj


def _writable(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), dict, list, tuple))


def _load_data(path: Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_DATA_PATH
    if not p.exists():
        return {"_meta": {}, "cases": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _agent_bytes(payload: dict) -> int:
    """算出这份结果按 agent 档输出后占多少字节——这是 Agent 每次真正付出的上下文代价。

    直接调用产品代码里的投影函数，不另算一套，免得「这里量出来的数」和
    「用户实际拿到的」对不上。
    """
    import search
    try:
        view = search._strip_for_agent(payload)
    except Exception:
        view = {"count": payload.get("count", 0)}
    return len(dumps(view).encode("utf-8"))


def _dcg(grades: list[float]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def _rank_metrics(urls: list[str], relevant: dict[str, Any] | None) -> dict:
    """算排序质量分（nDCG@10 / MRR@10）。没有人工标注就不给分，而不是给 0。

    「得了 0 分」和「没有测」必须分开：前者会让检查在没有标注的数据上误报成退步。
    """
    if not relevant:
        return {}
    grades = [float(relevant.get(u, 0)) for u in urls[:10]]
    mrr = 0.0
    for i, g in enumerate(grades):
        if g >= 2:
            mrr = 1.0 / (i + 1)
            break
    ideal = sorted(float(v) for v in relevant.values() if v > 0)[::-1][:10]
    idcg = _dcg(ideal)
    return {
        "mrr": round(mrr, 4),
        "ndcg": round(_dcg(grades) / idcg, 4) if idcg > 0 else 0.0,
    }


# ── 换掉网络出口 ───────────────────────────────────────────────────────────────

class _SwappedEngine:
    """跑的时候把网络出口换成自己的函数，跑完逐个还原。

    不还原会污染同进程后面跑的代码——这是 search_benchmark 注释里写过的教训，
    照抄一份。
    """

    def __init__(self, replacement: Callable) -> None:
        self._replacement = replacement
        self._originals: dict[str, Any] = {}

    def __enter__(self):
        import search
        self._originals = {n: getattr(search, n) for n in _REPLACED_NAMES}
        search.engine_search = self._replacement
        search._missing_env_for = lambda _eng: []
        return search

    def __exit__(self, *exc) -> None:
        import search
        for name, value in self._originals.items():
            setattr(search, name, value)


# ── 跑一条数据 ─────────────────────────────────────────────────────────────────

def _route_decision(case: dict) -> dict:
    """取录制时那一份路由结果。

    录制跑在真实状态目录下，重跑跑在临时空目录下，两边选出的引擎组合可能不同，
    所以要用录制时存下来的那一份。保存的字段里若缺了后来新增的键，就用当前路由
    补齐，这样老的录制文件不会因为代码加了字段而整条失效。

    路由本身选得对不对，由已有的 matrix_search_eval --offline 与路由金标负责，
    两边的职责不重叠。
    """
    from route import route_query

    query = case["query"]
    mode = case.get("mode", "auto")
    depth = case.get("depth", "fast")
    saved = dict(case.get("decision") or {})
    current = route_query(query, mode=mode, depth=depth)
    for key, value in current.items():
        saved.setdefault(key, value)
    return saved


def _run_pipeline(case: dict, *, engine_call: Callable) -> dict:
    """用指定的引擎调用方式跑一遍完整流程，返回原始结果。"""
    import search
    from cache import SearchCache

    query = case["query"]
    mode = case.get("mode", "auto")
    depth = case.get("depth", "fast")
    max_results = int(case.get("max_results", 5))
    timeout = int(case.get("timeout", 10))

    decision = _route_decision(case)
    payload = search.execute_search(
        query, decision, max_results, timeout, depth,
        SearchCache(), True, mode=mode,
    )
    payload["_domain_decided"] = decision.get("domain")
    payload["_engines_combo"] = decision.get("engines_combo") or decision.get("engines")
    return payload


def _collect_metrics(case: dict, payload: dict, not_recorded: set[str],
                     over_limit: set[str]) -> dict:
    urls = [r.get("url", "") for r in (payload.get("results") or [])
            if isinstance(r, dict)]
    out = {
        "id": case["id"],
        "kept": payload.get("count", len(urls)),
        "status": payload.get("status"),
        "domain": payload.get("_domain_decided"),
        "engines": list(payload.get("_engines_combo") or []),
        "funnel": payload.get("funnel"),
        "urls": urls,
        "bytes": _agent_bytes(payload),
        "errors": len(payload.get("errors") or []),
        # 每条结果的**全量字段**：urls 只锁住位次，锁不住「分数/来源/信号怎么变的」。
        # 拆 execute_search 这类加工层重构时，位次不变但分数漂移正是要抓的东西，
        # 因此这里存全量而不是摘要（--compare 会做字段级对比）。
        "results_full": [_jsonable(r) for r in (payload.get("results") or [])
                         if isinstance(r, dict)],
    }
    out.update(_rank_metrics(urls, case.get("relevant")))
    if not_recorded:
        out["not_recorded"] = sorted(not_recorded)
    if over_limit:
        out["extra_attempts"] = sorted(over_limit)
    return out


def replay_case(case: dict, recording: dict[str, list[list[dict]]]) -> dict:
    """按录制的数据重跑一条（全程不联网）。"""
    not_recorded: set[str] = set()
    call_count: dict[str, int] = {}
    over_limit: set[str] = set()

    def fake_engine_call(query: str, engine: str, n: int = 5,
                         timeout: float | None = None, depth: str = "fast",
                         mode: str = "auto", **_: Any) -> list[dict]:
        attempts = recording.get(engine)
        if not attempts:
            not_recorded.add(engine)
            return []
        idx = call_count.get(engine, 0)
        call_count[engine] = idx + 1
        if idx >= len(attempts):
            over_limit.add(engine)
        # 深拷贝：流程会就地在结果里补 _engine、score、rerank_dims 这些字段，
        # 不拷贝会把改动写回录制文件，下一次重跑就带着上一次的残留。
        return copy.deepcopy(attempts[min(idx, len(attempts) - 1)])

    with _SwappedEngine(fake_engine_call):
        payload = _run_pipeline(case, engine_call=fake_engine_call)
    return _collect_metrics(case, payload, not_recorded, over_limit)


def record_case(query: str, *, case_id: str | None = None,
                mode: str = "auto", depth: str = "fast",
                max_results: int = 5, timeout: int = 10) -> dict:
    """真跑一次，把每个引擎返回的内容录下来（唯一会联网的路径）。"""
    import search
    real = search.engine_search
    recorded: dict[str, list[list[dict]]] = {}

    def recording_engine_call(query: str, engine: str, n: int = 5,
                              timeout: float | None = None, depth: str = "fast",
                              mode: str = "auto", **kw: Any) -> list[dict]:
        rows = real(query, engine, n=n, timeout=timeout, depth=depth,
                    mode=mode, **kw)
        recorded.setdefault(engine, []).append(_jsonable(rows))
        return rows

    case = {
        "id": case_id or _slugify(query),
        "query": query, "mode": mode, "depth": depth,
        "max_results": max_results, "timeout": timeout,
    }
    # 路由结果随录制一起存下来，重跑时用它，避免「真实状态与临时空状态不同」
    # 带来的组合漂移（见 _route_decision）。
    from route import route_query
    case["decision"] = _jsonable(route_query(query, mode=mode, depth=depth))
    with _SwappedEngine(recording_engine_call):
        payload = _run_pipeline(case, engine_call=recording_engine_call)

    case["engines"] = recorded
    case["_recorded"] = {
        "kept": payload.get("count", 0),
        "domain": payload.get("_domain_decided"),
        "engines_combo": payload.get("_engines_combo"),
    }
    return case


def _slugify(query: str) -> str:
    keep = [c if c.isalnum() else "-" for c in query.strip().lower()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "case")[:40]


# ── 重跑、对比、检查 ───────────────────────────────────────────────────────────

def evaluate_all(data_path: Path | None = None) -> dict:
    doc = _load_data(data_path)
    cases = doc.get("cases") or []
    results = [replay_case(c, c.get("engines") or {}) for c in cases]
    out: dict[str, Any] = {"n_cases": len(cases), "cases": results}
    if results:
        out["mean_kept"] = round(sum(r["kept"] for r in results) / len(results), 3)
        out["mean_bytes"] = round(
            sum(r["bytes"] for r in results) / len(results), 1)
        scored = [r["ndcg"] for r in results if "ndcg" in r]
        if scored:
            out["mean_ndcg"] = round(sum(scored) / len(scored), 4)
        out["cases_with_missing"] = sum(1 for r in results if r.get("not_recorded"))
        out["total_missing"] = sum(len(r.get("not_recorded") or []) for r in results)
    return out


def _clip_field(value: Any, limit: int = 90) -> Any:
    """字段值截断：diff 是给人看的，整段 snippet 会把输出冲掉。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, list) and len(value) > 12:
        return value[:12] + ["…"]
    return value


def _field_changes(base_results: list | None,
                   new_results: list | None) -> list[dict]:
    """同一 URL 上的字段级变化（分数、来源、信号、共识引擎…）。

    位次表（entered/left/moved）只看得到「谁进谁出」；加工层重构最容易造成的是
    「位次没动但分数/字段变了」——那类漂移必须能看见，否则拆排序或信号阶段时
    没有可信的锁。只比两边都有的 URL，进出的 URL 由 entered/left 表达。
    """
    bmap = {r.get("url"): r for r in (base_results or [])
            if isinstance(r, dict) and r.get("url")}
    rows: list[dict] = []
    for r in new_results or []:
        if not isinstance(r, dict):
            continue
        base = bmap.get(r.get("url"))
        if base is None:
            continue
        diffs = {k: {"from": _clip_field(base.get(k)), "to": _clip_field(r.get(k))}
                 for k in sorted(set(base) | set(r))
                 if base.get(k) != r.get(k)}
        if diffs:
            rows.append({"url": r.get("url"), "fields": diffs})
    return rows


def diff_reports(base: dict, new: dict) -> dict:
    """两次运行之间的差异：指标变化、哪些结果新进来、哪些掉了、位次怎么动的。

    这是这个脚本存在的理由——分数本身 ranking_eval 已经能给，这里要的是
    「这次相对上次差在哪」。
    """
    base_by_id = {c["id"]: c for c in base.get("cases", [])}
    rows = []
    for c in new.get("cases", []):
        b = base_by_id.get(c["id"])
        if b is None:
            rows.append({"id": c["id"], "note": "基准里没有这条（新增的）"})
            continue
        b_urls = list(b.get("urls") or [])
        n_urls = list(c.get("urls") or [])
        entered = [u for u in n_urls if u not in b_urls]
        left = [u for u in b_urls if u not in n_urls]
        moved = []
        for u in n_urls:
            if u in b_urls:
                bi, ni = b_urls.index(u) + 1, n_urls.index(u) + 1
                if bi != ni:
                    moved.append({"url": u, "from": bi, "to": ni})
        row = {
            "id": c["id"],
            "d_kept": c["kept"] - b["kept"],
            "d_bytes": c["bytes"] - b["bytes"],
            "entered": entered,
            "left": left,
            "moved": moved,
        }
        fields = _field_changes(b.get("results_full"), c.get("results_full"))
        if fields:
            row["field_changes"] = fields
        if "ndcg" in c and "ndcg" in b:
            row["d_ndcg"] = round(c["ndcg"] - b["ndcg"], 4)
        rows.append(row)
    changed = [r for r in rows
               if r.get("d_kept") or r.get("d_bytes") or r.get("entered")
               or r.get("left") or r.get("d_ndcg") or r.get("field_changes")]
    out: dict[str, Any] = {
        "n_cases": len(rows),
        "n_changed": len(changed),
        "cases": rows,
        "entered_total": sum(len(r.get("entered") or []) for r in rows),
        "left_total": sum(len(r.get("left") or []) for r in rows),
    }
    for key in ("kept", "bytes", "ndcg"):
        if f"mean_{key}" in base and f"mean_{key}" in new:
            out[f"d_mean_{key}"] = round(new[f"mean_{key}"] - base[f"mean_{key}"],
                                         4 if key == "ndcg" else 1)
    return out


def check_limits(report: dict, doc: dict) -> list[str]:
    """逐条核对下限，返回不合格的说明（空表示全部通过）。

    下限是**可选的**：没设下限的那条不参与条数与体积的判断，而不是默认算过。
    但「录制的数据里没有这个引擎」跟下限无关——那是「这次重跑没能还原录下来的
    那份场景」，任何一条出现都要报出来，否则对比工具自己会变成新的误报来源。
    """
    bad: list[str] = []
    by_id = {c["id"]: c for c in doc.get("cases", [])}
    for r in report.get("cases", []):
        for eng in r.get("not_recorded") or []:
            bad.append(f"{r['id']}: 录制的数据里没有 {eng}"
                       f"（路由变了？还是录制时漏了？）")
        limit = (by_id.get(r["id"]) or {}).get("limit") or {}
        if not limit:
            continue
        if "min_kept" in limit and r["kept"] < limit["min_kept"]:
            bad.append(f"{r['id']}: 结果 {r['kept']} 条，少于下限 "
                       f"{limit['min_kept']} 条")
        if "max_bytes" in limit and r["bytes"] > limit["max_bytes"]:
            bad.append(f"{r['id']}: 输出 {r['bytes']} 字节，超过上限 "
                       f"{limit['max_bytes']} 字节")
        if "min_ndcg" in limit and "ndcg" in r and r["ndcg"] < limit["min_ndcg"]:
            bad.append(f"{r['id']}: 排序质量分 {r['ndcg']} 低于下限 "
                       f"{limit['min_ndcg']}")
    return bad


def save_baseline(report: dict, path: Path) -> None:
    path.write_text(dumps_pretty({
        "_meta": {"saved_at": datetime.now().isoformat(timespec="seconds"),
                  "note": "对照基准：--compare 用它算差异"},
        "report": report,
    }), encoding="utf-8")


def load_baseline(path: Path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return doc.get("report", doc)


# ── 打印 ──────────────────────────────────────────────────────────────────────

def _fmt_case(r: dict) -> str:
    line = (f"  {r['id']:<28} 结果 {r['kept']:<3} 条   输出 {r['bytes']:>5} 字节"
            f"   领域={r.get('domain')}")
    if "ndcg" in r:
        line += f"   质量分={r['ndcg']:.3f}"
    if r.get("not_recorded"):
        line += f"   没录到[{','.join(r['not_recorded'])}]"
    if r.get("extra_attempts"):
        line += f"   多调了[{','.join(r['extra_attempts'])}]"
    return line


def _fmt_diff(d: dict) -> str:
    lines = [f"对比：{d['n_cases']} 条里有 {d['n_changed']} 条出现变化 | "
             f"新进来 {d['entered_total']} 条 / 掉了 {d['left_total']} 条"]
    for key, label, unit in (("d_mean_kept", "平均结果数", " 条"),
                             ("d_mean_bytes", "平均输出", " 字节"),
                             ("d_mean_ndcg", "平均质量分", "")):
        if key in d:
            lines.append(f"  {label} {d[key]:+}{unit}")
    for r in d["cases"]:
        flags = []
        if r.get("note"):
            flags.append(r["note"])
        if r.get("d_kept"):
            flags.append(f"结果{r['d_kept']:+d}条")
        if r.get("d_bytes"):
            flags.append(f"输出{r['d_bytes']:+d}字节")
        if r.get("d_ndcg"):
            flags.append(f"质量分{r['d_ndcg']:+.4f}")
        if r.get("entered"):
            flags.append(f"新进来{len(r['entered'])}条")
        if r.get("left"):
            flags.append(f"掉了{len(r['left'])}条")
        if r.get("moved"):
            flags.append(f"位次变了{len(r['moved'])}条")
        if r.get("field_changes"):
            flags.append(f"字段变了{len(r['field_changes'])}条")
        if flags:
            lines.append(f"  {r['id']:<28} " + "  ".join(flags))
        for fc in r.get("field_changes") or []:
            keys = ", ".join(sorted(fc["fields"]))
            lines.append(f"      {fc['url'][:60]} → 字段变化：{keys}")
    return "\n".join(lines)


def _fmt_header(doc: dict) -> str:
    meta = doc.get("_meta") or {}
    return (f"录制数据：{len(doc.get('cases') or [])} 条 | "
            f"录制于 {meta.get('recorded_at', '未知')}")


# ── 命令行 ────────────────────────────────────────────────────────────────────

def _cmd_record(args) -> int:
    queries = args.queries or _DEFAULT_QUERIES
    out_path = Path(args.out) if args.out else DEFAULT_DATA_PATH
    cases: list[dict] = []
    for q in queries:
        print(f"正在录制：{q}", file=sys.stderr)
        try:
            cases.append(record_case(q, mode=args.mode, depth=args.depth,
                                     max_results=args.n, timeout=args.timeout))
        except Exception as e:  # 单条失败不该让整批录制作废
            print(f"  这条失败了，跳过：{type(e).__name__}: {e}", file=sys.stderr)
    if not cases:
        print("一条都没录到", file=sys.stderr)
        return 1
    doc = {
        "_meta": {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "note": "真实引擎返回的录制数据。改动之后如果确定变好，"
                    "请更新各条的下限并在这里记一笔。",
            "usage": "录下来的引擎返回只属于录制那一天。重跑保证的是"
                     "「我们的处理逻辑没有退步」，不是「现在网上就是这个样子」"
                     "——网页结构会变、配额会变、不同地区的可达性也不同。",
        },
        "cases": cases,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dumps_pretty(doc), encoding="utf-8")
    print(f"已写入 {out_path}（{len(cases)} 条）", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="离线重跑与对比")
    ap.add_argument("--data", help="录制数据文件（默认 tests/golden/pipeline_golden.json）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--check", action="store_true",
                    help="检查是否低于下限（低于则退出码为 1）")
    ap.add_argument("--case", help="只看某一条的明细")
    ap.add_argument("--compare", help="与基准文件对比，列出差异")
    ap.add_argument("--save-baseline", help="把当前结果存为对照基准")
    ap.add_argument("--record", action="store_true", help="联网录制（唯一会联网的模式）")
    ap.add_argument("--queries", nargs="*", help="录制用的查询词")
    ap.add_argument("--out", help="录制结果写到哪里")
    ap.add_argument("--mode", default="auto")
    ap.add_argument("--depth", default="fast")
    ap.add_argument("-n", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    if args.record:
        return _cmd_record(args)

    data_path = Path(args.data) if args.data else None
    doc = _load_data(data_path)
    if not doc.get("cases"):
        print(f"录制数据是空的：{data_path or DEFAULT_DATA_PATH}\n"
              f"先用 `--record` 录一批真实返回。", file=sys.stderr)
        return 2

    report = evaluate_all(data_path)

    if args.case:
        case = next((c for c in doc["cases"] if c["id"] == args.case), None)
        if case is None:
            print(f"没有这条：{args.case}", file=sys.stderr)
            return 2
        r = replay_case(case, case.get("engines") or {})
        print(_fmt_case(r))
        for pos, u in enumerate(r["urls"], 1):
            print(f"   #{pos} {u[:78]}")
        return 0

    if args.save_baseline:
        save_baseline(report, Path(args.save_baseline))
        print(f"基准已存到：{args.save_baseline}", file=sys.stderr)

    if args.compare:
        diff = diff_reports(load_baseline(Path(args.compare)), report)
        if args.json:
            print(dumps(diff))
        else:
            print(_fmt_header(doc))
            print(_fmt_diff(diff))
        return 0

    if args.json:
        print(dumps(report))
        return 0

    print(_fmt_header(doc))
    for r in report["cases"]:
        print(_fmt_case(r))

    if args.check:
        bad = check_limits(report, doc)
        if bad:
            print("\n".join(f"不合格 {b}" for b in bad), file=sys.stderr)
            return 1
        print("全部通过：结果条数与输出体积都在下限之内，也没有漏录的引擎")
    return 0


if __name__ == "__main__":
    sys.exit(main())
