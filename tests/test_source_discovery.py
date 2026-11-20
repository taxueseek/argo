#!/usr/bin/env python3
"""test_source_discovery.py — 协议族探测、HAR 脱敏合成 Spec 与日志盲区反推单测。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import source_discovery  # noqa: E402


def test_probe_domain_detects_discourse_and_llms_txt():
    def fake_fetch(url: str, timeout: float):
        if url.endswith("/latest.json"):
            return 200, {"content-type": "application/json"}, json.dumps({"topic_list": {"topics": []}})
        if url.endswith("/llms.txt"):
            return 200, {"content-type": "text/plain"}, "# Example Docs\n- https://forum.example.com/guide\n"
        return 404, {}, ""

    rep = source_discovery.probe_domain_protocols("forum.example.com", fetcher=fake_fetch)
    assert rep["recommended_protocol"] == "discourse"
    assert [d["protocol"] for d in rep["detected"]] == ["discourse", "llms_txt"]
    assert rep["spec_draft"]["url"] == "https://forum.example.com/search.json"
    assert rep["spec_draft"]["query_param"] == "q"


def test_probe_domain_detects_mediawiki_opensearch_rss():
    def fake_fetch(url: str, timeout: float):
        if "meta=siteinfo" in url:
            return 200, {}, json.dumps({"query": {"general": {"sitename": "DemoWiki"}}})
        if url.endswith("/opensearch.xml"):
            xml = (
                '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
                '<Url type="application/json" template="https://wiki.example.com/search?q={searchTerms}"/>'
                "</OpenSearchDescription>"
            )
            return 200, {}, xml
        if url.endswith("/feed"):
            return 200, {}, '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        return 404, {}, ""

    rep = source_discovery.probe_domain_protocols("wiki.example.com", fetcher=fake_fetch)
    assert rep["recommended_protocol"] == "mediawiki"
    assert {d["protocol"] for d in rep["detected"]} == {"mediawiki", "opensearch", "rss_atom"}
    assert rep["spec_draft"]["query_param"] == "srsearch"


def test_probe_domain_blocks_private_ssrf():
    with pytest.raises(ValueError, match="SSRF"):
        source_discovery.probe_domain_protocols("http://127.0.0.1:8080")


def test_spec_from_har_strips_sensitive_headers_and_maps_fields():
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": "https://api.example.com/v1/search?keyword=agent&page=1&sort=new",
                        "headers": [
                            {"name": "Cookie", "value": "session=SECRET_COOKIE_VAL"},
                            {"name": "Authorization", "value": "Bearer SECRET_TOKEN"},
                            {"name": "X-CSRF-Token", "value": "SECRET_CSRF"},
                            {"name": "Accept-Language", "value": "zh-CN"},
                        ],
                    },
                    "response": {
                        "content": {
                            "text": json.dumps({
                                "code": 0,
                                "data": {
                                    "items": [
                                        {
                                            "title": "Agent Search",
                                            "url": "https://example.com/p/1",
                                            "summary": "Overview of search",
                                        }
                                    ]
                                },
                            })
                        }
                    },
                }
            ]
        }
    }
    spec = source_discovery.spec_from_har(har, query_hint="agent", transport="ego_inpage_api")
    assert spec["type"] == "ego_inpage_api"
    assert spec["query_param"] == "keyword"
    assert spec["extra_params"] == {"page": "1", "sort": "new"}
    assert spec["output_map"]["items"] == "data.items"
    assert spec["output_map"]["item_title"] == "title"
    assert spec["output_map"]["item_summary"] == "summary"
    assert spec["cache_eligible"] is False
    # 严禁任何敏感凭证进入合成 Spec
    blob = json.dumps(spec)
    assert "SECRET_COOKIE_VAL" not in blob
    assert "SECRET_TOKEN" not in blob
    assert "SECRET_CSRF" not in blob
    assert "Cookie" not in spec["headers"]
    assert "Authorization" not in spec["headers"]


def test_discover_gaps_from_logs(tmp_path):
    log_file = tmp_path / "usage_log.jsonl"
    events = [
        {"data": {"domain": "biotech", "result_count": 0, "top_urls": ["https://docs.newsite.io/a"]}},
        {"data": {"domain": "biotech", "result_count": 0, "top_urls": ["https://docs.newsite.io/b"]}},
        {"data": {"domain": "general", "result_count": 5, "top_urls": ["https://github.com/org/repo"]}},
    ]
    log_file.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    rep = source_discovery.discover_gaps_from_logs(log_file, min_hits=2, known_hosts={"github.com"})
    assert rep["scanned_events"] == 3
    assert rep["collapsed_domains"] == [{"domain": "biotech", "zero_result_hits": 2}]
    assert rep["candidate_hosts"] == [{"host": "docs.newsite.io", "hits": 2}]
