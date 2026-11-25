"""按 ID 读取公开推文。每个通道一次 GET，按请求内的响应决定是否补读。"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error

import engine_states as st
from engines_base import note_failure
from fxtwitter_api import FxTwitterError, http_get

ENGINE = "twitter_syndication"
FX_SOURCE = f"{ENGINE}/fxtwitter"
_RESTRICTED = re.compile(r"private|protected|forbidden|unauthori[sz]ed|not found|does not exist|deleted|log[ -]?in|sign[ -]?in|not authorized|authentication required|tweet (?:is )?unavailable", re.I)


class ReadFailure(Exception):
    def __init__(self, reason, *, temporary=False, category=st.FAILURE_UPSTREAM, status=None):
        super().__init__(reason)
        self.temporary = temporary
        self.category = category
        self.status = status


def _request(url, headers, timeout):
    try:
        raw, code = http_get(url, headers=headers, timeout=timeout, max_retries=0)
    except FxTwitterError as exc:
        code = exc.code
        if code is None:
            network = isinstance(exc.__cause__, (OSError, urllib.error.URLError))
            raise ReadFailure("network failure" if network else "request refused", temporary=network,
                              category=st.FAILURE_NETWORK if network else st.FAILURE_BLOCKED) from exc
        if _RESTRICTED.search(str(exc)):
            raise ReadFailure(f"restricted response (HTTP {code})", category=st.FAILURE_BLOCKED, status=code) from exc
        raw = b""
    except http.client.HTTPException as exc:
        raise ReadFailure("incomplete response", temporary=True, category=st.FAILURE_NETWORK) from exc
    if code != 200:
        category = {401: st.FAILURE_AUTH, 403: st.FAILURE_BLOCKED, 429: st.FAILURE_RATE_LIMITED}
        raise ReadFailure(f"HTTP {code}", temporary=code in (408, 429) or 500 <= code < 600,
                          category=category.get(code, st.FAILURE_UPSTREAM), status=code)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        denied = _RESTRICTED.search(raw[:4096].decode("utf-8", errors="replace"))
        raise ReadFailure("restricted response" if denied else "invalid JSON", temporary=not denied) from None
    if data == {} or data is None:
        raise ReadFailure("empty response", temporary=True)
    if not isinstance(data, dict):
        raise ReadFailure("unexpected response shape")
    return data


def _result(data, tweet_id, source):
    fx = source == FX_SOURCE
    if fx:
        if data.get("code") not in (None, 200, "200") or data.get("error") or data.get("errors"):
            raise ReadFailure("FxTwitter rejected lookup")
        data = data.get("tweet")
    if not isinstance(data, dict) or data.get("__typename", "Tweet") != "Tweet" or data.get("errors") or data.get("error"):
        raise ReadFailure("tweet unavailable")
    author = data.get("author" if fx else "user") or {}
    if not isinstance(author, dict) or author.get("protected") or data.get("protected"):
        raise ReadFailure("author unavailable")
    if str(data.get("id" if fx else "id_str")) != tweet_id:
        raise ReadFailure("unverified tweet ID")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ReadFailure("missing tweet text")
    handle = author.get("screen_name") or ""
    if not isinstance(handle, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,15}", handle):
        handle = ""
    container = data.get("media" if fx else "entities")
    media = container.get("all" if fx else "media") if isinstance(container, dict) else None
    if not fx:
        media = data.get("mediaDetails") or media
    snippet = ["FxTwitter" if fx else "", data.get("created_at"), author.get("name"), text[:280]]
    if isinstance(media, list) and media:
        snippet.append(f"（含 {len(media)} 项媒体）")
    return [{"title": (f"@{handle}: {text[:80]}" if handle else text[:80])[:100],
             "url": f"https://x.com/{handle or 'i/web'}/status/{tweet_id}",
             "snippet": " | ".join(s for s in snippet if isinstance(s, str) and s),
             "source": source, "score": 0.9}]


def read_tweet(tweet_id, token, timeout):
    deadline = time.monotonic() + timeout
    primary = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token={token}"
    endpoints = [(ENGINE, primary, {"Referer": "https://platform.twitter.com/"}),
                 (FX_SOURCE, f"https://api.fxtwitter.com/status/{tweet_id}", {})]
    reasons = []
    for source, url, headers in endpoints:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadFailure("timeout", category=st.FAILURE_NETWORK)
            budget = remaining * 0.6 if source == ENGINE else remaining
            return _result(_request(url, headers, budget), tweet_id, source)
        except ReadFailure as exc:
            reasons.append(f"{source}: {exc}")
            if source == ENGINE and exc.temporary:
                continue
            note_failure(ENGINE, exc.category, "tweet-read-failed", "; ".join(reasons), status=exc.status)
            return []
