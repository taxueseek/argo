#!/usr/bin/env python3
"""test_route_matrix_coverage.py — 每个域至少要有一条路由用例（或登记为债务）。

## 治什么

2026-10-22 发现：97 个域里 **61 个零路由用例**。`company_search` 就是其中之一，
「中国企业股权/工商信息」被路由到本域却无中文能力的问题因此长期无人发现
（实测返回 5 条同名「XX科技有限公司」干扰）；`geo_places` 的「在哪」劫持企业
总部查询同理。**零用例 = 路由回归无人挡**。

本文件锁的是：域清单 vs 用例覆盖（`matrix_search_eval.ROUTE_MATRIX` 的
`domain` 字段 + `test_domain_pattern_precision.LEGITIMATE_ROUTES` 的期望域），
差集必须登记在 `_NO_CASE_DEBT` 里并说明理由。新增域若落入清单，要么补一条
用例，要么显式登记——不允许静默增长。

## 与既有门的关系

- `test_combo_budget_coverage` 管「前 2 位有没有通用源」（组合宽度）
- `gen_routing_snapshot` 管「这批用例的路由决策没被碰翻」（快照对账）
- 本文件管「每个域是否进了那批用例」（覆盖广度）

## 用法

    python3 -m pytest tests/test_route_matrix_coverage.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SCRIPTS))

from config import load_config  # noqa: E402
import matrix_search_eval as mse  # noqa: E402

# 债务清单：暂无路由用例的域（2026-10-22 登记）。
# 清单是「检查当前已知集合」而非「检查它们都对」——新增域若落进来，必须
# 显式决定是补用例还是登记，不允许静默增长（同 _NARROW_TOP2_DEBT 的语义）。
# 注意：`local_*` 六个域是 local-search 子技能的兜底域，触发词与主清单重叠，
# 优先级最低，但仍登记在册以免被认为「已覆盖」。
_NO_CASE_DEBT = {
    "ai_model", "aviation_weather", "chinese_tech_deep", "claim_check", "cn_encyclopedia",
    "cn_tech_community", "code_search", "crypto_search", "dataset_search",
    "dictionary_search", "earth_science", "em_news_search", "energy_grid",
    "entity_search", "fact_check", "futures_market", "fx_query", "game_search",
    "global_event", "hk_filings", "hot_trending", "image_search",
    "intl_news_flash", "japan_law", "jin10_flash", "kor_law", "law_text",
    "legal", "local_academic", "local_chinese", "local_code", "local_general",
    "local_news", "local_reference", "meme_slang", "ml_models", "news_realtime",
    "outbreak_health", "patent_search", "prediction_market", "protein_search",
    "redskill_search", "scholar_search", "sec_filings", "semantic_discovery",
    "shopping", "skill_search", "species_search", "stackoverflow_search",
    "standards", "tech_deep", "us_fundamentals", "us_legal", "v2ex_search",
    "web_archive", "web_docs", "wechat_search", "wenshu_query", "world_news",
    "zhihu_content", "zhihu_hot_list", "zhihu_user_data",
}


def _covered_domains() -> set[str]:
    """被路由用例覆盖的域。

    三类都算（都在 `test_domain_pattern_precision.py` 里）：
      - 正向（ROUTE_MATRIX 的 domain + LEGITIMATE_ROUTES 的期望域）：保护
        「该命中时命中」；
      - 反向（SUBSTRING_ACCIDENTS 的 forbidden 域）：保护「不该命中时不命中」
        ——域正则一旦放宽就会碰翻这些用例，所以它同样是该域路由的回归保护；
      - 多义（POLYSEMY_MISROUTES）：保护「整词多义的另一种说法不被误让位」。
    只有**三类都没有**的域才算零用例（那才是真正无人挡的）。
    """
    covered = {c["domain"] for c in mse.ROUTE_MATRIX if c.get("domain")}
    # 从源码抽（避免 import 触发 pytest 依赖）：三个列表都是 (query, domain) 元组
    src = (ROOT / "tests" / "test_domain_pattern_precision.py").read_text(encoding="utf-8")
    for name in ("LEGITIMATE_ROUTES", "SUBSTRING_ACCIDENTS", "POLYSEMY_MISROUTES"):
        block = re.search(rf"{name} = \[(.*?)\n\]", src, re.S)
        if block:
            covered |= set(re.findall(r'"([a-z_0-9]+)"\)', block.group(1)))
    return covered


def test_zero_case_domains_are_registered():
    domains = {d["name"] for d in load_config()["domains"]}
    zero = domains - _covered_domains()
    new = sorted(zero - _NO_CASE_DEBT)
    assert not new, (
        "新增零用例域：路由回归无人挡（company_search 就是因此漏了"
        "「中国企业工商信息」问题）。请补一条用例，或登记进 "
        f"_NO_CASE_DEBT 并说明理由：\n  {new}"
    )
    stale = sorted(_NO_CASE_DEBT - zero)
    assert not stale, (
        "这些域已有用例，应从 _NO_CASE_DEBT 删除，否则清单失真：\n  " + "\n  ".join(stale)
    )


def test_debt_entries_are_real_domains():
    domains = {d["name"] for d in load_config()["domains"]}
    unknown = sorted(_NO_CASE_DEBT - domains)
    assert not unknown, f"债务清单里的域已不存在: {unknown}"
