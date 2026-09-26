#!/usr/bin/env python3
"""test_tineye_engine.py — TinEye 反向图片搜索引擎回归测试。

argo 此前零反搜图能力，本文件锁定四条契约：
  1. 用法门槛：query 不是 http(s) URL → 带提示的 error 记录（必须可见，
     让模型知道这引擎要喂图片 URL），且 error 记录不带 url/title（不污染结果集）
  2. matches 解析：url=原页面 backlink、snippet=相似度/域名/尺寸组合、
     score 按 TinEye 相似度归一后序位衰减
  3. 无 backlink 的 match 跳过；同页去重
  4. 空 matches / 坏 JSON / 网络失败 → 诚实空列表

打桩点打在读取处（ebt._http_get_raw），fixture 离线，无真实网络。
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_tech as ebt  # noqa: E402

_FIXTURE = {
    "query_msec": 122,
    "total_results": 3,
    "matches": [
        {
            "image_url": "https://cdn.example.com/img/logo.png",
            "domain": "example.com",
            "score": 96.5,
            "width": 272,
            "height": 92,
            "backlinks": [
                {"url": "https://cdn.example.com/img/logo.png",
                 "backlink": "https://example.com/about",
                 "crawl_date": "2026-01-02"},
                {"url": "https://cdn.example.com/img/logo.png",
                 "backlink": "https://example.com/older-page",
                 "crawl_date": "2024-05-01"},
            ],
        },
        {
            "image_url": "https://cdn.example.com/img/logo.png",
            "domain": "mirror.org",
            "score": 88.0,
            "width": 272,
            "height": 92,
            "backlinks": [
                {"url": "https://cdn.example.com/img/logo.png",
                 "backlink": "https://mirror.org/logo-history",
                 "crawl_date": "2025-11-30"},
            ],
        },
        {
            # 无 backlink 的 match：没有可核验页面，必须跳过
            "image_url": "https://cdn.example.com/img/orphan.png",
            "domain": "dead.io",
            "score": 70.0,
            "backlinks": [],
        },
    ],
}

IMAGE_URL = "https://cdn.example.com/img/logo.png"


@pytest.fixture()
def engine(monkeypatch):
    monkeypatch.setattr(ebt, "_http_get_raw",
                        lambda u, h, t, engine=None: json.dumps(_FIXTURE))
    return ebt._build_tineye_engine({})


class TestUsageGuard:
    """query 必须是图片 URL；关键词文本要被明确打回并提示用法。"""

    def test_non_url_query_returns_hint_error(self, engine):
        rs = engine("一只橙色猫的图片", n=5)
        assert len(rs) == 1
        assert "error" in rs[0]
        assert rs[0]["source"] == "tineye"
        assert "http" in rs[0]["error"]
        # 不污染结果集：融合与去重只看带 url/title 的 goods
        assert "url" not in rs[0]
        assert "title" not in rs[0]

    def test_non_http_scheme_rejected(self, engine):
        rs = engine("ftp://example.com/cat.png", n=5)
        assert "error" in rs[0]

    def test_image_url_fully_encoded_in_request(self, monkeypatch):
        seen = {}

        def fake(url, headers, timeout, engine=None):
            seen["url"] = url
            return json.dumps(_FIXTURE)

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        ebt._build_tineye_engine({})("https://a.io/p?x=1&b=2", n=3)
        # ://?& 必须整体编码，否则会被当成请求参数边界
        assert "url=https%3A%2F%2Fa.io%2Fp%3Fx%3D1%26b%3D2" in seen["url"]


class TestMatchParsing:
    def test_url_is_page_backlink(self, engine):
        rs = engine(IMAGE_URL, n=5)
        assert rs[0]["url"] == "https://example.com/about"

    def test_snippet_is_similarity_domain_size_combo(self, engine):
        s = engine(IMAGE_URL, n=5)[0]["snippet"]
        assert "96.5%" in s
        assert "example.com" in s
        assert "272x92" in s

    def test_order_follows_upstream_with_score_decay(self, engine):
        rs = engine(IMAGE_URL, n=5)
        # 上游相关性顺序保持（无 backlink 的 match 已剔除）
        assert [r["url"] for r in rs] == [
            "https://example.com/about", "https://mirror.org/logo-history"]
        assert rs[0]["score"] > rs[1]["score"]
        # 相似度归一到 0-1
        assert 0 < rs[0]["score"] <= 1.0

    def test_first_backlink_of_match_used(self, engine):
        rs = engine(IMAGE_URL, n=5)
        assert rs[0]["url"] == "https://example.com/about"
        assert rs[0]["url"] != "https://example.com/older-page"

    def test_backlinkless_match_skipped(self, engine):
        rs = engine(IMAGE_URL, n=5)
        assert all("dead.io" not in (r["snippet"] + r["title"]) for r in rs)

    def test_metadata_keeps_upstream_fields(self, engine):
        r = engine(IMAGE_URL, n=1)[0]
        md = r["metadata"]
        assert md["similarity"] == 96.5
        assert md["image_url"] == IMAGE_URL
        assert md["domain"] == "example.com"
        assert md["width"] == 272 and md["height"] == 92
        assert md["crawl_date"] == "2026-01-02"

    def test_n_caps_results(self, engine):
        assert len(engine(IMAGE_URL, n=1)) == 1


class TestHonestEmpty:
    """失败与无结果必须是空列表，不许伪装成成功。"""

    def test_empty_matches(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: '{"matches": []}')
        assert ebt._build_tineye_engine({})(IMAGE_URL, n=5) == []

    def test_bad_json(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: "not-json")
        assert ebt._build_tineye_engine({})(IMAGE_URL, n=5) == []

    def test_http_failure(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: None)
        assert ebt._build_tineye_engine({})(IMAGE_URL, n=5) == []

    def test_matches_missing_key(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: '{"error": "blocked"}')
        assert ebt._build_tineye_engine({})(IMAGE_URL, n=5) == []
