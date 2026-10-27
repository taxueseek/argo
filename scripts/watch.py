#!/usr/bin/env python3
"""watch.py — 观察模式：网页快照与变化检测（Monitor 形态）。

吸纳自 2026-09 对标研究：Parallel Monitor / Seltz Monitor / Exa Monitors
三家同方向，已是 agent 搜索的标配能力。MVP 落点=本地快照 + 内容指纹比对：
抓取走 fetch_v3 降级链（含 Parallel 免费 web_fetch 级），比对用规范化文本的
sha256（空白折叠，消除抓取批次间的排版噪声），不依赖上游 Monitor 付费端点。
cron 可用：argo watch check --json。

用法：
  argo watch add <url> [--note 注释]    建立观察并抓首帧快照
  argo watch check [--url URL] [--json] 复检（默认全部已观察 URL）
  argo watch list [--json]              列出观察目标与最近状态
  argo watch remove <url>               移除观察
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from typing import Any

from cli_io import dumps, dumps_pretty

_SNAP_CAP = 10  # 每个 URL 保留的快照历史上限（旧的丢弃）


# ── 存储 ─────────────────────────────────────────────────────────────────────

def _store_path():
    from argo_paths import ensure_state_dir
    return ensure_state_dir("watch") / "watch.json"


def _load() -> dict[str, Any]:
    p = _store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("urls"), dict):
            return data
    except Exception:
        pass
    return {"_v": 1, "urls": {}}


def _save(data: dict[str, Any]) -> None:
    p = _store_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(dumps_pretty(data), encoding="utf-8")
    tmp.replace(p)


# ── 快照 ─────────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """规范化：折叠空白。抓取批次间排版噪声不触发假变化。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


# ── 噪声感知 diff ────────────────────────────────────────────────────────────
#
# 只比哈希会把「价格变了 1 元」「阅读数从 100 涨到 101」都报成变化，观察列表
# 很快被噪音淹没。这里先把日期、相对时间、价格、计数替换成占位符再比：
# 占位符之外完全一致 → noise；任何文字差异 → substantive。
#
# 纯数字（bare number）故意不算噪声：把「支持 3 种模式」改成「5 种」当成噪声
# 会漏掉真实内容变化。宁可多报一次，不可漏报。

_NOISE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("date", re.compile(
        r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?"
        r"|\d{4}年\d{1,2}月(?:\d{1,2}[日号])?"
        r"|\d{1,2}月\d{1,2}[日号]"
        r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(?:, \d{4})?\b)",
        re.IGNORECASE)),
    ("relative_time", re.compile(
        r"(?:\b(?:\d+|an?|one)\s?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?"
        r"|weeks?|months?|years?|yrs?)\s+ago\b|\bjust now\b|\byesterday\b"
        r"|\d+\s?(?:个)?(?:秒钟?|分钟|小时|天|周|星期|个月|年)[前后]|刚刚|昨天|前天)",
        re.IGNORECASE)),
    ("price", re.compile(
        r"(?:(?:US\$|[$€£¥￥₹₩])\s?\d[\d,]*(?:\.\d+)?"
        r"|\b(?:USD|EUR|GBP|CNY|RMB|JPY)\s?\d[\d,]*(?:\.\d+)?"
        r"|\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|CNY|RMB|JPY|元|块钱|美元|欧元|日元|港元))",
        re.IGNORECASE)),
    ("count", re.compile(
        r"(?:\d[\d,]*(?:\.\d+)?[kKmM万]?\s?(?:次阅读|次浏览|次播放|次下载|条评论|条回复"
        r"|个赞|人关注|人在看|阅读|浏览|评论|点赞|收藏|播放|转发|分享|下载|回复|关注|赞"
        r"|views?|comments?|likes?|reads?|shares?|replies|retweets?|stars?|downloads?"
        r"|followers?|subscribers?|votes?|points?|visits?|viewers?|reviews?|ratings?)\b"
        r"|(?:次阅读|次浏览|次播放|次下载|条评论|条回复|个赞|人关注|人在看|阅读|浏览|评论"
        r"|点赞|收藏|播放|转发|分享|下载|回复|关注|赞)[:：]?\s?\d[\d,]*)",
        re.IGNORECASE)),
]


