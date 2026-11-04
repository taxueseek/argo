#!/usr/bin/env python3
"""专用构建器：反向图片搜索（SauceNAO / AnimeTrace）

以图搜图与其他引擎的根本差别在输入：query 不是关键词，而是**公网可访问的
图片 URL**——引擎把这串 URL 交给上游做图片指纹比对，回答「这张图出自哪里」，
输出的是原图/出处页，而不是「讲述了这张图的网页」。因此：

- 两条引擎都只接受 http(s) 图片 URL，收到关键词直接返回可读的 error 条目
  （静默返回 [] 会让调用方误以为「查了但没找到」）；
- 只做显式调用（spec 里 explicit_only），不进自动路由候选池——没有图片 URL
  的普通查询落进来必然是空结果；
- known-url 输入不应被离线 handoff 拦截：显式指定引擎时按 URL 取数，
  见 `search.py` 的 URL 输入引擎豁免。

两条实测约束（2026-11-04）：

1. SauceNAO 匿名档已关闭 API 调用：不带 api_key 时返回
   `{"header":{"status":-1,"message":"The anonymous account type does not permit
   API usage."}}`。站点在 Cloudflare 之后（urllib 直连拿到 `Cf-Mitigated:
   challenge` 的 403 页），取数须走 TLS 指纹伪造（curl_cffi impersonate），
   故 `_http_get_raw` 传 `tls_profiles`。
2. AnimeTrace 免 key 但按出口 IP 限频：超限返回 HTTP 429 + `code:17737`，
   配额用尽是 `code:17728`。两者都映射成 error 条目而不是空结果——调用方
   需要分清「这张图认不出」与「这次没资格查」。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from engines_base import _http_get_raw, http_open, rank_score, safe_search
from engine_env import get_env

# 只认公网图片地址；裸关键词、本地路径、file:// 一律在入口挡下并说明原因。
_IMAGE_URL_RE = re.compile(r"(?i)^https?://[^\s]+$")


def _image_url_error(engine: str, query: str) -> list[dict[str, Any]]:
    """输入不是图片 URL 时的可读错误（各反搜图引擎共用同一口径）。"""
    return [{
        "error": f"{engine}: 反搜图引擎要喂公网图片 URL（http(s):// 开头）作为查询，"
                 f"收到的是「{(query or '')[:50]}」",
        "source": engine,
    }]


# ── SauceNAO（二次元图源索引，需免费 API key）───────────────────────────────

_SAUCENAO_ENDPOINT = "https://saucenao.com/search.php"

# 上游 header.status 的语义（见 saucenao.com 文档）；负值都是「没查成」，
# 不能当「没找到」，否则缺 key 的实例会显得一切正常。
_SAUCENAO_STATUS_HINT = {
    -1: "匿名档已不允许 API 调用，请在 saucenao.com 注册免费账号并配 ARGO_SAUCENAO_API_KEY",
    -2: "API key 无效或已失效",
    -3: "API 用量超出限制（免费档 150 次/日）",
    -4: "单次请求参数超出限制",
    -5: "上游内部错误，稍后重试",
}


def _saucenao_source_url(data: dict[str, Any]) -> str:
    """按来源站 ID 还原出处页；SauceNAO 只给 ID，不给页面 URL。"""
    if data.get("pixiv_id"):
        return f"https://www.pixiv.net/artworks/{data['pixiv_id']}"
    if data.get("pawoo_id"):
        return f"https://pawoo.net/@{data.get('pawoo_user_acct', '')}/{data['pawoo_id']}"
    if data.get("getchu_id"):
        return f"https://www.getchu.com/soft.phtml?id={data['getchu_id']}"
    ext = data.get("ext_urls") or []
    if isinstance(ext, list) and ext and isinstance(ext[0], str):
        return ext[0]
    return ""


def _saucenao_author(data: dict[str, Any]) -> str:
    """作者名：不同数据库的字段名不一致，按优先级取第一个非空。"""
    for key in ("author", "member_name", "creator", "twitter_user_handle",
                "pawoo_user_display_name", "author_name", "user_name",
                "artist", "company"):
        val = data.get(key)
        if isinstance(val, list) and val:
            return ", ".join(str(v) for v in val)
        if isinstance(val, str) and val:
            return val
    return ""


def _saucenao_title(data: dict[str, Any], index_name: str) -> str:
    for key in ("title", "material", "jp_name", "eng_name", "source", "created_at"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return index_name or "SauceNAO 匹配"


def _build_saucenao_engine(spec: dict[str, Any]) -> Any:
    """SauceNAO 以图搜图（POST/GET 同一端点，需 API key，走 TLS 指纹伪造过 CF）。"""
    timeout = spec.get("timeout", 20)
    res_cap = int(spec.get("num_results", 5) or 5)
    min_sim = int(spec.get("min_similarity", 30) or 30)
    # hide：0 全显示 / 1 隐藏露骨 / 2 再隐藏疑似 / 3 只留安全内容。群聊默认 1。
    try:
        hide = min(max(int(spec.get("hide", 1)), 0), 3)
    except (TypeError, ValueError):
        hide = 1
    # dedupe：0 不去重 / 1 按条目去重 / 2 连系列一起并（SauceNAO 默认 2）。
    try:
        dedupe = min(max(int(spec.get("dedupe", 2)), 0), 2)
    except (TypeError, ValueError):
        dedupe = 2

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not _IMAGE_URL_RE.match(q):
            return _image_url_error("saucenao", q)
        key = get_env(["ARGO_SAUCENAO_API_KEY", "SAUCENAO_API_KEY"])
        if not key:
            return [{"error": "saucenao: 缺 ARGO_SAUCENAO_API_KEY"
                              "（匿名档已不允许 API 调用；saucenao.com 免费注册 150 次/日）",
                     "source": "saucenao"}]
        params = {
            "testmode": "0",
            "numres": str(min(max(int(n), 1), res_cap)),
            "output_type": "2",
            "hide": str(hide),
            "dedupe": str(dedupe),
            "db": str(spec.get("db", 999) or 999),
            "minsim": str(min_sim),
            "url": q,
            "api_key": key,
        }
        dbs = spec.get("dbs")
        if isinstance(dbs, (list, tuple)) and dbs:
            # dbs[] 是重复键，urlencode(dict) 拼不出来，单独拼在末尾。
            params.pop("db", None)
            extra = "&".join(f"dbs[]={urllib.parse.quote(str(i))}" for i in dbs)
            url = f"{_SAUCENAO_ENDPOINT}?{urllib.parse.urlencode(params)}&{extra}"
        else:
            url = f"{_SAUCENAO_ENDPOINT}?{urllib.parse.urlencode(params)}"
        raw = _http_get_raw(url, {"Accept": "application/json",
                                  "Referer": "https://saucenao.com/"},
                            _timeout or timeout, engine=spec.get("_name", ""),
                            tls_profiles=["chrome", "safari"])
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        header = payload.get("header")
        status = 0
        if isinstance(header, dict):
            try:
                status = int(header.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
        if status:
            msg = str(header.get("message") or "").strip()
            hint = _SAUCENAO_STATUS_HINT.get(status, "")
            detail = " · ".join(b for b in (f"status {status}", msg, hint) if b)
            return [{"error": f"saucenao: {detail}", "source": "saucenao"}]
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload.get("results") or []:
            if len(results) >= max(1, int(n)):
                break
            if not isinstance(item, dict):
                continue
            item_header = item.get("header") if isinstance(item.get("header"), dict) else {}
            item_data = item.get("data") if isinstance(item.get("data"), dict) else {}
            page_url = _saucenao_source_url(item_data)
            if not page_url or page_url in seen:
                continue
            seen.add(page_url)
            try:
                sim = float(item_header.get("similarity"))
            except (TypeError, ValueError):
                sim = 0.0
            index_name = str(item_header.get("index_name") or "").strip()
            author = _saucenao_author(item_data)
            thumb = str(item_header.get("thumbnail") or "").strip()
            bits = [b for b in (
                f"相似度 {sim:g}%" if sim > 0 else "",
                f"库 {index_name}" if index_name else "",
                f"作者 {author}" if author else "",
                f"素材 {item_data.get('material')}" if item_data.get("material") else "",
            ) if b]
            results.append({
                "title": _saucenao_title(item_data, index_name)[:200],
                "url": page_url,
                "snippet": " · ".join(bits)[:300],
                "source": spec.get("_name") or "saucenao",
                "score": rank_score(min(sim / 100.0, 1.0) if sim > 0 else 0.6, len(results)),
                "metadata": {
                    "similarity": sim,
                    "index_name": index_name,
                    "author": author,
                    "thumbnail": thumb,
                    "pixiv_id": item_data.get("pixiv_id"),
                    "ext_urls": item_data.get("ext_urls") or [],
                },
                **({"image_url": thumb} if thumb else {}),
            })
        return results
    return _engine


# ── Google Lens（SerpAPI 托管，付费档）─────────────────────────────────────

_SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# 抓取顺序即相关度顺序：先「同一张图」（exact），再「长得像的图」（visual），
# 最后才是「讲到这张图的网页」（organic）。三者混在一列里必须各自带标，
# 否则调用方会把「视觉相似的另一张图」当成这张图的出处。
_GOOGLE_LENS_BUCKETS: tuple[tuple[str, str, float], ...] = (
    ("exact_matches", "精确匹配", 0.95),
    ("visual_matches", "视觉相似", 0.78),
    ("organic_results", "相关网页", 0.6),
)


def _google_lens_error(status: int, payload_text: str) -> dict[str, Any]:
    """SerpAPI 的错误统一走 `error` 字段（无效 key、额度用尽、上游失败）。"""
    msg = ""
    try:
        obj = json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        msg = str(obj.get("error") or "").strip()
    if not msg:
        msg = (payload_text or "").strip()[:120]
    detail = " · ".join(b for b in (f"HTTP {status}" if status else "", msg) if b)
    return {"error": f"google_lens: {detail or '请求失败'}", "source": "google_lens"}


def _google_lens_item(item: dict[str, Any], match_label: str, base: float,
                      index: int, engine_name: str) -> dict[str, Any] | None:
    """把一条 Lens 条目归一成 Argo 结果；没有可用链接的条目直接丢弃。"""
    link = str(item.get("link") or "").strip()
    if not link:
        return None
    title = str(item.get("title") or "").strip()
    site = str(item.get("source") or "").strip()
    if not title:
        title = site or link
    thumb = str(item.get("thumbnail") or "").strip()
    image = str(item.get("image") or "").strip()
    price = item.get("price")
    price_txt = ""
    if isinstance(price, dict):
        price_txt = str(price.get("value") or price.get("extracted_value") or "").strip()
    elif isinstance(price, str):
        price_txt = price.strip()
    bits = [b for b in (
        match_label,
        f"站点 {site}" if site else "",
        price_txt,
        str(item.get("snippet") or "").strip()[:160],
    ) if b]
    result: dict[str, Any] = {
        "title": title[:200],
        "url": link,
        "snippet": " · ".join(bits)[:300],
        "source": engine_name or "google_lens",
        "score": rank_score(base, index),
        "metadata": {
            "match_type": match_label,
            "site": site,
            "thumbnail": thumb,
            "image": image,
            "position": item.get("position"),
        },
    }
    if thumb:
        result["image_url"] = thumb
    return result


def _build_google_lens_engine(spec: dict[str, Any]) -> Any:
    """Google Lens 反搜图（SerpAPI 托管：付费档，按次计费，免费额度 250 次/月）。"""
    timeout = spec.get("timeout", 30)
    search_type = str(spec.get("search_type") or "all").strip()
    if search_type not in {"all", "exact_matches", "visual_matches"}:
        search_type = "all"
    hl = str(spec.get("hl") or "").strip()
    country = str(spec.get("country") or "").strip()
    res_cap = int(spec.get("num_results", 8) or 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not _IMAGE_URL_RE.match(q):
            return _image_url_error("google_lens", q)
        key = get_env(["ARGO_SERPAPI_API_KEY", "SERPAPI_API_KEY"])
        if not key:
            return [{"error": "google_lens: 缺 ARGO_SERPAPI_API_KEY"
                              "（serpapi.com 注册后免费 250 次/月）",
                     "source": "google_lens"}]
        params = {
            "engine": "google_lens",
            "url": q,
            "type": search_type,
            "api_key": key,
        }
        if hl:
            params["hl"] = hl
        if country:
            params["country"] = country
        url = f"{_SERPAPI_ENDPOINT}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with http_open(req, timeout=_timeout or timeout, engine=spec.get("_name", "")) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            payload = ""
            try:
                payload = e.read().decode("utf-8", errors="replace")[:400]
            except OSError:
                payload = ""
            return [_google_lens_error(e.code, payload)]
        except (urllib.error.URLError, TimeoutError, OSError):
            return []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("error"):
            return [_google_lens_error(0, raw)]
        cap = min(max(int(n), 1), res_cap)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket, label, base in _GOOGLE_LENS_BUCKETS:
            for item in payload.get(bucket) or []:
                if len(results) >= cap:
                    return results
                if not isinstance(item, dict):
                    continue
                link = str(item.get("link") or "").strip()
                if link in seen:
                    continue
                parsed = _google_lens_item(item, label, base, len(results), spec.get("_name", ""))
                if parsed is None:
                    continue
                seen.add(link)
                results.append(parsed)
        return results
    return _engine


# ── AnimeTrace（动漫角色识别，免 key、按 IP 限频）────────────────────────────

_ANIMETRACE_ENDPOINT = "https://api.animetrace.com/v1/search"

# 已实测到的业务码：17728 配额用尽、17737 频率限制。其余码只在 error 里透传原码。
_ANIMETRACE_CODE_HINT = {
    17728: "用量上限（配额已用尽，次日或换 key 再试）",
    17737: "触发频率限制（稍后重试）",
}


def _animetrace_error(status: int, payload: str) -> dict[str, Any]:
    """把 HTTP 状态 + 上游业务码合成一条可读错误。"""
    code: Any = None
    msg = ""
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        code = obj.get("code")
        msg = str(obj.get("zh_message") or obj.get("message") or obj.get("msg") or "").strip()
    elif payload:
        msg = payload.strip()[:120]
    hint = _ANIMETRACE_CODE_HINT.get(code) if isinstance(code, int) else None
    detail = " · ".join(b for b in (
        f"HTTP {status}" if status else "",
        f"code {code}" if code is not None else "",
        msg,
        hint or "",
    ) if b)
    return {"error": f"animetrace: {detail or '请求失败'}", "source": "animetrace"}


def _bgm_search_url(work: str) -> str:
    """作品检索入口：AnimeTrace 只给作品名/角色名，不给出处页。

    这里给的是 Bangumi 的作品检索页——「下一步去哪儿查证」的确定性入口，
    不是宣称这张图就出自该页。url 槽必须非空（serp_guard 会丢弃空 url 条目），
    而填一张不存在的原文页就是造假，故取检索页并如实标注。
    """
    return f"https://bgm.tv/subject_search/{urllib.parse.quote(work)}"


def _build_animetrace_engine(spec: dict[str, Any]) -> Any:
    """AnimeTrace 角色识别（免 key，POST JSON 传图片 URL）。"""
    timeout = spec.get("timeout", 20)
    model = str(spec.get("model") or "").strip()
    is_multi = 1 if spec.get("is_multi") else 0

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not _IMAGE_URL_RE.match(q):
            return _image_url_error("animetrace", q)
        body: dict[str, Any] = {"url": q}
        if is_multi:
            body["is_multi"] = 1
        if model:
            body["model"] = model
        req = urllib.request.Request(
            _ANIMETRACE_ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": "https://www.animetrace.com/",
                "Origin": "https://www.animetrace.com",
            },
        )
        try:
            with http_open(req, timeout=_timeout or timeout, engine=spec.get("_name", "")) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            payload = ""
            try:
                payload = e.read().decode("utf-8", errors="replace")[:400]
            except OSError:
                payload = ""
            return [_animetrace_error(e.code, payload)]
        except (urllib.error.URLError, TimeoutError, OSError):
            return []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        code = payload.get("code")
        if code not in (0, None):
            return [_animetrace_error(0, raw)]
        trace_id = str(payload.get("trace_id") or "")
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for box in payload.get("data") or []:
            if len(results) >= max(1, int(n)):
                break
            if not isinstance(box, dict):
                continue
            not_confident = bool(box.get("not_confident"))
            for char in box.get("character") or []:
                if not isinstance(char, dict):
                    continue
                work = str(char.get("work") or "").strip()
                name = str(char.get("character") or "").strip()
                if not work and not name:
                    continue
                key = (work, name)
                if key in seen:
                    continue
                seen.add(key)
                title = " · ".join(b for b in (work, name) if b) or "AnimeTrace 角色识别"
                bits = [b for b in (
                    f"角色 {name}" if name else "",
                    f"作品 {work}" if work else "",
                    "低置信（建议人工复核）" if not_confident else "",
                ) if b]
                results.append({
                    "title": title[:200],
                    "url": _bgm_search_url(work or name),
                    "snippet": " · ".join(bits)[:300],
                    "source": spec.get("_name") or "animetrace",
                    "score": rank_score(0.55 if not_confident else 0.8, len(results)),
                    "metadata": {
                        "work": work,
                        "character": name,
                        "not_confident": not_confident,
                        "box": box.get("box"),
                        "trace_id": trace_id,
                    },
                })
                if len(results) >= max(1, int(n)):
                    break
        return results
    return _engine
