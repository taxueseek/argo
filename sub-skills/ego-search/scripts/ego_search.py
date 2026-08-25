#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ego-search — argo 子技能：登录态专业搜索（双运行时完全态）。

模式：
  enable / disable / status
  search / fetch / act / api  [--runtime auto|ego|webbridge]

双运行时（互补，保留两者）：
  - ego lite（ego-browser）：Agent 浏览器 + 任务空间隔离
  - WebBridge：用户 Chrome/Edge + 扩展桥
  任一可用即可做登录态搜索；--runtime auto 优先 ego，否则 webbridge。

与常规 argo 检索：缓存/分区隔离（cache_eligible=false）；
汇总分析阶段可把 public + login 两路结果一起喂 evidence。

专业搜索模式默认关闭；开启后 search/fetch/act/api 才可用。
输出强制 login provenance。见 SKILL.md「完全态」。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

# 同目录模块（runtime / webbridge_adapter）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import merge as merge_mod  # noqa: E402
import quality as quality  # noqa: E402
import runtime as rt  # noqa: E402
import safety as safety  # noqa: E402
import webbridge_adapter as wb  # noqa: E402

# argo 核心 scripts：query_enhance 归一化（词形规范化，保留平台语法）；核心不可用时降级
_CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_CORE_SCRIPTS) not in sys.path and _CORE_SCRIPTS.exists():
    sys.path.insert(0, str(_CORE_SCRIPTS))
try:
    from query_enhance import normalize_query  # noqa: E402
except Exception:  # 核心包缺失时保持原 query
    normalize_query = None


def _normalized_query(q: str) -> str:
    """词形规范化（全角→半角、拆斜杠、压空格）；保留 from:/site: 等平台语法；失败回退原 query。"""
    if normalize_query is None or not isinstance(q, str):
        return q
    try:
        return normalize_query(q)
    except Exception:
        return q

# ── 常量 ──────────────────────────────────────────────────────────────
EGO_BIN = rt.EGO_BIN
RESULT_MARKER = "EGO_RESULT|"
SOURCE = "ego-browser"

SEARCH_URLS = {
    "bing": "https://www.bing.com/search?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}",
    "google": "https://www.google.com/search?q={q}",
}

DEFAULT_TIMEOUT = 120  # 秒
API_DATA_LIMIT = 100_000  # 字符；超限截断并标记

# 专业搜索模式状态文件（默认 ~/.local/state/ego-search/pro-mode.json；
# 可用环境变量 EGO_SEARCH_STATE 覆盖，测试用）
DEFAULT_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".local", "state", "ego-search", "pro-mode.json"
)
STATE_PATH = os.environ.get("EGO_SEARCH_STATE") or DEFAULT_STATE_PATH

# 登录态 provenance 字段（公共 SearchCache 必须拒绝此类载荷）
LOGIN_PROVENANCE = {
    "login_state_used": True,
    "auth_partition": "login",
    "cache_eligible": False,
}


# ── 通用工具 ──────────────────────────────────────────────────────────
def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def stamp_login_provenance(payload: dict[str, Any], *, runtime: str | None = None) -> dict[str, Any]:
    """打上登录态 provenance；不覆盖调用方已显式设置的同名字段。"""
    if not isinstance(payload, dict):
        return payload
    for k, v in LOGIN_PROVENANCE.items():
        payload.setdefault(k, v)
    if runtime:
        payload.setdefault("runtime", runtime)
        if runtime == "webbridge":
            payload.setdefault("source", "webbridge")
        else:
            payload.setdefault("source", SOURCE)
    else:
        payload.setdefault("source", payload.get("source") or SOURCE)
    # 融合提示：可与常规 public 检索在分析层合并，不可进公共缓存
    payload.setdefault("search_partition", "login")
    payload.setdefault("merge_with_public_ok", True)
    return payload


