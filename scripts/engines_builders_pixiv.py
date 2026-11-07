#!/usr/bin/env python3
"""Pixiv 公开作品检索：关键词搜索与画师作品列表，匿名 Web 与 OAuth App API 双通道。

配置 refresh token 后只走 App API，避免标签、日期、AI 过滤或内容分级在
通道切换时静默改变。Web 与 App 接口均无公开稳定性承诺；结果标出实际
取数通道。R-18 是部署级内容边界，默认拒绝，只有显式授权才放行。
画师作品列表只有 App 接口能给出可用的图片直链，匿名通道明确返回提示。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from engine_env import env_flag, get_env
from engines_base import finalize_image_fields, http_open, rank_score, safe_search


class _LazyLogger:
    """延迟创建 logger，避免 import logging 的重量级 import 链。"""

    _logger = None

    def __getattr__(self, name: str):
        if _LazyLogger._logger is None:
            import logging

            _LazyLogger._logger = logging.getLogger("unified_search.engines")
            if not _LazyLogger._logger.handlers:
                _LazyLogger._logger.setLevel(logging.WARNING)
                _LazyLogger._logger.addHandler(logging.StreamHandler(sys.stderr))
        return getattr(_LazyLogger._logger, name)


logger = _LazyLogger()

_WEB_API = "https://www.pixiv.net/ajax/search/artworks"
_APP_API = "https://app-api.pixiv.net/v1/search/illust"
_APP_USER_ILLUSTS_API = "https://app-api.pixiv.net/v1/user/illusts"
_APP_SEARCH_USER_API = "https://app-api.pixiv.net/v1/search/user"
_OAUTH_API = "https://oauth.secure.pixiv.net/auth/token"
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_APP_HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "14.6",
    "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
}
# Pixiv App 的公共客户端参数与请求签名盐，协议参考 pixivpy（Unlicense）。
_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
_HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
_WEB_PAGE_SIZE = 60
_APP_PAGE_SIZE = 30
_ACCESS_TOKEN_TTL = 3000.0
_ACCESS_TOKEN_EARLY_REFRESH = 120.0

# 进程内只缓存短期 access token；refresh token 每次从环境读取，支持热更新。
_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"refresh_token": "", "access_token": "", "expires_at": 0.0}


def _error(message: str, engine: str) -> list[dict[str, Any]]:
    return [{"error": message, "source": engine}]


# 标签匹配口径：App 与 Web 用两套枚举名，统一到一套对调用方可见的取值。
_SEARCH_TARGETS = {
    "partial_match_for_tags": "s_tag",
    "exact_match_for_tags": "s_tag_full",
    "title_and_caption": "s_tc",
}


def _search_target(spec: dict[str, Any], kwargs: dict[str, Any]) -> str:
    target = str(
        kwargs.get("search_target") or spec.get("search_target") or "partial_match_for_tags"
    )
    return target if target in _SEARCH_TARGETS else "partial_match_for_tags"


def _search_url(query: str, *, order: str, target: str) -> str:
    """Web 搜索要求关键词进入路径；动态部分统一按 URL 组件编码。"""
    encoded = urllib.parse.quote(query, safe="")
    params = urllib.parse.urlencode(
        {
            "word": query,
            "p": 1,
            "order": order,
            "mode": "all",
            "lang": "zh",
            "s_mode": _SEARCH_TARGETS.get(target, "s_tag"),
        }
    )
    return f"{_WEB_API}/{encoded}?{params}"


def _web_preview_url(raw_url: str) -> str:
    """把上游的方形裁剪地址换成保持作品宽高比的 master1200 预览图。"""
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme != "https" or parsed.hostname != "i.pximg.net":
        return ""
    path = parsed.path
    if not path.endswith("_square1200.jpg"):
        return ""
    parts = path.split("/")
    if len(parts) < 5 or parts[1] != "c" or not parts[2] or not parts[-1] or not parts[3]:
        return ""
    preview_path = "/" + "/".join(parts[3:])
    preview_path = preview_path[: -len("_square1200.jpg")] + "_master1200.jpg"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, preview_path, "", ""))


def _app_image_url(item: dict[str, Any]) -> str:
    images = item.get("image_urls")
    if not isinstance(images, dict):
        return ""
    for key in ("large", "medium"):
        raw = str(images.get(key) or "").strip()
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme == "https" and parsed.hostname == "i.pximg.net" and parsed.path:
            return raw
    return ""


def _result_row(
    item: dict[str, Any], *, channel: str, engine: str, index: int, allow_r18: bool
) -> dict[str, Any] | None:
    """把 Web/App 的作品条目归一到同一份 Argo 图片结果契约。"""
    restriction = item.get("xRestrict" if channel == "web" else "x_restrict")
    try:
        is_r18 = int(restriction or 0) != 0
    except (TypeError, ValueError):
        return None
    if is_r18 and not allow_r18:
        return None
    illust_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    image_url = (
        _web_preview_url(str(item.get("url") or "").strip())
        if channel == "web"
        else _app_image_url(item)
    )
    if not illust_id.isdigit() or not title or not image_url:
        return None
    if channel == "web":
        author = str(item.get("userName") or "").strip()
        created = str(item.get("createDate") or "")[:64]
        page_count = item.get("pageCount")
    else:
        user = item.get("user")
        author = str(user.get("name") or "").strip() if isinstance(user, dict) else ""
        created = str(item.get("create_date") or "")[:64]
        page_count = item.get("page_count")
    tags = []
    for tag in item.get("tags") or []:
        name = str(tag.get("name") or "").strip() if isinstance(tag, dict) else str(tag).strip()
        # R-18 是分级标签不是主题标签，不应进 snippet（bot 会复述）。
        if name and name.upper() not in {"R-18", "R-18G"}:
            tags.append(name)
    snippet_bits = [author and f"作者 {author}"]
    if page_count:
        snippet_bits.append(f"{page_count} 页")
    if tags:
        snippet_bits.append("标签 " + " / ".join(tags[:6]))
    return {
        "title": title[:200],
        "url": f"https://www.pixiv.net/artworks/{illust_id}",
        "snippet": " · ".join(bit for bit in snippet_bits if bit)[:300],
        "source": engine,
        "channel": channel,
        "score": rank_score(0.8, index),
        "published_at": created,
        "image_url": image_url,
        # 上游宽高描述作品原图；预览图保持同一宽高比。
        "image_width": item.get("width"),
        "image_height": item.get("height"),
    }


def _web_search(
    query: str, n: int, timeout: float, spec: dict[str, Any], target: str
) -> list[dict[str, Any]]:
    engine = spec.get("_name", "pixiv")
    order = str(spec.get("order") or "date")
    if order not in {"date", "popular"}:
        order = "date"
    req = urllib.request.Request(
        _search_url(query, order=order, target=target),
        headers={
            "User-Agent": _WEB_UA,
            "Accept": "application/json",
            "Referer": "https://www.pixiv.net/",
        },
    )
    try:
        with http_open(req, timeout=timeout, engine=engine) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning(f"Pixiv Web 检索失败: {exc}")
        return []
    if not isinstance(payload, dict) or payload.get("error") is not False:
        logger.warning("Pixiv Web 返回业务错误或响应结构变化")
        return []
    body = payload.get("body")
    section = body.get("illustManga") if isinstance(body, dict) else None
    items = section.get("data") if isinstance(section, dict) else None
    if not isinstance(items, list):
        logger.warning("Pixiv Web 响应缺少 illustManga.data")
        return []
    allow_r18 = env_flag("ARGO_PIXIV_ALLOW_R18", default=False, expand=False, strict=True)
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _result_row(item, channel="web", engine=engine, index=len(results), allow_r18=allow_r18)
        if row is not None:
            results.append(row)
        if len(results) >= min(n, _WEB_PAGE_SIZE):
            break
    return results


def _refresh_access_token(refresh_token: str, timeout: float, engine: str) -> str:
    """用 refresh token 换短期 access token；失败只给行动提示，不回显密钥。"""
    now = time.monotonic()
    with _token_lock:
        if (
            _token_cache["refresh_token"] == refresh_token
            and _token_cache["access_token"]
            and now < _token_cache["expires_at"]
        ):
            return str(_token_cache["access_token"])
        client_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        client_hash = hashlib.md5((client_time + _HASH_SECRET).encode("utf-8")).hexdigest()
        body = urllib.parse.urlencode(
            {
                "get_secure_url": 1,
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            _OAUTH_API,
            data=body,
            method="POST",
            headers={
                **_APP_HEADERS,
                "X-Client-Time": client_time,
                "X-Client-Hash": client_hash,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with http_open(req, timeout=timeout, engine=engine) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ValueError("Pixiv OAuth 认证失败，请更新 refresh token") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ValueError("Pixiv OAuth 暂时不可用，请检查网络后重试") from exc
        response = payload.get("response") if isinstance(payload, dict) else None
        access_token = str(response.get("access_token") or "").strip() if isinstance(response, dict) else ""
        if not access_token:
            raise ValueError("Pixiv OAuth 未返回 access token，请更新 refresh token")
        expires_in = response.get("expires_in") if isinstance(response, dict) else None
        try:
            ttl = max(60.0, float(expires_in) - _ACCESS_TOKEN_EARLY_REFRESH)
        except (TypeError, ValueError):
            ttl = _ACCESS_TOKEN_TTL
        _token_cache.update(
            refresh_token=refresh_token,
            access_token=access_token,
            expires_at=now + ttl,
        )
        return access_token


def _app_params(query: str, spec: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    target = _search_target(spec, kwargs)
    sort = str(kwargs.get("sort") or spec.get("sort") or "date_desc")
    if sort not in {"date_desc", "date_asc", "popular_desc"}:
        sort = "date_desc"
    params: dict[str, Any] = {
        "word": query,
        "search_target": target,
        "sort": sort,
        "filter": "for_ios",
    }
    for key in ("start_date", "end_date"):
        value = str(kwargs.get(key) or spec.get(key) or "").strip()
        if value:
            params[key] = value
    ai_type = kwargs.get("search_ai_type", spec.get("search_ai_type"))
    if ai_type in (0, 1, "0", "1"):
        params["search_ai_type"] = int(ai_type)
    return params


def _app_search(
    query: str, n: int, timeout: float, spec: dict[str, Any], kwargs: dict[str, Any]
) -> list[dict[str, Any]]:
    engine = spec.get("_name", "pixiv")
    refresh_token = get_env("ARGO_PIXIV_REFRESH_TOKEN").strip()
    try:
        access_token = _refresh_access_token(refresh_token, timeout, engine)
    except ValueError as exc:
        return _error(str(exc), engine)
    url = f"{_APP_API}?{urllib.parse.urlencode(_app_params(query, spec, kwargs))}"
    req = urllib.request.Request(
        url,
        headers={**_APP_HEADERS, "Accept": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    try:
        with http_open(req, timeout=timeout, engine=engine) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        logger.warning(f"Pixiv App HTTP 错误: {exc.code}")
        if exc.code in (400, 401, 403):
            return _error("Pixiv App 认证或权限不足，请检查 refresh token 与账号状态", engine)
        return _error(f"Pixiv App 请求失败（HTTP {exc.code}）", engine)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning(f"Pixiv App 检索失败: {type(exc).__name__}")
        return _error("Pixiv App 暂时不可用，请检查网络后重试", engine)
    items = payload.get("illusts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.warning("Pixiv App 响应缺少 illusts")
        return _error("Pixiv App 返回了无法识别的响应，请稍后重试", engine)
    allow_r18 = env_flag("ARGO_PIXIV_ALLOW_R18", default=False, expand=False, strict=True)
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _result_row(item, channel="app", engine=engine, index=len(results), allow_r18=allow_r18)
        if row is not None:
            results.append(row)
        if len(results) >= min(n, _APP_PAGE_SIZE):
            break
    return results


_USER_URL_RE = re.compile(
    r"^https?://(?:www\.)?pixiv\.net/users/(\d+)(?:[/?#].*)?$", re.IGNORECASE
)
_ARTIST_ID_RE = re.compile(
    r"^(?:user|artist|画师|画師)\s*[:：]\s*(\d+)$", re.IGNORECASE
)
_ARTIST_NAME_RE = re.compile(r"^(?:artist|画师|画師)\s*[:：]\s*(\S.*)$", re.IGNORECASE)
_BARE_ARTIST_ID_RE = re.compile(r"^\d{5,9}$")


def _artist_target(query: str) -> tuple[int | None, str]:
    """识别画师作品查询，返回 (画师 id, 画师名)；不是画师查询时返回 (None, "")。"""
    text = (query or "").strip()
    match = _USER_URL_RE.match(text)
    if match:
        return int(match.group(1)), ""
    match = _ARTIST_ID_RE.match(text)
    if match:
        return int(match.group(1)), ""
    match = _ARTIST_NAME_RE.match(text)
    if match:
        return None, match.group(1).strip()
    if _BARE_ARTIST_ID_RE.match(text):
        return int(text), ""
    return None, ""


def _app_json(
    url: str, access_token: str, timeout: float, engine: str
) -> tuple[dict[str, Any] | None, str]:
    """请求 App 接口，把网络与业务错误统一成 (None, 可读文案)。"""
    req = urllib.request.Request(
        url,
        headers={
            **_APP_HEADERS,
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    try:
        with http_open(req, timeout=timeout, engine=engine) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        logger.warning(f"Pixiv App HTTP 错误: {exc.code}")
        if exc.code in (400, 401, 403):
            return None, "Pixiv App 认证或权限不足，请检查 refresh token 与账号状态"
        return None, f"Pixiv App 请求失败（HTTP {exc.code}）"
    except (urllib.error.URLError, OSError, ValueError):
        logger.warning("Pixiv App 请求失败")
        return None, "Pixiv App 暂时不可用，请检查网络后重试"
    if not isinstance(payload, dict):
        return None, "Pixiv App 返回了无法识别的响应，请稍后重试"
    return payload, ""


def _app_resolve_artist_id(
    name: str, access_token: str, timeout: float, engine: str
) -> tuple[int | None, str]:
    """按画师名查账号 id，取搜索结果的第一位。"""
    url = f"{_APP_SEARCH_USER_API}?{urllib.parse.urlencode({'word': name, 'filter': 'for_ios'})}"
    payload, error = _app_json(url, access_token, timeout, engine)
    if error:
        return None, error
    previews = payload.get("user_previews") if payload else None
    if not isinstance(previews, list):
        return None, "Pixiv App 返回了无法识别的响应，请稍后重试"
    for entry in previews:
        user = entry.get("user") if isinstance(entry, dict) else None
        user_id = str(user.get("id") or "").strip() if isinstance(user, dict) else ""
        if user_id.isdigit():
            return int(user_id), ""
    return None, f"没有找到匹配的画师：{name}"


def _app_artist_works(
    artist_id: int,
    n: int,
    timeout: float,
    engine: str,
    access_token: str,
    allow_r18: bool,
) -> list[dict[str, Any]]:
    """拉取画师作品列表；R-18 被过滤后条数不足时最多再翻一页。"""
    results: list[dict[str, Any]] = []
    offset = 0
    for _ in range(2):
        params = {
            "user_id": artist_id,
            "type": "illust",
            "filter": "for_ios",
            "offset": offset,
        }
        url = f"{_APP_USER_ILLUSTS_API}?{urllib.parse.urlencode(params)}"
        payload, error = _app_json(url, access_token, timeout, engine)
        if error:
            return results or _error(error, engine)
        items = payload.get("illusts") if payload else None
        if not isinstance(items, list):
            return results or _error(
                "Pixiv App 返回了无法识别的响应，请稍后重试", engine
            )
        for item in items:
            if not isinstance(item, dict):
                continue
            row = _result_row(
                item,
                channel="app",
                engine=engine,
                index=len(results),
                allow_r18=allow_r18,
            )
            if row is not None:
                results.append(row)
            if len(results) >= n:
                break
        if len(results) >= n or not items or not payload.get("next_url"):
            break
        offset += len(items)
    if not results:
        return _error("该画师的作品里没有可展示的条目（可能都被内容分级过滤）", engine)
    return results[:n]


def _artist_search(
    artist_id: int | None,
    artist_name: str,
    n: int,
    timeout: float,
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    engine = spec.get("_name", "pixiv")
    refresh_token = get_env("ARGO_PIXIV_REFRESH_TOKEN").strip()
    if not refresh_token:
        return _error(
            "按画师浏览作品需要登录凭证（Pixiv App 接口），当前是匿名模式，请改用关键词搜索",
            engine,
        )
    try:
        access_token = _refresh_access_token(refresh_token, timeout, engine)
    except ValueError as exc:
        return _error(str(exc), engine)
    if artist_name:
        resolved, error = _app_resolve_artist_id(
            artist_name, access_token, timeout, engine
        )
        if error:
            return _error(error, engine)
        artist_id = resolved
    if not artist_id:
        return _error("没有找到匹配的画师", engine)
    allow_r18 = env_flag(
        "ARGO_PIXIV_ALLOW_R18", default=False, expand=False, strict=True
    )
    return _app_artist_works(artist_id, n, timeout, engine, access_token, allow_r18)


def _build_pixiv_engine(spec: dict[str, Any]) -> Any:
    timeout = float(spec.get("timeout", 20))

    @safe_search
    def _engine(
        query: str, n: int = 5, _timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, int(n))
        to = _timeout or timeout
        artist_id, artist_name = _artist_target(q)
        if artist_id or artist_name:
            results = _artist_search(artist_id, artist_name, limit, to, spec)
        elif get_env("ARGO_PIXIV_REFRESH_TOKEN").strip():
            results = _app_search(q, limit, to, spec, kwargs)
        else:
            results = _web_search(q, limit, to, spec, _search_target(spec, kwargs))
        return finalize_image_fields(results, spec.get("image_license") or "")

    return _engine