def _mask_noise(text: str) -> str:
    for name, pattern in _NOISE_PATTERNS:
        text = pattern.sub(f"<{name.upper()}>", text)
    return text


def _noise_categories(old: str, new: str) -> list[str]:
    """找出「把该类替换成占位符后两版就一致」的类别——即这次差异由谁解释。"""
    changed = []
    for name, pattern in _NOISE_PATTERNS:
        if not (pattern.search(old) or pattern.search(new)):
            continue
        if _norm(pattern.sub("<X>", old)) == _norm(pattern.sub("<X>", new)):
            changed.append(name)
    return changed


def _blocks(text: str) -> list[str]:
    """按空行切块；围栏代码块内部的空行不切（否则 diff 会把代码拆散）。"""
    blocks: list[str] = []
    current: list[str] = []
    fence = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
        if not stripped and not fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def _block_key(block: str) -> str:
    return _norm(block)


def diff_text(old: str, new: str) -> dict:
    """比较两版正文。返回 change_type（none/noise/substantive）与变化块样本。"""
    if _norm(old) == _norm(new):
        return {"change_type": "none", "noise_categories": [],
                "added_blocks": [], "removed_blocks": []}
    old_blocks = _blocks(old)
    new_blocks = _blocks(new)
    old_keys = {_block_key(b) for b in old_blocks}
    new_keys = {_block_key(b) for b in new_blocks}
    added = [b for b in new_blocks if _block_key(b) not in old_keys][:3]
    removed = [b for b in old_blocks if _block_key(b) not in new_keys][:3]
    if _norm(_mask_noise(old)) == _norm(_mask_noise(new)):
        change_type = "noise"
    else:
        change_type = "substantive"
    return {
        "change_type": change_type,
        "noise_categories": _noise_categories(old, new) if change_type == "noise" else [],
        "added_blocks": [b[:200] for b in added],
        "removed_blocks": [b[:200] for b in removed],
    }


def _snapshot(url: str, skip_cache: bool = True) -> dict:
    """抓一帧快照；skip_cache=True 观察必须拿新鲜内容（fetch 缓存会掩盖变化）。"""
    from fetch_v3 import fetch_v3
    r = fetch_v3(url, max_chars=20000, skip_cache=skip_cache)
    content = r.get("content") or ""
    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hash": _content_hash(content) if content else "",
        "length": len(content),
        "title": str(r.get("title") or "")[:200],
        "method": str(r.get("fetch_method") or ""),
        "head": _norm(content)[:200],
        # 只留最近一帧的正文：diff 需要上一版内容，历史帧只留指纹就够。
        # 全部保留会让 watch.json 随观察数线性膨胀（每帧 20k 字符）。
        "content": content[:20000],
        "success": bool(r.get("success")) and bool(content),
        "error": str(r.get("error") or "")[:200],
    }


def _trim_contents(entry: dict) -> None:
    """只保留最新一帧正文，历史帧删掉内容字段（旧数据本来就没有）。"""
    snaps = entry.get("snapshots") or []
    for snap in snaps[:-1]:
        snap.pop("content", None)


def _latest(url: str, data: dict) -> dict | None:
    snaps = (data["urls"].get(url) or {}).get("snapshots") or []
    return snaps[-1] if snaps else None


# ── 子命令 ───────────────────────────────────────────────────────────────────

def cmd_add(url: str, note: str) -> dict:
    data = _load()
    entry = data["urls"].get(url) or {"note": note, "added_at":
                                      time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                      "snapshots": []}
    entry["note"] = note or entry.get("note", "")
    snap = _snapshot(url)
    entry["snapshots"] = (entry["snapshots"] + [snap])[-_SNAP_CAP:]
    _trim_contents(entry)
    data["urls"][url] = entry
    _save(data)
    return {"url": url, "snapshot": snap}


