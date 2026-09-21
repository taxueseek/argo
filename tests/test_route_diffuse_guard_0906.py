#!/usr/bin/env python3
"""test_route_diffuse_guard_0906 — 面查意图守卫 + 同构垃圾早停守卫 单测。

2026-09-06 实测根因修复的回归门：
  1. 「pnpm file: directory dependency no content hash reinstall」词面命中
     package_search 域 → 锁死 pypi，返回单字垃圾包；
  2. 「DeepSeek Harness DSH 插件开发」词面命中 ai_model 域 → 锁死 models_dev；
  3. pypi 单 token 包名垃圾结果词面覆盖 100% → 骗过 _query_coverage_ok 早停。

守卫语义：面查信号 > 意图豁免 > token 门槛；短查询点查行为不变。

2026-09-21：守卫从「Python 侧 `_POINTED_INTENT_RE` 字典表」搬成域声明的
`intent_required` 字段（见 route_domains 模块 docstring），函数名随之改为
`_intent_gate`。判据本身逐位不变——本文件保留原用例，只把夹具换成**编译态**
域（与 match_domains 交给守卫的形态一致），避免测试自己拼一个「看起来像域」的
裸字典而绕过真实契约。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from route_domains import (  # noqa: E402
    _compile_domain_patterns,
    _domain_fires,
    _intent_gate,
    match_domains,
)
from search import _query_coverage_ok  # noqa: E402
from config import load_config  # noqa: E402


PKG_DIFFUSE_Q = "pnpm file: directory dependency no content hash reinstall"
MODEL_DIFFUSE_Q = "DeepSeek Harness DSH 插件开发"


def _fake_domain(name: str) -> dict:
    """真实域声明 + 规范编译（不手搓裸字典，否则测不到 intent_required 契约）。"""
    declared = [d for d in load_config().get("domains", [])
                if isinstance(d, dict) and d.get("name") == name]
    if not declared:
        raise AssertionError(f"config.yaml 里没有域 {name!r}")
    return _compile_domain_patterns(declared)[0]


# ── 1. 词面命中前置态（确认误抢确实发生，守卫有靶子） ──
#
# 守卫现在是 match_domains 内部的一环，因此「未守卫的命中」不再能从
# match_domains 的返回值观察到；前置态改用原始触发词判据表达——它才是守卫
# 真正要拦的东西（触发词命中 ∧ 意图门否决 = 让位）。

def test_precondition_pkg_domain_trigger_fires():
    assert _domain_fires(_fake_domain("package_search"), PKG_DIFFUSE_Q), \
        "前置态变化：query 已不再词面命中 package_search"
    assert not any(h.get("name") == "package_search"
                   for h in match_domains(PKG_DIFFUSE_Q)), \
        "意图门没拦住（match_domains 是守卫后的结果）"


def test_precondition_model_domain_trigger_fires():
    assert _domain_fires(_fake_domain("ai_model"), MODEL_DIFFUSE_Q), \
        "前置态变化：query 已不再词面命中 ai_model"
    assert not any(h.get("name") == "ai_model"
                   for h in match_domains(MODEL_DIFFUSE_Q)), \
        "意图门没拦住（match_domains 是守卫后的结果）"


# ── 2. 守卫让位（负例） ──

def test_guard_pkg_diffuse_yields():
    hits = match_domains(PKG_DIFFUSE_Q)
    kept = _intent_gate(hits, PKG_DIFFUSE_Q)
    assert not any(h.get("name") == "package_search" for h in kept)


def test_guard_model_diffuse_yields():
    hits = match_domains(MODEL_DIFFUSE_Q)
    kept = _intent_gate(hits, MODEL_DIFFUSE_Q)
    assert not any(h.get("name") == "ai_model" for h in kept)


def test_guard_diffuse_signal_beats_intent():
    # 「报错」面查信号压过「安装」意图豁免：报错排查不该进包索引
    q = "npm 包 安装 报错"
    hits = match_domains(q)
    if any(h.get("name") == "package_search" for h in hits):
        kept = _intent_gate(hits, q)
        assert not any(h.get("name") == "package_search" for h in kept)


# ── 3. 点查正例不误伤（短查询 / 意图词命中 / 非点查域不受影响） ──

def test_keep_short_pointed_queries():
    for q in ("requests pypi", "GPT-4o", "serde crate", "pnpm add lodash"):
        hits = match_domains(q)
        if any(h.get("name") == "package_search" for h in hits):
            assert _intent_gate(hits, q) == hits, q


def test_keep_intent_word_hits():
    # token≥5 但意图词命中（安装/下载）→ 豁免保留
    q = "python 环境安装 requests 库 教程 推荐"
    hits = match_domains(q)
    if any(h.get("name") == "package_search" for h in hits):
        # 「教程」不是面查信号词，意图词「安装」命中 → 保留
        kept = _intent_gate(hits, q)
        assert any(h.get("name") == "package_search" for h in kept)


def test_keep_intent_word_no_space_cjk():
    """连写中文意图词（2026-09-07 审查修复）：\\b 对 CJK 失配，真实点查
    「python环境安装requests库」这类连写此前被误让位。"""
    q = "python环境安装requests库哪个版本好"
    hits = [_fake_domain("package_search")]
    assert _intent_gate(hits, q) == hits, "连写意图词未豁免"


def test_diffuse_still_yields_no_space_with_error_word():
    """连写含面查信号（报错）：让位语义不变（信号词 CJK 备选本就不带 \\b）。"""
    q = "npm包安装报错怎么排查"
    hits = [_fake_domain("package_search")]
    assert _intent_gate(hits, q) == []


def test_non_pointed_domain_untouched():
    hits = [_fake_domain("film_search"), _fake_domain("geo_places")]
    assert _intent_gate(hits, "a long theme sentence about movies") == hits


def test_fail_open_on_tokenizer_error(monkeypatch):
    hits = [_fake_domain("package_search")]
    import builtins

    real_import = builtins.__import__

    def broken_import(name, *a, **kw):
        if name == "tfidf_router":
            raise ImportError("broken")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    assert _intent_gate(hits, PKG_DIFFUSE_Q) == hits


# ── 4. 覆盖守卫：同构垃圾拒早停 ──

def _pkg_junk(n: int = 5) -> list[dict]:
    # pypi 域外误抢的典型形态：查询切词 → 逐词单 token 包名
    return [
        {"title": w, "url": f"https://pypi.org/project/{w}/",
         "snippet": f"{w} package · v0.0.1"}
        for w in ("dependency", "hash", "directory", "file", "no")[:n]
    ]


def test_coverage_rejects_homogeneous_junk():
    q = PKG_DIFFUSE_Q
    assert _query_coverage_ok(_pkg_junk(), q) is False


def test_coverage_still_accepts_normal_results():
    q = "pnpm file: directory dependency reinstall"
    goods = [
        {"title": "pnpm install no longer detects changes in file: dependencies",
         "snippet": "On pnpm v11 the second install returns Already up to date"},
        {"title": "How pnpm handles file: directory dependencies",
         "snippet": "file: protocol copies the package into node_modules"},
        {"title": "pnpm issue: workspace directory dependency stale",
         "snippet": "workaround with pnpm update or rm -rf node_modules"},
    ]
    assert _query_coverage_ok(goods, q) is True


def test_coverage_short_query_not_flagged():
    # 短查询（<3 token）不触发同构垃圾检测：正常单包名结果放行
    q = "serde crate"
    goods = [{"title": "serde", "snippet": "serde crate docs", "url": "https://crates.io/crates/serde"}]
    assert _query_coverage_ok(goods, q) is True


def test_coverage_empty_query_fail_open():
    assert _query_coverage_ok(_pkg_junk(), "") is True
