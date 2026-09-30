#!/usr/bin/env python3
"""X (Twitter) Trending 热榜（ego-browser，纯标准库，零文件读写）。

定位：X Trending 热榜是别处拿不到的独家热点源（科技/娱乐/全球趋势分类、
带实时排名）。借 ego-browser 驱动用户默认浏览器（Ego Lite）抓取
x.com/explore/tabs/trending 的渲染结果——复用浏览器 profile 的 cookie 与
指纹，避免匿名请求被 X 风控拦下。

与 sogou_weixin_browser 的差别：本脚本**不做任何状态文件读写**（不持久化
task space、不加文件锁），每次调用自建 task space 抓完即走。取舍：放弃
跨调用的会话复用（X 的登录态与 cookie 在浏览器 profile 层，与 task space
无关，冷空间实测同样取到数据），换来脚本零路径写入面。

输出：热榜条目（排名、类别、话题、跳转链接），YAML 列表。

依赖：ego-browser CLI（Ego Lite 浏览器）。缺失 / 未运行 / 页面结构变化 →
stderr 报因、退出码 1（argo _run 对 rc!=0 只取 stderr 留痕，引擎返回空）。

用法：
  python3 scripts/x_trending.py --n 10
  python3 scripts/x_trending.py "热榜" --n 5      # 位置参数兼容引擎契约，忽略
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

JS_RESULT_WAIT_S = 12          # 浏览器内等渲染的墙钟
SUBPROC_TIMEOUT = 45           # ego-browser 子进程墙钟封顶
X_TRENDING_URL = "https://x.com/explore/tabs/trending"
JSON_MARKER = "@@JSON@@"

# JS 侧：抓 X Trending 页面。CFG 为 JSON 字面量（ensure_ascii 注入）。
_JS_TEMPLATE = """
const CFG = %(cfg)s;
const task = await taskSpace("argo X热榜");
let page = null;
try { page = task.page("p1"); } catch (e) { page = null; }
if (!page) page = await task.newPage();
await page.goto(%(url)s);
// 等热榜渲染（trend 元素出现或超时）
let got = false;
for (let i = 0; i < %(wait_s)d; i++) {
  await page.waitForTimeout(1000);
  const n = await page.evaluate(() => document.querySelectorAll('[data-testid="trend"]').length);
  if (n > 0) { got = true; break; }
}
let items = [];
if (got) {
  items = await page.evaluate(() => {
    const out = [];
    for (const el of document.querySelectorAll('[data-testid="trend"]')) {
      const text = el.innerText.trim();
      if (!text) continue;
      // 过滤 Promoted 广告
      if (/promoted by/i.test(text)) continue;
      // X 的行结构：["1", "·", "科技 趋势", "<话题>"]，分隔点行要丢掉
      const lines = text.split("\\n").map(s => s.trim())
        .filter(s => s && s !== "·" && s !== "．");
      if (lines.length < 2) continue;
      let rank = "";
      if (/^\\d+$/.test(lines[0])) rank = lines.shift();
      // 话题恒为最后一行；其前一行是分类（趋势类目），再前不再取
      const topic = lines[lines.length - 1].slice(0, 100);
      const category = lines.length >= 2 ? lines[lines.length - 2].slice(0, 40) : "";
      if (!topic) continue;
      const a = el.querySelector("a[href]");
      out.push({ rank, category, topic, href: a ? a.href : "" });
    }
    return out;
  });
}
console.log("%(marker)s" + JSON.stringify({
  ok: got,
  items: items.slice(0, CFG.n),
}));
"""


def _build_js(n: int) -> str:
    """把配置注入 JS 模板。"""
    return _JS_TEMPLATE % {
        "cfg": json.dumps({"n": n}, ensure_ascii=True),
        "url": json.dumps(X_TRENDING_URL),
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


# 泛词：这些 query 只表示「我要看热榜」，不做关键词过滤
_GENERIC_QUERIES = frozenset({
    "", "热榜", "热搜", "热搜榜", "热门", "热门话题", "趋势", "热议", "今日热搜",
    "trending", "trend", "trends", "hot", "top trends", "hot search",
})


def _focus(rows: list[dict], query: str) -> list[dict]:
    """位置参数 query 的语义：非泛词时按关键词聚焦热榜。

    例：`argo search "AI" --engine x_trending` → 只留命中 AI 的趋势条目。
    热榜是固定榜单而非检索结果，**无命中时回落全榜**（过滤只是聚焦，
    不该让「今天 AI 没上榜」表现为引擎空手）。
    """
    q = (query or "").strip()
    if q.casefold() in _GENERIC_QUERIES:
        return rows
    ql = q.casefold()
    hit = [r for r in rows
           if ql in f"{r.get('title', '')} {r.get('snippet', '')}".casefold()]
    return hit or rows


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="X (Twitter) Trending 热榜")
    ap.add_argument("query", nargs="?", default="",
                    help="兼容引擎契约的位置参数（本引擎按热榜取数，忽略该值）")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=SUBPROC_TIMEOUT)
    args = ap.parse_args(argv)

    if not shutil.which("ego-browser"):
        print("x_trending: ego-browser CLI 不存在（Ego Lite 未安装？）",
              file=sys.stderr)
        return 1

    try:
        proc = subprocess.run(
            ["ego-browser", "nodejs", "-e", _build_js(args.n)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"x_trending: ego-browser 超时（>{args.timeout:.0f}s）", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("x_trending: ego-browser 调用失败", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[:200]
        print(f"x_trending: ego-browser rc={proc.returncode}: {tail}",
              file=sys.stderr)
        return 1
    data = _extract_json(proc.stdout) or _extract_json(proc.stderr)
    if not data:
        print("x_trending: 未能解析 ego-browser 输出", file=sys.stderr)
        return 1
    if not data.get("ok"):
        print("x_trending: X Trending 页面未渲染或结构变化", file=sys.stderr)
        return 1

    rows = []
    for it in data.get("items", []):
        topic = str(it.get("topic") or "").strip()
        if not topic:
            continue
        rank = str(it.get("rank") or "").strip()
        category = str(it.get("category") or "").strip()
        href = str(it.get("href") or "").strip()
        if not href.startswith(("http://", "https://")):
            href = f"https://x.com/search?q={topic.replace(' ', '%20')}"
        rows.append({
            "title": (f"{rank}. {topic}" if rank else topic)[:80],
            "url": href,
            "snippet": (f"X Trending | {category}" if category else "X Trending")[:200],
        })

    import yaml
    rows = _focus(rows, args.query)
    print(yaml.safe_dump(rows[: max(1, args.n)], allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