def cmd_check(only_url: str | None) -> list[dict]:
    data = _load()
    urls = [only_url] if only_url else sorted(data["urls"].keys())
    if only_url and only_url not in data["urls"]:
        return [{"url": only_url, "error": "未在观察列表中（先 argo watch add）"}]
    reports = []
    for url in urls:
        prev = _latest(url, data)
        snap = _snapshot(url)
        report: dict[str, Any] = {"url": url}
        if not snap["success"]:
            report.update({"changed": None, "error": snap["error"] or "抓取失败",
                           "kept_snapshot": prev is not None})
            # 抓取失败不改快照：网络抖动不该伪造「无变化」或「变化」
            reports.append(report)
            continue
        if prev is None:
            report.update({"changed": None, "note": "无历史快照（首帧已记录）"})
        else:
            report.update({
                "changed": snap["hash"] != prev.get("hash"),
                "previous_hash": prev.get("hash"),
                "new_hash": snap["hash"],
                "length_delta": snap["length"] - prev.get("length", 0),
                "previous_fetched_at": prev.get("fetched_at"),
            })
            # 有上一帧正文才做噪声判定；旧快照只有指纹，如实标 unknown，
            # 不能拿哈希不同就断言「实质变化」。
            if prev.get("content") and snap.get("content"):
                diff = diff_text(prev["content"], snap["content"])
                report.update(diff)
                # 以正文 diff 为准：哈希只做快速路径，diff 说没变就是没变。
                report["changed"] = diff["change_type"] != "none"
            elif report["changed"]:
                report["change_type"] = "unknown"
        entry = data["urls"][url]
        entry["snapshots"] = (entry["snapshots"] + [snap])[-_SNAP_CAP:]
        _trim_contents(entry)
        entry["last_checked"] = snap["fetched_at"]
        reports.append(report)
    _save(data)
    return reports


def cmd_list() -> list[dict]:
    data = _load()
    out = []
    for url, entry in sorted(data["urls"].items()):
        latest = (entry.get("snapshots") or [{}])[-1]
        out.append({"url": url, "note": entry.get("note", ""),
                    "added_at": entry.get("added_at"),
                    "last_checked": entry.get("last_checked"),
                    "snapshots": len(entry.get("snapshots") or []),
                    "last_hash": latest.get("hash"),
                    "last_method": latest.get("method")})
    return out


def cmd_remove(url: str) -> bool:
    data = _load()
    if url in data["urls"]:
        del data["urls"][url]
        _save(data)
        return True
    return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="观察模式：网页快照与变化检测")
    ap.add_argument("action", choices=["add", "check", "list", "remove"])
    ap.add_argument("url", nargs="?", default="", help="目标 URL")
    ap.add_argument("--note", default="", help="add 时的备注")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.action == "add":
        if not args.url:
            print(dumps({"status": "error", "error": "用法: argo watch add <url>"}))
            sys.exit(1)
        r = cmd_add(args.url, args.note)
        payload = {"status": "completed", **r}
    elif args.action == "check":
        payload = {"status": "completed", "results": cmd_check(args.url or None)}
    elif args.action == "list":
        payload = {"status": "completed", "results": cmd_list()}
    else:
        if not args.url or not cmd_remove(args.url):
            print(dumps({"status": "error",
                         "error": f"未找到观察目标: {args.url}"}))
            sys.exit(1)
        payload = {"status": "completed", "removed": args.url}

    if args.json or args.action in ("check", "list"):
        print(dumps(payload))
        return
    # 人类可读（add / remove）
    if args.action == "add":
        s = payload["snapshot"]
        if s["success"]:
            print(f"已观察 {payload['url']}")
            print(f"  首帧: hash={s['hash']} length={s['length']} method={s['method']}")
            print(f"  开头: {s['head'][:100]}")
        else:
            print(f"已记录 {payload['url']}，但首帧抓取失败: {s['error']}")
    else:
        print(f"已移除 {payload['removed']}")


if __name__ == "__main__":
    main()
