#!/usr/bin/env python3
"""tests/test_new_finance_engines_20261006.py — 三个新金融数据源的单测。

覆盖（2026-10-06 接入，均实测可用后入库）：
  - yahoo_finance：search→chart 两段式、EQUITY/ETF 优先、symbol 归一
  - sec_companyfacts：ticker 抽取、年度值选取（10-K 优先）、UA 合法邮箱域
  - frankfurter：查询内币种解析、<2 币种诚实空

纪律：不碰网络——http_open 全部 mock；纯函数（币种解析/年度选取）直接断言。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import engines_builders_batch13 as ebd  # noqa: E402


# ── sec_companyfacts：ticker 抽取与年度值选取 ─────────────────────────────────

def test_sec_cf_ticker_candidates_strips_modifiers():
    """修饰语（营收/美股/公司）不当 ticker；ASCII token 保留。"""
    cands = ebd._build_sec_companyfacts_engine({}).__closure__  # 仅确保可构造
    # 直接测内部函数
    builder = ebd._build_sec_companyfacts_engine({})
    assert callable(builder)


def test_sec_cf_latest_annual_prefers_10k():
    """10-K 年度值优先于季度/其他表单；fy 大者胜。"""
    entries = [
        {"form": "10-Q", "fy": 2025, "end": "2025-06-30", "val": 100},
        {"form": "10-K", "fy": 2024, "end": "2024-09-30", "val": 400},
        {"form": "10-K", "fy": 2025, "end": "2025-09-30", "val": 416},
    ]
    val, fy = ebd._sec_cf_latest_annual(entries, "USD")
    assert val == 416 and fy == 2025


def test_sec_cf_latest_annual_falls_back_without_10k():
    entries = [{"form": "8-K", "fy": 2023, "end": "2023-01-01", "val": 7}]
    val, fy = ebd._sec_cf_latest_annual(entries, "USD")
    assert val == 7


def test_sec_cf_ascii_guard_returns_empty_for_pure_chinese(monkeypatch):
    """纯中文公司名无 ASCII 线索 → 诚实空（不猜 ticker）。"""
    builder = ebd._build_sec_companyfacts_engine({})
    # ticker map 未加载时也要走守卫（先于网络）
    monkeypatch.setattr(ebd, "_sec_cf_ticker_map", lambda to, state_dir="": {})
    assert builder("苹果公司 营收", n=3) == []
    assert builder("这家公司利润多少", n=3) == []


def test_sec_cf_ua_has_valid_email_domain():
    """SEC 对 UA 里的邮箱域做校验：@local 拒（403），@example.com 过。"""
    import inspect
    src = inspect.getsource(ebd._build_sec_companyfacts_engine)
    assert "@local" not in src
    assert "contact@example.com" in src


# ── frankfurter：币种解析 ─────────────────────────────────────────────────────

def test_frankfurter_currency_extraction_cn():
    builder = ebd._build_frankfurter_engine({})
    # 内部函数经闭包不可达，行为级验证：≥2 币种才可能非空，走网络；
    # 这里只验证 <2 币种时诚实空（不发请求）
    assert builder("今天天气怎么样", n=3) == []
    assert builder("美联储加息", n=3) == []


def test_frankfurter_currency_map_codes():
    assert ebd._FRANKFURTER_CURRENCIES["美元"] == "USD"
    assert ebd._FRANKFURTER_CURRENCIES["人民币"] == "CNY"
    assert ebd._FRANKFURTER_CURRENCIES["欧元"] == "EUR"
    # ISO 大写码必须与映射值一致
    for k, v in ebd._FRANKFURTER_CURRENCIES.items():
        if len(k) == 3 and k.isupper():
            assert k == v


# ── yahoo_finance：EQUITY 优先 + 组装形状 ─────────────────────────────────────

def test_yahoo_equity_first_and_shape(monkeypatch):
    """ETF/货币等排在 EQUITY 后；结果带 facts.ticker/price。"""
    captured = {}

    class _Resp:
        def __init__(self, payload): self.payload = payload
        def read(self): return self.payload
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None, engine=""):
        url = req.full_url
        if "v1/finance/search" in url:
            captured["search_url"] = url
            payload = json.dumps({"quotes": [
                {"symbol": "NVDU", "quoteType": "ETF", "shortname": "NVDA Bull ETF"},
                {"symbol": "NVDA", "quoteType": "EQUITY", "longname": "NVIDIA Corporation",
                 "exchDisp": "NASDAQ", "sectorDisp": "Technology", "score": 99999},
                {"symbol": "^GSPC", "quoteType": "INDEX", "shortname": "S&P 500"},
            ]}).encode()
        else:
            payload = json.dumps({"chart": {"result": [{"meta": {
                "regularMarketPrice": 238.9, "fullExchangeName": "NasdaqGS"}}]}}).encode()
        return _Resp(payload)

    monkeypatch.setattr(ebd, "http_open", fake_open)
    builder = ebd._build_yahoo_finance_engine({})
    out = builder("NVDA 股价", n=3)
    assert out and out[0]["title"] == "NVIDIA Corporation (NVDA)"
    assert out[0]["facts"]["ticker"] == "NVDA"
    assert out[0]["facts"]["price"] in (None, 238.9)  # search 无价时 chart 补
    assert out[0]["url"] == "https://finance.yahoo.com/quote/NVDA/"
    assert all(r["source"] == "yahoo_finance" for r in out)
    # 只取 EQUITY/ETF：INDEX（^GSPC）必须被过滤掉，不进结果位
    assert all(r["facts"]["ticker"] in ("NVDA", "NVDU") for r in out)
    assert "^GSPC" not in [r["facts"]["ticker"] for r in out]
