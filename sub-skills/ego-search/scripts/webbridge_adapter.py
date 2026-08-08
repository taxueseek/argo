#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebBridge 适配：登录态 search / fetch / api（用户 Chrome/Edge + 扩展桥）。

协议：POST http://127.0.0.1:10086/command
输出字段与 ego 路径对齐，source=webbridge，login provenance 由调用方 stamp。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 同目录 safety
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import safety as safety  # noqa: E402

WEBBRIDGE_URL = os.environ.get(
    "EGO_SEARCH_WEBBRIDGE_URL", "http://127.0.0.1:10086/command"
)
DEFAULT_TIMEOUT = int(os.environ.get("EGO_SEARCH_WEBBRIDGE_TIMEOUT", "90"))

SEARCH_URLS = {
    "bing": "https://www.bing.com/search?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}",
    "google": "https://www.google.com/search?q={q}",
}

# 页面内 SERP 提取（与 ego 选择器同构）
SERP_JS = r"""
(() => {
  const engine = %ENGINE_JSON%;
  const n = %N%;
  const configs = {
    bing: { item: 'li.b_algo', link: 'h2 a', snippet: '.b_caption p, p' },
    baidu: { item: "div#content_left div[class*='c-container'], div#content_left div.result", link: 'h3 a', snippet: ".c-abstract, [class*='content-right']" },
    google: { item: 'div.g, div[data-sncf]', link: 'a h3', snippet: 'div.VwiC3b, div[data-sncf]' }
  };
  const cfg = configs[engine] || configs.bing;
  const items = [];
  const extractDate = (t) => {
    const m = t.match(/(20\d{2})[年\/\-\.](\d{1,2})[月\/\-\.](\d{1,2})/);
    if (m) return m[1] + '-' + String(m[2]).padStart(2,'0') + '-' + String(m[3]).padStart(2,'0');
    const m2 = t.match(/(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})/i);
    if (m2) { const mo={jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'}; return m2[3] + '-' + mo[m2[2].toLowerCase().slice(0,3)] + '-' + String(m2[1]).padStart(2,'0'); }
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
      if (items.length >= n) return;
      items.push({ title: (a.innerText || '').trim(), url: a.href || '', snippet: '', published_at: extractDate((a.innerText || '') + ' ' + (a.href || '')) });
    });
  }
  return JSON.stringify(items.slice(0, n));
})()
"""

