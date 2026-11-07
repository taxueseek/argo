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


def _run_web(monkeypatch, items, query="hatsune miku", n=5, **kwargs):
    monkeypatch.delenv(_REFRESH_ENV, raising=False)
    monkeypatch.delenv(_R18_ENV, raising=False)
    monkeypatch.setattr(pixiv, "get_env", lambda name, default="": default)
    requests = []

    def fake_open(req, timeout, engine):
        requests.append((req, timeout, engine))
        return _FakeResp(json.dumps(_web_payload(items)).encode())

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    results = pixiv._build_pixiv_engine(_SPEC)(query, n, **kwargs)
    return requests, results


def _detail_payload(illust_id, *, pages=None):
    illust = {"id": int(illust_id), "title": "测试作品"}
    if pages:
        illust["meta_pages"] = [
            {
                "image_urls": {
                    "original": (
                        "https://i.pximg.net/img-original/img/2026/11/07/20/30/09/"
                        f"{illust_id}_p{index}.jpg"
                    )
                }
            }
            for index in range(pages)
        ]
    else:
        illust["meta_single_page"] = {
            "original_image_url": (
                "https://i.pximg.net/img-original/img/2026/11/07/20/30/09/"
                f"{illust_id}_p0.jpg"
            )
        }
    return {"illust": illust}


def _run_app(monkeypatch, items, query="初音ミク", n=5, detail=True, **kwargs):
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
        url = req.full_url
        if "/v1/illust/detail" in url:
            if not detail:
                return _FakeResp(json.dumps({"error": True}).encode())
            illust_id = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["illust_id"][0]
            return _FakeResp(json.dumps(_detail_payload(illust_id)).encode())
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


def test_web_maps_search_target_to_s_mode(monkeypatch):
    requests, _ = _run_web(monkeypatch, [_web_item()])
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0][0].full_url).query)
    assert params["s_mode"] == ["s_tag"]

    strict, _ = _run_web(monkeypatch, [_web_item()], search_target="exact_match_for_tags")
    strict_params = urllib.parse.parse_qs(
        urllib.parse.urlsplit(strict[0][0].full_url).query
    )
    assert strict_params["s_mode"] == ["s_tag_full"]


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


def test_app_results_prefer_original_and_keep_preview(monkeypatch):
    """App 结果取作品详情里的原图直链，large 档留在 image_url_preview 供体积超限时回退。"""
    item = _app_item("151792368")
    item["image_urls"]["large"] = (
        "https://i.pximg.net/c/600x1200_90/img-master/img/2026/11/07/20/30/09/"
        "151792368_p0_master1200.jpg"
    )
    requests, results = _run_app(monkeypatch, [item])
    row = results[0]
    assert row["image_url"] == (
        "https://i.pximg.net/img-original/img/2026/11/07/20/30/09/151792368_p0.jpg"
    )
    assert row["image_url_preview"].endswith("151792368_p0_master1200.jpg")
    assert row["image_url_preview"] != row["image_url"]
    detail = [url for url, _ in ((req.full_url, t) for req, t, _ in requests) if "/v1/illust/detail" in url]
    assert detail and "illust_id=151792368" in detail[0]


def test_app_keeps_large_when_detail_fails(monkeypatch):
    _, results = _run_app(monkeypatch, [_app_item("151792368")], detail=False)
    row = results[0]
    assert row["image_url"].endswith("151792368_p0.jpg")
    assert "image_url_preview" not in row


def test_detail_is_requested_once_per_result(monkeypatch):
    requests, results = _run_app(
        monkeypatch, [_app_item("151792368"), _app_item("151792369")], n=5
    )
    assert len(results) == 2
    detail = [req.full_url for req, _, _ in requests if "/v1/illust/detail" in req.full_url]
    assert len(detail) == 2
    assert {urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["illust_id"][0] for url in detail} == {
        "151792368",
        "151792369",
    }


