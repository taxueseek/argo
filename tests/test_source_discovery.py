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


def test_spec_from_har_redacts_sensitive_query_params():
    """HAR query 里的 token/apikey 等凭证不得随 extra_params 落盘（2026-11-21 审计）。"""
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": (
                            "https://api.example.com/v1/search?q=agent&page=2"
                            "&token=SECRET_QTOKEN&apikey=SECRET_QKEY&sig=abcdef123456"
                        ),
                        "headers": [{"name": "Accept", "value": "application/json"}],
                    },
                    "response": {
                        "content": {
                            "text": json.dumps({
                                "results": [{"title": "A", "url": "https://e.com/1", "snippet": "s"}],
                            })
                        }
                    },
                }
            ]
        }
    }
    spec = source_discovery.spec_from_har(har, transport="http")
    assert spec["query_param"] == "q"
    assert spec["extra_params"] == {"page": "2"}
    blob = json.dumps(spec)
    assert "SECRET_QTOKEN" not in blob
    assert "SECRET_QKEY" not in blob
    assert "abcdef123456" not in blob
    # 剥离要可见化：desc 必须声明剥了几个敏感参数，方便人工补回非敏感误杀项
    assert "敏感查询参数" in spec["desc"]


def test_isolated_smoke_probe_code_compiles_and_imports_proxy():
    """烟测子进程 probe 必须语法合法且走 net_proxy 出口——裸 urlopen 在强制代理
    环境会把好源误判失败归档（2026-11-21 审计）。用坏 spec 路径触发子进程
    提前失败，检查 stderr 无 SyntaxError（语法非法会在 traceback 里出现）。"""
    import subprocess
    import sys

    probe_code = (
        "import json, sys, urllib.request, urllib.parse; "
        "sys.path.insert(0, sys.argv[3]); "
        "import net_proxy; "
        "spec = json.loads(open(sys.argv[1], encoding='utf-8').read()); "
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code + "open(sys.argv[1], encoding='utf-8').read()",
         "/nonexistent/spec.json", "10", "scripts"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False, cwd=str(source_discovery.Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode != 0
    assert "SyntaxError" not in proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr  # net_proxy 可导入（argv[3] 生效）


def test_har_sensitive_param_variants_and_hint_steal():
    """探针 1/2 回归：大写与连字符变体键（X-API-Key/Token/session_id）必须脱敏；
    hint 词落在敏感参数值里不得把敏感键选成 query_param。"""
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": (
                            "https://api.example.com/v1/search?q=agent&page=1"
                            "&Token=S1&X-API-Key=S2&Auth=S3&session_id=S4"
                        ),
                        "headers": [{"name": "Accept", "value": "application/json"}],
                    },
                    "response": {
                        "content": {
                            "text": json.dumps({"results": [{"title": "A", "url": "https://e.com/1"}]}),
                        }
                    },
                }
            ]
        }
    }
    spec = source_discovery.spec_from_har(har, transport="http")
    blob = json.dumps(spec)
    for secret in ("S1", "S2", "S3", "S4"):
        assert secret not in blob, f"{secret} 泄漏进合成 Spec"
    assert spec["extra_params"] == {"page": "1"}

    har2 = json.loads(json.dumps(har))
    har2["log"]["entries"][0]["request"]["url"] = (
        "https://api.example.com/v1/search?q=realquery&sig=agent-secret-value"
    )
    spec2 = source_discovery.spec_from_har(har2, query_hint="agent", transport="http")
    assert spec2["query_param"] == "q", "hint 词在敏感值里不得顶替 q"
    assert spec2.get("extra_params") in (None, {})
