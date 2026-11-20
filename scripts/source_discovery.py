#!/usr/bin/env python3
"""source_discovery.py — 新信息源发掘与零维护协议适配（方案 B）。

三大正交能力（供 `argo engine add` 调用）：
  1. probe_domain_protocols(domain)：自动探测目标域名的原生零维护协议族
     （Discourse JSON / MediaWiki API / OpenSearch XML / llms.txt / RSS-Atom），
     所有网络请求强制走 url_safety.check_url SSRF 防护，并直接产出 L1 YAML Spec 草稿。
  2. spec_from_har(har_data)：从浏览器抓包 HAR（或简化 XHR 记录）自动剥离
     Cookie/Authorization 等敏感头，推断查询参数与响应 JSON 字段映射，合成声明式
     engines/specs/<id>.yaml 草稿（支持 http 与 ego_inpage_api 两种传输）。
  3. discover_gaps_from_logs(log_path)：从本地使用日志聚合高频零召回/塌陷主题与
     高频出现但无专属直连源的域名，输出待探测候选清单。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import net_proxy
import url_safety

Fetcher = Callable[[str, float], tuple[int, dict[str, str], str]]

_SENSITIVE_HEADER_NAMES = frozenset({
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-csrf-token", "x-xsrf-token", "x-api-key", "api-key", "x-auth-token",
})

_DEFAULT_UA = "ArgoSourceDiscovery/1.0 (+https://github.com/taxue2025)"


def _default_fetcher(url: str, timeout: float = 6.0) -> tuple[int, dict[str, str], str]:
    ok, reason = url_safety.check_url(url)
    if not ok:
        raise ValueError(f"SSRF blocked: {reason}")
    req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA, "Accept": "*/*"})
    try:
        # 出口必须走 net_proxy.open_url：它才认 config.yaml 的 network.proxy，
        # 裸 urlopen 在「必须经代理才能出网」的环境里会连不上（issue #13 同形态）。
        with net_proxy.open_url(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(256 * 1024).decode("utf-8", errors="replace")
            return status, headers, body
    except urllib.error.HTTPError as err:
        return int(err.code), {}, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}, ""


def _normalize_origin(domain_or_url: str) -> tuple[str, str]:
    raw = (domain_or_url or "").strip()
    if not raw:
        raise ValueError("domain 不能为空")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"非法域名或 URL: {domain_or_url!r}")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    host = (parsed.hostname or "").lower().removeprefix("www.")
    ok, reason = url_safety.check_url(origin)
    if not ok:
        raise ValueError(f"SSRF 防护拦截 {origin}: {reason}")
    return origin, host


def _slug_from_host(host: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_")
    return stem[:32] or "custom_source"


def _parse_opensearch_template(xml_text: str) -> str | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag == "url":
            tmpl = elem.attrib.get("template") or ""
            mime = (elem.attrib.get("type") or "").lower()
            if tmpl and ("json" in mime or "html" in mime or "rss" in mime or not mime):
                return tmpl
    return None


def probe_domain_protocols(
    domain_or_url: str,
    *,
    timeout: float = 6.0,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """探测目标域名的原生零维护协议，并给出优先级最高的 L1 Spec 草稿。"""
    origin, host = _normalize_origin(domain_or_url)
    fetch = fetcher or _default_fetcher
    slug = _slug_from_host(host)
    detected: list[dict[str, Any]] = []

    # 1. Discourse JSON（一个协议族通吃所有 Discourse 论坛）
    st, _, body = fetch(f"{origin}/latest.json", timeout)
    if st == 200 and body:
        try:
            doc = json.loads(body)
            if isinstance(doc, dict) and isinstance(doc.get("topic_list"), dict):
                detected.append({
                    "protocol": "discourse",
                    "priority": 1,
                    "endpoint": f"{origin}/search.json",
                    "spec_draft": {
                        "id": slug,
                        "type": "http",
                        "method": "GET",
                        "url": f"{origin}/search.json",
                        "query_param": "q",
                        "cost_tier": "free",
                        "timeout": 10.0,
                        "desc": f"{host} Discourse 论坛搜索（自动协议探测生成）",
                        "headers": {"Accept": "application/json"},
                        "output_map": {
                            "items": "topics",
                            "item_title": "title",
                            "item_url": f"{origin}/t/{{slug}}/{{id}}",
                            "item_summary": "fancy_title",
                        },
                    },
                })
        except json.JSONDecodeError:
            pass

    # 2. MediaWiki API
    for mw_path in ("/w/api.php", "/api.php"):
        st, _, body = fetch(f"{origin}{mw_path}?action=query&meta=siteinfo&format=json", timeout)
        if st == 200 and body:
            try:
                doc = json.loads(body)
                if isinstance(doc, dict) and isinstance(doc.get("query"), dict) and "general" in doc["query"]:
                    detected.append({
                        "protocol": "mediawiki",
                        "priority": 2,
                        "endpoint": f"{origin}{mw_path}",
                        "spec_draft": {
                            "id": slug,
                            "type": "http",
                            "method": "GET",
                            "url": f"{origin}{mw_path}",
                            "query_param": "srsearch",
                            "extra_params": {"action": "query", "list": "search", "format": "json"},
                            "cost_tier": "free",
                            "timeout": 10.0,
                            "desc": f"{host} MediaWiki 站内搜索（自动协议探测生成）",
                            "output_map": {
                                "items": "query.search",
                                "item_title": "title",
                                "item_url": f"{origin}/wiki/{{title}}",
                                "item_summary": "snippet",
                            },
                        },
                    })
                    break
            except json.JSONDecodeError:
                pass

    # 3. OpenSearch XML
    st, _, body = fetch(f"{origin}/opensearch.xml", timeout)
    if st == 200 and body:
        tmpl = _parse_opensearch_template(body)
        if tmpl:
            detected.append({
                "protocol": "opensearch",
                "priority": 3,
                "endpoint": f"{origin}/opensearch.xml",
                "template": tmpl,
                "spec_draft": {
                    "id": slug,
                    "type": "http",
                    "method": "GET",
                    "url": tmpl.replace("{searchTerms}", "{query}"),
                    "cost_tier": "free",
                    "timeout": 10.0,
                    "desc": f"{host} OpenSearch 端点（自动协议探测生成）",
                },
            })

    # 4. llms.txt（AI 原生文档索引）
    st, _, body = fetch(f"{origin}/llms.txt", timeout)
    if st == 200 and body and len(body.strip()) >= 20 and "<html" not in body[:200].lower():
        detected.append({
            "protocol": "llms_txt",
            "priority": 4,
            "endpoint": f"{origin}/llms.txt",
            "preview_lines": len(body.splitlines()),
        })

    # 5. RSS / Atom 订阅
    for feed_path in ("/feed", "/rss.xml", "/atom.xml", "/index.xml"):
        st, _, body = fetch(f"{origin}{feed_path}", timeout)
        if st == 200 and body and any(tok in body[:400].lower() for tok in ("<rss", "<feed", "<rdf:rdf")):
            detected.append({
                "protocol": "rss_atom",
                "priority": 5,
                "endpoint": f"{origin}{feed_path}",
                "spec_draft": {
                    "id": slug,
                    "type": "feed",
                    "url": f"{origin}{feed_path}",
                    "cost_tier": "free",
                    "timeout": 10.0,
                    "desc": f"{host} RSS/Atom 订阅源（自动协议探测生成）",
                },
            })
            break

    detected.sort(key=lambda x: x["priority"])
    recommended = next((d for d in detected if "spec_draft" in d), None)
    prev_rejected = lookup_rejected(slug) or lookup_rejected(host)
    return {
        "origin": origin,
        "host": host,
        "detected_count": len(detected),
        "detected": detected,
        "recommended_protocol": recommended["protocol"] if recommended else None,
        "spec_draft": recommended["spec_draft"] if recommended else None,
        **({"previously_rejected": prev_rejected} if prev_rejected else {}),
    }


def _find_list_path(obj: Any, prefix: str = "") -> tuple[str, list[dict[str, Any]]] | None:
    """递归寻找响应 JSON 中最像搜索结果列表的 dict 数组路径。"""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return prefix, obj
    if isinstance(obj, dict):
        # 优先看常见结果键名
        preferred = ("results", "items", "data", "list", "hits", "topics", "notes", "statuses")
        keys = [k for k in preferred if k in obj] + [k for k in obj if k not in preferred]
        for k in keys:
            sub_prefix = f"{prefix}.{k}" if prefix else k
            found = _find_list_path(obj[k], sub_prefix)
            if found is not None:
                return found
    return None


def _pick_field(sample: dict[str, Any], candidates: tuple[str, ...]) -> str:
    lower_map = {k.lower(): k for k in sample.keys()}
    for c in candidates:
        if c in lower_map:
            return lower_map[c]
    return candidates[0]


def spec_from_har(
    har_data: dict[str, Any],
    *,
    engine_id: str | None = None,
    query_hint: str = "",
    transport: str = "http",
) -> dict[str, Any]:
    """从浏览器 HAR 抓包合成脱敏的声明式 YAML Spec 草稿。

    强制安全纪律：彻底剥离 Cookie / Authorization / CSRF 等敏感请求头。
    """
    entries = ((har_data or {}).get("log") or {}).get("entries") or []
    if not isinstance(entries, list) or not entries:
        raise ValueError("HAR 缺少 log.entries")

    chosen: tuple[dict[str, Any], dict[str, Any], str, list[dict[str, Any]]] | None = None
    hint_low = query_hint.strip().lower()

    for entry in entries:
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        url = str(req.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        if hint_low and hint_low not in urllib.parse.unquote(url).lower():
            continue
        content = resp.get("content") or {}
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            continue
        found = _find_list_path(body)
        if found is None:
            continue
        list_path, sample_list = found
        chosen = (req, resp, list_path, sample_list)
        break

    if chosen is None:
        raise ValueError("未在 HAR 中找到包含 JSON 列表响应的搜索请求（可指定 --query-hint 过滤）")

    req, _resp, list_path, sample_list = chosen
    raw_url = str(req["url"])
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    eid = engine_id or _slug_from_host(host)

    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query_param = "q"
    extra_params: dict[str, str] = {}
    for k, vals in qs.items():
        val = vals[0] if vals else ""
        if hint_low and hint_low in val.lower():
            query_param = k
        elif k.lower() in ("q", "query", "keyword", "keywords", "search", "term", "k"):
            query_param = k
        else:
            extra_params[k] = val

    # 强制剥离所有敏感请求头，只留安全白名单头
    safe_headers: dict[str, str] = {"Accept": "application/json"}
    for h in req.get("headers") or []:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        val = str(h.get("value") or "").strip()
        low = name.lower()
        if not name or low.startswith(":") or low in _SENSITIVE_HEADER_NAMES:
            continue
        if low in ("accept", "accept-language", "referer", "origin"):
            safe_headers[name] = val

    sample = sample_list[0]
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    spec: dict[str, Any] = {
        "id": eid,
        "type": "http" if transport == "http" else "ego_inpage_api",
        "method": str(req.get("method") or "GET").upper(),
        "url": base_url,
        "query_param": query_param,
        "cost_tier": "free",
        "timeout": 10.0,
        "desc": f"{host} 搜索接口（由 HAR 抓包自动合成，已脱敏请求头）",
        "headers": safe_headers,
        "output_map": {
            "items": list_path or "results",
            "item_title": _pick_field(sample, ("title", "name", "headline", "text")),
            "item_url": _pick_field(sample, ("url", "link", "href", "permalink", "id")),
            "item_summary": _pick_field(sample, ("snippet", "summary", "excerpt", "description", "content")),
        },
    }
    if extra_params:
        spec["extra_params"] = extra_params
    if transport == "ego_inpage_api":
        spec["origin"] = f"{parsed.scheme}://{parsed.netloc}"
        spec["auth_partition"] = "login"
        spec["cache_eligible"] = False
    return spec


def discover_gaps_from_logs(
    log_path: str | Path | None = None,
    *,
    min_hits: int = 2,
    known_hosts: set[str] | None = None,
) -> dict[str, Any]:
    """从本地使用日志聚合高频零召回域与反复出现但无直连源的域名。"""
    if log_path is None:
        import argo_paths as _paths
        path = _paths.state_path("usage_log.jsonl")
    else:
        path = Path(log_path)

    known = {h.lower().removeprefix("www.") for h in (known_hosts or set())}
    collapsed_domains: Counter[str] = Counter()
    candidate_hosts: Counter[str] = Counter()
    scanned = 0

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return {
            "log_path": str(path),
            "scanned_events": 0,
            "collapsed_domains": [],
            "candidate_hosts": [],
        }

    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        scanned += 1
        data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
        count = data.get("result_count", data.get("count"))
        domain = data.get("domain")
        if domain and isinstance(count, int) and count == 0:
            collapsed_domains[str(domain)] += 1
        for raw_u in (data.get("top_urls") or data.get("urls") or []):
            if not isinstance(raw_u, str):
                continue
            h = (urllib.parse.urlparse(raw_u).hostname or "").lower().removeprefix("www.")
            if h and h not in known:
                candidate_hosts[h] += 1

    return {
        "log_path": str(path),
        "scanned_events": scanned,
        "collapsed_domains": [
            {"domain": d, "zero_result_hits": c}
            for d, c in collapsed_domains.most_common(15)
            if c >= min_hits
        ],
        "candidate_hosts": [
            {"host": h, "hits": c}
            for h, c in candidate_hosts.most_common(15)
            if c >= min_hits
        ],
    }


def _rejected_root(rejected_dir: str | Path | None = None) -> Path:
    if rejected_dir is not None:
        return Path(rejected_dir)
    import argo_paths as _paths
    return _paths.state_path("rejected_sources")


def lookup_rejected(
    key: str,
    *,
    rejected_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """查询指定引擎 ID 或主机名是否曾被隔离烟测拒绝，防止重复踩坑。"""
    slug = _slug_from_host(key)
    root = _rejected_root(rejected_dir)
    for cand in (key, slug):
        if not cand:
            continue
        meta_path = root / cand / "validation.json"
        try:
            doc = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return doc
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return None


def _archive_rejected(
    spec_draft: dict[str, Any],
    reason: str,
    *,
    detail: str = "",
    rejected_dir: str | Path | None = None,
) -> Path:
    import time as _t
    eid = _slug_from_host(str(spec_draft.get("id") or "unknown_draft"))
    dest = _rejected_root(rejected_dir) / eid
    dest.mkdir(parents=True, exist_ok=True)
    record = {
        "engine_id": eid,
        "rejected_at": _t.time(),
        "reason": reason,
        "detail": detail[:600],
        "url": str(spec_draft.get("url") or ""),
    }
    (dest / "validation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (dest / "candidate_spec.json").write_text(
        json.dumps(spec_draft, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return dest


def _default_isolated_smoke(spec_path: Path, timeout: float) -> tuple[bool, str]:
    """在独立隔离子进程中对候选 Spec 发起一次受控只读烟测（主进程永不直接加载草稿）。"""
    import subprocess
    import sys

    probe_code = (
        "import json, sys, urllib.request, urllib.parse; "
        "spec = json.loads(open(sys.argv[1], encoding='utf-8').read()); "
        "url = spec.get('url', ''); "
        "qp = spec.get('query_param', 'q'); "
        "sep = '&' if '?' in url else '?'; "
        "target = url if '{query}' in url else f'{url}{sep}{urllib.parse.urlencode({qp: \"test\"})}'; "
        "target = target.replace('{query}', 'test'); "
        "req = urllib.request.Request(target, headers={'Accept': 'application/json', 'User-Agent': 'ArgoSmoke/1.0'}); "
        "resp = urllib.request.urlopen(req, timeout=float(sys.argv[2])); "
        "code = getattr(resp, 'status', 200); "
        "body = resp.read(65536); "
        "print(json.dumps({'status': code, 'bytes': len(body)})); "
        "sys.exit(0 if (200 <= code < 400 and len(body) > 0) else 1)"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe_code, str(spec_path), str(timeout)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(2.0, float(timeout) + 2.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"isolated_smoke_error: {exc}"
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    return False, (proc.stderr or proc.stdout or f"exit_{proc.returncode}").strip()


def verify_spec_draft_isolated(
    spec_draft: dict[str, Any],
    *,
    timeout: float = 10.0,
    rejected_dir: str | Path | None = None,
    runner: Callable[[Path, float], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """对候选 Spec 草稿执行隔离烟测；未通过自动归档至拒绝档案（_rejected）。"""
    import tempfile

    if not isinstance(spec_draft, dict):
        raise ValueError("spec_draft 必须是 dict")
    eid = str(spec_draft.get("id") or "").strip()
    url = str(spec_draft.get("url") or "").strip()
    if not eid or not url:
        dest = _archive_rejected(spec_draft, "missing_required_fields", rejected_dir=rejected_dir)
        return {"passed": False, "reason": "missing_required_fields", "rejected_path": str(dest)}

    ok_url, ssrf_reason = url_safety.check_url(url.replace("{query}", "test"))
    if not ok_url:
        dest = _archive_rejected(
            spec_draft, "ssrf_blocked", detail=ssrf_reason, rejected_dir=rejected_dir,
        )
        return {"passed": False, "reason": "ssrf_blocked", "detail": ssrf_reason, "rejected_path": str(dest)}

    # 强制检查：草稿 headers 不得夹带任何敏感认证头
    headers = spec_draft.get("headers") or {}
    if isinstance(headers, dict):
        leaked = [k for k in headers if str(k).lower() in _SENSITIVE_HEADER_NAMES]
        if leaked:
            dest = _archive_rejected(
                spec_draft, "sensitive_header_leaked", detail=",".join(leaked), rejected_dir=rejected_dir,
            )
            return {
                "passed": False,
                "reason": "sensitive_header_leaked",
                "detail": ",".join(leaked),
                "rejected_path": str(dest),
            }

    smoke = runner or _default_isolated_smoke
    with tempfile.TemporaryDirectory(prefix="argo_smoke_") as tmpdir:
        cand_path = Path(tmpdir) / f"{_slug_from_host(eid)}.json"
        cand_path.write_text(json.dumps(spec_draft, ensure_ascii=False), encoding="utf-8")
        passed, detail = smoke(cand_path, timeout)

    if passed:
        return {"passed": True, "engine_id": eid, "detail": detail, "spec": spec_draft}

    dest = _archive_rejected(
        spec_draft, "smoke_test_failed", detail=detail, rejected_dir=rejected_dir,
    )
    return {
        "passed": False,
        "engine_id": eid,
        "reason": "smoke_test_failed",
        "detail": detail,
        "rejected_path": str(dest),
    }