@pytest.mark.parametrize(
    "illust,expected",
    [
        (
            {"meta_single_page": {"original_image_url": "https://i.pximg.net/a_p0.png"}},
            "https://i.pximg.net/a_p0.png",
        ),
        (
            {
                "meta_pages": [
                    {"image_urls": {"original": "https://i.pximg.net/b_p0.jpg"}},
                    {"image_urls": {"original": "https://i.pximg.net/b_p1.jpg"}},
                ]
            },
            "https://i.pximg.net/b_p0.jpg",
        ),
        (
            {"meta_single_page": {"original_image_url": "http://i.pximg.net/c.jpg"}},
            "",
        ),
        (
            {"meta_single_page": {"original_image_url": "https://example.com/c.jpg"}},
            "",
        ),
        ({}, ""),
    ],
)
def test_original_url_from_detail(illust, expected):
    assert pixiv._original_url_from_detail(illust) == expected


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

@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("user:29456671", (29456671, "", [])),
        ("画师：29456671", (29456671, "", [])),
        ("https://www.pixiv.net/users/29456671", (29456671, "", [])),
        ("https://www.pixiv.net/users/29456671/artworks", (29456671, "", [])),
        ("29456671", (29456671, "", [])),
        ("画师:米山舞", (None, "米山舞", [])),
        ("artist:米山舞", (None, "米山舞", [])),
        ("画师:ke-ta フランドール", (None, "ke-ta", ["フランドール"])),
        ("user:29456671 白タイツ", (29456671, "", ["白タイツ"])),
        (
            "https://www.pixiv.net/users/29456671 白タイツ 制服",
            (29456671, "", ["白タイツ", "制服"]),
        ),
        ("初音ミク", (None, "", [])),
        ("1234", (None, "", [])),
    ],
)
def test_artist_target_recognizes_query_forms(query, expected):
    assert pixiv._artist_target(query) == expected


def _run_artist(monkeypatch, routes, query, n=5):
    monkeypatch.setattr(
        pixiv,
        "get_env",
        lambda name, default="": (
            "test-refresh-token" if name == _REFRESH_ENV else default
        ),
    )
    monkeypatch.setattr(
        pixiv,
        "_refresh_access_token",
        lambda token, timeout, engine: "test-access-token",
    )
    monkeypatch.delenv(_R18_ENV, raising=False)
    requests = []

    def fake_open(req, timeout, engine):
        requests.append(req.full_url)
        for fragment, payload in routes:
            if fragment in req.full_url:
                return _FakeResp(json.dumps(payload).encode())
        if "/v1/illust/detail" in req.full_url:
            illust_id = urllib.parse.parse_qs(
                urllib.parse.urlsplit(req.full_url).query
            )["illust_id"][0]
            return _FakeResp(json.dumps(_detail_payload(illust_id)).encode())
        raise AssertionError(f"未覆盖的请求: {req.full_url}")

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    results = pixiv._build_pixiv_engine(_SPEC)(query, n)
    return requests, results


def test_artist_works_are_listed_by_user_id(monkeypatch):
    routes = [
        (
            "/v1/user/illusts",
            {"illusts": [_app_item("151792368"), _app_item("151792369")]},
        )
    ]
    requests, results = _run_artist(monkeypatch, routes, "user:29456671", n=2)
    listing = [url for url in requests if "/v1/user/illusts" in url]
    assert len(listing) == 1
    assert "user_id=29456671" in listing[0]
    assert len(results) == 2
    assert results[0]["channel"] == "app"
    assert results[0]["url"] == "https://www.pixiv.net/artworks/151792368"
    assert results[0]["snippet"].startswith("作者 测试作者")


def test_artist_name_is_resolved_then_listed(monkeypatch):
    routes = [
        (
            "/v1/search/user",
            {"user_previews": [{"user": {"id": 1554775, "name": "米山舞"}}]},
        ),
        ("/v1/user/illusts", {"illusts": [_app_item()]}),
    ]
    requests, results = _run_artist(monkeypatch, routes, "画师:米山舞", n=5)
    listing = [url for url in requests if "/v1/illust/detail" not in url]
    assert len(listing) == 2
    assert "word=" in listing[0] and "%E7%B1%B3%E5%B1%B1%E8%88%9E" in listing[0]
    assert "user_id=1554775" in listing[1]
    assert len(results) == 1