def emit_json(payload: dict[str, Any], *, runtime: str | None = None) -> None:
    """stdout 输出已打 provenance + 质量信号的 JSON。"""
    p = stamp_login_provenance(payload, runtime=runtime)
    # fetch / act 正文挂 quality（登录墙/空页）
    if p.get("content") is not None or p.get("detail") is not None:
        p = quality.assess_body(p)
    print(json.dumps(p, ensure_ascii=False, indent=2))


def apply_site_space(args: argparse.Namespace) -> None:
    """--site example.com → 粘性 task-space，利于登录态保温。"""
    site = getattr(args, "site", None)
    if not site:
        return
    host = site.strip().lower()
    host = host.removeprefix("https://").removeprefix("http://").split("/")[0]
    if host:
        # 用户未改默认 task-space 时才覆盖
        if getattr(args, "task_space", None) in (None, "ego-search"):
            args.task_space = f"site:{host}"
        if not getattr(args, "keep_space", False):
            # 站点粘性默认保温；用户可仍显式不加 --keep-space 若想关
            # 仅当 --site 时默认 keep（登录稳定）
            args.keep_space = True


def _pick_runtime(args: argparse.Namespace) -> str:
    explicit = getattr(args, "runtime", None) or "auto"
    name = rt.resolve_runtime(explicit)
    if name == "none":
        info = rt.detect_runtimes(try_start_webbridge=True)
        hints = "\n".join(f"  - {h}" for h in info.get("install_hints") or [])
        sys.exit(
            "错误: 无可用登录态运行时（ego lite 与 WebBridge 均不可用）。\n"
            f"{hints}"
        )
    return name

def build_js(template: str, **kwargs: Any) -> str:
    """把 JS 模板里的 %%KEY%% 占位符替换为实际值（避免与 JS 大括号冲突）。"""
    for k, v in kwargs.items():
        template = template.replace("%%" + k + "%%", str(v))
    return template


def run_ego(js_script: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """执行 ego-browser nodejs -e 脚本，返回 {ok, payload, error}。"""
    cmd = [EGO_BIN, "nodejs", "-e", js_script]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "未找到 ego-browser 命令，请先安装浏览器运行时（见 references/install.md）",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ego-browser 执行超时（>{timeout}s）"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"ego-browser 退出码 {proc.returncode}: {proc.stderr[-800:]}",
        }

    # cliLog 输出在 stderr，取最后一个 EGO_RESULT| 标记行
    payload = None
    for line in reversed(proc.stderr.splitlines()):
        line = line.strip()
        if line.startswith(RESULT_MARKER):
            raw = line[len(RESULT_MARKER):]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            break
    if payload is None:
        return {
            "ok": False,
            "error": f"未解析到结果标记，stderr={proc.stderr[-800:]}",
        }
    return {"ok": True, "payload": payload}


# ── JS 模板（单源：SERP / BODY 各一份，search·fetch·act 组合）──────────
# 转义纪律：页面内选择器写死在模板；Node 层只注入纯安全占位符。

