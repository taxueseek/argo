#!/usr/bin/env python3
"""搜狗微信搜索·浏览器兜底通道（ego-browser，纯标准库）。

定位：HTTP 快车道 wechat_sogou（engines_builders_tech._build_wechat_sogou_engine）
被搜狗反爬墙拦截（302 antispiderwall，实测 raw curl 第 2 次查询即触发）时的
域内兜底车道——借 ego-browser 驱动用户默认浏览器（Ego Lite）里的真实会话拉取
weixin.sogou.com 结果页：指纹与 cookie 连续，2026-09-30 实测连查无风控。

与 HTTP 车道输出同构（字段语义对齐）：
  - published_at：timeConvert('unix') → 本时区 ISO（同 HTTP 车道格式）
  - url_resolved：中间链解析成功 true / 失败回落中间链 false（同语义）
  - 解析封顶：单轮 ≤5 条（同 _SOGOU_RESOLVE_MAX_PER_CALL），带 cookie 的
    浏览器内 fetch 拉反爬 JS 页，`url += '片段'` 拼回真实链接
  - account 字段 CLI 解析器（_parse_yaml_output）不保留，折进 snippet 头部
    「公众号「X」 」前缀，信息不丢

会话复用：task space 与 page 持久化在状态文件 STATE_PATH，跨调用复用同一
浏览器页——保持指纹与 cookie 连续性正是本通道的降反爬手段，故刻意不走
task.finish()（ego-browser「默认关闭空间」之例外：结果必须留在浏览器里供
下一轮复用）。Ego Lite 重启后空间失效则自动新建。文件锁串行化并发调用；
调用间最小间隔默认 6 秒（状态文件记最近调用时刻），贴近 HTTP 车道的
域族节流量级。

依赖：ego-browser CLI。缺失 / Ego Lite 未运行 / 反爬墙拦截 → stderr 报因、
退出码 1（argo _run 对 rc!=0 只取 stderr 留痕，引擎返回空，路由轮到下一候选）。

用法：
  python3 scripts/sogou_weixin_browser.py "<query>" --n 5
  python3 scripts/sogou_weixin_browser.py "<query>" --resolve-limit 3 --min-interval 8
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import quote

STATE_PATH = os.path.expanduser("~/.config/argo/sogou_weixin_browser.json")
DEFAULT_MIN_INTERVAL = 6.0     # 调用间最小间隔（秒），贴近域族节流量级
MIN_INTERVAL_CAP = 20.0        # 补睡封顶：状态漂移时不至于睡死
JS_RESULT_WAIT_S = 15          # 浏览器内等结果/判反爬的墙钟
RESOLVE_FETCH_TIMEOUT_MS = 6000
SUBPROC_TIMEOUT = 70           # ego-browser 子进程墙钟封顶（argo spec timeout=75 先杀）
SOGOU_SEARCH_URL = "https://weixin.sogou.com/weixin?type={type}&query={query}&ie=utf8"
JSON_MARKER = "@@JSON@@"

# 反爬信号：与 HTTP 车道同一目标页，命中即宣告车道被墙
_ANTISPIDER_RE = re.compile(r"antispider|验证码|异常访问|请输入")

# JS 侧：结果抽取 + 中间链片段抓取。CFG 为 JSON 字面量（ensure_ascii 注入，
# 不存在引号/换行逃逸问题）。查询 URL 由 Python 拼好后注入，JS 不再做编码。
# 抽原始片段不在浏览器里拼接——拼接与校验放 Python 侧（_assemble_link），可离线单测。
_JS_TEMPLATE = """
const CFG = %(cfg)s;
let task = null;
if (CFG.spaceId) {
  try { task = await taskSpace(CFG.spaceId); } catch (e) { task = null; }
}
if (!task) task = await taskSpace("argo 搜狗微信兜底");
let page = null;
try { page = task.page("p1"); } catch (e) { page = null; }
if (!page) page = await task.newPage();
await page.goto(%(search_url)s);
let blocked = false, got = false;
for (let i = 0; i < %(wait_s)d; i++) {
  await page.waitForTimeout(1000);
  const st = await page.evaluate(() => ({
    n: document.querySelectorAll('li[id^="sogou_vr_11002601_box_"]').length,
    text: document.body.innerText.slice(0, 2500),
  }));
  if (/(antispider|验证码|异常访问|请输入)/.test(st.text)) { blocked = true; break; }
  if (st.n > 0) { got = true; break; }
}
let items = [];
if (!blocked) {
  items = await page.evaluate(() => {
    const out = [];
    for (const li of document.querySelectorAll('li[id^="sogou_vr_11002601_box_"]')) {
      const a = li.querySelector("h3 a");
      if (!a) continue;
      const info = li.querySelector(".txt-info");
      const acc = li.querySelector(".all-time-y2");
      const tm = li.innerHTML.match(/timeConvert\\('?([0-9]{10})'?\\)/);
      out.push({
        title: a.innerText.trim(),
        url: a.href,
        snippet: info ? info.innerText.trim() : "",
        account: acc ? acc.innerText.trim() : "",
        epoch: tm ? tm[1] : "",
        frag: [],
        fallback_url: "",
      });
    }
    return out;
  });
  let resolvedCount = 0;
  for (const it of items) {
    if (resolvedCount >= CFG.resolveLimit) break;
    if (!/weixin\\.sogou\\.com.*(\\/link\\?|\\/weixin\\?)/.test(it.url)) continue;
    try {
      const resp = await page.fetch(it.url, { timeout: %(resolve_to_ms)d });
      const body = String(resp.body || "");
      const frags = [];
      for (const m of body.matchAll(/url\\s*\\+=\\s*(['"])(.*?)\\1/g)) frags.push(m[2]);
      const fb = body.match(/https?:\\/\\/mp\\.weixin\\.qq\\.com[^'"<>\\s]+/);
      it.frag = frags;
      it.fallback_url = fb ? fb[0] : "";
      resolvedCount++;
    } catch (e) { /* 解析失败保留中间链，Python 侧标 url_resolved=false */ }
  }
}
console.log("%(marker)s" + JSON.stringify({
  ok: !blocked,
  space_id: task.spaceId,
  blocked: blocked,
  got: got,
  items: items.slice(0, CFG.n),
}));
"""


def _build_js(query: str, n: int, stype: int, resolve_limit: int,
              space_id: object) -> str:
    """把配置注入 JS 模板。ensure_ascii 保证任意查询词的引号/换行/行分隔符全转义。"""
    final_url = SOGOU_SEARCH_URL.format(type=stype, query=quote(query))
    js = _JS_TEMPLATE % {
        "cfg": json.dumps({"n": n, "resolveLimit": resolve_limit,
                           "spaceId": space_id}, ensure_ascii=True),
        "search_url": json.dumps(final_url),
        "wait_s": JS_RESULT_WAIT_S,
        "resolve_to_ms": RESOLVE_FETCH_TIMEOUT_MS,
        "marker": JSON_MARKER,
    }
    return js


def _assemble_link(frag: list | None, fallback_url: str | None,
                   original: str) -> tuple[str, bool]:
    """搜狗反爬 JS 页 → 真实链接。与 HTTP 车道 _resolve_sogou_link 同判据：
    url += 片段拼接优先，拼不出 http(s) 时回落响应体内的 mp.weixin 直链，
    再不行保留中间链并标 false（降级不丢结果）。入参容忍 None（JS 侧缺键）。"""
    real = "".join(frag or [])
    if real.startswith(("http://", "https://")):
        return real, True
    if (fallback_url or "").startswith(("http://", "https://")):
        return fallback_url, True
    return original, False


def _norm_published_at(epoch: str) -> str:
    """10 位 unix 秒 → 本时区 ISO（与 HTTP 车道 datetime.fromtimestamp().astimezone() 一致）。"""
    if not epoch or not str(epoch).isdigit():
        return ""
    try:
        return datetime.fromtimestamp(int(epoch)).astimezone().isoformat(timespec="seconds")
    except (ValueError, OSError):
        return ""


def _extract_json(stdout: str) -> dict | None:
    """从 ego-browser 输出里取标记行 JSON（CLI 可能夹杂 notice 行）。"""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(JSON_MARKER):
            try:
                return json.loads(line[len(JSON_MARKER):])
            except json.JSONDecodeError:
                continue
    return None


def _safe_state_path(p: str) -> str:
    """状态文件路径硬化：白名单限定在 ~/.config/argo/ 或系统临时目录（测试）
    之内，显式拒绝 .. 段。路径是模块常量面，不随命令行开放。"""
    norm = os.path.normpath(os.path.abspath(os.path.expanduser(p)))
    if ".." in norm.split(os.sep):
        raise ValueError(f"state 路径不允许包含 ..：{p}")
    allowed_roots = (
        os.path.realpath(os.path.expanduser("~/.config/argo")),
        os.path.realpath(tempfile.gettempdir()),
    )
    rp = os.path.realpath(norm)
    if not any(rp == root or rp.startswith(root + os.sep) for root in allowed_roots):
        raise ValueError(f"state 路径必须在 {' 或 '.join(allowed_roots)} 之内：{p}")
    return rp


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="搜狗微信搜索·浏览器兜底通道")
    ap.add_argument("query")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--type", type=int, choices=(1, 2), default=2,
                    help="2=文章（默认）1=公众号")
    ap.add_argument("--resolve-limit", type=int, default=5)
    ap.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL)
    args = ap.parse_args(argv)
    try:
        state_path = _safe_state_path(STATE_PATH)
    except ValueError as e:
        print(f"sogou_weixin_browser: {e}", file=sys.stderr)
        return 1

    if not shutil.which("ego-browser"):
        print("sogou_weixin_browser: ego-browser CLI 不存在（Ego Lite 未安装？）",
              file=sys.stderr)
        return 1

    # 文件锁串行化：并发调用会争抢同一浏览器页（goto 竞态）与节流窗口
    lock_path = state_path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        state = _load_state(state_path)
        last_ts = float(state.get("last_ts") or 0)
        wait = args.min_interval - (time.time() - last_ts)
        if 0 < wait <= MIN_INTERVAL_CAP:
            time.sleep(wait)

        js = _build_js(args.query, args.n, args.type, args.resolve_limit,
                       state.get("space_id"))
        try:
            proc = subprocess.run(
                ["ego-browser", "nodejs", "-e", js],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=SUBPROC_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"sogou_weixin_browser: ego-browser 超时（>{SUBPROC_TIMEOUT}s）",
                  file=sys.stderr)
            return 1
        except FileNotFoundError:
            print("sogou_weixin_browser: ego-browser 调用失败", file=sys.stderr)
            return 1

        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[:200]
            print(f"sogou_weixin_browser: ego-browser rc={proc.returncode}: {tail}",
                  file=sys.stderr)
            return 1
        data = _extract_json(proc.stdout) or _extract_json(proc.stderr)
        if not data:
            print("sogou_weixin_browser: 未能解析 ego-browser 输出", file=sys.stderr)
            return 1

        # 空间句柄持久化（Ego Lite 重启后 taskSpace(id) 会抛错 → 下轮自动新建）
        if data.get("space_id"):
            state["space_id"] = data["space_id"]
        state["last_ts"] = time.time()
        _save_state(state_path, state)

    if data.get("blocked"):
        print("sogou_weixin_browser: 搜狗反爬墙拦截——请在浏览器打开 "
              "weixin.sogou.com 手动通过验证后重试", file=sys.stderr)
        return 1
    if not data.get("ok"):
        print("sogou_weixin_browser: 浏览器通道未取到结果页", file=sys.stderr)
        return 1

    rows = []
    for it in data.get("items", []):
        title = str(it.get("title") or "").strip()
        url = str(it.get("url") or "").strip()
        if not title and not url:
            continue
        real, resolved = _assemble_link(it.get("frag"), it.get("fallback_url"), url)
        snippet = str(it.get("snippet") or "").strip()[:200]
        account = str(it.get("account") or "").strip()
        if account:
            snippet = f"公众号「{account}」 {snippet}".strip()
        row = {"title": title[:80], "url": real, "snippet": snippet}
        published = _norm_published_at(it.get("epoch", ""))
        if published:
            row["published_at"] = published
        row["url_resolved"] = resolved
        rows.append(row)

    import yaml
    print(yaml.safe_dump(rows[: max(1, args.n)], allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
