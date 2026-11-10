#!/usr/bin/env python3
"""test_image_sources — 图源的 image_url / image_license 契约（mock 网络）。

## 守的是什么

批次八/九收录了 4 个图源（nasa_images / met_museum / artic / cleveland），它们
**都从上游拿到了图片标识，却全部丢掉了**：
  - met_museum  请求带 hasImages=true，却只留 objectURL；Met 的 primaryImage
                没被读（且该字段只在 isPublicDomain=true 时有值——上游权利策略）
  - artic       请求 fields 里就含 image_id，结果只有作品页 URL
  - cleveland   请求带 has_image=1，结果只有作品页 URL
  - nasa_images 声明式 output_map 没映射 item.links（图片直链）
结果 schema 里也没有 image 字段的任何约定，于是「搜到图源」只能给一个详情页
链接，用户拿不到图。本文件锁住修复后的契约，并覆盖三类易错点：

  1. 有图时必须带 image_url，且不得是空串 / 详情页 URL；
  2. **没图时不得凭空造字段**（Met 受版权保护的件上游不给图，宁缺勿假）；
  3. 数组取值的路径形状：NASA 的 item.links 是 [{href,rel,...}]，必须走
     `links.0.href`——写成 `links.0` 会被 _coerce_field 当 dict 丢成空串。

实现改动必跑：`python3 -m pytest tests/test_image_sources.py -q`
"""

from __future__ import annotations

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_intl as bintl  # noqa: E402
from engines_base import _parse_http_payload  # noqa: E402


class _FakeResp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_json(monkeypatch, mod, obj, fn_name="http_open"):
    payload = json.dumps(obj).encode()
    monkeypatch.setattr(mod, fn_name,
                        lambda *a, **k: _FakeResp(payload), raising=False)


# ── AIC（图片 URL 由响应自带 config.iiif_url 拼出）─────────────────────────

_AIC = {
    "config": {"iiif_url": "https://www.artic.edu/iiif/2", "website_url": "https://www.artic.edu"},
    "data": [
        {"id": 16568, "title": "Water Lilies", "artist_display": "Claude Monet",
         "date_display": "1906", "image_id": "3c27b499-af56-f0d5-93b5-a7f2f1ad5813"},
        {"id": 99999, "title": "No Image Work", "artist_display": "x",
         "date_display": "1900", "image_id": None},
    ],
}

_ARTIC_SPEC = {
    "_name": "artic", "format": "json",
    "response": {"parser": "artic"},
}


class TestArtic:
    # 2026-11-10：artic 的专用 builder 已归一为 type: http + response.parser: artic
    # （见 engines/specs/artic.yaml 与 engines_parsers.parse_artic）。IIIF 图片
    # 依赖响应级 config.iiif_url 拼接，output_map 表达不了，故走注册解析器。
    def test_image_url_built_from_response_iiif_base(self):
        out = _parse_http_payload(json.dumps(_AIC), "json", "artic", 5, {}, _ARTIC_SPEC)
        assert len(out) == 2
        assert out[0]["image_url"] == (
            "https://www.artic.edu/iiif/2/3c27b499-af56-f0d5-93b5-a7f2f1ad5813"
            "/full/843,/0/default.jpg")
        assert out[0]["image_license"]

    def test_no_image_id_means_no_field(self):
        """没有 image_id 的藏品不得凭空造 image_url。"""
        out = _parse_http_payload(json.dumps(_AIC), "json", "artic", 5, {}, _ARTIC_SPEC)
        assert "image_url" not in out[1]
        assert "image_license" not in out[1]

    def test_iiif_base_missing_does_not_emit_broken_url(self):
        """响应没带 config.iiif_url 时不得拼出半截 URL（如 '//full/843,'）。"""
        payload = {"data": [dict(_AIC["data"][0])]}  # 去掉 config
        out = _parse_http_payload(json.dumps(payload), "json", "artic", 5, {}, _ARTIC_SPEC)
        assert "image_url" not in out[0]