# 页面内 SERP 提取 IIFE 体（返回 JSON 字符串）。占位：%%ENGINE%% %%N%%
SERP_EXTRACT_IIFE = r"""(() => {
  const configs = {
    bing: { item: 'li.b_algo', link: 'h2 a', snippet: '.b_caption p, p' },
    baidu: { item: "div#content_left div[class*='c-container'], div#content_left div.result", link: 'h3 a', snippet: ".c-abstract, [class*='content-right']" },
    google: { item: 'div.g, div[data-sncf]', link: 'a h3', snippet: 'div.VwiC3b, div[data-sncf]' }
  };
  const cfg = configs['%%ENGINE%%'] || configs.bing;
  const items = [];
  const extractDate = (t) => {
    const m = t.match(/(20\d{2})[年\/\-\.](\d{1,2})[月\/\-\.](\d{1,2})/);
    if (m) return m[1] + '-' + String(m[2]).padStart(2,'0') + '-' + String(m[3]).padStart(2,'0');
    const m2 = t.match(/(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})/i);
    if (m2) { const mo={jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'}; return m2[3] + '-' + mo[m2[2].toLowerCase().slice(0,3)] + '-' + String(m2[1]).padStart(2,'0'); }
    const m3 = t.match(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2}),? (\d{4})/i);
    if (m3) { const mo={jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'}; return m3[3] + '-' + mo[m3[1].toLowerCase().slice(0,3)] + '-' + String(m3[2]).padStart(2,'0'); }
    return '';
  };
  document.querySelectorAll(cfg.item).forEach(el => {
    const a = el.querySelector(cfg.link);
    if (!a) return;
    const p = el.querySelector(cfg.snippet);
    const text = (el.innerText || '') + ' ' + (a.href || '');
    items.push({
      title: (a.innerText || '').trim(),
      url: a.href || '',
      snippet: (p ? p.innerText : '').trim().slice(0, 300),
      published_at: extractDate(text),
    });
  });
  if (!items.length) {
    document.querySelectorAll('h2 a, h3 a').forEach(a => {
      if (items.length >= %%N%%) return;
      items.push({ title: (a.innerText || '').trim(), url: a.href || '', snippet: '', published_at: extractDate((a.innerText || '') + ' ' + (a.href || '')) });
    });
  }
  return JSON.stringify(items.slice(0, %%N%%));
})()"""

# 页面内正文提取 IIFE 体（返回 JSON 字符串）。占位：%%CONTENT_MAX%%
BODY_EXTRACT_IIFE = r"""(() => {
  const q = (sel) => document.querySelector(sel);
  const title = (q('h1') || {}).innerText || document.title || '';
  const candidates = ['article', '[role="main"]', '.article-content', '.post-content',
                      '.entry-content', '#content', '.content', 'main'];
  let best = null, bestLen = 0;
  for (const sel of candidates) {
    const el = q(sel);
    if (!el) continue;
    const len = (el.innerText || '').length;
    if (len > bestLen) { best = el; bestLen = len; }
  }
  const root = best || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll('nav, footer, header, aside, script, style, noscript, iframe, .ad, .ads, .banner, .comment, .recommend, .related, .share, .sidebar').forEach(n => n.remove());
  let content = clone.innerText || '';
  if (!content && document.body) content = document.body.innerText;
  return JSON.stringify({ title: title.trim(), content: content.slice(0, %%CONTENT_MAX%%), url: location.href });
})()"""

FOCUS_FILTER = r"""
const kw = '%%FOCUS%%'.toLowerCase();
if (kw) {
  const paras = d.content.split(/\n\n+/);
  const hit = paras.filter(p => p.toLowerCase().includes(kw));
  body = hit.length ? hit.join('\n\n') : d.content.slice(0, 8000);
  if (body.length < 200) body = d.content.slice(0, 8000);
}
"""

# 收尾：默认关掉任务空间，避免标签堆积导致登录态漂移；%%KEEP_SPACE%% 为 true 时保留
JS_FINISH = r"""
try {
  if (%%KEEP_SPACE%%) {
    /* keep space warm for multi-round login stability */
  } else {
    await completeTaskSpace('%%TASK_SPACE%%', { keep: false })
  }
} catch (e) { /* space already gone */ }
"""

JS_SEARCH = r"""
const task = await useOrCreateTaskSpace('%%TASK_SPACE%%')
await openOrReuseTab('%%URL%%', { wait: true, timeout: 25 })
const info = await pageInfo()
const serp = await js(String.raw`%%SERP_IIFE%%`)
const parsed = JSON.parse(serp)
cliLog('EGO_RESULT|' + JSON.stringify({
  url: info.url, title: info.title, results: parsed,
  task_space: '%%TASK_SPACE%%', task_id: task && task.id,
}))
""" + JS_FINISH

