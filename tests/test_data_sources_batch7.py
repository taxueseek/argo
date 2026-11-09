#!/usr/bin/env python3
"""test_data_sources_batch7.py — A 档六数据源引擎离线回归测试。

覆盖（全部 mock 网络层，不真实请求）：
  deps_dev     查询解析（生态别名/默认 npm/maven 冒号编码）+ 版本解析 + 弃用计数
  endoflife    slug 提取（剥生命周期尾词）+ 生命周期字段拼接 + 未识别诚实空
  biorxiv      DOI 提取（含链接/medRxiv 路由）+ 时效模式本地过滤 + 假 DOI 空结果
  un_comtrade  参数解析（国家/HS 码/年份/流向）+ 缺参诚实空 + 伙伴维度唯一 URL
  nvd          output_map 声明式解析（items 路径 + 点分字段 + url_template）
  openreview   output_map 声明式解析（{value:...} 包装结构）
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_base  # noqa: E402
from engines_builders_tech import (  # noqa: E402
    _build_deps_dev_engine,
    _build_endoflife_engine,
)
from engines_builders_science import (
    _build_biorxiv_engine,
)
from engines_builders_finance import (
    _build_un_comtrade_engine,
)


def _mock_http(monkeypatch, url_marker: str, payload):
    """mock engines_base._http_get_raw：URL 含 marker 时返回 payload。"""
    def fake_raw(url, headers, timeout, engine="?"):
        if url_marker in url:
            return json.dumps(payload) if not isinstance(payload, str) else payload
        return None
    monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)


class TestDepsDev:
    @pytest.fixture
    def engine(self):
        return _build_deps_dev_engine({"timeout": 2})

    def test_parse_and_output(self, engine, monkeypatch):
        _mock_http(monkeypatch, "api.deps.dev", {
            "versions": [
                {"versionKey": {"system": "NPM", "name": "express", "version": "5.2.1"},
                 "publishedAt": "2026-08-05T00:00:00Z", "isDefault": True},
                {"versionKey": {"system": "NPM", "name": "express", "version": "5.1.0"},
                 "publishedAt": "2026-01-01T00:00:00Z", "isDeprecated": True},
                {"versionKey": {"system": "NPM", "name": "express", "version": "5.0.0"},
                 "publishedAt": "2025-01-01T00:00:00Z"},
            ]})
        out = engine("npm express")
        assert len(out) == 3
        head = out[0]
        assert head["source"] == "deps_dev"
        assert "express" in head["title"] and "5.2.1" in head["title"]
        assert "2" in head["snippet"] and "1 个已弃用" in head["snippet"]
        assert head["published_at"] == "2026-08-05"

    def test_alias_python(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps({"versions": [
                {"versionKey": {"system": "PYPI", "name": "requests", "version": "2.0.0"},
                 "publishedAt": "2026-01-01T00:00:00Z", "isDefault": True}]})
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        out = engine("python requests")
        assert "pypi" in captured["url"] and "requests" in captured["url"]
        assert len(out) == 1

    def test_bare_package_defaults_npm(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps({"versions": [
                {"versionKey": {"system": "NPM", "name": "left-pad", "version": "1.0.0"},
                 "publishedAt": "2026-01-01T00:00:00Z", "isDefault": True}]})
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        engine("left-pad")
        assert captured["url"] == "https://api.deps.dev/v3/systems/npm/packages/left-pad"

    def test_maven_colon_encoded(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps({"versions": [
                {"versionKey": {"system": "MAVEN", "name": "c:g", "version": "1"},
                 "publishedAt": "2026-01-01T00:00:00Z", "isDefault": True}]})
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        engine("maven com.google:guava")
        assert "com.google%3Aguava" in captured["url"]

    def test_empty_query_returns_empty(self, engine):
        assert engine("") == []
        assert engine("npm") == []  # 生态词后无包名


class TestEndoflife:
    @pytest.fixture
    def engine(self):
        return _build_endoflife_engine({"timeout": 2})

    def test_slug_extraction_strips_tail_words(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps([
                {"cycle": "3.14", "latest": "3.14.7", "releaseDate": "2025-10-07",
                 "support": "2027-10-01", "eol": "2030-10-31",
                 "latestReleaseDate": "2026-08-05"}])
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        out = engine("python 生命周期")
        assert captured["url"] == "https://endoflife.date/api/python.json"
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "python 3.14（最新）"
        assert "3.14.7" in r["snippet"] and "EOL 2030-10-31" in r["snippet"]
        assert r["url"] == "https://endoflife.date/python"

    def test_chinese_only_query_returns_empty(self, engine, monkeypatch):
        _mock_http(monkeypatch, "endoflife.date", [])
        assert engine("最新产品生命周期") == []

    def test_unknown_product_404(self, engine, monkeypatch):
        _mock_http(monkeypatch, "endoflife.date", None)
        assert engine("notaproduct123") == []


class TestBiorxiv:
    @pytest.fixture
    def engine(self):
        return _build_biorxiv_engine({"timeout": 2})

    def test_doi_mode_single_paper(self, engine, monkeypatch):
        _mock_http(monkeypatch, "api.biorxiv.org", {
            "collection": [{
                "doi": "10.1101/2026.01.01.000001",
                "title": "A test preprint on synthetic biology",
                "authors": "Zhang, S.; Li, W.",
                "abstract": "We show that ...",
                "date": "2026-09-01",
            }]})
        out = engine("10.1101/2026.01.01.000001")
        assert len(out) == 1
        assert out[0]["url"] == "https://doi.org/10.1101/2026.01.01.000001"
        assert "synthetic biology" in out[0]["title"]

    def test_doi_from_url_medrxiv_routed(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps({"collection": [
                {"doi": "10.1101/2026.01.01.000002", "title": "medRxiv paper",
                 "authors": "A", "abstract": "B", "date": "2026-09-01"}]})
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        engine("https://www.medrxiv.org/content/10.1101/2026.01.01.000002v1")
        assert "/medrxiv/" in captured["url"]

    def test_keyword_plus_trigger_short_circuits(self, engine, monkeypatch):
        """关键词查询不发请求：API 无搜索端点，拉全量再本地过滤只产假阳性。

        2026-11-09 起「crispr 最新」属关键词查询（kws 非空），按设计返回空，
        旧断言 len(out)==1 守的是已删除的本地过滤实现（e82815e 有意短路，
        实测 2.7-12s 换 0 条有效结果）。这里改为钉住短路本身，并确认
        **没有发出 HTTP 请求**——否则「返回空」可能是网络失败掩出来的。
        """
        def boom(*_a, **_k):
            raise AssertionError("关键词查询不应发起 HTTP 请求")

        monkeypatch.setattr(engines_base, "_http_get_raw", boom, raising=True)
        assert engine("crispr 最新") == []

    def test_recent_mode_bare_trigger_passes_all(self, engine, monkeypatch):
        _mock_http(monkeypatch, "api.biorxiv.org", {
            "collection": [
                {"doi": "10.1101/a", "title": "t1", "authors": "X",
                 "abstract": "a", "date": "2026-09-10"},
                {"doi": "10.1101/b", "title": "t2", "authors": "Y",
                 "abstract": "b", "date": "2026-09-11"},
            ]})
        assert len(engine("最新")) == 2


class TestUnComtrade:
    @pytest.fixture
    def engine(self):
        return _build_un_comtrade_engine({"timeout": 2})

    def _payload(self):
        return {"data": [
            {"partnerCode": 1, "primaryValue": 26395290787.0, "netWgt": 142445856.09},
            {"partnerCode": 276, "primaryValue": 753191144.0, "netWgt": 408497.7},
        ]}

    def test_full_parse(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps(self._payload())
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        out = engine("中国 8541 2023")
        assert "reporterCode=156" in captured["url"]
        assert "cmdCode=8541" in captured["url"] and "period=2023" in captured["url"]
        assert len(out) == 2
        # 按金额降序：全球合计在前，title/url 唯一（伙伴维度）
        assert "全球合计" in out[0]["title"]
        assert out[0]["url"] != out[1]["url"]
        assert "PartnerAreas=1" in out[0]["url"]

    def test_export_flow(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps(self._payload())
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        engine("美国 8703 2024 出口")
        assert "reporterCode=842" in captured["url"]
        assert "flowCode=X" in captured["url"] and "period=2024" in captured["url"]

    def test_missing_country_returns_empty(self, engine, monkeypatch):
        _mock_http(monkeypatch, "comtradeapi", self._payload())
        assert engine("8541 2023") == []  # 无国家词：不猜测

    def test_missing_hs_code_returns_empty(self, engine, monkeypatch):
        _mock_http(monkeypatch, "comtradeapi", self._payload())
        assert engine("中国 芯片 2023") == []  # 无 HS 码：不猜测

    def test_year_not_mistaken_as_hs_code(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return json.dumps(self._payload())
        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        engine("中国 8541 2023")
        # 年份只进 period，不抢占 cmdCode
        assert "cmdCode=8541" in captured["url"]


class TestNvdDeclarative:
    """NVD 走声明式 output_map：锁定 items 路径 + 点分字段 + url_template。"""

    def test_parse(self, monkeypatch):
        from engines_base import _parse_http_payload
        spec = {"_name": "nvd", "output_map": {
            "items": "vulnerabilities",
            "item_title": "cve.id",
            "item_summary": "cve.descriptions.0.value",
            "item_published_at": "cve.published",
            "url_template": "https://nvd.nist.gov/vuln/detail/{cve.id}",
        }}
        data = {"resultsPerPage": 5, "vulnerabilities": [{
            "cve": {
                "id": "CVE-2021-44228",
                "published": "2021-12-10T10:15:09.223",
                "descriptions": [{"lang": "en", "value": "Apache Log4j2 JNDI RCE"}],
            }}]}
        out = _parse_http_payload(json.dumps(data), "", "nvd", 5, spec["output_map"], spec)
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "CVE-2021-44228"
        assert "Log4j2" in r["snippet"]
        assert r["url"] == "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"
        assert r["published_at"].startswith("2021-12-10")


class TestOpenReviewDeclarative:
    """OpenReview 走声明式 output_map：{value:...} 包装结构点路径。"""

    def test_parse(self):
        from engines_base import _parse_http_payload
        spec = {"_name": "openreview", "output_map": {
            "items": "notes",
            "item_title": "content.title.value",
            "item_summary": "content.abstract.value",
            "url_template": "https://openreview.net/forum?id={id}",
        }}
        data = {"notes": [{
            "id": "abc123",
            "content": {
                "title": {"value": "Attention Is All You Need (replication)"},
                "abstract": {"value": "We replicate the transformer."},
            }}]}
        out = _parse_http_payload(json.dumps(data), "", "openreview", 5,
                                  spec["output_map"], spec)
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "Attention Is All You Need (replication)"
        assert "transformer" in r["snippet"]
        assert r["url"] == "https://openreview.net/forum?id=abc123"