# ── 时间窗工具（ego/webbridge 双路径共用）──────────────────────────────
def _parse_time(s: str | None) -> str | None:
    """相对（7d/30d/12h/1w/1y）或绝对（2026-08-01/ISO）→ ISO 日期。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+)([hdwmy])", s.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    now = datetime.now()
    delta = {
        "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n),
        "m": timedelta(days=30 * n), "y": timedelta(days=365 * n),
    }[unit]
    return (now - delta).date().isoformat()


def _to_epoch_ms(s: str | None) -> str:
    iso = _parse_time(s)
    if not iso:
        return ""
    try:
        return str(int(datetime.fromisoformat(iso).timestamp() * 1000))
    except ValueError:
        return ""


def time_url_params(engine: str, since: str | None, until: str | None) -> dict[str, str]:
    """各引擎 URL 时间筛选参数（尽力而为；引擎改版失效时由解析后过滤兜底）。"""
    since_iso, until_iso = _parse_time(since), _parse_time(until)
    if engine == "google":
        if since_iso or until_iso:
            parts = ["cdr:1"]
            if since_iso:
                parts.append(f"cd_min:{since_iso}")
            if until_iso:
                parts.append(f"cd_max:{until_iso}")
            return {"tbs": ",".join(parts)}
    elif engine == "bing":
        if since_iso:
            try:
                days = max((datetime.now() - datetime.fromisoformat(since_iso)).days, 1)
                return {"qft": f"+filterui:age-lt{days * 86400}"}
            except ValueError:
                pass
    elif engine == "baidu":
        if since_iso or until_iso:
            return {"gpc": f"stf%3D{_to_epoch_ms(since_iso)}%2C{_to_epoch_ms(until_iso)}%7Cstftype%3D2"}
    return {}


def filter_window(results: list, since: str | None, until: str | None) -> list:
    """解析后时间窗过滤：仅保留 published_at 落在 [since, until] 内的结果。

    时间窗查询必须保证结果新鲜，无日期字段的条目剔除（与 local-search 行为一致）。
    """
    since_iso, until_iso = _parse_time(since), _parse_time(until)
    if not since_iso and not until_iso:
        return results

    def _key(d: str) -> tuple[int, ...]:
        return tuple(int(x) for x in str(d).split("-"))

    out = []
    for r in results:
        pa = r.get("published_at") if isinstance(r, dict) else None
        if not pa:
            continue
        if since_iso and _key(pa) < _key(since_iso):
            continue
        if until_iso and _key(pa) > _key(until_iso):
            continue
        out.append(r)
    return out

BODY_JS = r"""
(() => {
  const max = %MAX%;
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
  return JSON.stringify({ title: (title || '').trim(), content: (content || '').slice(0, max), url: location.href });
})()
"""


def _command(action: str, args: dict | None, session: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    body = json.dumps(
        {"action": action, "args": args or {}, "session": session},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        WEBBRIDGE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"webbridge_unreachable: {e}"}
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"webbridge_bad_json: {raw[:200]}"}
    if data.get("ok") is False:
        return {"ok": False, "error": data.get("error") or data.get("message") or str(data)[:300]}
    return {"ok": True, "data": data.get("data", data)}


def _eval_value(data: Any) -> Any:
    """从 evaluate 响应抽出 value（兼容多种包装）。"""
    if not isinstance(data, dict):
        return data
    if "value" in data:
        return data["value"]
    inner = data.get("data")
    if isinstance(inner, dict) and "value" in inner:
        return inner["value"]
    return data


def _parse_jsonish(val: Any) -> Any:
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return val
    return val


def navigate(url: str, session: str, *, new_tab: bool = True, group_title: str = "ego-search") -> dict:
    v = safety.validate_browser_url(url, context="webbridge_navigate")
    if not v.get("ok"):
        return {"ok": False, "error": v.get("error")}
    return _command(
        "navigate",
        {"url": v["url"], "newTab": new_tab, "group_title": group_title},
        session,
    )


def evaluate(code: str, session: str) -> dict:
    r = _command("evaluate", {"code": code}, session)
    if not r.get("ok"):
        return r
    val = _eval_value(r.get("data"))
    return {"ok": True, "value": _parse_jsonish(val)}


def search(
    query: str,
    *,
    engine: str = "bing",
    n: int = 8,
    session: str = "ego-search",
    timeout: int = DEFAULT_TIMEOUT,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    if engine not in SEARCH_URLS:
        return {"ok": False, "error": f"unsupported_engine: {engine}"}
    url = SEARCH_URLS[engine].format(q=urllib.parse.quote(query))
    tparams = time_url_params(engine, since, until)
    if tparams:
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode(tparams)
    nav = navigate(url, session, group_title=f"search:{query[:40]}")
    if not nav.get("ok"):
        return nav
    code = (
        SERP_JS.replace("%ENGINE_JSON%", json.dumps(engine))
        .replace("%N%", str(int(n)))
    )
    ev = evaluate(code, session)
    if not ev.get("ok"):
        return ev
    results = ev.get("value")
    if isinstance(results, str):
        results = _parse_jsonish(results)
    if not isinstance(results, list):
        results = []
    # 解析后时间窗过滤（URL 参数之外的通用兜底）
    if since or until:
        results = filter_window(results, since, until)
    return {
        "ok": True,
        "payload": {
            "query": query,
            "engine": f"webbridge_{engine}",
            "source": "webbridge",
            "runtime": "webbridge",
            "url": url,
            "results": results,
            "count": len(results),
            "fetch_method": "browser",
            "since": since,
            "until": until,
        },
    }


def fetch(
    url: str,
    *,
    focus: str | None = None,
    session: str = "ego-search",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    nav = navigate(url, session, group_title=f"fetch:{url[:48]}")
    if not nav.get("ok"):
        return nav
    code = BODY_JS.replace("%MAX%", "60000")
    ev = evaluate(code, session)
    if not ev.get("ok"):
        return ev
    data = ev.get("value")
    if isinstance(data, str):
        data = _parse_jsonish(data)
    if not isinstance(data, dict):
        data = {"title": "", "content": str(data or ""), "url": url}
    body = (data.get("content") or "").replace("\n\n\n", "\n\n").strip()
    if focus:
        kw = focus.lower()
        paras = body.split("\n\n")
        hit = [p for p in paras if kw in p.lower()]
        body = "\n\n".join(hit) if hit else body[:8000]
        if len(body) < 200:
            body = (data.get("content") or "")[:8000]
    return {
        "ok": True,
        "payload": {
            "url": data.get("url") or url,
            "title": data.get("title") or "",
            "content": body,
            "word_count": len(body.split()),
            "fetch_method": "browser",
            "source": "webbridge",
            "runtime": "webbridge",
        },
    }


def api(
    api_url: str,
    *,
    origin: str | None = None,
    session: str = "ego-search",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if not origin:
        p = urllib.parse.urlparse(api_url)
        origin = f"{p.scheme}://{p.netloc}"
    nav = navigate(origin, session, group_title=f"api:{origin[:48]}")
    if not nav.get("ok"):
        return nav
    # 页面上下文 fetch，继承登录态
    code = (
        "(() => fetch(%s, {credentials:'include'}).then(async r => {"
        " const t = await r.text();"
        " try { return JSON.stringify({ok:true, data: JSON.parse(t), data_type:'json'}); }"
        " catch(e) { return JSON.stringify({ok:true, data: t.slice(0,100000), data_type:'text'}); }"
        "}).catch(e => JSON.stringify({ok:false, error: String(e).slice(0,300)})))()"
    ) % json.dumps(api_url)
    ev = evaluate(code, session)
    if not ev.get("ok"):
        return ev
    raw = ev.get("value")
    if isinstance(raw, str):
        raw = _parse_jsonish(raw)
    if not isinstance(raw, dict):
        return {"ok": False, "error": f"api_bad_response: {raw!r}"[:300]}
    if raw.get("ok") is False:
        return {
            "ok": True,
            "payload": {
                "api_url": api_url,
                "page_url": origin,
                "data": raw.get("error"),
                "data_type": "error",
                "fetch_method": "browser_api",
                "source": "webbridge",
                "runtime": "webbridge",
            },
        }
    return {
        "ok": True,
        "payload": {
            "api_url": api_url,
            "page_url": origin,
            "data": raw.get("data"),
            "data_type": raw.get("data_type") or "json",
            "fetch_method": "browser_api",
            "source": "webbridge",
            "runtime": "webbridge",
        },
    }
