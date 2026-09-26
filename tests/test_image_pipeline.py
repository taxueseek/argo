#!/usr/bin/env python3
"""test_image_pipeline — 图片检索链路的契约与行为（mock 网络）。

## 守的是什么

本文件锁 2026-09-26 图片检索补强的四组契约，每组对应一类曾经真实存在的
静默缺陷（都是「看起来在跑，实际拿不到图」）：

  1. **字段落地**：图源的 image_url/license/width/height 必须真的出现在结果里。
     此前 Openverse 只映射了 title/url/summary，上游明明返回 url（图片直链）却
     没人接——「搜到图源拿不到图」。CLI 引擎同理：`_parse_text_output` 只放行
     五个字段，ddgs images 解析好的 image/width/height 到那一层被全删。

  2. **许可判定**：`_license_allows_commercial` 的三值语义。非商用必须判 False
     （判错方向会闯版权祸），认不出来必须 None（猜「可商用」是事故，猜「不可」
     会误杀可用素材）。

  3. **图片去重**：同一素材跨 CDN 的两个 URL 要判为同一张。URL 去重和文本
     去重都只看页面，对图无感，这是链路里唯一的图片粒度环节。

  4. **可用性过滤**：字段层面可确定不可用的（过小/极端比例/明确非商用）要能
     筛掉，但**尺寸未知必须放行**——拿不到尺寸不等于图小，判成不可用会误杀
     一整批不提供尺寸字段的源。

实现改动必跑：`python3 -m pytest tests/test_image_pipeline.py -q`
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

import image_ops as io  # noqa: E402


# ── 1. 许可判定（三值语义）────────────────────────────────────────────────

class TestLicenseAllowsCommercial:
    """非商用必须判 False、认不出必须 None——两个方向都不能猜。"""

    @pytest.mark.parametrize("label", [
        "by-nc-sa", "by-nc-nd", "by-nc",          # Openverse 短码
        "CC BY-NC 4.0",                           # Wikimedia 标准写法
        "https://creativecommons.org/licenses/by-nc-sa/2.0/",
        "Copyrighted", "All rights reserved", "版权所有",
        "non-commercial", "非商用",
    ])
    def test_non_commercial_is_false(self, label):
        assert io._license_allows_commercial(label) is False

    @pytest.mark.parametrize("label", [
        "cc0", "CC0", "by", "by-sa", "by-nd",     # Openverse 短码
        "CC BY-SA 4.0", "CC BY 4.0",              # Wikimedia
        "https://creativecommons.org/publicdomain/zero/1.0/",
        "公有领域（NASA，美国政府作品）",
        "cc-by-3.0", "Apache-2.0", "MIT", "BSD-3-Clause",
    ])
    def test_commercial_ok_is_true(self, label):
        assert io._license_allows_commercial(label) is True

    @pytest.mark.parametrize("label", [
        "", "   ", "见作品页", "权利状态见作品页",
        "AIC 公开接口（权利状态见作品页）",       # Artic 的自有术语
        None,
    ])
    def test_unrecognized_is_none(self, label):
        """认不出来时说「不知道」，不猜方向。"""
        assert io._license_allows_commercial(label) is None

    def test_nc_wins_over_free_looking_substring(self):
        """`by-nc-sa` 含 `by`——顺序反了会把「不可商用」判成「可商用」。

        这是本函数唯一会酿成真事故的方向，故单独钉一条。
        """
        assert io._license_allows_commercial("by-nc-sa") is False
        assert io._license_allows_commercial("cc-by-nc-nd-4.0") is False

    def test_multiple_candidates_any_hit(self):
        """多候选（短码 + URL + spec 常量）任一命中即可，且 NC 优先。"""
        assert io._license_allows_commercial("by-sa", "2.0") is True
        # 短码说 by、URL 说 nc → NC 优先
        assert io._license_allows_commercial(
            "by", "https://creativecommons.org/licenses/by-nc/4.0/") is False


class TestNormalizeLicenseLabel:
    """短码+版本 → 标准标注；已是标准写法的原样保留。"""

    def test_shortcode_plus_version(self):
        assert io._normalize_license_label("by-sa", "2.0") == "CC BY-SA 2.0"
        assert io._normalize_license_label("by", "2.0") == "CC BY 2.0"
        assert io._normalize_license_label("cc0") == "CC0"

    def test_standard_written_form_kept(self):
        """Wikimedia 给的 `CC BY-SA 4.0` 已是最准写法，不得被改写。"""
        assert io._normalize_license_label("CC BY-SA 4.0") == "CC BY-SA 4.0"
        assert io._normalize_license_label("公有领域（NASA）") == "公有领域（NASA）"

    def test_unknown_passthrough(self):
        """认不出来时原样返回，不发明一个协议名。"""
        assert io._normalize_license_label("见作品页") == "见作品页"
        assert io._normalize_license_label("") == ""


class TestFinalizeImageFields:
    """共同出口：尺寸转 int、许可归一、算商用判据、无图不判定。"""

    def test_dimensions_become_int(self):
        rows = [{"image_url": "https://x/a.jpg", "image_width": "1920",
                 "image_height": "1080"}]
        out = io.finalize_image_fields(rows)
        assert out[0]["image_width"] == 1920
        assert isinstance(out[0]["image_width"], int)
        assert isinstance(out[0]["image_height"], int)

    def test_unparseable_dimension_dropped(self):
        """转不动就删键——留字符串会让下游 `w >= 800` 抛 TypeError。"""
        out = io.finalize_image_fields(
            [{"image_url": "https://x/a.jpg", "image_width": "1024px"}])
        assert "image_width" not in out[0]

    def test_no_image_means_no_license_verdict(self):
        """无图条目不得带授权结论：否则下游按它过滤会捞出一批假素材。"""
        out = io.finalize_image_fields([{"title": "无图", "image_license": "CC0"}])
        assert "image_commercial_ok" not in out[0]

    def test_license_version_is_consumed_not_delivered(self):
        """`_license_version` 是拼接原料，不得出现在交付结果里。"""
        out = io.finalize_image_fields(
            [{"image_url": "https://x/a.jpg", "image_license": "by-sa",
              "_license_version": "2.0"}])
        assert "_license_version" not in out[0]
        assert out[0]["image_license"] == "CC BY-SA 2.0"

    def test_idempotent(self):
        rows = [{"image_url": "https://x/a.jpg", "image_license": "by-sa",
                 "image_width": "800", "image_height": "600",
                 "_license_version": "2.0"}]
        once = io.finalize_image_fields(rows)
        twice = io.finalize_image_fields(once)
        assert once == twice


# ── 2. CLI 字段透传（ddgs images 的字段曾被静默丢弃）────────────────────

class TestCliResultRow:
    """CLI 子引擎输出 → 对外契约，图片字段必须透传且别名收敛。"""

    def test_ddgs_images_fields_passthrough(self):
        """ddgs images 的原生键名（image/width/height）必须收敛到契约键名。

        不收敛的后果是静默的：过滤拿到 None → 判「尺寸未知」→ 一律放行。
        """
        row = io._cli_result_row({
            "title": "Snow", "url": "https://page/1", "source": "qianye88.com",
            "image": "https://img/x.jpg", "thumbnail": "https://t/x.jpg",
            "width": "1275", "height": "891",
        }, "ddgs_images")
        assert row["image_url"] == "https://img/x.jpg"
        assert row["image_width"] == "1275"
        assert row["image_height"] == "891"
        assert row["thumbnail"] == "https://t/x.jpg"
        # 原生键保留：可能仍有消费者读 image
        assert row["image"] == "https://img/x.jpg"

    def test_existing_image_url_not_overwritten(self):
        """已按契约命名的字段优先，别名不覆盖。"""
        row = io._cli_result_row({
            "title": "t", "url": "u",
            "image": "https://a.jpg", "image_url": "https://b.jpg",
        }, "eng")
        assert row["image_url"] == "https://b.jpg"

    def test_internal_fields_not_leaked(self):
        """内部计时字段（_elapsed）不得跟进结果。"""
        row = io._cli_result_row(
            {"title": "t", "url": "u", "_elapsed": 1.23, "_engine": "x"}, "eng")
        assert "_elapsed" not in row
        assert "_engine" not in row


# ── 3. 图片去重 ───────────────────────────────────────────────────────────

class TestImageKey:
    """图片直链归一键：跨 CDN/变换参数可比的才是「同一张图」。"""

    @pytest.mark.parametrize("a,b,why", [
        ("https://x.com/a/b/480x320/photo.jpg", "https://x.com/a/b/photo.jpg",
         "路径里的尺寸段"),
        ("https://x.com/p.jpg?x-oss-process=style/watermark", "https://x.com/p.jpg",
         "OSS 水印变换参数"),
        ("https://x.com/p.jpg?w=800&q=80", "https://x.com/p.jpg?q=80&w=800",
         "参数顺序不同"),
        ("https://x.com/p.jpg?resize=480", "https://x.com/p.jpg",
         "resize 参数"),
    ])
    def test_same_image(self, a, b, why):
        assert io.image_key(a) == io.image_key(b), why

    @pytest.mark.parametrize("a,b", [
        ("https://a.com/1.jpg", "https://b.com/2.jpg"),
        ("https://x.com/a.jpg", "https://x.com/b.jpg"),
    ])
    def test_different_image(self, a, b):
        assert io.image_key(a) != io.image_key(b)

    def test_empty(self):
        assert io.image_key("") == ""
        assert io.image_key(None) == ""


class TestDeduplicateImages:
    """同一张图占多个位时只留首条；无图条目不参与。"""

    def test_same_image_via_transform_params(self):
        rows = [
            {"title": "A 原图", "image_url": "https://x.com/p.jpg"},
            {"title": "A 水印", "image_url": "https://x.com/p.jpg?x-oss-process=w"},
        ]
        kept, removed = io.deduplicate_images(rows)
        assert len(kept) == 1 and removed == 1
        assert kept[0]["title"] == "A 原图"
        assert rows[1].get("_image_dup") is True

    def test_same_filename_and_dims_across_cdn(self):
        """跨 CDN 的同名同尺寸文件判为同图（第二道判据）。"""
        rows = [
            {"image_url": "https://p0.so.qhimg.com/t040f7e0e65116a8110.jpg",
             "image_width": 1275, "image_height": 891},
            {"image_url": "https://p0.ssl.qhimgs1.com/t040f7e0e65116a8110.jpg",
             "image_width": 1275, "image_height": 891},
        ]
        kept, removed = io.deduplicate_images(rows)
        assert len(kept) == 1 and removed == 1

    def test_generic_filename_not_used_as_evidence(self):
        """`1.jpg` 这类通用名不得当同图证据——会误杀不同图。"""
        rows = [
            {"image_url": "https://cdn-a.com/1.jpg",
             "image_width": 800, "image_height": 600},
            {"image_url": "https://cdn-b.com/1.jpg",
             "image_width": 800, "image_height": 600},
        ]
        kept, _ = io.deduplicate_images(rows)
        assert len(kept) == 2

    def test_rows_without_image_pass_through(self):
        """无图是另一类问题（由可用性过滤处理），去重不负责丢弃。"""
        rows = [{"title": "无图", "url": "https://x"}, {"title": "也无图"}]
        kept, removed = io.deduplicate_images(rows)
        assert len(kept) == 2 and removed == 0


# ── 4. 可用性过滤 ─────────────────────────────────────────────────────────

class TestImageUsability:
    """字段层面能确定的判据；尺寸未知必须放行。"""

    def test_hires_ok(self):
        ok, reason = io.image_usability(
            {"image_url": "https://x/a.jpg", "image_width": 4928,
             "image_height": 3264})
        assert ok and reason == "ok"

    def test_too_small(self):
        ok, reason = io.image_usability(
            {"image_url": "https://x/a.jpg", "image_width": 120,
             "image_height": 120})
        assert not ok and reason == "too_small"

    def test_extreme_aspect_ratio(self):
        """4000x150 同时满足「某边过小」和「比例极端」，报更具体的那个。"""
        ok, reason = io.image_usability(
            {"image_url": "https://x/a.jpg", "image_width": 4000,
             "image_height": 150})
        assert not ok and reason == "extreme_aspect"

    def test_unknown_dimensions_pass(self):
        """拿不到尺寸 ≠ 图小。判成不可用会误杀整批不提供尺寸的源。"""
        ok, reason = io.image_usability({"image_url": "https://x/a.jpg"})
        assert ok and reason == "ok"

    def test_missing_image(self):
        ok, reason = io.image_usability({"title": "无图"})
        assert not ok and reason == "no_image"

    def test_non_commercial_flagged(self):
        ok, reason = io.image_usability(
            {"image_url": "https://x/a.jpg", "image_commercial_ok": False})
        assert not ok and reason == "not_commercial"


class TestFilterUsableImages:
    """过滤 + 原因计数（静默丢弃是搜索工具最招人恨的行为）。"""

    ROWS = [
        {"image_url": "https://x/big.jpg", "image_width": 4000, "image_height": 3000},
        {"image_url": "https://x/small.jpg", "image_width": 120, "image_height": 120},
        {"image_url": "https://x/wide.jpg", "image_width": 4000, "image_height": 150},
    ]

    def test_default_keeps_non_commercial(self):
        """默认不因授权剔图：「搜图看看」和「找可发布素材」是两个需求。"""
        rows = [{"image_url": "https://x/a.jpg", "image_commercial_ok": False}]
        kept, dropped = io.filter_usable_images(rows)
        assert len(kept) == 1 and not dropped

    def test_require_commercial_drops_nc(self):
        rows = [{"image_url": "https://x/a.jpg", "image_commercial_ok": False}]
        kept, dropped = io.filter_usable_images(rows, require_commercial=True)
        assert not kept and dropped.get("not_commercial") == 1

    def test_reason_counts(self):
        kept, dropped = io.filter_usable_images([dict(r) for r in self.ROWS])
        assert len(kept) == 1
        assert dropped["too_small"] == 1
        assert dropped["extreme_aspect"] == 1


class TestLooksLikeImageUrl:
    @pytest.mark.parametrize("url", [
        "https://x/a.jpg", "https://x/a.PNG", "https://x/a.webp?w=1",
        "https://x/a.jpeg", "https://x/a.avif",
    ])
    def test_yes(self, url):
        assert io.looks_like_image_url(url)

    @pytest.mark.parametrize("url", [
        "https://x/a.html", "https://x/page", "", None, "https://x/a",
    ])
    def test_no(self, url):
        assert not io.looks_like_image_url(url)


# ── 5. 图源引擎的字段契约（mock 网络）─────────────────────────────────────

class _FakeResp:
    def __init__(self, data: dict):
        self._d = json.dumps(data).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestWikimediaCommonsContract:
    """Wikimedia 的 query.pages 字典结构 + extmetadata 双层包装。"""

    _PAYLOAD = {
        "query": {
            "pages": {
                "123": {
                    "pageid": 123,
                    "title": "File:Snow mountain.jpg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Snow_mountain.jpg",
                    "imageinfo": [{
                        "url": "https://upload.wikimedia.org/snow.jpg",
                        "thumburl": "https://upload.wikimedia.org/thumb/snow.jpg",
                        "width": 4928, "height": 3264,
                        "extmetadata": {
                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            "Artist": {"value": '<a href="/wiki/User:X">Jane Doe</a>'},
                            "ImageDescription": {"value": "<span>Snow scene</span>"},
                            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                        },
                    }],
                },
            },
        },
    }

    def _engine(self, monkeypatch, payload=None):
        from engines_builders_batch11 import _build_wikimedia_commons_engine
        import engines_builders_batch11 as b11
        monkeypatch.setattr(b11, "http_open",
                            lambda *a, **k: _FakeResp(payload or self._PAYLOAD))
        return _build_wikimedia_commons_engine({"_name": "wikimedia_commons", "timeout": 5})

    def test_dict_pages_expanded_with_image_fields(self, monkeypatch):
        out = self._engine(monkeypatch)("snow", 5)
        assert len(out) == 1
        r = out[0]
        # File: 前缀被剥掉
        assert r["title"] == "Snow mountain.jpg"
        assert r["image_url"] == "https://upload.wikimedia.org/snow.jpg"
        assert r["image_license"] == "CC BY-SA 4.0"
        assert r["image_license_url"].endswith("/by-sa/4.0")
        assert r["image_width"] == 4928 and isinstance(r["image_width"], int)
        assert r["image_commercial_ok"] is True

    def test_html_stripped_from_snippet(self, monkeypatch):
        """extmetadata 带 HTML，交付文本不得含标签。"""
        out = self._engine(monkeypatch)("snow", 5)
        snippet = out[0]["snippet"]
        assert "<" not in snippet and ">" not in snippet
        assert "Jane Doe" in snippet

    def test_list_pages_also_accepted(self, monkeypatch):
        """formatversion 切换会改 pages 形状（dict↔list），两种都得收。"""
        payload = {"query": {"pages": list(self._PAYLOAD["query"]["pages"].values())}}
        out = self._engine(monkeypatch, payload)("snow", 5)
        assert len(out) == 1 and out[0]["image_url"]

    def test_missing_imageinfo_skipped(self, monkeypatch):
        """没有 imageinfo 的条目（非位图/无图）不得凭空造 image_url。"""
        payload = {"query": {"pages": {"1": {"pageid": 1, "title": "File:X.pdf"}}}}
        out = self._engine(monkeypatch, payload)("x", 5)
        assert out == []

    def test_non_commercial_flagged_false(self, monkeypatch):
        payload = json.loads(json.dumps(self._PAYLOAD))
        payload["query"]["pages"]["123"]["imageinfo"][0]["extmetadata"][
            "LicenseShortName"]["value"] = "CC BY-NC 4.0"
        out = self._engine(monkeypatch, payload)("snow", 5)
        assert out[0]["image_commercial_ok"] is False