# ── Cleveland（CC0，授权随条目）────────────────────────────────────────────

_CLE = {
    "data": [
        {"id": 1, "title": "The Red Kerchief", "tombstone": "Claude Monet, 1868",
         "url": "https://www.clevelandart.org/art/1958.39",
         "share_license_status": "CC0",
         "images": {"web": {"url": "https://openaccess-cdn.clevelandart.org/1958.39/1958.39_web.jpg"}}},
        {"id": 2, "title": "No Image", "tombstone": "x",
         "url": "https://www.clevelandart.org/art/2",
         "share_license_status": "Copyrighted", "images": {}},
    ],
}


class TestCleveland:
    # 2026-11-10：cleveland 的专用 builder 已归一为 type: http + output_map
    # （见 engines/specs/cleveland.yaml）。此处改为验证声明式路径的等价契约：
    # 图片、授权、以及原 builder 拼接进 snippet 的「tombstone · 授权」都要在。
    SPEC = {
        "_name": "cleveland",
        "format": "json",
        "output_map": {
            "items": "data",
            "item_title": "title",
            "item_url": "url",
            "item_summary_template": "{tombstone} · 授权 {share_license_status}（克利夫兰艺术博物馆）",
            "item_image": "images.web.url",
            "item_image_license": "share_license_status",
        },
    }

    def test_image_url_and_license(self):
        out = _parse_http_payload(json.dumps(_CLE), "json", "cleveland", 5,
                                  self.SPEC["output_map"], self.SPEC)
        assert out[0]["image_url"] == "https://openaccess-cdn.clevelandart.org/1958.39/1958.39_web.jpg"
        assert out[0]["image_license"] == "CC0"
        # 归一不得丢原 builder 的授权拼接（这是本轮归一修的重点）
        assert "CC0" in out[0]["snippet"]
        assert "Claude Monet" in out[0]["snippet"]

    def test_missing_images_block_no_field(self):
        out = _parse_http_payload(json.dumps(_CLE), "json", "cleveland", 5,
                                  self.SPEC["output_map"], self.SPEC)
        assert "image_url" not in out[1]


# ── Met（上游权利策略：仅公版件给图）──────────────────────────────────────

def _met_obj(oid, title, img="", small="", pd=False):
    return {"objectID": oid, "title": title, "objectURL": f"https://www.metmuseum.org/art/collection/search/{oid}",
            "artistDisplayName": "Claude Monet", "objectDate": "1900", "medium": "oil",
            "department": "European Paintings",
            "primaryImage": img, "primaryImageSmall": small, "isPublicDomain": pd}


class _MetResp:
    """按 URL 分派：search 回 objectIDs，objects/{id} 回详情。

    注意 patch 的是 `_http_get_raw`（met builder 用它取原始字节），不是
    `http_open`——后者是批次九 builder 的取数口，两者不同层。
    """

    def __init__(self, search_ids, objects):
        self._ids = search_ids
        self._objects = objects

    def __call__(self, url, headers=None, timeout=None, engine=""):
        if "/search?" in url:
            return json.dumps({"objectIDs": self._ids})
        oid = int(str(url).rstrip("/").rsplit("/", 1)[-1])
        return json.dumps(self._objects[oid])


