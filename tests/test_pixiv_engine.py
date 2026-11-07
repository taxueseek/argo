#!/usr/bin/env python3
"""Pixiv 双通道请求、结果归一与内容分级契约（不访问网络）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_pixiv as pixiv  # noqa: E402

_SPEC = {"_name": "pixiv", "image_license": "版权所有（Pixiv 作品，作者保留权利）"}
_REFRESH_ENV = "ARGO_PIXIV_REFRESH_TOKEN"
_R18_ENV = "ARGO_PIXIV_ALLOW_R18"


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _web_item(illust_id="151792368", *, x_restrict=0):
    return {
        "id": illust_id,
        "title": "测试作品",
        "url": (
            "https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/11/07/"
            f"20/30/09/{illust_id}_p0_square1200.jpg"
        ),
        "width": 5504,
        "height": 3072,
        "pageCount": 4,
        "userName": "测试作者",
        "createDate": "2026-11-07T20:30:09+09:00",
        "tags": ["初音ミク", "VOCALOID"],
        "xRestrict": x_restrict,
    }


def _app_item(illust_id="151792368", *, x_restrict=0):
    return {
        "id": int(illust_id),
        "title": "测试作品",
        "image_urls": {
            "large": f"https://i.pximg.net/img-original/img/2026/11/07/20/30/09/{illust_id}_p0.jpg"
        },
        "width": 5504,
        "height": 3072,
        "page_count": 4,
        "user": {"name": "测试作者"},
        "create_date": "2026-11-07T20:30:09+09:00",
        "tags": [{"name": "初音ミク"}, {"name": "VOCALOID"}],
        "x_restrict": x_restrict,
    }


def _web_payload(items, *, error=False):
    if error:
        return {"error": True, "message": "rate limited"}
    return {"error": False, "body": {"illustManga": {"data": items, "total": len(items)}}}


def _run_web(monkeypatch, items, query="hatsune miku", n=5):
    monkeypatch.delenv(_REFRESH_ENV, raising=False)
    monkeypatch.delenv(_R18_ENV, raising=False)
    monkeypatch.setattr(pixiv, "get_env", lambda name, default="": default)
    requests = []

    def fake_open(req, timeout, engine):
        requests.append((req, timeout, engine))
        return _FakeResp(json.dumps(_web_payload(items)).encode())

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    results = pixiv._build_pixiv_engine(_SPEC)(query, n)
    return requests, results


def _run_app(monkeypatch, items, query="初音ミク", n=5, **kwargs):
    monkeypatch.setattr(
        pixiv,
        "get_env",
        lambda name, default="": (
            "test-refresh-token"
            if name == _REFRESH_ENV
            else default
        ),
    )
    monkeypatch.setattr(pixiv, "_refresh_access_token", lambda token, timeout, engine: "test-access-token")
    monkeypatch.delenv(_R18_ENV, raising=False)
    requests = []

    def fake_open(req, timeout, engine):
        requests.append((req, timeout, engine))
        return _FakeResp(json.dumps({"illusts": items}).encode())

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    results = pixiv._build_pixiv_engine(_SPEC)(query, n, **kwargs)
    return requests, results


def test_query_is_percent_encoded_in_path_and_query(monkeypatch):
    requests, results = _run_web(monkeypatch, [_web_item()])
    assert len(results) == 1
    req = requests[0][0]
    parsed = urllib.parse.urlsplit(req.full_url)
    assert parsed.path.endswith("/hatsune%20miku")
    assert "hatsune miku" not in req.full_url
    assert urllib.parse.parse_qs(parsed.query)["word"] == ["hatsune miku"]


def test_web_preview_removes_crop_segment_and_keeps_dimensions(monkeypatch):
    _, results = _run_web(monkeypatch, [_web_item()])
    row = results[0]
    assert row["url"] == "https://www.pixiv.net/artworks/151792368"
    assert row["image_url"] == (
        "https://i.pximg.net/img-master/img/2026/11/07/20/30/09/"
        "151792368_p0_master1200.jpg"
    )
    assert (row["image_width"], row["image_height"]) == (5504, 3072)
    assert row["image_commercial_ok"] is False
    assert row["channel"] == "web"
    assert "is_r18" not in row


def test_web_business_error_returns_empty_results(monkeypatch):
    monkeypatch.setattr(pixiv, "get_env", lambda name, default="": default)

    def fake_open(req, timeout, engine):
        return _FakeResp(json.dumps(_web_payload([], error=True)).encode())

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    assert pixiv._build_pixiv_engine(_SPEC)("初音ミク", 5) == []


def test_web_request_count_is_bounded_to_one_page(monkeypatch):
    requests, results = _run_web(monkeypatch, [_web_item(str(i)) for i in range(60)], n=60)
    assert len(requests) == 1
    assert len(results) == 60


def test_app_channel_normalizes_fields_and_uses_bearer_header(monkeypatch):
    requests, results = _run_app(monkeypatch, [_app_item()])
    row = results[0]
    assert row["channel"] == "app"
    assert row["url"] == "https://www.pixiv.net/artworks/151792368"
    assert row["image_url"].endswith("151792368_p0.jpg")
    assert row["image_commercial_ok"] is False
    assert requests[0][0].get_header("Authorization") == "Bearer test-access-token"
    serialized = json.dumps(results, ensure_ascii=False)
    assert "test-access-token" not in serialized
    assert "test-refresh-token" not in serialized


def test_app_supports_search_target_dates_and_ai_filter(monkeypatch):
    requests, _ = _run_app(
        monkeypatch,
        [_app_item()],
        search_target="exact_match_for_tags",
        start_date="2026-01-01",
        end_date="2026-11-07",
        search_ai_type=0,
        sort="date_asc",
    )
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0][0].full_url).query)
    assert params["search_target"] == ["exact_match_for_tags"]
    assert params["start_date"] == ["2026-01-01"]
    assert params["end_date"] == ["2026-11-07"]
    assert params["search_ai_type"] == ["0"]
    assert params["sort"] == ["date_asc"]


def test_r18_is_filtered_by_default_on_both_channels(monkeypatch):
    _, web_results = _run_web(monkeypatch, [_web_item("1"), _web_item("2", x_restrict=1)])
    assert [row["url"] for row in web_results] == ["https://www.pixiv.net/artworks/1"]
    _, app_results = _run_app(monkeypatch, [_app_item("1"), _app_item("2", x_restrict=2)])
    assert [row["url"] for row in app_results] == ["https://www.pixiv.net/artworks/1"]


def test_r18_tag_is_filtered_from_snippet(monkeypatch):
    """R-18 作为标签名出现时不得进 snippet（bot 会复述标签）。即使内容放行，分级标签也要过滤。"""
    monkeypatch.setattr(
        pixiv,
        "env_flag",
        lambda name, default=True, **kwargs: name == _R18_ENV,
    )
    item = _app_item("2", x_restrict=1)
    item["tags"] = [{"name": "R-18"}, {"name": "初音ミク"}, {"name": "VOCALOID"}]
    _, results = _run_app(monkeypatch, [item])
    assert len(results) == 1
    assert "R-18" not in results[0]["snippet"]
    assert "初音ミク" in results[0]["snippet"]


def test_r18_requires_explicit_authorization(monkeypatch):
    monkeypatch.setattr(
        pixiv,
        "env_flag",
        lambda name, default=True, **kwargs: name == _R18_ENV,
    )
    _, results = _run_app(monkeypatch, [_app_item("2", x_restrict=1)])
    assert len(results) == 1
    assert "is_r18" not in results[0]
    assert "R-18" not in results[0]["snippet"]


def test_app_auth_error_is_visible_and_does_not_fall_back_to_web(monkeypatch):
    monkeypatch.setattr(pixiv, "get_env", lambda name, default="": "bad-token" if name == _REFRESH_ENV else default)
    monkeypatch.setattr(pixiv, "_refresh_access_token", lambda *args: "expired-access-token")
    calls = []

    def fake_open(req, timeout, engine):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    results = pixiv._build_pixiv_engine(_SPEC)("初音ミク", 5)
    assert len(calls) == 1
    assert calls[0].startswith(pixiv._APP_API)
    assert results[0]["source"] == "pixiv"
    assert "认证或权限不足" in results[0]["error"]


def test_oauth_success_uses_refresh_grant_and_never_returns_tokens(monkeypatch):
    pixiv._token_cache.update(refresh_token="", access_token="", expires_at=0.0)
    captured = {}

    def fake_open(req, timeout, engine):
        captured["url"] = req.full_url
        captured["body"] = urllib.parse.parse_qs(req.data.decode())
        return _FakeResp(json.dumps({"response": {"access_token": "new-access", "expires_in": 3600}}).encode())

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    token = pixiv._refresh_access_token("refresh-secret", 5, "pixiv")
    assert token == "new-access"
    assert captured["url"] == pixiv._OAUTH_API
    assert captured["body"]["grant_type"] == ["refresh_token"]
    assert captured["body"]["refresh_token"] == ["refresh-secret"]
    assert "username" not in captured["body"]
    assert "password" not in captured["body"]
    pixiv._token_cache.update(refresh_token="", access_token="", expires_at=0.0)


def test_oauth_failure_is_an_actionable_error(monkeypatch):
    pixiv._token_cache.update(refresh_token="", access_token="", expires_at=0.0)

    def fake_open(req, timeout, engine):
        raise urllib.error.HTTPError(req.full_url, 400, "bad grant", {}, None)

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    with pytest.raises(ValueError, match="更新 refresh token"):
        pixiv._refresh_access_token("expired-refresh", 5, "pixiv")


@pytest.mark.parametrize(
    "raw_url",
    [
        "",
        "http://i.pximg.net/img/a_square1200.jpg",
        "https://example.com/a.jpg",
        "https://i.pximg.net/c//a_square1200.jpg",
        "https://i.pximg.net/c/250x250_80_a2/_square1200.jpg",
    ],
)
def test_preview_url_rejects_non_canonical_or_insecure_input(raw_url):
    assert pixiv._web_preview_url(raw_url) == ""
