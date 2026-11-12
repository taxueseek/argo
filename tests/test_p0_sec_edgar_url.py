#!/usr/bin/env python3
"""P0 回归门：sec_edgar 的每条结果必须带可打开的文档 URL。

## 守的是什么缺陷

2026-11-11 实测：`argo search "apple" --engine sec_edgar --json` 返回 0 条，
但 `engines.search('apple', 'sec_edgar')` 返回 10 条，且 `engine_validate
--stage health` 报 pass。

根因：`engines/specs/sec_edgar.yaml` 的 `output_map.url_template` 里写了
`{forms}` 占位，但 `_format_url_template` 的占位符一律从**响应条目**取值，
`forms` 是 spec 里的请求参数（常量 "10-K"），不在 efts 响应的 `_source` 里。
于是模板渲染恒为空串 → `engines_parsers.py:246` 把 `url` pop 掉 → 下游按
「无 URL 不可溯源」过滤掉全部条目 → 用户看到 0 条。

health 阶段只验证「引擎有响应」，不验证「结果字段完整」，所以这类
「有记录但 0 条」的静默空结果能一路过门禁。

## 判据

用 efts 的真实响应结构（离线 fixture，不联网）跑一遍 URL 模板渲染：
1. 模板中不得出现响应里不存在的占位符（`{forms}` 这类请求参数）；
2. 按 spec 的 url_template 渲染后必须得到 http(s) 开头的 URL；
3. 渲染出的 URL 必须包含该条目的 CIK，且指向 sec.gov 的文档本体。

第 3 条防止「为了过测试把模板改成硬编码 URL」——URL 必须随条目变化。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# efts.sec.gov/LATEST/search-index?q=apple&forms=10-K 的真实 _source 结构，
# 取两条不同 CIK 的条目，用于验证 URL 随条目变化（不是硬编码）。
EftsItem = dict
ITEMS: list[EftsItem] = [
    {
        "_id": "0001185185-15-000457:ex21-1.htm",
        "_source": {
            "ciks": ["0001418121"],
            "display_names": ["Apple Hospitality REIT, Inc.  (APLE)  (CIK 0001418121)"],
            "file_description": "EX-21.1",
            "file_type": "10-K",
            "file_date": "2015-03-02",
            "adsh": "0001185185-15-000457",
        },
    },
    {
        "_id": "0000320193-24-000123:aapl-20240928.htm",
        "_source": {
            "ciks": ["0000320193"],
            "display_names": ["APPLE COMPUTER INC  (AAPL)  (CIK 0000320193)"],
            "file_description": "10-K",
            "file_type": "10-K",
            "file_date": "2024-11-01",
            "adsh": "0000320193-24-000123",
        },
    },
]


def _spec() -> dict:
    return yaml.safe_load(
        (ROOT / "engines" / "specs" / "sec_edgar.yaml").read_text(encoding="utf-8")
    )


def _render(template: str, item: dict) -> str:
    from engines_parsers import _format_url_template
    return _format_url_template(template, item)


def test_url_template_has_no_request_only_placeholders():
    """模板占位必须都能从响应条目取到——{forms} 是请求参数，恒为空。"""
    template = _spec()["output_map"]["url_template"]
    placeholders = set(re.findall(r"\{([^}]+)\}", template))
    assert placeholders, "url_template 没有占位符，无法生成逐条 URL"
    missing = []
    for ph in placeholders:
        # 支持点分路径与数组下标，如 _source.ciks.0
        cur: object = ITEMS[0]
        for part in ph.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                cur = cur[int(part)] if int(part) < len(cur) else None
            else:
                cur = None
                break
            if cur is None:
                break
        if cur is None or cur == "":
            missing.append(ph)
    assert not missing, (
        "url_template 引用了响应里取不到的占位符（会渲染成空串，"
        f"结果因无 URL 被全部过滤）：{sorted(missing)}"
    )


def test_url_template_renders_http_url_for_every_item():
    """每条记录都要渲染出 http(s) URL，不能只让第一条过。"""
    template = _spec()["output_map"]["url_template"]
    rendered = [_render(template, item) for item in ITEMS]
    for item, url in zip(ITEMS, rendered):
        assert url.startswith(("http://", "https://")), (
            f"{item['_source']['ciks']} 渲染结果不是合法 URL：{url!r}"
        )
        assert "sec.gov" in url, f"URL 未指向 SEC 官方域名：{url!r}"


def test_url_varies_by_item():
    """URL 必须随条目 CIK 变化，防止用硬编码 URL 糊弄过测试。"""
    template = _spec()["output_map"]["url_template"]
    urls = [_render(template, item) for item in ITEMS]
    assert urls[0] != urls[1], "两条不同 CIK 的记录渲染出了同一个 URL（疑似硬编码）"
    assert ITEMS[0]["_source"]["ciks"][0] in urls[0]
    assert ITEMS[1]["_source"]["ciks"][0] in urls[1]


def test_spec_defaults_are_still_intact():
    """修复不能顺手改掉请求语义：q 参数与 forms 默认值保持可用。"""
    spec = _spec()
    assert spec["url"] == "https://efts.sec.gov/LATEST/search-index"
    assert spec["query_param"] == "q"
    assert spec["extra_params"]["forms"] == "10-K"
    assert spec["output_map"]["items"] == "hits.hits"
