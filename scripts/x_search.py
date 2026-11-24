#!/usr/bin/env python3
"""X (Twitter) 关键词搜索（ego-browser，纯标准库，零文件读写）。

定位：X 原生搜索是唯一能吃 X 布尔语法的通道——`from:`（盯人）、
`-is:retweet`（去转发）、`min_faves:`（热度门）、`lang:`（语言）、
`since:`/`until:`（时间窗）。这些语法在任何通用网页搜索引擎上都会被忽略，
所以「盯一批官方号 / 按关键词找发布」这件事只能走 x.com/search 本体。

通道原理：借 ego-browser 驱动用户默认浏览器（Ego Lite）打开 x.com/search，
复用浏览器 profile 的登录态与指纹。**为什么必须登录态**：X 对未登录请求
一律拒绝或给降级页；而 x_trending 已证明该 profile 通道长期可用。

与 fxtwitter（engines/specs/fxtwitter.yaml）的关系：那是 HTTP 免登录通道，
曾经是主路径，但上游公开实例的 `/2/search` 已停用（对任意查询恒返回
404 + 空 results，status/profile 端点仍正常）——本脚本是它的可用替代，
只做关键词搜索，不做单条打包（单条走 `argo tweet`）。

输出：YAML 列表（title/url/snippet/source/published_at/social_meta），
与引擎统一 schema 对齐，便于被 `argo template` 直接转发。

依赖：ego-browser CLI（Ego Lite）+ 已登录的 X 会话。缺失 / 未渲染 / 结构变化
→ stderr 报因、退出码 1（调用方对 rc!=0 只取 stderr 留痕、结果为空）。

用法：
  python3 scripts/x_search.py "OpenAI" -n 20
  python3 scripts/x_search.py '(GPT OR Claude) release -is:retweet' --sort top --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

JS_RESULT_WAIT_S = 15          # 浏览器内等首屏渲染的墙钟（秒）
SUBPROC_TIMEOUT = 90           # ego-browser 子进程墙钟封顶（含滚动加载）
X_SEARCH_BASE = "https://x.com/search"
JSON_MARKER = "@@JSON@@"
MAX_SCROLL = 3                 # 最多下滚几次以凑够 n（每次约一屏半）

# JS 侧：抓 x.com/search 渲染结果。CFG 为 JSON 字面量（ensure_ascii 注入）。
# 抽取口径全部走 data-testid，不碰 class 名（X 的 class 是哈希串，改版即失效）。
_JS_TEMPLATE = """
const CFG = %(cfg)s;
const task = await taskSpace("argo X搜索");
let page = null;
try { page = task.page("p1"); } catch (e) { page = null; }
if (!page) page = await task.newPage();
await page.goto(%(base)s + "?q=" + encodeURIComponent(CFG.query) + "&f=" + CFG.sort);
// 等首屏：出 article 视为渲染完成；出「无结果」文案也视为渲染完成（真·空结果）
let rendered = false, empty = false;
for (let i = 0; i < %(wait_s)d; i++) {
  await page.waitForTimeout(1000);
  const st = await page.evaluate(() => ({
    arts: document.querySelectorAll('[data-testid="cellInnerDiv"] article').length,
    empty: /没有结果|No results for|无结果/i.test(document.body.innerText || ""),
  }));
  if (st.arts > 0 || st.empty) { rendered = true; empty = st.empty && !st.arts; break; }
}
let items = [];
if (rendered) {
  for (let s = 0; s < CFG.maxScroll; s++) {
    const cnt = await page.evaluate(() =>
      document.querySelectorAll('[data-testid="cellInnerDiv"] article').length);
    if (cnt >= CFG.n) break;
    await page.evaluate(() => window.scrollBy(0, window.innerHeight * 3));
    await page.waitForTimeout(1200);
  }
  items = await page.evaluate(() => {
    // 互动数从按钮 aria-label 取首个整数（本地化文案不同，但数字恒在前）
    const num = (s) => {
      const m = (s || "").match(/\\d[\\d,\\.]*/);
      return m ? parseInt(m[0].replace(/[,\\.]/g, ""), 10) : 0;
    };
    const out = [];
    const seen = new Set();
    for (const art of document.querySelectorAll('[data-testid="cellInnerDiv"] article')) {
      const t = art.querySelector('[data-testid="tweetText"]');
      const a = art.querySelector('a[href*="/status/"]');
      const tm = art.querySelector('time[datetime]');
      const href = a ? a.href : "";
      if (!href || seen.has(href)) continue;
      seen.add(href);
      const text = t ? t.innerText.trim() : "";
      const like = art.querySelector('[data-testid="like"]');
      const rt = art.querySelector('[data-testid="retweet"]');
      const rp = art.querySelector('[data-testid="reply"]');
      out.push({
        text: text,
        href: href,
        published_at: tm ? tm.getAttribute("datetime") : "",
        likes: num(like && like.getAttribute("aria-label")),
        reposts: num(rt && rt.getAttribute("aria-label")),
        replies: num(rp && rp.getAttribute("aria-label")),
      });
    }
    return out;
  });
}
console.log("%(marker)s" + JSON.stringify({ok: rendered, empty, items: items.slice(0, CFG.n)}));
"""


def _build_js(query: str, n: int, sort: str) -> str:
    """把配置注入 JS 模板。"""
    return _JS_TEMPLATE % {
        "cfg": json.dumps({"query": query, "n": n, "sort": sort,
                           "maxScroll": MAX_SCROLL}, ensure_ascii=True),
        "base": json.dumps(X_SEARCH_BASE),
        "wait_s": JS_RESULT_WAIT_S,
        "marker": JSON_MARKER,
    }


def _extract_json(stdout: str) -> dict | None:
    """从 ego-browser 输出里取标记行 JSON（CLI 可能夹杂 notice 行）。"""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(JSON_MARKER):
            try:
                return json.loads(line[len(JSON_MARKER):])
            except json.JSONDecodeError:
                continue
    return None


def _handle_from_url(url: str) -> str:
    """从 status URL 里取作者 handle（https://x.com/<handle>/status/<id>）。"""
    try:
        parts = url.split("/")
        i = parts.index("x.com")
        return parts[i + 1] if i + 1 < len(parts) else ""
    except (ValueError, IndexError):
        return ""