JS_EXTRACT = r"""
const task = await useOrCreateTaskSpace('%%TASK_SPACE%%')
await openOrReuseTab('%%URL%%', { wait: true, timeout: 25 })
const info = await pageInfo()
const raw = await js(`%%BODY_IIFE%%`)
const d = JSON.parse(raw)
let body = d.content.replace(/\n{3,}/g, '\n\n').trim()
%%FOCUS_FILTER%%
cliLog('EGO_RESULT|' + JSON.stringify({
  url: info.url, title: d.title,
  content: body, word_count: body.split(/\s+/).length,
  fetch_method: 'browser',
  task_space: '%%TASK_SPACE%%', task_id: task && task.id,
}))
""" + JS_FINISH

# act：SERP 取 1 条 → 打开首条 → 正文（CONTENT_MAX 略小于 fetch）
JS_ACT = r"""
const task = await useOrCreateTaskSpace('%%TASK_SPACE%%')
await openOrReuseTab('%%URL%%', { wait: true, timeout: 25 })
const info = await pageInfo()
const serp = await js(String.raw`%%SERP_IIFE%%`)
const parsed = JSON.parse(serp)
let detail = null
if (parsed.length && parsed[0].url) {
  await openOrReuseTab(parsed[0].url, { wait: true, timeout: 25 })
  const rawd = await js(`%%BODY_IIFE%%`)
  detail = JSON.parse(rawd)
  detail.content = (detail.content || '').replace(/\n{3,}/g, '\n\n').trim()
}
cliLog('EGO_RESULT|' + JSON.stringify({
  query: '%%QUERY%%',
  url: info.url,
  results: parsed,
  detail: detail,
  task_space: '%%TASK_SPACE%%', task_id: task && task.id,
}))
""" + JS_FINISH

# api：主动同源请求（非被动 network 旁路）
JS_API = r"""
const task = await useOrCreateTaskSpace('%%TASK_SPACE%%')
await openOrReuseTab('%%ORIGIN%%', { wait: true, timeout: 25 })
const info = await pageInfo()
let data = null, dataType = 'unknown'
try {
  const r = await browserFetch('%%API_URL%%', {})
  if (typeof r === 'string') {
    data = r; dataType = 'text'
    try { data = JSON.parse(r); dataType = 'json' } catch (e) {}
  } else {
    data = r; dataType = 'json'
  }
} catch (e) {
  dataType = 'error'
  data = String(e).slice(0, 300)
}
cliLog('EGO_RESULT|' + JSON.stringify({
  api_url: '%%API_URL%%',
  page_url: info.url,
  page_title: info.title,
  data: data,
  data_type: dataType,
  fetch_method: 'browser_api',
  task_space: '%%TASK_SPACE%%', task_id: task && task.id,
}))
""" + JS_FINISH

def _serp_iife(engine: str, n: int) -> str:
    return build_js(SERP_EXTRACT_IIFE, ENGINE=engine, N=n)


def _body_iife(content_max: int) -> str:
    return build_js(BODY_EXTRACT_IIFE, CONTENT_MAX=content_max)


# ── 模式实现 ──────────────────────────────────────────────────────────
def _keep_js(args: argparse.Namespace) -> str:
    """JS 布尔：是否保留任务空间（登录态保温）。"""
    return "true" if getattr(args, "keep_space", False) else "false"


def _require_url(url: str, context: str) -> str:
    v = safety.validate_browser_url(url, context=context)
    if not v.get("ok"):
        sys.exit(f"错误: {v.get('error')}")
    return v["url"]


def run_with_fallback(
    args: argparse.Namespace,
    *,
    ego_fn,
    wb_fn,
    label: str,
) -> tuple[dict[str, Any], str]:
    """统一双运行时：ego 优先 + auto 降级 WebBridge（删掉各 cmd 重复分支）。"""
    runtime = _pick_runtime(args)
    if runtime == "webbridge":
        return wb_fn(), "webbridge"

    r = ego_fn()
    auto = (getattr(args, "runtime", "auto") or "auto") == "auto"
    if not r.get("ok") and auto and rt.webbridge_available(try_start=True).get("available"):
        log(f"ego 失败，降级 WebBridge ({label}): {r.get('error')}")
        return wb_fn(), "webbridge"
    return r, "ego"


