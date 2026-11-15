#!/usr/bin/env python3
"""声明式 YAML 收敛（方案 2）与共享基建回归测试（纯离线）。"""

from __future__ import annotations

import gzip
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SPECS_DIR = Path(__file__).resolve().parent.parent / "engines" / "specs"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engines_base  # noqa: E402
import engines_http  # noqa: E402
from engines_fields import _format_url_template  # noqa: E402
from caliber_isolation import merge_cross_caliber_duplicates  # noqa: E402
from rrf_classic import rrf_classic  # noqa: E402


def _load_spec(name: str) -> dict:
    spec = yaml.safe_load((SPECS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    spec["_name"] = name
    return spec


def test_format_url_template_supports_url_filter_and_empty_fail():
    assert (
        _format_url_template(
            "https://s.weibo.com/weibo?q=%23{word|url}%23",
            {"word": "AI 搜索"},
        )
        == "https://s.weibo.com/weibo?q=%23AI%20%E6%90%9C%E7%B4%A2%23"
    )
    assert _format_url_template("https://example.com/{word|url}", {"word": ""}) == ""


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        (
            "itotii",
            [
                {
                    "title": {"rendered": "<em>哈基米</em> &amp; 梗"},
                    "link": "https://geng.itotii.com/1",
                    "content": {"rendered": "<p>网络<b>热词</b></p>"},
                    "date": "2026-10-01",
                }
            ],
            {
                "title": "哈基米 & 梗",
                "url": "https://geng.itotii.com/1",
                "snippet": "2026-10-01 | 网络热词",
                "published_at": "2026-10-01",
            },
        ),
        (
            "toutiao_hot",
            {
                "data": [
                    {
                        "Title": "今日科技头条",
                        "Url": "https://www.toutiao.com/trending/123/",
                        "HotValue": "98765",
                    }
                ]
            },
            {
                "title": "今日科技头条",
                "url": "https://www.toutiao.com/trending/123/",
                "snippet": "热度 98765",
            },
        ),
        (
            "bilibili_hot",
            {"data": {"trending": {"list": [{"keyword": "黑神话 悟空"}]}}},
            {
                "title": "黑神话 悟空",
                "url": "https://search.bilibili.com/all?keyword=%E9%BB%91%E7%A5%9E%E8%AF%9D%20%E6%82%9F%E7%A9%BA",
                "snippet": "B站热搜",
            },
        ),
        (
            "weibo_hot",
            {
                "data": {
                    "realtime": [
                        {"word": "春节假期", "num": 123456, "label_name": "热"},
                        {"word": "", "num": 999},
                    ]
                }
            },
            {
                "title": "春节假期",
                "url": "https://s.weibo.com/weibo?q=%23%E6%98%A5%E8%8A%82%E5%81%87%E6%9C%9F%23",
                "snippet": "微博热搜 · 热度 123456 · 热",
            },
        ),
        (
            "douyin_hot",
            {"word_list": [{"word": "AI编程", "hot_value": 54321}]},
            {
                "title": "AI编程",
                "url": "https://www.douyin.com/search/AI%E7%BC%96%E7%A8%8B",
                "snippet": "抖音热榜 · 热度 54321",
            },
        ),
        (
            "vam",
            {
                "records": [
                    {
                        "systemNumber": "O417199",
                        "_primaryTitle": "Wave Print",
                        "_primaryMaker": {"name": "Hokusai"},
                        "_primaryDate": "1831",
                        "objectType": "Woodblock print",
                        "_images": {
                            "_iiif_image_base_url": "https://framemark.vam.ac.uk/collections/abc/"
                        },
                    }
                ]
            },
            {
                "title": "Wave Print",
                "url": "https://collections.vam.ac.uk/item/O417199/",
                "snippet": "Hokusai · 1831 · Woodblock print",
                "image_url": "https://framemark.vam.ac.uk/collections/abc/full/!800,800/0/default.jpg",
            },
        ),
    ],
)
def test_declarative_specs_parse_equivalently(name: str, payload: object, expected: dict):
    spec = _load_spec(name)
    assert spec["type"] == "http"
    rows = engines_base._parse_http_payload(
        json.dumps(payload), "", name, 5, spec["output_map"], spec
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == name
    for k, v in expected.items():
        assert row[k] == v


def test_engines_http_lazy_logger_and_gzip_partial():
    lg1 = engines_http.lazy_logger()
    lg2 = engines_http.lazy_logger()
    assert lg1 is lg2

    body = gzip.compress('{"ok": "中文"}'.encode("utf-8"))

    class _Resp(io.BytesIO):
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    seen_urls: list[str] = []

    def _opener(req, timeout=5, engine=""):
        seen_urls.append(req.full_url)
        return _Resp(body)

    assert engines_http.http_json(
        "https://example.com/search?q=北京", 3, opener=_opener
    ) == {"ok": "中文"}
    assert "%E5%8C%97%E4%BA%AC" in seen_urls[0]


def test_caliber_cross_merge_and_rrf_classic():
    merged = merge_cross_caliber_duplicates(
        [
            {"url": "https://a.com/1?utm_source=x", "title": "短标题", "snippet": "s", "source": "e1", "_rrf_score": 0.4, "consensus_engines": ["e1"]},
            {"url": "https://a.com/1", "title": "更完整的标题", "snippet": "更长摘要内容", "source": "e2", "_rrf_score": 0.2, "consensus_engines": ["e2"]},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["title"] == "更完整的标题"
    assert set(merged[0]["consensus_engines"]) == {"e1", "e2"}
    assert merged[0]["_rrf_score"] == round(0.4 + 0.35 * 0.2, 6)

    classic = rrf_classic([[{"url": "https://a.com/1", "title": "A", "source": "e1"}], [{"url": "https://a.com/1", "title": "A 详细", "source": "e2"}]])
    assert len(classic) == 1
    assert classic[0]["title"] == "A 详细"
    assert classic[0]["consensus_engines"] == ["e1", "e2"]