def _rows(items: list[dict], n: int) -> list[dict]:
    """ego-browser 抽取结果 → 统一 result schema（与引擎输出对齐）。"""
    rows: list[dict] = []
    for it in items[:n]:
        href = str(it.get("href") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        text = str(it.get("text") or "").strip()
        sid = href.rsplit("/", 1)[-1]
        rows.append({
            "title": (text[:100] + ("..." if len(text) > 100 else "")) or f"Tweet {sid}",
            "url": href,
            "snippet": text[:300],
            "source": "x_search",
            "published_at": it.get("published_at") or None,
            "social_meta": {
                "platform": "twitter",
                "content_type": "tweet",
                "id": sid,
                "author": _handle_from_url(href),
                "likes": it.get("likes") or 0,
                "reposts": it.get("reposts") or 0,
                "replies": it.get("replies") or 0,
                "provider": "x_search",
            },
        })
    return rows


def search_browser(query: str, n: int, timeout: float, sort: str = "latest") -> list[dict]:
    """Fetch rendered posts; an empty list requires an explicit no-results page."""
    if not shutil.which("ego-browser"):
        raise FileNotFoundError("ego-browser CLI 不存在")
    proc = subprocess.run(
        ["ego-browser", "nodejs", "-e", _build_js(query, n, "top" if sort == "top" else "live")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"ego-browser rc={proc.returncode}")
    data = _extract_json(proc.stdout) or _extract_json(proc.stderr)
    if not data or not data.get("ok") or not isinstance(data.get("items"), list):
        raise RuntimeError("x.com/search 无可解析结果（未登录 / 未渲染 / 页面结构异常）")
    rows = _rows(data["items"], n)
    if not rows and not data.get("empty"):
        raise RuntimeError("x.com/search 未提取到可识别帖子")
    return rows


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="argo x-search",
        description="X (Twitter) 关键词搜索（原生布尔语法：from:/min_faves:/-is:retweet/lang:）")
    ap.add_argument("query", help="搜索词，支持 X 原生语法")
    ap.add_argument("-n", "--n", type=int, default=20, help="最多返回条数（默认 20）")
    ap.add_argument("--sort", choices=["latest", "top"], default="latest",
                    help="latest=最新（默认，f=live）/ top=热门（f=top）")
    ap.add_argument("--timeout", type=float, default=SUBPROC_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    query = (args.query or "").strip()
    if not query:
        print("x_search: 搜索词为空", file=sys.stderr)
        return 1

    try:
        rows = search_browser(query, max(1, args.n), args.timeout, args.sort)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"x_search: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print(f"x_search: 「{query}」无结果", file=sys.stderr)
        return 0
    for i, r in enumerate(rows, 1):
        meta = r.get("social_meta") or {}
        print(f"### {i}. {r['title']}")
        print(f"- **URL**: {r['url']}")
        print(f"- @{meta.get('author', '')} · {r.get('published_at') or ''} · "
              f"♥{meta.get('likes', 0)} ⇄{meta.get('reposts', 0)} 💬{meta.get('replies', 0)}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