def _search_ego(args: argparse.Namespace) -> dict[str, Any]:
    q = urllib.parse.quote(_normalized_query(args.query))
    url = SEARCH_URLS[args.engine].format(q=q)
    # 时间窗：URL 参数下推（google cdr / bing age-lt / baidu gpc）+ 解析后过滤兜底
    tparams = wb.time_url_params(args.engine, getattr(args, "since", None), getattr(args, "until", None))
    if tparams:
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode(tparams)
    js = build_js(
        JS_SEARCH,
        TASK_SPACE=args.task_space,
        URL=url,
        SERP_IIFE=_serp_iife(args.engine, args.n),
        KEEP_SPACE=_keep_js(args),
    )
    r = run_ego(js, args.timeout)
    if not r["ok"]:
        return r
    p = r["payload"]
    results = p.get("results", [])
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    if since or until:
        results = wb.filter_window(results, since, until)
    return {
        "ok": True,
        "payload": {
            "query": args.query,
            "engine": f"ego_browser_{args.engine}",
            "source": SOURCE,
            "runtime": "ego",
            "url": p.get("url"),
            "page_title": p.get("title"),
            "results": results,
            "count": len(results),
            "fetch_method": "browser",
            "task_space": p.get("task_space") or args.task_space,
            "task_id": p.get("task_id"),
            "space_kept": bool(getattr(args, "keep_space", False)),
        },
    }


def cmd_search(args: argparse.Namespace) -> None:
    apply_site_space(args)

    def wb_fn():
        return wb.search(
            _normalized_query(args.query), engine=args.engine, n=args.n,
            session=args.task_space, timeout=args.timeout,
            since=getattr(args, "since", None), until=getattr(args, "until", None),
        )

    r, runtime = run_with_fallback(args, ego_fn=lambda: _search_ego(args), wb_fn=wb_fn, label="search")
    if not r.get("ok"):
        sys.exit(f"错误: {r.get('error')}")
    emit_json(r["payload"], runtime=runtime)


def cmd_fetch(args: argparse.Namespace) -> None:
    apply_site_space(args)
    args.url = _require_url(args.url, "fetch")

    def ego_fn():
        focus = (
            build_js(FOCUS_FILTER, FOCUS=args.focus or "") if args.focus else ""
        )
        js = build_js(
            JS_EXTRACT,
            TASK_SPACE=args.task_space,
            URL=args.url,
            BODY_IIFE=_body_iife(60_000),
            FOCUS_FILTER=focus,
            KEEP_SPACE=_keep_js(args),
        )
        r = run_ego(js, args.timeout)
        if not r.get("ok"):
            return r
        p = r["payload"]
        p.setdefault("runtime", "ego")
        p["space_kept"] = bool(getattr(args, "keep_space", False))
        return {"ok": True, "payload": p}

    def wb_fn():
        return wb.fetch(
            args.url, focus=args.focus,
            session=args.task_space, timeout=args.timeout,
        )

    r, runtime = run_with_fallback(args, ego_fn=ego_fn, wb_fn=wb_fn, label="fetch")
    if not r.get("ok"):
        sys.exit(f"错误: {r.get('error')}")
    emit_json(r["payload"], runtime=runtime)


