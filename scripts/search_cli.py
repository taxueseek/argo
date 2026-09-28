#!/usr/bin/env python3
"""search_cli.py — 搜索的 CLI 入口（参数解析、人读输出、本地 seek 合并）。

从 search.py 拆出：那里是「执行流水线」（execute_search / super_search），这里是
「怎么被调用、结果怎么打印」。分开的实际收益是 `--help`/参数校验这类改动的验证
成本——它们不必跑整条流水线的测试，也不必读 2000 行的执行层。

入口不变：`python3 scripts/search.py "查询"`（search.py 的 __main__ 转到这里）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from cli_io import dumps, ensure_utf8_stdio
from engine_env import env_flag
from stage_timing import StageTiming
from time_utils import WINDOW_FORMATS_HINT, is_valid_time_window
from search import (
    available_engines,
    execute_search,
    format_text_output,
    format_timing,
    super_search,
    _IMPORTS_DONE,
    _MODULE_T0,
    _strip_for_agent,
)


# 真值时点的交接槽。由 search.py 的 __main__ 块在跑完整条 import 链后写入
# （见该文件底部），因为那一刻本模块的身份是 __main__，而 runpy 事后会把
# sys.modules['__main__'] 恢复成原模块——事后从 __main__ 上取不到了。
_TRUE_IMPORT_T0: float | None = None
_TRUE_IMPORTS_DONE: float | None = None


def _resolve_module_timing() -> tuple[float, float]:
    """取 import 计时的两个时点。

    三个来源按可靠性排序：

    1. `_TRUE_IMPORT_T0` —— search.py 以 `__main__` 身份跑完整条 import 链
       后写入的交接值，这是**真值**（约 30 ms）。
    2. `sys.modules['__main__']` 上的同名属性 —— 直接 `python3 search.py` 时
       成立。
    3. 本模块 import 进来的 `search._MODULE_T0` —— **兜底，也是恒为 0 的那个**：
       入口经 `bin/argo → runpy.run_module('search', run_name='__main__')`，
       本模块执行 `from search import _MODULE_T0` 时，search 已以 __main__
       跑过一遍，这是**第二次**导入，依赖全在 sys.modules 里，差值 ≈ 0。

    走 3 正是 --explain-timing 的 import_ms 恒为 0.0 的原因，而它这一栏
    恰恰是给「优化固定开销」用的——报 0 等于把优化者指向错误的数字。
    """
    if _TRUE_IMPORT_T0 is not None and _TRUE_IMPORTS_DONE is not None:
        return _TRUE_IMPORT_T0, _TRUE_IMPORTS_DONE
    main_mod = sys.modules.get("__main__")
    if main_mod is not None:
        t0 = getattr(main_mod, "_MODULE_T0", None)
        done = getattr(main_mod, "_IMPORTS_DONE", None)
        if isinstance(t0, float) and isinstance(done, float):
            return t0, done
    return _MODULE_T0, _IMPORTS_DONE


# ── CLI 主入口 ─────────────────────────────────────────────────────────────────



def _window_arg(value: str) -> str:
    """argparse 的 `type=`：坏时间窗直接拒（判据在 time_utils，提示语同源）。

    用 `ArgumentTypeError` 而不是裸 `ValueError`：两者的差别是**用户看不看得见
    原因**——argparse 对 ValueError 只印 "invalid _window_arg value: 'garbage'"
    并把说明丢掉，对 ArgumentTypeError 则原样印出我们的提示。
    """
    if not is_valid_time_window(value):
        raise argparse.ArgumentTypeError(
            f"无法解析的时间窗 {value!r}：{WINDOW_FORMATS_HINT}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。

    从 main 里抽出来的唯一理由：让「CLI 是否支持某个已文档化的开关」可以
    对着**解析器对象**断言，而不是 grep 源码文本——后者在重构（如把 CLI
    搬到另一个模块）时会假红，且断言的是字面量而不是行为。
    """
    parser = argparse.ArgumentParser(
        description="Unified Search v2 — 统一搜索 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 search.py "python async"
  python3 search.py "英伟达财报" --explain --json
  python3 search.py "基金推荐" --mode fast
  python3 search.py "AAPL" --engine anysearch --domain finance --sub_domain finance.us_stock
        """,
    )
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="auto")
    parser.add_argument("--max-results", "-n", type=int, default=5)
    parser.add_argument("--depth", "-d", default="fast",
                        choices=["fast", "balanced", "deep"])
    # --no-cache 与 ARGO_NO_CACHE 合并成一个判据。此前只有 flag：用户照文档
    # 设 ARGO_NO_CACHE=1 却静默拿到缓存结果（cached=true、0 ms）——开关看着
    # 生效、其实没生效，是最难查的一类问题。走 engine_env.env_flag 这一个
    # 全仓统一的解析器，语义与其它 ARGO_* 开关一致。
    parser.add_argument("--no-cache", action="store_true",
                        default=env_flag("ARGO_NO_CACHE", False))
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", "-t", type=int, default=10)
    parser.add_argument("--list-engines", action="store_true",
                        help="列出引擎；加 --detail 看 env/准入/routable 状态")
    parser.add_argument("--detail", action="store_true",
                        help="与 --list-engines 联用：输出详细状态")
    parser.add_argument("--routable-only", action="store_true",
                        help="与 --list-engines 联用：仅可自动路由的引擎")
    parser.add_argument("--all", action="store_true", dest="list_all",
                        help="与 --list-engines --detail 联用：输出全部引擎的"
                             "逐条清单（默认只给分组摘要 + 非可路由项的成因）")
    parser.add_argument("--mode", default="auto",
                        choices=["fast", "auto", "deep", "budget"],
                        help="预算模式: fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制")
    parser.add_argument("--since", default=None,
                        type=_window_arg,
                        help="发布时间下限（7d / 2026-08-01），下推到支持时间窗的引擎")
    parser.add_argument("--until", default=None,
                        type=_window_arg,
                        help="发布时间上限（7d / 2026-08-01），下推到支持时间窗的引擎")
    parser.add_argument("--sort", default="relevance",
                        choices=["relevance", "oldest", "newest"],
                        help="时间排序：relevance=相关度（默认）, oldest=最早在前（溯源）, newest=最新在前")
    parser.add_argument("--include-domains", default="",
                        help="仅保留这些域名（含子域），逗号分隔，如 github.com,arxiv.org")
    parser.add_argument("--exclude-domains", default="",
                        help="排除这些域名（含子域），逗号分隔，如 pinterest.com")
    parser.add_argument("--local-first", action="store_true",
                        help="强制优先使用 local_search 零成本聚合引擎")
    parser.add_argument(
        "--include-local", action="store_true", default=None,
        help="强制并入本机文件命中（source=local_files；fast/budget 模式默认已开）",
    )
    parser.add_argument(
        "--no-local", action="store_true",
        help="关闭本机文件命中（fast/budget 模式默认并入，此项退出）",
    )
    parser.add_argument("--domain", default="", help="AnySearch 垂直域")
    parser.add_argument("--sub_domain", default="", help="AnySearch 子域")
    parser.add_argument(
        "--input-kind", default="auto",
        choices=["auto", "keyword", "url-seed", "known-url"],
        help="输入类型：known-url 默认不热搜；url-seed 只作发现线索",
    )
    parser.add_argument("--plan-only", action="store_true",
                        help="仅输出离线计划（不联网）")
    parser.add_argument("--force-search", action="store_true",
                        help="known-url 也强制多引擎搜索（不推荐）")
    parser.add_argument("--envelope", action="store_true",
                        help="附加 candidates/sources/coverage（归档/来源追溯用；"
                             "--archive 自动开启）")
    parser.add_argument("--no-envelope", action="store_true",
                        help="兼容保留：默认已不附加，此开关现在等同默认")
    _tg = parser.add_mutually_exclusive_group()
    _tg.add_argument(
        "--timing", "--explain-timing", dest="timing",
        action="store_true", default=True,
        help="附加各阶段墙钟耗时（默认为开）：stages 按耗时降序带占比、"
             "dispatch 给出引擎并发效率，另附固定开销与进程总计。"
             "想知道「这次搜索慢在哪」直接看它",
    )
    _tg.add_argument(
        "--no-timing", dest="timing", action="store_false",
        help="不附加阶段耗时（省几百字节上下文）",
    )
    parser.add_argument(
        "--fields", choices=("full", "agent"), default="full",
        help="JSON 字段档位：full=全量（默认）；agent=只留答案内容（剥观测标量"
             "与 null 键，每条 result 留 title/url/snippet/source/score 等；"
             "fetch_required 保留），配合 --no-envelope 供 Agent 消费",)
    parser.add_argument(
        "--archive",
        action="store_true",
        help="将本次搜索 envelope 落盘到工作区归档（不抓正文/不下载）",
    )
    parser.add_argument(
        "--archive-dir",
        type=str,
        default=None,
        help="归档根目录（默认 ARGO_ARCHIVE_ROOT 或 工作区/数据/argo-search-archive）",
    )
    parser.add_argument("--archive-tag", default=None, help="归档标签，便于 list 过滤")
    parser.add_argument("--archive-note", default=None, help="归档备注")
    parser.add_argument(
        "--verify",
        nargs="?",
        const=3,
        type=int,
        default=None,
        metavar="TOP_K",
        help="证据核验：对 top-k 未核验结果 fetch 正文、回填证据分、输出 evidence_revision 分布（默认 3）",
    )

    return parser


# ── --list-engines --detail 无过滤时的摘要（2026-09-27）────────────────────────
#
# 逐条清单要回答的问题只有两个：**有多少源可用**、**不可用的那些为什么不可用**。
# 245 个健康源逐条列出对这两个问题的贡献是零（每一条的答案都是「可用」），
# 而代价是 54 KB ≈ 14k token 进上下文。所以默认给分组计数 + 只列非可路由项。

# 不可自动路由的成因，顺序即优先级：一个源可能同时缺 env 又缺依赖，若把每种
# 成立的理由都计一次，分组之和会大于「不可路由总数」，自相矛盾。只认第一个。
_ENGINE_BLOCK_REASONS: tuple[tuple[str, str], ...] = (
    ("disabled", "enabled=False（未启用）"),
    ("missing_env", "缺环境变量（配好密钥即可用）"),
    ("missing_deps", "缺可选依赖（装好即可用）"),
    ("quota_exhausted", "配额耗尽（等窗口重置）"),
    ("not_routable", "其它不可路由（见 --engine <名> 全量行）"),
)


def _engine_block_reason(row: dict[str, Any]) -> str | None:
    """一条引擎记录「不可自动路由」的成因；可自动路由返回 None。"""
    if not row.get("enabled", True):
        return "disabled"
    if row.get("missing_env"):
        return "missing_env"
    if row.get("missing_deps"):
        return "missing_deps"
    if row.get("quota_exhausted"):
        return "quota_exhausted"
    if not row.get("routable", True):
        return "not_routable"
    return None


def _summarize_engine_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐条清单压成「计数 + 只列非可路由项」。"""
    routable: list[str] = []
    explicit_only: list[str] = []
    blocked: list[dict[str, Any]] = []
    by_reason: dict[str, int] = {}
    for row in rows:
        name = str(row.get("engine_id") or "")
        if row.get("explicit_only"):
            # 只能显式点名、不进自动路由——这是设计如此，不是故障，
            # 所以单独一桶，不混进「不可用」，也不逐条展开。
            explicit_only.append(name)
            continue
        reason = _engine_block_reason(row)
        if reason is None:
            routable.append(name)
            continue
        by_reason[reason] = by_reason.get(reason, 0) + 1
        blocked.append({
            "engine_id": name,
            "reason": reason,
            **({"missing_env": row["missing_env"]} if row.get("missing_env") else {}),
            **({"missing_deps": row["missing_deps"]} if row.get("missing_deps") else {}),
            **({"status": row["status"]} if row.get("status") else {}),
        })
    return {
        "total": len(rows),
        "routable": len(routable),
        "explicit_only": len(explicit_only),
        "not_routable": len(blocked),
        "by_reason": {k: by_reason[k] for k, _ in _ENGINE_BLOCK_REASONS
                      if k in by_reason},
        "reason_legend": {k: label for k, label in _ENGINE_BLOCK_REASONS},
        "not_routable_engines": sorted(blocked, key=lambda r: r["reason"]),
        "hint": "逐条全量清单加 --all；单引擎全量诊断用 --engine <名>",
    }


def _format_engine_summary(summary: dict[str, Any]) -> str:
    """摘要的人类可读渲染（非 --json 时用）。"""
    lines = [
        f"引擎清单摘要：共 {summary['total']} 条，可自动路由 {summary['routable']} 条，"
        f"仅显式点名 {summary['explicit_only']} 条，不可路由 {summary['not_routable']} 条",
    ]
    for reason, count in (summary.get("by_reason") or {}).items():
        label = (summary.get("reason_legend") or {}).get(reason, reason)
        lines.append(f"  · {reason:<16}{count:>4}  {label}")
    blocked = summary.get("not_routable_engines") or []
    if blocked:
        lines.append("不可路由项：")
        for row in blocked:
            extra = ""
            if row.get("missing_env"):
                # 成因与细节可能同时成立（如「未启用」的源也缺密钥）：
                # 用「另缺」而不是直接并列，避免读成两个互斥的原因。
                extra = f"（另缺 {'/'.join(str(x) for x in row['missing_env'])}）"
            elif row.get("missing_deps"):
                extra = f"（另缺 {'/'.join(str(x) for x in row['missing_deps'])}）"
            lines.append(f"  - {row['engine_id']:<28}{row['reason']}{extra}")
    lines.append(str(summary.get("hint") or ""))
    return "\n".join(lines)


def main():
    # 入口第一件事：钉住 stdout/stderr 编码（实现唯一，见 cli_io）。
    # 放在这里而不是 import 期：本模块被 search.py 与 bin/argo 两条路调用，
    # 入口点是唯一能覆盖两者的位置。
    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()

    if args.list_engines:
        # --engine 兼作清单过滤（`--engine auto` 是搜索默认值，不算过滤）。
        # 全量详细行 2026-09-16 实测 151 KB（runtime/admission 嵌套是大头），
        # Agent 查单个引擎状态时不该付这个代价——实测此前
        # `--list-engines --detail --engine egov_law` 忽略过滤、照样吐全量。
        # 现无过滤时走 compact_engine_row 瘦身（~1/4 体积），有过滤给全量。
        _wanted = None
        if args.engine and args.engine != "auto":
            _wanted = [e.strip() for e in args.engine.split(",") if e.strip()] or None
        if args.detail:
            try:
                from engine_status import (list_engines_detail,
                                           compact_engine_row,
                                           format_engines_table)
                rows = list_engines_detail(routable_only=args.routable_only,
                                           engines=_wanted)
                if not _wanted:
                    # 不带过滤时**默认不吐逐条清单**（2026-09-27 收紧）。
                    #
                    # 沿革：全量转储 151 KB → 瘦身投影 54 KB（compact_engine_row，
                    # 2026-09-16）→ 仍在一次调用里塞 54 KB ≈ 14k token 进上下文。
                    # 而这一档要回答的问题只有两个：**有多少源可用**、**不可用的
                    # 那些为什么不可用**。逐条列出 245 个健康源，是对这两个问题
                    # 的零贡献——它们每一个的答案都是「可用」。
                    #
                    # 所以默认改成「分组计数 + 只列非可路由项（带成因）」，
                    # 实测 54 KB → 约 2 KB；要逐条全量照旧加 --all。
                    # 单引擎全量诊断仍走 --engine <名>（不受本分支影响）。
                    rows = [compact_engine_row(r) for r in rows]
                if not _wanted and not args.list_all and args.json_output:
                    print(dumps(_summarize_engine_rows(rows)))
                    return
                if not _wanted and not args.list_all:
                    print(_format_engine_summary(_summarize_engine_rows(rows)))
                    return
                if _wanted:
                    # 未命中的名字要显式报出（走 stderr，保持 stdout 是纯 JSON）——
                    # 否则「查了没输出」会被误读成「该引擎状态为空」。
                    _got = {r.get("engine_id") for r in rows}
                    _missing = [e for e in _wanted if e not in _got]
                    if _missing:
                        print(f"[list-engines] 未收录的引擎: {', '.join(_missing)}",
                              file=sys.stderr)
                if args.json_output:
                    print(dumps(rows))
                else:
                    print(format_engines_table(rows))
            except Exception as e:
                print(dumps({"error": str(e), "engines": available_engines()}))
        else:
            # 不包 try/except TypeError：available_engines 已收 routable_only
            # （它当初没收，被这里的回退静默吞掉，于是 --routable-only 一直
            # 返回全量）。契约见 test_engine_catalog 的签名用例。
            names = available_engines(routable_only=args.routable_only)
            if _wanted:
                names = [n for n in names if n in set(_wanted)]
            print(dumps(names))
        return

    if not args.query:
        parser.error("必须提供搜索关键词")

    # envelope 默认**关**，要时显式开（--envelope / --archive）。
    #
    # why（2026-09-17 实测）：默认附 envelope 时一次 5 条结果的 JSON 输出 15454 B，
    # 其中 14117 B（82%）是同一批结果的三视图重复——snippet 被写三遍（5701 B，
    # 占全文 40%），candidates 每条还重复一遍 query。而这三个视图的角色是
    # **归档与来源追溯**：归档路径（--archive）本来就会把 candidates 落成
    # candidates.jsonl、并在缺 sources 时从 results 回填，所以「归档要全量」
    # 由 `or args.archive` 这一支保证，默认翻转**不损失任何能力**——需要
    # provenance 的调用者加 --envelope 即可拿到与从前逐字节相同的文档。
    #
    # 之前的方向是「默认给全量、调用者自己记得减」（--no-envelope），
    # 于是忘记加开关的调用者每次多付约 2.4k token。默认档应当服务最常见的
    # 那个用途，而不是服务最重的那个用途。
    use_envelope = args.envelope or args.archive
    # 阶段耗时：**默认开**。它是「这次慢在哪」的自解释入口——不开的话，
    # 想知道瓶颈只能外部计时 + 临时代码，等于把优化门槛抬到只有维护者能过。
    _timing = StageTiming() if args.timing else None

    # 路由预热（`_prewarm`）**已删除**（2026-09-27）。历史：daemon 线程版 →
    # 同步版（「把 22ms 变成确定的支出」）→ 删除。
    #
    # 它建立在「预热的那笔钱 route 一定要付」这个前提上，而实测前提不成立：
    #
    #   1. **route-cache 命中时 route 根本不编译域正则**（生产默认，覆盖几乎
    #      所有重复查询）。交替 5 组实测：route 阶段在有无预热下都是 4.2ms，
    #      而整次调用的中位墙上时间 107.8ms → 68.9ms——预热是 100% 白付，
    #      省下的 38.9ms（36%）正是这笔支出本身。
    #   2. **未命中时也预付错了对象**。profile 显示 route_query 首次调用的
    #      开销主要来自懒加载 import 与构建 249 个引擎的注册表；先预热再
    #      `route_query`，route 阶段实测仍要 47–121ms。域编译只是其中一小截。
    #   3. 未命中路径的总额不因删除而变：编译挪回 route 内部，同样只付一次。
    #
    # 想重新引入时先读这三条：任何「预热」都必须证明它预热的正是 route 会在
    # 同一进程、同一路径上付的那一笔，否则只是把支出提前，不是省下。
    # 回归守卫见 tests/test_plan_a_optimizations.py::TestNoUnconditionalPrewarm。
    results = super_search(
        query=args.query,
        engine=args.engine,
        n=args.max_results,
        explain=args.explain,
        skip_cache=args.no_cache,
        timeout=args.timeout,
        depth=args.depth,
        mode=args.mode,
        local_first=args.local_first,
        input_kind=args.input_kind,
        plan_only=args.plan_only,
        force_search=args.force_search,
        envelope=use_envelope,
        since=args.since,
        until=args.until,
        sort=args.sort,
        include_domains=[d for d in args.include_domains.split(",") if d.strip()] or None,
        exclude_domains=[d for d in args.exclude_domains.split(",") if d.strip()] or None,
        engine_domain=args.domain or None,
        engine_sub_domain=args.sub_domain or None,
        timing=_timing,
        # 三态：--include-local 强制开 / --no-local 强制关 / 都不给 = None
        #（由 super_search 按模式自动解析，fast/budget 开）。本地命中并入
        # 统一走 shape_response（search_output）——此前 CLI 在搜索结束后
        # 串行补一次合并（白等一个 seek 子进程，且与库路径双份逻辑），
        # 交回 super_search 后自动获得 L1 并行提交（与主搜索同时跑）。
        include_local=(True if args.include_local
                       else (False if args.no_local else None)),
    )

    # 固定开销（import + argparse + 收尾）：缓存命中时它占墙钟大头，而它不出现
    # 在任何阶段里——不显式报出来，看的人会把「启动 80 ms」当成「搜索 80 ms」，
    # 优化方向就找错了。
    if _timing is not None and "timing" in results:
        _mod_t0, _imports_done = _resolve_module_timing()
        _now_ms = (time.perf_counter() - _mod_t0) * 1000.0
        _stages = results["timing"].get("stages_ms") or 0
        results["timing"]["import_ms"] = round(
            (_imports_done - _mod_t0) * 1000.0, 1)
        results["timing"]["overhead_ms"] = round(max(0.0, _now_ms - _stages), 1)
        results["timing"]["process_ms"] = round(_now_ms, 1)

    # 本地命中并入已上移进 super_search（shape_response 统一出口，L1 并行），
    # 这里不再串行补合并——双份合并会让同一批命中出现两次。

    if args.archive and results.get("status") != "handoff_required":
        try:
            from archive_run import write_search_archive, resolve_archive_root
            root = resolve_archive_root(args.archive_dir) if args.archive_dir else None
            if args.archive_dir:
                root = resolve_archive_root(args.archive_dir)
            meta = write_search_archive(
                results,
                root=root,
                tag=args.archive_tag,
                note=args.archive_note,
                source="argo_search",
            )
            if not args.json_output:
                print(
                    f"  [archive] {meta.get('run_id')} → {meta.get('run_dir')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"  [archive error] {type(e).__name__}: {e}", file=sys.stderr)

    # 证据完整链路 P0：--verify 显式核验 top-k 未核验结果（fetch + 回填 + revision 分布）
    _t_verify0 = time.perf_counter()
    if args.verify:
        try:
            from evidence_loop import verify_results, reorder_by_evidence
            v = verify_results(results.get("results") or [], args.query, top_k=args.verify)
            results["verify"] = v
            # A-3（2026-09-27）：正文级证据**回写排序**。此前 verify 只改字段不重排，
            # 抓回的正文质量到不了排序器——花了 RTT 却不改结果次序。
            try:
                reorder = reorder_by_evidence(results.get("results") or [])
                if reorder.get("reordered"):
                    results["evidence_reorder"] = reorder
            except Exception as _re:
                print(f"  [verify reorder skipped] {type(_re).__name__}", file=sys.stderr)
            results["fetch_required"] = bool(results.get("fetch_required"))
            # verify 已回填/核验结果 → 刷新门控汇总，避免 suggested 含已核验 URL
            try:
                from evidence_loop import gate_results
                gate = gate_results(results.get("results") or [], results.get("domain"))
                results["evidence_loop"] = {
                    "high_consequence_domain": gate["high_consequence_domain"],
                    "suggested": gate["suggested"],
                    "verified_count": gate["verified_count"],
                    "pending_count": gate["pending_count"],
                }
            except Exception as _ge:
                # 内层 pass 会把 evidence_loop 从 --json 输出里静默抹掉，
                # 调用方无从得知门控汇总已缺失。与上方 verify reorder 同款：
                # 打到 stderr，不打断主流程。
                print(f"  [evidence_loop refresh skipped] {type(_ge).__name__}",
                      file=sys.stderr)
            if not args.json_output:
                rs = v.get("revision_summary") or {}
                print(
                    f"  [verify] 核验 {rs.get('n', 0)} 条，"
                    f"improved={rs.get('improved', 0)} unchanged={rs.get('unchanged', 0)} "
                    f"degraded={rs.get('degraded', 0)} mean_delta={rs.get('mean_delta', 0)}",
                    file=sys.stderr,
                )
                ro = results.get("evidence_reorder") or {}
                if ro.get("reordered"):
                    print(f"  [verify] 证据回写排序：调整 {len(ro.get('adjusted') or [])} 条，"
                          f"位次变动 {ro.get('moved', 0)} 处", file=sys.stderr)
        except Exception as e:
            print(f"  [verify error] {type(e).__name__}: {e}", file=sys.stderr)

    # 计时盲区修补（2026-09-29）：--verify 的 top-k fetch 发生在上面计时快照
    # （process_ms 定格处）之后，净增 280–1160ms 完全不进任何账——实测
    # wall 776–1746ms vs process_ms 494–701ms，「这次为什么慢」归因指错
    # 方向。快照后移做不到（verify 是装配后的可选后处理），补一个 stage
    # 并把耗时并回 stages_ms / process_ms，两个总数保持自洽。
    if _timing is not None and "timing" in results and args.verify:
        _verify_ms = (time.perf_counter() - _t_verify0) * 1000.0
        _t = results["timing"]
        _total = float(_t.get("stages_ms") or 0) + _verify_ms
        _t.setdefault("stages", []).append({
            "stage": "verify",
            "ms": round(_verify_ms, 1),
            "pct": round(_verify_ms * 100.0 / _total, 1) if _total > 0 else 0.0,
        })
        _t["stages_ms"] = round(_total, 1)
        _t["process_ms"] = round(
            float(_t.get("process_ms") or 0) + _verify_ms, 1)

    if args.json_output:
        public = {k: v for k, v in results.items() if not k.startswith("_")}
        if args.fields == "agent":
            # 静默白开：_strip_for_agent 会把 candidates/coverage 全剥掉，实测
            # --envelope 在 agent 档下的增量是 0 字节（envelope 单独 19KB，叠加后
            # 与纯 agent 档逐字节相同）。调用方会以为拿到了 provenance、实际没有，
            # 所以这里明确告知，而不是让它默默失效。
            if args.envelope:
                print(
                    "  [warning] --envelope 与 --fields agent 同时给时 envelope 会被"
                    "剥光（实测增量 0 字节）：要 provenance 请去掉 --fields agent；"
                    "只要瘦身就别加 --envelope。",
                    file=sys.stderr,
                )
            public = _strip_for_agent(public)
        print(dumps(public))
    else:
        print(format_text_output(results))
        if results.get("timing"):
            print(format_timing(results["timing"]))
        if results.get("archive"):
            ar = results["archive"]
            print(f"  archived → {ar.get('run_dir')}")


if __name__ == "__main__":
    main()
