#!/usr/bin/env python3
"""tests/test_wechat_article.py — 微信文章多级降级抓取单元测试

覆盖：URL 验证、降级链编排、各通道成功/失败场景、输出格式。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from wechat_article import fetch_wechat_article, _http_fetch, _cdp_fetch


# ── URL 验证 ───────────────────────────────────────────────────────────────

def test_non_wechat_url_rejected():
    """非微信域名应被拒绝。"""
    result = fetch_wechat_article("https://example.com/article")
    assert not result["ok"]
    assert "仅支持 mp.weixin.qq.com" in result["error"]


def test_non_http_url_rejected():
    """非 http(s) URL 应被拒绝。"""
    result = fetch_wechat_article("ftp://mp.weixin.qq.com/s/xxx")
    assert not result["ok"]


# ── 通道1: HTTP 直取 ───────────────────────────────────────────────────────

def test_http_fetch_success():
    """HTTP 直取成功时返回结构化结果。"""
    mock_result = {
        "ok": True,
        "url": "https://mp.weixin.qq.com/s/abc",
        "title": "测试文章",
        "author": "作者",
        "content": "正文内容",
        "char_count": 4,
    }
    with patch("wechat_article.fetch_article", return_value=mock_result):
        result = _http_fetch("https://mp.weixin.qq.com/s/abc")
        assert result["ok"]
        assert result["title"] == "测试文章"


def test_http_fetch_failure():
    """HTTP 直取失败时返回错误。"""
    with patch("wechat_article.fetch_article", side_effect=Exception("robots.txt 拦截")):
        result = _http_fetch("https://mp.weixin.qq.com/s/abc")
        assert not result["ok"]
        assert "HTTP 直取失败" in result["error"]


# ── 通道2: CDP 浏览器 ──────────────────────────────────────────────────────

def test_cdp_fetch_success():
    """CDP 抓取成功时返回结构化结果。"""
    mock_browser_result = {
        "success": True,
        "content": "这是正文内容",
        "title": "文章标题",
        "html": '<div id="js_name">作者名</div><div id="publish_time">2026-10-11</div>'
                  '<img data-src="https://mmbiz.qpic.cn/img1.jpg">',
    }
    with patch("wechat_article.browser_fetch", return_value=mock_browser_result):
        result = _cdp_fetch("https://mp.weixin.qq.com/s/abc")
        assert result["ok"]
        assert result["title"] == "文章标题"
        assert result["author"] == "作者名"
        assert result["publish_time"] == "2026-10-11"
        assert result["fetch_method"] == "chrome_cdp"
        assert len(result["images"]) == 1


def test_cdp_fetch_failure():
    """CDP 抓取失败时返回错误。"""
    with patch("wechat_article.browser_fetch", return_value={
        "success": False, "error": "Chrome failed to start"
    }):
        result = _cdp_fetch("https://mp.weixin.qq.com/s/abc")
        assert not result["ok"]
        assert "Chrome failed to start" in result["error"]


# ── 降级链编排 ─────────────────────────────────────────────────────────────

def test_fallback_to_cdp_when_http_fails():
    """HTTP 失败时应降级到 CDP。"""
    with patch("wechat_article._http_fetch", return_value={"ok": False, "error": "拦截"}):
        with patch("wechat_article._cdp_fetch", return_value={
                "ok": True, "title": "CDP 文章", "content": "内容", "fetch_method": "chrome_cdp"
        }):
            with patch("wechat_article._sogou_fallback") as mock_sogou:
                result = fetch_wechat_article("https://mp.weixin.qq.com/s/abc")
                assert result["ok"]
                assert result["title"] == "CDP 文章"
                mock_sogou.assert_not_called()


def test_fallback_to_sogou_when_cdp_fails():
    """CDP 失败时应降级到搜狗。"""
    with patch("wechat_article._http_fetch", return_value={"ok": False, "error": "拦截"}):
        with patch("wechat_article._cdp_fetch", return_value={"ok": False, "error": "CDP 失败"}):
            with patch("wechat_article._sogou_fallback", return_value={
                "ok": True, "title": "搜狗文章", "content": "摘要", "fetch_method": "sogou_fallback"
            }):
                result = fetch_wechat_article("https://mp.weixin.qq.com/s/abc")
                assert result["ok"]
                assert result["title"] == "搜狗文章"


def test_all_channels_fail():
    """所有通道都失败时返回结构化错误。"""
    with patch("wechat_article._http_fetch", return_value={"ok": False, "error": "拦截"}):
        with patch("wechat_article._cdp_fetch", return_value={"ok": False, "error": "CDP 失败"}):
            with patch("wechat_article._sogou_fallback", return_value={"ok": False, "error": "搜狗失败"}):
                result = fetch_wechat_article("https://mp.weixin.qq.com/s/abc")
                assert not result["ok"]
                assert "所有通道均失败" in result["error"]


def test_empty_body_is_failure_and_falls_through():
    """空正文的「HTTP 200 错误页」不算成功，必须降级到下一通道。

    2026-10-12 实测：不存在的文章 URL 走 HTTP 直取返回 ok=True 但
    title/content 全空；此前被当成功直接交付（失败伪装成成功）。
    """
    empty = {"ok": True, "url": "https://mp.weixin.qq.com/s/gone",
             "title": "", "author": "", "content": "",
             "char_count": 0, "image_count": 0, "images": []}
    with patch("wechat_article._http_fetch", return_value=dict(empty)):
        with patch("wechat_article._cdp_fetch", return_value={
                "ok": True, "title": "CDP 文章", "content": "真正文",
                "char_count": 3, "fetch_method": "chrome_cdp"}):
            result = fetch_wechat_article("https://mp.weixin.qq.com/s/gone")
    assert result["ok"] and result["fetch_method"] == "chrome_cdp"
    assert result["content"] == "真正文"


def test_json_failure_exits_nonzero(monkeypatch):
    """--json 失败分支必须退非零（agent 按 rc 判成败；此前退 0）。"""
    import wechat_article

    def fake_main(argv):
        monkeypatch.setattr(sys, "argv", ["wechat_article.py", "https://mp.weixin.qq.com/s/x", "--json"])
        return wechat_article.main()

    with patch("wechat_article._http_fetch", return_value={"ok": False, "error": "a"}), \
            patch("wechat_article._cdp_fetch", return_value={"ok": False, "error": "b"}), \
            patch("wechat_article._sogou_fallback", return_value={"ok": False, "error": "c"}):
        with pytest.raises(SystemExit) as ei:
            fake_main(None)
    assert ei.value.code == 1


# ── 通道3: 搜狗兜底 ────────────────────────────────────────────────────────

def _fake_og_response(title: str = "测试标题"):
    """net_proxy.http_get 的替身：返回 (body_bytes, status)。"""
    return (f'<html><meta property="og:title" content="{title}"></html>'.encode(), 200)


def test_sogou_cmd_absolute_script_path():
    """子进程脚本路径必须绝对（曾用裸相对路径，非仓根 CWD 调用即断）。"""
    from wechat_article import _sogou_cmd
    cmd = _sogou_cmd("标题")
    assert Path(cmd[1]).is_absolute(), f"脚本路径必须绝对：{cmd[1]}"
    assert cmd[1].endswith("sogou_weixin_browser.py")
    assert Path(cmd[1]).is_file(), "指向的脚本必须真实存在"


def test_sogou_fallback_cwd_independent(tmp_path, monkeypatch):
    """CWD 换到任意目录，子进程仍拿到绝对脚本路径。"""
    import wechat_article

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        m = MagicMock()
        m.returncode = 1
        m.stderr = "boom"
        return m

    monkeypatch.chdir(tmp_path)
    with patch("wechat_article.http_get", return_value=_fake_og_response()), \
            patch.object(wechat_article.subprocess, "run", side_effect=fake_run):
        out = wechat_article._sogou_fallback("https://mp.weixin.qq.com/s/x")
    assert Path(captured["cmd"][1]).is_absolute()
    assert not out["ok"]
    assert "搜狗搜索失败" in out["error"]


def test_sogou_fallback_parses_yaml_list(tmp_path, monkeypatch):
    """搜狗返回 YAML 列表时取首条并回填结构化字段（走集中 loader）。"""
    import wechat_article

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = (
            "- url: https://mp.weixin.qq.com/s/fallback\n"
            "  title: 搜狗兜底标题\n"
            "  snippet: 兜底摘要内容\n")
        return m

    monkeypatch.chdir(tmp_path)
    with patch("wechat_article.http_get", return_value=_fake_og_response()), \
            patch.object(wechat_article.subprocess, "run", side_effect=fake_run):
        out = wechat_article._sogou_fallback("https://mp.weixin.qq.com/s/x")
    assert out["ok"]
    assert out["title"] == "搜狗兜底标题"
    assert out["fetch_method"] == "sogou_fallback"
    assert out["content"] == "兜底摘要内容"


def test_sogou_fallback_without_title_fails_fast():
    """取不到 og:title 时结构化报错，不发起搜狗子进程。"""
    import wechat_article

    with patch("wechat_article.http_get", return_value=(b"<html>no title</html>", 200)), \
            patch.object(wechat_article.subprocess, "run") as fake_run:
        out = wechat_article._sogou_fallback("https://mp.weixin.qq.com/s/x")
    assert not out["ok"]
    assert "无法获取标题" in out["error"]
    fake_run.assert_not_called()


# ── 输出格式 ───────────────────────────────────────────────────────────────

def test_output_contains_required_fields():
    """输出应包含必要字段。"""
    with patch("wechat_article._http_fetch", return_value={
        "ok": True, "url": "https://mp.weixin.qq.com/s/abc", "title": "标题", "author": "作者", "publish_time": "2026-10-11",
        "content": "正文", "char_count": 2, "image_count": 0, "images": [],
        "fetch_method": "http_direct",
    }):
        result = fetch_wechat_article("https://mp.weixin.qq.com/s/abc")
        for field in ["ok", "url", "title", "author", "content", "fetch_method"]:
            assert field in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_error_page_not_treated_as_success():
    """微信错误提示页（「参数错误」等短提示页，HTTP 200 有正文）不算成功。"""
    from wechat_article import _has_content
    err_page = {"ok": True, "title": "", "content": "参数错误",
                "char_count": 4, "image_count": 0, "images": []}
    assert not _has_content(err_page), "微信错误页必须判失败"
    # 正常文章：有标题
    assert _has_content({"ok": True, "title": "标题", "content": "短",
                         "char_count": 1, "images": []})
    # 无标题但有实质正文（≥60 字）
    assert _has_content({"ok": True, "title": "", "content": "x" * 80,
                         "char_count": 80, "images": []})
    # 无标题但有图（图集型文章）
    assert _has_content({"ok": True, "title": "", "content": "abc",
                         "char_count": 3, "images": ["u"]})
    # 特征串出现在正文中部（正常文章讨论该话题）不误杀——判据只看开头 20 字
    prefix = "今天我们聊一个技术话题，关于公众号风控机制的原理与应对方式。"
    assert len(prefix) > 20
    mid = prefix + "参数错误" + "y" * 40
    assert _has_content({"ok": True, "title": "T", "content": mid,
                         "char_count": len(mid), "images": []})
    # 特征串居首 + 有标题的长页仍判失败（CDP 错误页带 title 的形态）
    head = "环境异常" + "z" * 100
    assert not _has_content({"ok": True, "title": "访问受限", "content": head,
                             "char_count": len(head), "images": []})
