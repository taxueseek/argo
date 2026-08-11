#!/usr/bin/env python3
"""多语言与开放数据引擎测试（离线 mock 解析 + 注册一致性）。

覆盖 13 个新引擎：gov_policy / qiita / fr_opendata（config 驱动）
+ cnii / ndl / kor_law / hatena_bookmark / dnb / doaj / europeana /
hal / eu_opendata / open_meteo（builder 驱动）。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_intl_engines.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import engines_builders_intl as intl  # noqa: E402


def _mk(spec=None):
    return intl  # 占位


class _BuilderCase(unittest.TestCase):
    """builder 引擎：mock _http_json/_http_text 验证解析。"""

    def run_case(self, builder, payload, query="q", n=3, expect_min=1,
                 http_json=None, http_text=None):
        engine = builder({"timeout": 8})
        with patch.object(intl, "_http_json", return_value=http_json or payload) as mj, \
             patch.object(intl, "_http_text", return_value=http_text or payload):
            res = engine(query, n)
        return res


class TestCnii(_BuilderCase):
    def test_parse(self):
        payload = {"items": [
            {"title": "Deep learning in visual computing", "@id": "https://cir.nii.ac.jp/crid/1",
             "link": {"@id": "https://cir.nii.ac.jp/crid/1"},
             "dc:creator": ["Ugail, H."], "prism:publicationDate": "2024",
             "description": "A book on deep learning."},
            {"title": "Second paper", "link": {"@id": "https://cir.nii.ac.jp/crid/2"}},
        ]}
        res = self.run_case(intl._build_cnii_engine, payload)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["title"], "Deep learning in visual computing")
        self.assertEqual(res[0]["url"], "https://cir.nii.ac.jp/crid/1")
        self.assertIn("Ugail", res[0]["snippet"])

    def test_network_error(self):
        with patch.object(intl, "_http_json", side_effect=OSError("x")):
            res = intl._build_cnii_engine({"timeout": 8})("q", 3)
        self.assertEqual(res, [])


class TestNdl(_BuilderCase):
    XML = """<?xml version="1.0"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><item>
<title>本1</title><link>https://ndlsearch.ndl.go.jp/books/1</link>
<dc:description>著者：山田</dc:description>
</item><item>
<title>本2</title><link>https://ndlsearch.ndl.go.jp/books/2</link>
</item></channel></rss>"""

    def test_parse(self):
        res = self.run_case(intl._build_ndl_engine, self.XML)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["title"], "本1")
        self.assertEqual(res[0]["url"], "https://ndlsearch.ndl.go.jp/books/1")
        self.assertIn("山田", res[0]["snippet"])


class TestKorLaw(_BuilderCase):
    XML = """<?xml version="1.0"?>
<PrecSearch><totalCnt>100</totalCnt>
<prec id="1"><사건명><![CDATA[계약 위반 사건]]></사건명>
<사건번호>대법원-2026-두-1</사건번호><선고일자>2026.01.01</선고일자>
<판례상세링크>/DRF/lawService.do?OC=test&amp;target=prec&amp;ID=1</판례상세링크></prec>
</PrecSearch>"""

    def test_parse(self):
        res = self.run_case(intl._build_kor_law_engine, self.XML)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "계약 위반 사건")
        self.assertTrue(res[0]["url"].startswith("https://law.go.kr/"))
        self.assertIn("대법원", res[0]["snippet"])

    def test_empty_xml(self):
        res = self.run_case(intl._build_kor_law_engine, "<?xml version='1.0'?><x/>")
        self.assertEqual(res, [])


class TestHatena(_BuilderCase):
    XML = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://a.example/1">
<title>Post A</title><link>https://a.example/1</link>
<dc:date>2026-08-10T08:00:00Z</dc:date>
</item></rdf:RDF>"""

    def test_parse(self):
        res = self.run_case(intl._build_hatena_bookmark_engine, self.XML)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Post A")
        self.assertIn("2026-08-10", res[0]["snippet"])


