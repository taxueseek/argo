#!/usr/bin/env python3
"""crawl.py — 站点级爬取（fetch_v3 降级链：增强 HTTP → TLS 指纹 → Wayback）。

与旧版的区别在「抓什么」和「怎么给」两处：

  抓什么：URL 先过 crawl_frontier（归一化、资产过滤、坏路径、范围），正文哈希去重；
          每次请求过 crawl_policy（每主机间隔/并发、429 退避），robots 合规仍由
          fetch_v3 内部的 robots_guard 负责。
  怎么给：默认每页正文按 --max-chars 截断，--query 用现成的 BM25 聚焦只回相关段落，
          --full 回全文。旧版固定 content[:500]、每页只跟进前 5 个链接，这两处
          是「抓到了但没法用」的主因。

批量场景禁浏览器降级：BFS 可能爬几十页，每页启动 Chrome CDP 会拖慢整体。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import deque

from bounded_run import TaskError, run_bounded
from cli_io import dumps
from crawl_frontier import (
    Frontier,
    canonical,
    extract_links,
    looks_html,
    strip_session_params,
)
from crawl_policy import HostGate, retry_after_seconds, status_of

# 单页抓取额度：需要完整 HTML 才能提取全部链接。fetch_v3 的 html 交付视图是
# max_chars*2，旧版用 2000 当抓取额度等于只看前 4KB，正文中后段的链接全丢。
_FETCH_CHARS = 200000
_PER_PAGE_LINKS = 200
_DEFAULT_MAX_CHARS = 2000


def _crawl_fetch(url: str, max_chars: int, timeout: int, raw: bool = True) -> dict:
    """批量爬取用轻量降级链（不升浏览器）：HTTP → TLS 指纹 → Wayback。

    raw 默认 True：爬取需要原始 HTML 提取链接，且 fetch_v3 缓存不存 html，
    必须绕过缓存才能拿到完整页面。通过模块级 fetch_page 转发，
    测试可 monkeypatch crawl.fetch_page 注入假抓取。
    """
    from fetch_v3 import fetch_v3
    return fetch_v3(url, max_chars=max_chars, timeout=float(timeout),
                    use_browser_fallback=False, skip_cache=raw, need_html=raw)


# 默认实现指向 fetch_v3 的兼容入口；测试可整体替换 fetch_page，
# 也可以按旧契约替换 _crawl_fetch（这里保留一层转发，两种打桩都生效）。
def fetch_page(url: str, max_chars: int, timeout: int, raw: bool = True) -> dict:
    return _crawl_fetch(url, max_chars, timeout, raw=raw)


def _content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text or "").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _focus(content: str, query: str) -> str:
    if not query or not content:
        return content
    try:
        from focus_extract import focus_extract
        return focus_extract(content, query, top_k=5)
    except Exception:
        return content


def _shape_page(result: dict, url: str, depth: int, max_chars: int,
                query: str) -> dict | None:
    content = result.get("content") or ""
    if not content:
        return None
    focused = _focus(content, query)
    delivered = focused if max_chars <= 0 else focused[:max_chars]
    return {
        "url": url,
        "depth": depth,
        "title": str(result.get("title") or "")[:200],
        "content": delivered,
        "length": len(focused),
        "hash": _content_hash(content),
        "status": status_of(result),
    }


def _fetch_batch(batch: list[tuple[str, int]], timeout: int, gate: HostGate,
                 stats: dict) -> list[tuple[str, int, dict]]:
    """并发抓一层；gate 在 worker 内部阻塞，所以等待预算要算上排队时间。"""
    interval = gate.interval

    def worker(item: tuple[str, int]) -> dict:
        url, _depth = item
        with gate.slot(url) as lease:
            try:
                result = fetch_page(url, _FETCH_CHARS, timeout, raw=True)
            except Exception as exc:  # noqa: BLE001 - 单页失败不拖垮整次爬取
                lease.note(0)
                stats["fetch_failed"] += 1
                return {"success": False, "error": str(exc)[:160]}
            lease.note(status_of(result), retry_after_seconds(result))
            return result

    finished, unfinished = run_bounded(
        batch, worker,
        wait_s=float(timeout) * 2 + interval * max(1, len(batch)) + 5,
        max_workers=min(len(batch), 8),
    )
    stats["unfinished_urls"] += len(unfinished)
    out = []
    for (url, depth), result in finished:
        if isinstance(result, TaskError) or not isinstance(result, dict):
            stats["fetch_failed"] += 1
            continue
        out.append((url, depth, result))
    # 按入队顺序返回，不按线程完成先后：同一份输入两次爬取要得到同样的页序，
    # 否则测试和调用方的 diff 都会被调度抖动影响。
    order = {url: index for index, (url, _depth) in enumerate(batch)}
    out.sort(key=lambda item: order.get(item[0], len(order)))
    return out


def _crawl(url: str, max_pages: int, max_depth: int, timeout: int,
           strategy: str, query: str = "", scope: str = "auto",
           max_chars: int = _DEFAULT_MAX_CHARS, interval: float = 1.0,
           policy: HostGate | None = None) -> dict:
    t0 = time.monotonic()
    frontier = Frontier(url, scope=scope)
    gate = policy or HostGate(interval=interval)
    stats = {key: 0 for key in (
        "offsite", "asset", "pathological", "out_of_scope", "non_html",
        "duplicate", "fetch_failed", "unfinished_urls")}
    pages: list[dict] = []
    seen_hashes: set[str] = set()
    visited: set[str] = set()

    if strategy == "sitemap":
        seeds = _sitemap_seeds(url, timeout, gate, stats)
        queue = deque((seed, 0) for seed in seeds[:max_pages * 2])
    else:
        queue = deque([(strip_session_params(canonical(url)), 0)])

    while queue and len(pages) < max_pages:
        batch: list[tuple[str, int]] = []
        while queue and len(batch) < max_pages - len(pages):
            candidate, depth = queue.popleft()
            if depth > max_depth:
                continue
            ok, reason = frontier.accept(candidate)
            if not ok:
                stats[reason] += 1
                continue
            if candidate in visited:
                continue
            visited.add(candidate)
            batch.append((candidate, depth))
        if not batch:
            break
        for candidate, depth, result in _fetch_batch(batch, timeout, gate, stats):
            if not result.get("success"):
                stats["fetch_failed"] += 1
                continue
            html = result.get("html") or ""
            if html and not looks_html(html):
                stats["non_html"] += 1
                continue
            page = _shape_page(result, candidate, depth, max_chars, query)
            if page is None:
                stats["non_html"] += 1
                continue
            if page["hash"] in seen_hashes:
                stats["duplicate"] += 1
            else:
                seen_hashes.add(page["hash"])
                pages.append(page)
            if len(pages) >= max_pages:
                break
            if depth >= max_depth:
                continue
            # 正文重复的页面仍要跟进链接：列表页/分页常常正文相同但链接不同。
            for link in extract_links(html, candidate)[:_PER_PAGE_LINKS]:
                link = strip_session_params(link)
                if link not in visited:
                    queue.append((link, depth + 1))
    return {
        "url": url,
        "pages": pages,
        "total": len(pages),
        "skipped": stats,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


def _sitemap_seeds(url: str, timeout: int, gate: HostGate, stats: dict) -> list[str]:
    """取 sitemap 里的 <loc>，过前沿规则后返回；取不到退回空列表。"""
    sitemap_url = url.rstrip("/") + "/sitemap.xml"
    try:
        with gate.slot(sitemap_url) as lease:
            result = fetch_page(sitemap_url, _FETCH_CHARS, timeout, raw=True)
            lease.note(status_of(result), retry_after_seconds(result))
    except Exception:
        return []
    if not result.get("success"):
        return []
    text = result.get("html") or ""
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, flags=re.IGNORECASE | re.DOTALL)
    # sitemap 天然列全站，范围按整个主机算，不套起始路径前缀。
    frontier = Frontier(url, scope="domain")
    seeds = []
    for loc in locs:
        loc = strip_session_params(canonical(loc))
        ok, reason = frontier.accept(loc)
        if not ok:
            stats[reason] += 1
            continue
        seeds.append(loc)
    return seeds


def crawl_sitemap(url: str, max_pages: int = 20, timeout: int = 10,
                  query: str = "", scope: str = "auto",
                  max_chars: int = _DEFAULT_MAX_CHARS, interval: float = 1.0,
                  policy: HostGate | None = None) -> dict:
    return _crawl(url, max_pages, 0, timeout, "sitemap", query=query,
                  scope=scope, max_chars=max_chars, interval=interval,
                  policy=policy)


def crawl_bfs(url: str, max_pages: int = 10, max_depth: int = 2,
              timeout: int = 8, query: str = "", scope: str = "auto",
              max_chars: int = _DEFAULT_MAX_CHARS, interval: float = 1.0,
              policy: HostGate | None = None) -> dict:
    return _crawl(url, max_pages, max_depth, timeout, "bfs", query=query,
                  scope=scope, max_chars=max_chars, interval=interval,
                  policy=policy)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('url')
    p.add_argument('--strategy', default='bfs', choices=['sitemap', 'bfs'])
    p.add_argument('--max-pages', type=int, default=10)
    p.add_argument('--max-depth', type=int, default=2)
    p.add_argument('--query', default='',
                   help='BM25 聚焦：每页只回与查询相关的段落')
    p.add_argument('--scope', default='auto',
                   choices=['auto', 'path', 'domain'],
                   help='auto=起始目录（叶子页上提一层）/ path=严格前缀 / domain=整站')
    p.add_argument('--max-chars', type=int, default=_DEFAULT_MAX_CHARS,
                   help='每页交付字符数，0=不截断')
    p.add_argument('--full', action='store_true', help='等价于 --max-chars 0')
    p.add_argument('--interval', type=float, default=1.0,
                   help='同一主机两次请求的最小间隔（秒），默认 1')
    # bin/argo 的 usage 承诺了 Common flags「--json」，此前 crawl/extract 的
    # parser 里没有它，Agent 照文档传参直接撞墙（error: unrecognized
    # arguments，exit 2）。这里接住该开关并给出真实语义：默认仍是截断预览
    # （防上下文膨胀），--json 输出完整可解析 JSON——截断的 JSON 不是合法
    # JSON，调用方 json.loads 必失败，等于「拿不到数据还得自己猜」。
    p.add_argument('--json', action='store_true',
                   help='输出完整 JSON（默认为 2000 字符截断预览）')
    args = p.parse_args()
    max_chars = 0 if args.full else args.max_chars
    if args.strategy == 'sitemap':
        r = crawl_sitemap(args.url, args.max_pages, query=args.query,
                          scope=args.scope, max_chars=max_chars,
                          interval=args.interval)
    else:
        r = crawl_bfs(args.url, args.max_pages, args.max_depth,
                      query=args.query, scope=args.scope, max_chars=max_chars,
                      interval=args.interval)
    out = dumps(r)
    print(out if args.json else out[:2000])