def cmd_act(args: argparse.Namespace) -> None:
    """act：优先 ego 链式；WebBridge 路径 = search + 打开首条 fetch。"""
    apply_site_space(args)

    def ego_fn():
        q = urllib.parse.quote(_normalized_query(args.query))
        url = SEARCH_URLS[args.engine].format(q=q)
        # 时间窗：URL 参数下推 + 解析后过滤（与 search 一致）
        tparams = wb.time_url_params(args.engine, getattr(args, "since", None), getattr(args, "until", None))
        if tparams:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(tparams)
        js = build_js(
            JS_ACT,
            TASK_SPACE=args.task_space,
            URL=url,
            QUERY=args.query,
            # 时间窗时多取候选（首条可能无日期/窗口外），过滤后取第一条打开
            SERP_IIFE=_serp_iife(args.engine, 5 if (getattr(args, "since", None) or getattr(args, "until", None)) else 1),
            BODY_IIFE=_body_iife(30_000),
            KEEP_SPACE=_keep_js(args),
        )
        r = run_ego(js, args.timeout)
        if not r.get("ok"):
            return r
        p = r["payload"]
        since = getattr(args, "since", None)
        until = getattr(args, "until", None)
        if since or until:
            results = wb.filter_window(p.get("results") or [], since, until)
            # detail 若对应被过滤掉的首条则丢弃，避免结果与详情不一致
            detail = p.get("detail")
            if detail:
                d_url = (detail.get("url") or "") if isinstance(detail, dict) else ""
                if not results or not d_url or d_url != results[0].get("url"):
                    p["detail"] = None
            p["results"] = results
        p.setdefault("engine", f"ego_browser_{args.engine}")
        p.setdefault("runtime", "ego")
        p["space_kept"] = bool(getattr(args, "keep_space", False))
        return {"ok": True, "payload": p}

    def wb_fn():
        sr = wb.search(
            _normalized_query(args.query), engine=args.engine, n=1,
            session=args.task_space, timeout=args.timeout,
            since=getattr(args, "since", None), until=getattr(args, "until", None),
        )
        if not sr.get("ok"):
            return sr
        results = (sr.get("payload") or {}).get("results") or []
        detail = None
        if results and results[0].get("url"):
            first = results[0]["url"]
            v = safety.validate_browser_url(first, context="act_result")
            if v.get("ok"):
                fr = wb.fetch(
                    v["url"], session=args.task_space, timeout=args.timeout,
                )
                if fr.get("ok"):
                    detail = fr["payload"]
            else:
                detail = {"error": v.get("error"), "url": first}
        return {
            "ok": True,
            "payload": {
                "query": args.query,
                "engine": f"webbridge_{args.engine}",
                "results": results,
                "detail": detail,
                "runtime": "webbridge",
                "source": "webbridge",
            },
        }

    r, runtime = run_with_fallback(args, ego_fn=ego_fn, wb_fn=wb_fn, label="act")
    if not r.get("ok"):
        sys.exit(f"错误: {r.get('error')}")
    emit_json(r["payload"], runtime=runtime)


def _origin_from_url(url: str) -> str:
    """从 API URL 提取默认 origin（scheme://host），跨子域场景须显式 --origin。"""
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def cmd_api(args: argparse.Namespace) -> None:
    apply_site_space(args)
    args.api_url = _require_url(args.api_url, "api")
    origin = args.origin or _origin_from_url(args.api_url)
    origin = _require_url(origin, "api_origin")

    def ego_fn():
        js = build_js(
            JS_API,
            TASK_SPACE=args.task_space,
            ORIGIN=origin,
            API_URL=args.api_url,
            KEEP_SPACE=_keep_js(args),
        )
        r = run_ego(js, args.timeout)
        if not r.get("ok"):
            return r
        p = r["payload"]
        p.setdefault("runtime", "ego")
        p["space_kept"] = bool(getattr(args, "keep_space", False))
        return {"ok": True, "payload": p}

    def wb_fn():
        return wb.api(
            args.api_url, origin=origin,
            session=args.task_space, timeout=args.timeout,
        )

    r, runtime = run_with_fallback(args, ego_fn=ego_fn, wb_fn=wb_fn, label="api")
    if not r.get("ok"):
        sys.exit(f"错误: {r.get('error')}")
    p = r["payload"]
    data = p.get("data")
    if data is not None:
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(s) > API_DATA_LIMIT:
            p["data"] = s[:API_DATA_LIMIT] + "…[truncated]"
            p["data_type"] = p.get("data_type", "json") + "_truncated"
            p["truncated"] = True
    emit_json(p, runtime=runtime)