class TestDnb(_BuilderCase):
    XML = """<?xml version="1.0"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">
<numberOfRecords>1000</numberOfRecords>
<records><record><recordData>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Wissenschaft und Technik</dc:title>
<dc:identifier>https://d-nb.info/12345</dc:identifier>
</rdf:RDF>
</recordData></record></records>
</searchRetrieveResponse>"""

    def test_parse(self):
        res = self.run_case(intl._build_dnb_engine, self.XML)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Wissenschaft und Technik")
        self.assertIn("d-nb.info", res[0]["url"])


class TestDoaj(_BuilderCase):
    def test_parse(self):
        payload = {"total": 10, "results": [
            {"bibjson": {"title": "Paper One",
                         "link": [{"url": "https://ieeexplore.ieee.org/document/1", "type": "fulltext"}],
                         "author": [{"name": "A. Smith"}], "year": 2024,
                         "abstract": "Abstract text here."}},
        ]}
        res = self.run_case(intl._build_doaj_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Paper One")
        self.assertEqual(res[0]["url"], "https://ieeexplore.ieee.org/document/1")
        self.assertIn("A. Smith", res[0]["snippet"])

    def test_empty(self):
        res = self.run_case(intl._build_doaj_engine, {"total": 0, "results": []})
        self.assertEqual(res, [])


class TestEuropeana(_BuilderCase):
    def test_parse_title_list(self):
        payload = {"totalResults": 5, "items": [
            {"title": ["Mona Lisa", "Mona Lisa"], "link": "https://api.europeana.eu/record/1",
             "dataProvider": ["The Louvre"]},
        ]}
        res = self.run_case(intl._build_europeana_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Mona Lisa")
        self.assertIn("Louvre", res[0]["snippet"])


class TestHal(_BuilderCase):
    def test_parse(self):
        payload = {"response": {"numFound": 5, "docs": [
            {"title_s": ["Titre de l'article"], "uri_s": "https://hal.science/hal-1",
             "authFullName_s": ["Jean Dupont"], "producedDate_s": "2024-01-01"},
        ]}}
        res = self.run_case(intl._build_hal_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Titre de l'article")
        self.assertIn("Jean Dupont", res[0]["snippet"])


class TestEuOpendata(_BuilderCase):
    def test_parse(self):
        payload = {"result": {"count": 10, "results": [
            {"title": {"en": "Climate Data", "fr": "Données climat"},
             "landing_page": [{"resource": "https://data.bs.ch/explore/dataset/1"}]},
        ]}}
        res = self.run_case(intl._build_eu_opendata_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Climate Data")
        self.assertEqual(res[0]["url"], "https://data.bs.ch/explore/dataset/1")


class TestOpenMeteo(_BuilderCase):
    def test_parse(self):
        def fake_json(url, timeout):
            if "geocoding" in url:
                return {"results": [{"name": "Tokyo", "country": "Japan",
                                     "latitude": 35.68, "longitude": 139.69}]}
            return {"current_weather": {"temperature": 23.3, "windspeed": 4.0,
                                        "weathercode": 3}}
        engine = intl._build_open_meteo_engine({"timeout": 8})
        with patch.object(intl, "_http_json", side_effect=fake_json):
            res = engine("Tokyo", 3)
        self.assertEqual(len(res), 1)
        self.assertIn("Tokyo", res[0]["title"])
        self.assertIn("23.3", res[0]["snippet"])

    def test_geocode_empty(self):
        engine = intl._build_open_meteo_engine({"timeout": 8})
        with patch.object(intl, "_http_json",
                          return_value={"generationtime_ms": 0.05}):
            res = engine("q", 3)
        self.assertEqual(res, [])


class TestConfigDrivenEngines(unittest.TestCase):
    """config 驱动引擎（type: http + output_map）：构建并解析样例响应。"""

    def setUp(self):
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        import engines
        self.reg = engines.get_registry()

    def _call_with_response(self, name, payload):
        import json as _json
        import io
        class R:
            def __init__(self, data):
                self._data = data
                self.headers = {}
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        with patch("urllib.request.urlopen", return_value=R(_json.dumps(payload).encode())):
            return self.reg[name]("q", 3)

    def test_gov_policy(self):
        payload = {"code": 200, "searchVO": {"totalCount": 737, "listVO": [
            {"title": "国务院关于印发文件的通知", "url": "https://www.gov.cn/zhengce/1",
             "summary": "摘要", "pubtimeStr": "2026.07.23"},
        ]}}
        res = self._call_with_response("gov_policy", payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "国务院关于印发文件的通知")
        self.assertEqual(res[0]["published_at"], "2026.07.23")

    def test_qiita(self):
        payload = [{"title": "Python入門", "url": "https://qiita.com/x/1",
                    "body": "本文", "likes_count": 10}]
        res = self._call_with_response("qiita", payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Python入門")

    def test_fr_opendata(self):
        payload = {"data": [{"title": "Budget Climat", "description": "desc",
                             "id": "67e1", "url": ""}]}
        res = self._call_with_response("fr_opendata", payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Budget Climat")
        self.assertIn("67e1", res[0]["url"])

    def test_all_registered(self):
        # europeana 已禁用（官方 demo key api2demo 失效 401，见 config.yaml note）
        for name in ("gov_policy", "qiita", "fr_opendata", "cnii", "ndl",
                     "kor_law", "hatena_bookmark", "dnb", "doaj",
                     "hal", "eu_opendata", "open_meteo"):
            self.assertIn(name, self.reg, f"{name} 未注册")


if __name__ == "__main__":
    unittest.main()


class TestSearchMySite(_BuilderCase):
    HTML = """<html><body>
<div class="search-result sms-b-72">
<h2 class="sms-result-title"><a href="https://blog.example.com/post/1" class="result-title">
<span class="result-title-txt">My Self Hosted Email Setup</span></a></h2>
<p><a href="https://blog.example.com/post/1" class="result-link">https://blog.example.com/post/1</a></p>
</div>
</body></html>"""

    def test_parse(self):
        res = self.run_case(intl._build_searchmysite_engine, self.HTML)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "My Self Hosted Email Setup")
        self.assertEqual(res[0]["url"], "https://blog.example.com/post/1")

    def test_empty(self):
        res = self.run_case(intl._build_searchmysite_engine, "<html></html>")
        self.assertEqual(res, [])


class TestLieu(_BuilderCase):
    HTML = """<html><body>
<ul><li class="result"><a href="https://q.pfiffer.org/resume">q.pfiffer.org/resume</a></li>
<li class="entry"><a href="https://flower.codes/category/web">flower.codes</a></li></ul>
</body></html>"""

    def test_parse(self):
        res = self.run_case(intl._build_lieu_engine, self.HTML)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["url"], "https://q.pfiffer.org/resume")


class TestOpenSky(_BuilderCase):
    def test_parse(self):
        payload = {"states": [
            ["87455c", "ANA270 ", "Japan", 1786365374, 1786365374, 139.78, 35.56,
             None, True, 7.72, 67.5, None, None, None, "4653", False, 0],
        ]}
        res = self.run_case(intl._build_opensky_engine, payload, query="东京航班")
        self.assertEqual(len(res), 1)
        self.assertIn("ANA270", res[0]["title"])
        self.assertIn("Japan", res[0]["title"])

    def test_no_known_city(self):
        res = self.run_case(intl._build_opensky_engine, {"states": []}, query="xyzabc")
        self.assertEqual(res, [])


class TestElectricityMaps(_BuilderCase):
    def test_parse(self):
        payload = {"FR": {"zoneName": "France", "countryCode": "FR",
                          "isCommerciallyAvailable": True, "tier": "TIER_A"},
                   "JP": {"zoneName": "Japan", "countryCode": "JP"}}
        res = self.run_case(intl._build_electricity_maps_engine, payload, query="france")
        self.assertEqual(len(res), 1)
        self.assertIn("France", res[0]["title"])

    def test_no_match(self):
        res = self.run_case(intl._build_electricity_maps_engine, {"FR": {"zoneName": "France"}},
                            query="zzzz")
        self.assertEqual(res, [])


class TestUsda(_BuilderCase):
    def test_parse(self):
        payload = {"totalHits": 25685, "foods": [
            {"description": "APPLE, RAW", "fdcId": 45400,
             "foodCategory": "Fruits", "brandOwner": "Generic"},
        ]}
        res = self.run_case(intl._build_usda_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "APPLE, RAW")
        self.assertIn("45400", res[0]["url"])


class TestTatoeba(_BuilderCase):
    def test_parse_list_results(self):
        payload = {"paging": {}, "results": [
            {"id": 14006447, "text": "Hello world!", "lang": "eng",
             "translations": [[{"id": 1, "text": "你好，世界！", "lang": "cmn"}]]},
        ]}
        res = self.run_case(intl._build_tatoeba_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Hello world!")
        self.assertIn("你好", res[0]["snippet"])


class TestFigshare(_BuilderCase):
    def test_parse(self):
        payload = [{"id": 33197788, "title": "Sinefungin synthesis",
                    "doi": "10.1021/jacs.6c10565.s001",
                    "url_public_api": "https://api.figshare.com/v2/articles/33197788"}]
        res = self.run_case(intl._build_figshare_engine, payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Sinefungin synthesis")
        self.assertIn("figshare", res[0]["url"])


class TestTencentKline(_BuilderCase):
    def test_parse(self):
        def fake_json(url, timeout):
            if "smartbox" in url:
                return None  # 走 _resolve 的 urlopen 路径（下面 mock）
            return {"code": 0, "data": {"sh600519": {"qfqday": [
                ["2026-08-06", "1310.000", "1308.550", "1314.400", "1300.010", "25463"],
                ["2026-08-07", "1308.660", "1309.220", "1315.280", "1301.000", "24976"],
            ]}}}
        engine = intl._build_tencent_kline_engine({"timeout": 8})
        import urllib.request as _ur

        class R:
            def __init__(self, b): self._b = b
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=8):
            u = req.full_url if hasattr(req, 'full_url') else str(req)
            if "smartbox" in u:
                return R('v_hint="sh~600519~贵州茅台~600519~gp~A股~贵州茅台~GP-A"'.encode("gbk"))
            return R(json.dumps(fake_json(u, timeout)).encode())

        real_open = _ur.urlopen
        _ur.urlopen = fake_open
        try:
            with patch.object(intl, "_http_json", side_effect=lambda u, t: fake_json(u, t)):
                res = engine("贵州茅台 k线", 2)
        finally:
            _ur.urlopen = real_open
        self.assertEqual(len(res), 2)
        self.assertIn("1310.000", res[0]["snippet"])


class TestQqMusic(_BuilderCase):
    def test_parse(self):
        payload = {"code": 0, "data": {"song": {"list": [
            {"songname": "搁浅", "songmid": "003X", "albumname": "七里香",
             "singer": [{"name": "周杰伦"}]},
        ]}}}
        engine = intl._build_qq_music_engine({"timeout": 8})
        import urllib.request as _ur

        class R:
            def __init__(self, b): self._b = b
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=8):
            return R(json.dumps(payload).encode())

        real_open = _ur.urlopen
        _ur.urlopen = fake_open
        try:
            res = engine("周杰伦", 3)
        finally:
            _ur.urlopen = real_open
        self.assertEqual(len(res), 1)
        self.assertIn("搁浅", res[0]["title"])
        self.assertIn("周杰伦", res[0]["title"])
        self.assertIn("003X", res[0]["url"])


class TestNewEnginesRegistered(unittest.TestCase):
    def test_all_registered(self):
        import engines
        reg = engines.get_registry()
        for name in ("searchmysite", "lieu", "opensky", "electricity_maps",
                     "usda", "tatoeba", "figshare", "tencent_kline", "qq_music"):
            self.assertIn(name, reg, f"{name} 未注册")