def test_unknown_artist_name_is_an_actionable_error(monkeypatch):
    routes = [("/v1/search/user", {"user_previews": []})]
    _, results = _run_artist(monkeypatch, routes, "画师:查无此人", n=5)
    assert len(results) == 1
    assert "没有找到匹配的画师：查无此人" in results[0]["error"]


def test_artist_mode_requires_login_on_anonymous_channel(monkeypatch):
    monkeypatch.setattr(pixiv, "get_env", lambda name, default="": default)
    monkeypatch.delenv(_R18_ENV, raising=False)
    results = pixiv._build_pixiv_engine(_SPEC)("user:29456671", 5)
    assert len(results) == 1
    assert "登录凭证" in results[0]["error"]


def test_artist_works_filter_r18_by_default(monkeypatch):
    items = [_app_item("151792368", x_restrict=1), _app_item("151792369", x_restrict=0)]
    routes = [("/v1/user/illusts", {"illusts": items})]
    _, results = _run_artist(monkeypatch, routes, "user:29456671", n=5)
    assert [row["url"] for row in results] == [
        "https://www.pixiv.net/artworks/151792369"
    ]


def test_artist_works_page_again_when_filtered_short(monkeypatch):
    first = [_app_item("151792368", x_restrict=1)]
    second = [_app_item("151792369", x_restrict=0)]
    calls = {"n": 0}

    monkeypatch.setattr(
        pixiv,
        "get_env",
        lambda name, default="": (
            "test-refresh-token" if name == _REFRESH_ENV else default
        ),
    )
    monkeypatch.setattr(
        pixiv,
        "_refresh_access_token",
        lambda token, timeout, engine: "test-access-token",
    )
    monkeypatch.delenv(_R18_ENV, raising=False)

    def fake_open(req, timeout, engine):
        if "/v1/illust/detail" in req.full_url:
            illust_id = urllib.parse.parse_qs(
                urllib.parse.urlsplit(req.full_url).query
            )["illust_id"][0]
            return _FakeResp(json.dumps(_detail_payload(illust_id)).encode())
        calls["n"] += 1
        if "offset=0" in req.full_url:
            return _FakeResp(
                json.dumps({"illusts": first, "next_url": "next"}).encode()
            )
        return _FakeResp(json.dumps({"illusts": second}).encode())

    monkeypatch.setattr(pixiv, "http_open", fake_open)
    results = pixiv._build_pixiv_engine(_SPEC)("user:29456671", 5)
    assert calls["n"] == 2
    assert [row["url"] for row in results] == [
        "https://www.pixiv.net/artworks/151792369"
    ]


def test_artist_works_keep_only_items_matching_tag_filters(monkeypatch):
    flandre = _app_item("151792368")
    flandre["tags"] = [{"name": "東方Project"}, {"name": "フランドール・スカーレット"}]
    miku = _app_item("151792369")
    routes = [
        (
            "/v1/search/user",
            {"user_previews": [{"user": {"id": 3104565, "name": "ke-ta"}}]},
        ),
        ("/v1/user/illusts", {"illusts": [miku, flandre]}),
    ]
    _, results = _run_artist(monkeypatch, routes, "画师:ke-ta フランドール", n=5)
    assert [row["url"] for row in results] == [
        "https://www.pixiv.net/artworks/151792368"
    ]


def test_artist_tag_filter_reports_missing_tags(monkeypatch):
    routes = [("/v1/user/illusts", {"illusts": [_app_item("151792368")]})]
    _, results = _run_artist(monkeypatch, routes, "user:29456671 白タイツ", n=5)
    assert len(results) == 1
    assert "没有同时带这些标签的条目" in results[0]["error"]
    assert "白タイツ" in results[0]["error"]