# ── 专业搜索模式（默认关闭）──────────────────────────────────────────
def _load_state() -> dict:
    """读取模式状态；文件缺失/损坏一律视为关闭。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(enabled: bool) -> None:
    """持久化模式状态。"""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state: dict[str, Any] = {"enabled": enabled}
    if enabled:
        state["enabled_at"] = datetime.now().isoformat(timespec="seconds")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def mode_enabled() -> bool:
    """专业搜索模式是否开启（默认关闭）。"""
    return bool(_load_state().get("enabled", False))


def mode_gate_blocked(mode: str) -> str | None:
    """闸门：搜索类模式未开启时返回拒绝提示，其余模式返回 None。"""
    if mode in ("search", "fetch", "act", "api") and not mode_enabled():
        return (
            "专业搜索模式未开启（默认关闭，保障登录态安全）。"
            "请先运行: python3 sub-skills/ego-search/scripts/ego_search.py enable"
        )
    return None


def cmd_enable(args: argparse.Namespace) -> None:
    if mode_enabled():
        print("专业搜索模式已开启。")
        return
    _save_state(True)
    print(
        "专业搜索模式已开启：登录态专业搜索可用（ego lite 与/或 WebBridge，"
        "auto 择优），状态已持久化。"
    )
    log("提示：仅用于用户已授权的查询目标；登录态结果不进公共缓存。")


def cmd_disable(args: argparse.Namespace) -> None:
    if not mode_enabled():
        print("专业搜索模式当前未开启。")
        return
    _save_state(False)
    print("专业搜索模式已关闭：登录态专业搜索已禁用。")


def cmd_status(args: argparse.Namespace) -> None:
    try_start = bool(getattr(args, "fix", False))
    runtimes = rt.detect_runtimes(try_start_webbridge=try_start)
    print(json.dumps(
        {
            "pro_mode": mode_enabled(),
            "state_file": STATE_PATH,
            "architecture": "dual_runtime_complete",
            "version": "1.6.0",
            "runtimes": runtimes,
            "isolation": {
                "public_search_cache": "~/.cache/unified-search/cache.db",
                "login_partition": "cache_eligible=false / SearchCache 硬拒绝",
                "merge_at_analysis": True,
                "merge_cli": "merge --public a.json --login b.json",
            },
            "login_stability": {
                "default_close_space": True,
                "keep_space_flag": "--keep-space",
                "site_sticky": "--site example.com → task-space site:example.com + keep",
            },
        },
        ensure_ascii=False, indent=2,
    ))


def cmd_merge(args: argparse.Namespace) -> None:
    """分析层融合：不经过专业模式闸门（只读本地 JSON）。"""
    public = merge_mod.load_json_file(args.public)
    login = merge_mod.load_json_file(args.login)
    out = merge_mod.merge_payloads(public, login, query=args.query)
    print(json.dumps(out, ensure_ascii=False, indent=2))


# ── CLI ───────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ego_search.py",
        description=(
            "argo 子技能：登录态专业搜索（ego lite + WebBridge 双运行时；"
            "专业搜索模式默认关闭）。"
        ),
    )
    sub = ap.add_subparsers(dest="mode", required=True)

    p_enable = sub.add_parser("enable", help="开启专业搜索模式（需用户明确要求）")
    p_enable.set_defaults(fn=cmd_enable)
    p_disable = sub.add_parser("disable", help="关闭专业搜索模式")
    p_disable.set_defaults(fn=cmd_disable)
    p_status = sub.add_parser("status", help="专业模式 + 双运行时探测")
    p_status.add_argument(
        "--fix", action="store_true",
        help="探测前幂等尝试启动 WebBridge 本地桥（永不 stop）",
    )
    p_status.set_defaults(fn=cmd_status)

    p_merge = sub.add_parser(
        "merge",
        help="融合 public 常规检索 JSON + login 专业检索 JSON（分析层，不写缓存）",
    )
    p_merge.add_argument("--public", required=True, help="常规检索结果 JSON 文件")
    p_merge.add_argument("--login", required=True, help="ego-search 登录态结果 JSON 文件")
    p_merge.add_argument("--query", default=None, help="可选查询词覆盖")
    p_merge.set_defaults(fn=cmd_merge)

    p_search = sub.add_parser("search", help="浏览器态搜索，输出 argo JSON schema")
    p_search.add_argument("query", help="查询词")
    p_search.add_argument(
        "--engine", choices=list(SEARCH_URLS), default="bing",
        help="搜索引擎（默认 bing；baidu 中文，google 可能弹验证）",
    )
    p_search.add_argument("--n", type=int, default=8, help="结果条数（默认 8）")
    p_search.add_argument("--since", default=None,
                          help="发布时间下限（7d / 2026-08-01）：SERP URL 时间筛选 + 解析后过滤")
    p_search.add_argument("--until", default=None,
                          help="发布时间上限（7d / 2026-08-01）：SERP URL 时间筛选 + 解析后过滤")
    p_search.set_defaults(fn=cmd_search)

    p_fetch = sub.add_parser("fetch", help="浏览器态正文提取（JS 渲染/反爬/登录墙页面）")
    p_fetch.add_argument("url", help="目标 URL")
    p_fetch.add_argument("--focus", help="只返回包含该关键词的段落")
    p_fetch.set_defaults(fn=cmd_fetch)

    p_act = sub.add_parser("act", help="搜索→点开第一个结果→抓正文（一键链式）")
    p_act.add_argument("query", help="查询词")
    p_act.add_argument(
        "--engine", choices=list(SEARCH_URLS), default="bing",
        help="搜索引擎（默认 bing；与 search 共用）",
    )
    p_act.add_argument("--since", default=None,
                       help="发布时间下限（7d / 2026-08-01）：SERP URL 时间筛选 + 解析后过滤")
    p_act.add_argument("--until", default=None,
                       help="发布时间上限（7d / 2026-08-01）：SERP URL 时间筛选 + 解析后过滤")
    p_act.set_defaults(fn=cmd_act)

    p_api = sub.add_parser("api", help="浏览器上下文数据直取（同源 API，继承登录态）")
    p_api.add_argument("api_url", help="API 完整 URL（须与 --origin 同源）")
    p_api.add_argument("--origin", help="登录态上下文页面 URL（默认取 API URL 同源）")
    p_api.set_defaults(fn=cmd_api)

    for p in (p_search, p_fetch, p_act, p_api):
        p.add_argument(
            "--task-space", default="ego-search",
            help="任务空间名 / WebBridge session（默认 ego-search；同站复用可稳登录态）",
        )
        p.add_argument(
            "--timeout", type=int, default=DEFAULT_TIMEOUT,
            help=f"超时秒数（默认 {DEFAULT_TIMEOUT}）",
        )
        p.add_argument(
            "--runtime", choices=["auto", "ego", "webbridge"], default="auto",
            help="运行时：auto=有 ego 用 ego 否则 WebBridge（默认 auto）",
        )
        p.add_argument(
            "--keep-space", action="store_true",
            help="保留任务空间不关闭（多轮保温登录态；默认关闭空间防标签堆积）",
        )
        p.add_argument(
            "--site", default=None,
            help="粘性站点主机名（如 zhihu.com）→ task-space=site:host 并默认 keep-space",
        )

    args = ap.parse_args()

    # merge 只读本地文件，不涉及浏览器登录态闸门
    if args.mode != "merge":
        blocked = mode_gate_blocked(args.mode)
        if blocked:
            sys.exit(blocked)

    args.fn(args)


if __name__ == "__main__":
    main()
