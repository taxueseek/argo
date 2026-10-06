#!/usr/bin/env python3
"""test_tweet — `argo tweet`（X 帖子完整打包）契约（全程离线，不联网）。

## 守的是什么

  1. **解析**：URL / 纯 ID / 非法输入 → status id 提取正确（错了会打错包）。
  2. **全文不截断**：post.md 与 stdout 必须给完整正文——这正是本命令存在的
     理由（搜索只给 300 字 snippet）。
  3. **按需采用**：默认不下载媒体（只记 URL）；`--media` 才落盘。
  4. **产物完整性**：--out 落四件套（post.md/raw.json/manifest.json/CHECKLIST.md），
     CHECKLIST 能标出媒体部分失败（PARTIAL 但仍交付）。
  5. **安全**：媒体下载走 SSRF 守卫（内网/环回/非 http(s) 一律拒）。
  6. **stdout 紧凑**：--json 不得输出缩进 JSON（常驻上下文预算门禁的另一面）。

实现改动必跑：`python3 -m pytest tests/test_tweet.py -q`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fxtwitter_api as fx  # noqa: E402
import tweet as tw  # noqa: E402


# ── 合成数据（不依赖网络）──────────────────────────────────────────────────

def _status(sid="20", text="hello world", *, photos=0, videos=0, author="jack"):
    media_all, media_photos, media_videos = [], [], []
    for i in range(photos):
        p = {"type": "photo", "url": f"https://pbs.twimg.com/media/{sid}_{i}.jpg"}
        media_photos.append(p)
        media_all.append(p)
    for i in range(videos):
        v = {"type": "video", "url": None, "formats": [
            {"url": f"https://video.twimg.com/{sid}_{i}_hi.mp4", "container": "mp4",
             "bitrate": 2000000},
            {"url": f"https://video.twimg.com/{sid}_{i}_lo.mp4", "container": "mp4",
             "bitrate": 500000},
            {"url": f"https://video.twimg.com/{sid}_{i}.m3u8", "container": "m3u8"},
        ]}
        media_videos.append(v)
        media_all.append(v)
    return {
        "id": sid, "url": f"https://x.com/{author}/status/{sid}",
        "text": text, "created_at": "Tue Mar 21 20:50:14 +0000 2006",
        "likes": 311483, "reposts": 124680, "replies": 18071, "views": None,
        "author": {"screen_name": author, "name": "Jack"},
        "media": {"all": media_all, "photos": media_photos, "videos": media_videos},
    }


def _patch_api(monkeypatch, *, status=None, thread=None, replies=None,
               quotes=None, reposts=None):
    """把 fx 的四个端点换成合成数据（离线）。"""
    st = status if status is not None else _status()
    monkeypatch.setattr(fx, "get_status", lambda sid, timeout=15: {
        "raw": {"status": st, "code": 200}, "status": st,
        "author": st.get("author"), "thread": thread or []})
    monkeypatch.setattr(fx, "get_thread", lambda sid, timeout=15: {
        "raw": {"code": 200}, "status": st, "thread": thread or [],
        "author": st.get("author")})
    monkeypatch.setattr(fx, "get_conversation", lambda sid, timeout=15: {
        "raw": {"code": 200}, "status": st, "thread": thread or [],
        "replies": replies or [], "author": st.get("author")})
    monkeypatch.setattr(fx, "get_quotes", lambda sid, limit=20, timeout=15: {
        "raw": {"code": 200}, "results": quotes or [], "kind": "quotes"})
    monkeypatch.setattr(fx, "get_reposts", lambda sid, limit=20, timeout=15: {
        "raw": {"code": 200}, "results": reposts or [], "kind": "reposts"})


# ── 1. 解析 ───────────────────────────────────────────────────────────────

class TestExtractStatusId:
    @pytest.mark.parametrize("text,expected", [
        ("https://x.com/jack/status/20", "20"),
        ("https://twitter.com/a/statuses/1234567890?s=20", None),  # statuses ≠ status
        ("https://twitter.com/jack/status/1234567890?s=20", "1234567890"),
        ("https://x.com/i/web/status/1585841080431321088", "1585841080431321088"),
        ("1585841080431321088", "1585841080431321088"),
        ("20", "20"),
        ("not a tweet", None),
        ("", None),
    ])
    def test_parses(self, text, expected):
        assert fx.extract_status_id(text) == expected


# ── 2. 媒体选取 ───────────────────────────────────────────────────────────

class TestMediaSelection:
    def test_photos_and_videos_split(self):
        st = _status(photos=2, videos=1)
        assert len(fx.iter_photos(st)) == 2
        assert len(fx.iter_videos(st)) == 1

    def test_video_quality_max_picks_highest_bitrate(self):
        v = fx.iter_videos(_status(videos=1))[0]
        url, chosen = fx.pick_video_url(v, quality="max")
        assert url.endswith("_hi.mp4") and chosen["bitrate"] == 2000000

    def test_video_quality_min(self):
        v = fx.iter_videos(_status(videos=1))[0]
        url, _ = fx.pick_video_url(v, quality="min")
        assert url.endswith("_lo.mp4")

    def test_m3u8_never_chosen(self):
        v = {"url": "https://video.twimg.com/x.m3u8", "formats": [
            {"url": "https://video.twimg.com/x.m3u8", "container": "m3u8"}]}
        url, _ = fx.pick_video_url(v, quality="max")
        assert url is None, "m3u8 不可直下，必须跳过"


# ── 3. 全文不截断 ─────────────────────────────────────────────────────────

class TestFullText:
    def test_md_block_keeps_full_text(self):
        long = "字" * 500
        block = tw.status_to_md_block(_status(text=long))
        assert long in block, "post.md 必须给完整正文（这是本命令存在的理由）"

    def test_stdout_text_keeps_full_text(self, monkeypatch):
        _patch_api(monkeypatch)
        sid, bundle = tw.build_bundle("20")
        out = tw._render_text(sid, "status", bundle)
        assert "hello world" in out
        assert "311483" in out  # 互动数也在


# ── 4. 按需采用：默认不下载媒体 ───────────────────────────────────────────

class TestOnDemandMedia:
    def test_default_records_url_without_download(self, monkeypatch, tmp_path):
        _patch_api(monkeypatch, status=_status(photos=2))
        m = tw.pack("20", out_dir=tmp_path / "o", download_media=False)
        assert m["counts"]["images_ok"] == 0
        assert not (tmp_path / "o" / "media").exists(), "默认不该落任何媒体文件"
        assert all(x.get("skipped_download") for x in m["media"])
        assert any(x.get("url") for x in m["media"]), "URL 仍要记下来"

    def test_media_flag_downloads(self, monkeypatch, tmp_path):
        _patch_api(monkeypatch, status=_status(photos=1, videos=1))
        calls = []

        def fake_dl(url, dest, timeout=60):
            calls.append(url)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"\xff\xd8\xff" + b"0" * 32)
            return {"ok": True, "path": str(dest), "bytes": 35, "url": url, "error": None}

        monkeypatch.setattr(tw, "download_file", fake_dl)
        m = tw.pack("20", out_dir=tmp_path / "o", download_media=True)
        assert m["counts"]["images_ok"] == 1 and m["counts"]["videos_ok"] == 1
        assert len(calls) == 2
        assert m["checklist_pass"] is True


# ── 5. 产物完整性 ─────────────────────────────────────────────────────────

class TestArtifacts:
    def test_writes_four_artifacts(self, monkeypatch, tmp_path):
        _patch_api(monkeypatch)
        m = tw.pack("20", out_dir=tmp_path / "o")
        root = tmp_path / "o"
        for name in ("post.md", "raw.json", "manifest.json", "CHECKLIST.md"):
            assert (root / name).is_file(), f"缺 {name}"
        assert m["checklist_pass"] is True
        assert "PASS" in (root / "CHECKLIST.md").read_text(encoding="utf-8")
        # raw.json 必须是上游原始响应，不是再加工的投影
        assert "status" in json.loads((root / "raw.json").read_text(encoding="utf-8"))

    def test_partial_when_media_fails(self, monkeypatch, tmp_path):
        _patch_api(monkeypatch, status=_status(photos=1))
        monkeypatch.setattr(tw, "download_file", lambda url, dest, timeout=60: {
            "ok": False, "path": str(dest), "bytes": 0, "url": url, "error": "boom"})
        m = tw.pack("20", out_dir=tmp_path / "o", download_media=True)
        assert m["checklist_pass"] is False, "媒体失败必须标 PARTIAL"
        cl = (tmp_path / "o" / "CHECKLIST.md").read_text(encoding="utf-8")
        assert "PARTIAL" in cl and "boom" in cl
        # 正文仍交付（部分失败不等于整体失败）
        assert (tmp_path / "o" / "post.md").stat().st_size > 10

    def test_pack_renders_section_modes(self, monkeypatch, tmp_path):
        """section() 驱动的 mode 也要能落盘。

        曾在此崩：section 是嵌套函数，里面 `md_parts += [...]` 会把 md_parts
        变成它的局部名，导致前面的 append UnboundLocalError——而当时只测了
        JSON 路径与 status 模式，section 分支从未被跑到。
        """
        _patch_api(monkeypatch, reposts=[{"screen_name": "a", "name": "A"}])
        m = tw.pack("20", mode="reposts", out_dir=tmp_path / "o")
        md = (tmp_path / "o" / "post.md").read_text(encoding="utf-8")
        assert "转发用户" in md and "@a" in md
        assert m["post_count"] == 1

    def test_empty_result_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fx, "get_status", lambda sid, timeout=15: {
            "raw": {}, "status": {}, "author": None, "thread": []})
        with pytest.raises(ValueError):
            tw.pack("20", out_dir=tmp_path / "o")


# ── 6. 安全：SSRF 守卫 ────────────────────────────────────────────────────

class TestDownloadGuard:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/x.jpg",
        "http://localhost/x.jpg",
        "http://192.168.1.1/x.jpg",
        "file:///etc/passwd",
    ])
    def test_private_and_bad_scheme_rejected(self, url, tmp_path):
        r = tw.download_file(url, tmp_path / "x.jpg")
        assert r["ok"] is False
        assert "安全策略拒绝" in (r["error"] or "")

    def test_public_url_downloads(self, monkeypatch, tmp_path):
        class _Resp:
            status = 200

            def read(self):
                return b"\x89PNG\r\n\x1a\n" + b"0" * 16

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(tw, "open_url", lambda req, timeout=60: _Resp())
        dest = tmp_path / "ok.png"
        r = tw.download_file("https://pbs.twimg.com/media/x.png", dest)
        assert r["ok"] and dest.read_bytes().startswith(b"\x89PNG")


# ── 7. 结构化输出 ─────────────────────────────────────────────────────────

class TestJsonShape:
    def test_to_json_carries_full_payload(self, monkeypatch):
        _patch_api(monkeypatch, status=_status(photos=1))
        sid, bundle = tw.build_bundle("20")
        d = tw.to_json(sid, "status", bundle)
        assert d["schema_version"] == "argo-tweet/1.0"
        p = d["posts"][0]
        assert p["text"] == "hello world"
        assert p["images"], "媒体 URL 必须进结构化输出"
        assert p["likes"] == 311483

    def test_cli_json_is_compact(self, monkeypatch, capsys):
        """stdout 的 JSON 必须紧凑（与 test_context_budget 的常驻预算同向）。"""
        _patch_api(monkeypatch)
        assert tw.main(["20", "--json"]) == 0
        out = capsys.readouterr().out
        assert "\n  " not in out, "stdout 出现缩进 JSON"
        assert json.loads(out)["status_id"] == "20"


# ── 8. 各 mode 不串味 ─────────────────────────────────────────────────────

class TestModes:
    def test_conversation_splits(self, monkeypatch):
        thread = [_status("21", "second"), _status("22", "third")]
        replies = [_status("30", "a reply")]
        _patch_api(monkeypatch, thread=thread, replies=replies)
        sid, b = tw.build_bundle("20", mode="conversation")
        assert len(b["thread"]) == 2 and len(b["replies"]) == 1
        assert len(b["posts"]) == 4  # root + 2 thread + 1 reply

    def test_reposts_returns_users_not_posts(self, monkeypatch):
        users = [{"screen_name": "a"}, {"screen_name": "b"}]
        _patch_api(monkeypatch, reposts=users)
        sid, b = tw.build_bundle("20", mode="reposts")
        assert b["repost_users"] == users
        assert len(b["posts"]) == 1, "reposts 模式下 posts 只含主帖"

    def test_with_quotes_flag_adds_section(self, monkeypatch):
        _patch_api(monkeypatch, quotes=[_status("99", "quoted")])
        sid, b = tw.build_bundle("20", with_quotes=True)
        assert len(b["quotes"]) == 1


class TestPartialFailureVisible:
    """部分失败可以交付，但不能静默——原因必须落进产物。"""

    def test_root_error_surfaces_in_raw(self, monkeypatch, tmp_path):
        def boom(sid, timeout=15):
            raise fx.FxTwitterError("404 not found")

        monkeypatch.setattr(fx, "get_status", boom)
        monkeypatch.setattr(fx, "get_reposts", lambda sid, limit=20, timeout=15: {
            "raw": {"code": 200}, "results": [{"screen_name": "a"}], "kind": "reposts"})
        m = tw.pack("20", mode="reposts", out_dir=tmp_path / "o")
        raw = json.loads((tmp_path / "o" / "raw.json").read_text(encoding="utf-8"))
        assert "status_error" in raw, "主帖失败原因被静默丢弃"
        assert m["counts"]["repost_users"] == 1, "转发用户仍应交付"


# ── 9. CLI 入口 ───────────────────────────────────────────────────────────

class TestCli:
    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as e:
            tw.main(["--help"])
        assert e.value.code == 0
        assert "argo tweet" in capsys.readouterr().out

    def test_bad_target_is_actionable(self, capsys):
        assert tw.main(["not-a-tweet-url"]) == 1
        assert "无法解析 status id" in capsys.readouterr().err
