#!/usr/bin/env python3
"""batch_probe.py — 批量 URL 预检报告。

第一性原理：批量抓取最贵的失败方式是「跑到一半才知道一半拿不到」。
整批直接开工，失败会在中途以各种形态冒出来（登录墙、链接已死、
内容早就有），此时预算已经花掉一半。预检把可预判的失败挪到开工前，
先给一张「什么能拿、什么拿不到、为什么」的清单，再决定怎么执行。

条目分四类：
  unsupported   无法处理：不是合法的 http(s) URL——执行前就该剔除
  needs_auth    存在但需要登录态：用户能解决（先补凭证），提前列出
  already_have  本地归档已有该 URL 的结果：跳过，不重复抓
  not_found     探测发现已死（404/410）：从批次里剔除或换源
                （仅在 --probe 开启时判定；不探测不臆测）

结论三档：
  go            全部可执行
  go_with_skips 部分可执行（清单随报告给出）
  stop          没有可执行条目

两条纪律：
  - needs_auth 是唯一「用户可行动」的问题，报告里单独一节；
    unsupported/not_found 是平台侧事实，列出即可。
  - 网络探测失败（连不上）不产生判定——探测不到不等于不存在，
    不能把「可能的问题」换成「确定的误杀」。未探测的条目类别为 None，
    只记 unknown，不影响 go 判定。

本模块只做判定，不做抓取：网络探测是可选步骤，抓取由 article/crawl
等执行器负责。判定与 I/O 分离，保证纯函数部分可单测。
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from cli_io import dumps
# 状态词表唯一真源：条目分类与批次结论（2026-11-09 收口）
import engine_states

UNSUPPORTED = engine_states.PROBE_UNSUPPORTED
NEEDS_AUTH = engine_states.PROBE_NEEDS_AUTH
ALREADY_HAVE = engine_states.PROBE_ALREADY_HAVE
NOT_FOUND = engine_states.PROBE_NOT_FOUND
UNKNOWN = engine_states.PROBE_UNKNOWN

# 用户可自行解决的问题（报告分节与文案依据）
ACTIONABLE = {NEEDS_AUTH}

# 常年登录墙的站点（公开内容也要登录才能看）。这里只列「正文必须登录」
# 的域——「有登录墙但首页可读」的站不列，避免误杀。
_KNOWN_LOGIN_WALLS = (
    "x.com", "twitter.com", "instagram.com", "threads.net",
    "facebook.com", "patreon.com", "medium.com",
    "linux.do", "bbs.zhihu.com",
)

# 登录墙里的例外：单条推文有免登录通道（twitter_syndication 引擎走
# cdn.syndication.twimg.com）。判 needs_auth 会把用户支去「补凭证」，
# 而这条 URL 其实现在就能抓——同一批次里两个功能互相打架。
_LOGIN_WALL_EXEMPT_HOSTS = ("x.com", "twitter.com")


def _has_login_free_channel(host: str, url: str) -> bool:
    """该 URL 是否走 argo 已有的免登录通道（目前仅单条推文）。"""
    if not any(host == d or host.endswith("." + d)
               for d in _LOGIN_WALL_EXEMPT_HOSTS):
        return False
    try:
        from engines_builders_intl import extract_tweet_id
    except ImportError:
        return False
    return extract_tweet_id(url) is not None


_TIER_LIMITS = {"probe": 8}


def classify_url(url: str, known_urls: set[str] | None = None) -> str:
    """本地规则分类（无网络）。返回四类之一或 UNKNOWN（合法但无本地证据）。"""
    u = (url or "").strip()
    if not u:
        return UNSUPPORTED
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return UNSUPPORTED
    host = parsed.hostname.lower()
    if known_urls and u in known_urls:
        return ALREADY_HAVE
    if any(host == d or host.endswith("." + d) for d in _KNOWN_LOGIN_WALLS):
        # 例外的判定要用原始 URL（推文 ID 在路径/查询串里，不是 host 的一部分）
        if _has_login_free_channel(host, u):
            return UNKNOWN
        return NEEDS_AUTH
    return UNKNOWN


def probe_liveness(url: str, timeout: float = 6.0) -> str | None:
    """网络探测单条 URL 的存活。

    返回 NOT_FOUND（404/410，确定已死）、UNKNOWN（任何其他结局——
    包括连接失败：连不上不是「不存在」的证据）。
    """
    try:
        from http_client import fetch_url
        resp = fetch_url(url, timeout=timeout)
        status = resp.get("status") or 0
        if status in (404, 410):
            return NOT_FOUND
        return UNKNOWN
    except Exception:
        return UNKNOWN


def probe_batch(
    urls: list[str],
    *,
    known_urls: set[str] | None = None,
    do_probe: bool = False,
) -> dict[str, Any]:
    """对一批 URL 生成预检报告。

    返回 {
      total, ready, verdict,
      items: [{url, problem(或 None), actionable}],
      ready_urls,          # 可执行清单
      by_problem: {类别: [url]},
    }
    """
    known = known_urls or set()
    items: list[dict[str, Any]] = []

    # 网络探测只对「本地无证据」的条目做——unsupported/auth/已归档的
    # 判定不需要网络；探测并发受 TIER_LIMITS 约束，不放大源站压力
    local_kinds = [classify_url(u, known) for u in urls]
    net_kinds: dict[int, str] = {}
    if do_probe:
        candidates = [(i, u) for i, (u, k) in enumerate(zip(urls, local_kinds))
                      if k == UNKNOWN]
        if candidates:
            with ThreadPoolExecutor(
                    max_workers=min(len(candidates), _TIER_LIMITS["probe"])) as ex:
                for (i, _u), kind in zip(candidates,
                                         ex.map(lambda t: probe_liveness(t[1]),
                                                candidates)):
                    net_kinds[i] = kind or UNKNOWN

    for i, u in enumerate(urls):
        kind = local_kinds[i]
        if kind == UNKNOWN and i in net_kinds:
            kind = net_kinds[i]
        items.append({
            "url": u,
            "problem": kind if kind != UNKNOWN else None,
            "actionable": kind in ACTIONABLE,
        })

    ready = [it for it in items if it["problem"] is None]
    by_problem: dict[str, list[str]] = {}
    for it in items:
        if it["problem"]:
            by_problem.setdefault(it["problem"], []).append(it["url"])

    if not ready:
        verdict = engine_states.PROBE_STOP
    elif len(ready) < len(items):
        verdict = engine_states.PROBE_GO_WITH_SKIPS
    else:
        verdict = engine_states.PROBE_GO

    return {
        "total": len(items),
        "ready": len(ready),
        "verdict": verdict,
        "items": items,
        "ready_urls": [it["url"] for it in ready],
        "by_problem": by_problem,
    }


def _format_report(report: dict[str, Any]) -> str:
    """纯文本报告（给人看）。"""
    lines = [
        f"预检报告：共 {report['total']} 条，可执行 {report['ready']} 条"
        f"（verdict={report['verdict']}）",
        "",
    ]
    labels = {
        NEEDS_AUTH: "需要登录态（可行动：先补凭证再抓）",
        UNSUPPORTED: "无法处理（非 http(s) URL）",
        ALREADY_HAVE: "本地归档已有（跳过不重抓）",
        NOT_FOUND: "探测已死（404/410）",
    }
    for kind in (NEEDS_AUTH, UNSUPPORTED, ALREADY_HAVE, NOT_FOUND):
        urls = report["by_problem"].get(kind) or []
        if urls:
            lines.append(f"■ {labels[kind]}（{len(urls)}）")
            lines.extend(f"  - {u}" for u in urls)
            lines.append("")
    lines.append(f"■ 可执行清单（{len(report['ready_urls'])}）")
    lines.extend(f"  - {u}" for u in report["ready_urls"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="批量 URL 预检")
    ap.add_argument("urls", nargs="*", help="URL 列表（空格分隔）")
    ap.add_argument("--file", "-f", help="从文件读取（每行一条 URL）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--probe", action="store_true",
                    help="联网探测存活（404/410 判死；默认纯本地规则）")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        p = Path(args.file).expanduser()
        if not p.exists():
            print(f"文件不存在: {p}", file=sys.stderr)
            return 1
        urls.extend(
            line.strip() for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#"))
    if not urls:
        from cli_io import read_stdin_if_piped
        urls = [line.strip() for line in read_stdin_if_piped().splitlines()
                if line.strip()]
    if not urls:
        ap.print_help()
        return 1

    report = probe_batch(urls, do_probe=args.probe)
    if args.json:
        print(dumps(report))
    else:
        print(_format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
