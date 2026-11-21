#!/usr/bin/env python3
"""tests/test_ego_auth_cooldown.py — 登录态误判降级与冷却记账边界（2026-11-21 审计）。

覆盖四案：
  1. quality.assess_auth_with_cookies：Cookie 命中时正文嫌疑不得升级 auth_failed（误封锁站）；
  2. ego_search.emit_json：冷却键 URL 优先，与读取侧 _target_site_key 域名键对称；
  3. ego_search.emit_json：冷却记账锁超时不吞抓取结果（stderr 留痕照常输出）；
  4. pacing：同一状态文件单一写锁（跨写并发互不覆盖）+ tmp 名带线程号防同进程碰撞。
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ego_search  # noqa: E402
import pacing  # noqa: E402
import quality  # noqa: E402


def _fake_profile():
    return SimpleNamespace(
        name="xueqiu", login_url="https://xueqiu.com/login", cookie_names=["xq_a_token"],
    )


def test_matched_cookies_keep_ok_despite_body_suspect(monkeypatch):
    """Cookie 全命中 + 正文嫌疑 → 不得 auth_failed（旧第三子句误判回归）。"""
    import site_profiles
    monkeypatch.setattr(site_profiles, "match_site", lambda url: _fake_profile())
    q = quality.assess_auth_with_cookies(
        {"auth_wall_suspected": True, "content_chars": 400},
        url="https://xueqiu.com/today",
        present_cookie_names=["xq_a_token"],
        strict_cookie_check=False,
    )
    assert q["auth_state"] != "auth_failed"
    assert q["handoff_required"] is False


def test_all_missing_with_body_suspect_still_fails(monkeypatch):
    """Cookie 全缺失 + 正文异常 → 保持 auth_failed（既有语义不回退）。"""
    import site_profiles
    monkeypatch.setattr(site_profiles, "match_site", lambda url: _fake_profile())
    q = quality.assess_auth_with_cookies(
        {"auth_wall_suspected": True, "content_chars": 20},
        url="https://xueqiu.com/today",
        present_cookie_names=[],
        strict_cookie_check=False,
    )
    assert q["auth_state"] == "auth_failed"
    assert q["handoff_required"] is True


def test_strict_all_missing_fails_even_with_clean_body(monkeypatch):
    """strict=True（CDP 含 HttpOnly）+ Cookie 全缺失 → 200 壳子也判失效。"""
    import site_profiles
    monkeypatch.setattr(site_profiles, "match_site", lambda url: _fake_profile())
    q = quality.assess_auth_with_cookies(
        {"content_chars": 5000},
        url="https://xueqiu.com/today",
        present_cookie_names=[],
        strict_cookie_check=True,
    )
    assert q["auth_state"] == "auth_failed"


def test_emit_json_cooldown_key_symmetric_with_read_side(monkeypatch, capsys):
    """写入键与 _target_site_key 同源：URL 归一域名优先，画像名只兜底。"""
    calls = {}

    def fake_record(site, reason, **kwargs):
        calls["site"] = pacing.normalize_site(str(site))
        calls["reason"] = reason

    monkeypatch.setattr(pacing, "record_cooldown", fake_record)
    payload = {
        "handoff_required": True,
        "url": "https://www.xueqiu.com/today/123",
        # 画像名：旧顺序会被优先选中；auth_state 为冷却触发的合法前提
        "quality": {"site": "xueqiu", "auth_state": "auth_failed"},
    }
    ego_search.emit_json(payload)
    out = json.loads(capsys.readouterr().out)
    assert out["handoff_required"] is True
    # 读取侧对同一 URL 得到的键，写入侧必须一致
    assert calls["site"] == pacing.normalize_site("https://www.xueqiu.com/today/123") == "xueqiu.com"


def test_emit_json_survives_cooldown_lock_timeout(monkeypatch, capsys):
    """记账锁超时（SlotBusyError）不吞结果：JSON 照常输出，stderr 留痕。"""
    def boom(site, reason, **kwargs):
        raise pacing.SlotBusyError("waited >15.0s for slots pacing:state")

    monkeypatch.setattr(pacing, "record_cooldown", boom)
    ego_search.emit_json({
        "handoff_required": True,
        "url": "https://example.com/a",
        "quality": {"auth_state": "auth_failed"},
    })
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["handoff_required"] is True
    assert "record_cooldown failed" in captured.err


def test_pacing_concurrent_writers_both_persist(tmp_path):
    """pace_site 与 record_cooldown 并发：统一写锁后两份状态都落盘（旧双锁名会丢更新）。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(pacing.pace_site, "https://xueqiu.com", min_interval=0.05, root=tmp_path)
        f2 = ex.submit(pacing.record_cooldown, "https://xueqiu.com", "auth_failed", root=tmp_path)
        f1.result(timeout=30)
        f2.result(timeout=30)
    data = json.loads((tmp_path / "pacing_state.json").read_text(encoding="utf-8"))
    assert "xueqiu.com" in (data.get("request_starts") or {})
    assert "xueqiu.com" in (data.get("cooldowns") or {})


def test_atomic_write_json_thread_safe(tmp_path):
    """同进程两线程并发写同一文件：tmp 名带线程号，os.replace 不再互踩炸 FileNotFoundError。"""
    errors = []

    def write_loop(tag):
        for i in range(20):
            try:
                pacing._atomic_write_json(tmp_path / "state.json", {"tag": tag, "i": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(write_loop, ["a", "b"]))
    assert errors == []
    final = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert final.get("tag") in ("a", "b")


def test_unknown_site_text_suspicion_not_escalated():
    """无画像站点正文嫌疑不升级 auth_failed（探针 5：误封类第三分支）——
    没有 login_url 的 handoff 与冷却都是死路，正文含 cloudflare 等词不代表真登录墙。"""
    q = quality.assess_auth_with_cookies(
        {"auth_wall_suspected": True, "content_chars": 400},
        url="https://unknown-site-xyz.example.com/page",
        present_cookie_names=["a"],
        strict_cookie_check=False,
    )
    assert q.get("auth_state") != "auth_failed"
    assert q.get("handoff_required") is not True
    assert q["auth_wall_suspected"] is True  # 启发式嫌疑保留，不吞信号


def test_emit_json_no_cooldown_on_text_suspicion_alone(monkeypatch, capsys):
    """冷却只认登录态判定结论：裸 auth_wall_suspected（无 auth_state）不记冷却
    （探针 5 同根因——emit_json 旧条件挂文本启发式）。"""
    calls = []
    monkeypatch.setattr(pacing, "record_cooldown", lambda *a, **k: calls.append(a))
    ego_search.emit_json({
        "url": "https://unknown-site-xyz.example.com/page",
        "quality": {"auth_wall_suspected": True, "empty_or_thin": False},
    })
    assert calls == [], "文本嫌疑不得触发站点冷却"
    out = json.loads(capsys.readouterr().out)
    assert out["quality"]["auth_wall_suspected"] is True  # 结果照常输出


def test_emit_json_no_site_key_no_crash(monkeypatch, capsys):
    """handoff 但无任何站点键：不崩、不记冷却、JSON 照常输出（探针 4）。"""
    calls = []
    monkeypatch.setattr(pacing, "record_cooldown", lambda *a, **k: calls.append(a))
    ego_search.emit_json({"handoff_required": True, "quality": {}})
    assert calls == []
    assert json.loads(capsys.readouterr().out)["handoff_required"] is True