class TestMetMuseum:
    def test_public_domain_object_gets_image_and_license(self, monkeypatch):
        objs = {1: _met_obj(1, "PD Work", img="https://images.metmuseum.org/a.jpg",
                            small="https://images.metmuseum.org/a-small.jpg", pd=True)}
        monkeypatch.setattr(bintl, "_http_get_raw", _MetResp([1], objs), raising=True)
        out = bintl._build_met_museum_engine({"_name": "met_museum", "timeout": 5})("monet", 5)
        assert out[0]["image_url"] == "https://images.metmuseum.org/a.jpg"
        assert "公版" in out[0]["image_license"]

    def test_falls_back_to_primary_image_small(self, monkeypatch):
        objs = {2: _met_obj(2, "Small Only", img="",
                            small="https://images.metmuseum.org/b-small.jpg", pd=True)}
        monkeypatch.setattr(bintl, "_http_get_raw", _MetResp([2], objs), raising=True)
        out = bintl._build_met_museum_engine({"_name": "met_museum", "timeout": 5})("monet", 5)
        assert out[0]["image_url"] == "https://images.metmuseum.org/b-small.jpg"

    def test_copyrighted_object_emits_no_image(self, monkeypatch):
        """上游对非公版件不给图片链接——不得造假链接（实测 Met 'monet' 全空）。"""
        objs = {3: _met_obj(3, "In Copyright", img="", small="", pd=False)}
        monkeypatch.setattr(bintl, "_http_get_raw", _MetResp([3], objs), raising=True)
        out = bintl._build_met_museum_engine({"_name": "met_museum", "timeout": 5})("monet", 5)
        assert len(out) == 1
        assert "image_url" not in out[0]
        assert "image_license" not in out[0]


# ── nasa_images（声明式 output_map）───────────────────────────────────────

_NASA = {
    "collection": {"items": [
        {"href": "https://images-assets.nasa.gov/image/x/collection.json",
         "data": [{"title": "Apollo 11", "description": "d", "date_created": "1969-07-11T00:00:00Z",
                   "nasa_id": "x"}],
         "links": [{"href": "https://images-assets.nasa.gov/image/x/x~medium.jpg",
                    "rel": "alternate", "render": "image", "width": 975}]},
    ]}
}

_NASA_SPEC = {
    "_name": "nasa_images",
    "image_license": "公有领域（NASA，美国政府作品）",
    "output_map": {
        "items": "collection.items",
        "item_title": "data.0.title",
        "item_summary": "data.0.description",
        "item_published_at": "data.0.date_created",
        "url_template": "https://images.nasa.gov/details/{data.0.nasa_id}",
        "item_image": "links.0.href",
    },
}


class TestNasaImages:
    def test_declarative_item_image_path(self):
        out = _parse_http_payload(json.dumps(_NASA), "", "nasa_images", 5,
                                  _NASA_SPEC["output_map"], _NASA_SPEC)
        assert len(out) == 1
        assert out[0]["image_url"] == "https://images-assets.nasa.gov/image/x/x~medium.jpg"
        assert out[0]["image_license"] == "公有领域（NASA，美国政府作品）"

    def test_links_0_without_href_is_dropped_not_stringified(self):
        """路径写成 links.0（dict）时必须落空，而不是把 dict 转成字符串。"""
        spec = dict(_NASA_SPEC)
        spec["output_map"] = dict(_NASA_SPEC["output_map"], item_image="links.0")
        out = _parse_http_payload(json.dumps(_NASA), "", "nasa_images", 5,
                                  spec["output_map"], spec)
        assert "image_url" not in out[0], "dict 被强转成字符串会产出点不开的伪链接"

    def test_no_image_field_when_mapping_absent(self):
        """未声明 item_image 的声明式源不得凭空多出 image_url（向后兼容）。"""
        spec = {"_name": "crt_sh", "output_map": {"items": ".", "item_title": "name"}}
        out = _parse_http_payload(json.dumps([{"name": "n"}]), "", "crt_sh", 5,
                                  spec["output_map"], spec)
        assert "image_url" not in out[0]


class TestSpecDeclaresNasaImageMapping:
    """真实 spec 文件必须带 item_image——防实现改了而部署声明没跟。"""

    def test_spec_file_has_item_image(self):
        import yaml
        from pathlib import Path
        path = Path(SCRIPTS_DIR).parent / "engines" / "specs" / "nasa_images.yaml"
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        om = spec.get("output_map") or {}
        assert om.get("item_image") == "links.0.href", (
            f"nasa_images 的 item_image 声明缺失或形态不对：{om.get('item_image')!r}"
            "——须指向字符串（links.0.href），links.0 是 dict 会被丢成空串")
        assert spec.get("image_license"), "缺少 spec 级 image_license 常量"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
