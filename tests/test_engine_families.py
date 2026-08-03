#!/usr/bin/env python3
"""test_engine_families.py — 能力族分类与标准化调用契约测试

S 级要求：搜索源按能力族归组，同族可互换、可测试、可组合。
本测试验证：
  1. 全量引擎都能映射到能力族（无遗漏/无未知族）
  2. 关键引擎的族归属正确（zhihu_global=web_general 等）
  3. 同族去重只作用于 web_general，垂直族保留多源
  4. route 层应用 family 去重后路由不劣化
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from config import get_engines, load_config
from engine_families import (
    FAMILY_LABELS,
    family_of,
    group_by_family,
    dedupe_by_family,
)


def test_all_engines_have_family():
    """所有启用引擎都能映射到已知能力族。"""
    cfg = load_config()
    eng = get_engines(cfg, routable_only=False)
    unknown = [
        name for name, spec in eng.items()
        if family_of(name, spec) not in FAMILY_LABELS
    ]
    assert not unknown, f"引擎未映射到已知族: {unknown}"
    print(f"  ✅ {len(eng)} 引擎全部映射到 {len(FAMILY_LABELS)} 个能力族")


def test_zhihu_global_is_web_general():
    """zhihu_global 是真全网搜索（Filter host== 可搜非知乎站），归 web_general。"""
    cfg = load_config()
    eng = get_engines(cfg, routable_only=False)
    assert family_of("zhihu_global", eng.get("zhihu_global")) == "web_general"
    assert family_of("byted") == "web_general"
    assert family_of("zhihu") == "social", "zhihu 站内搜索才是 social"
    print("  ✅ zhihu_global=web_general, byted=web_general, zhihu=social")


def test_vertical_families_correct():
    """垂直引擎族归属正确。"""
    assert family_of("fred") == "finance_macro"
    assert family_of("sina_quote") == "finance_market"
    assert family_of("arxiv") == "academic"
    assert family_of("github") == "code"
    assert family_of("cls_telegraph") == "news_flash"
    assert family_of("baidu_hot") == "hot_trending"
    assert family_of("wikipedia") == "knowledge"
    assert family_of("pubchem") == "science_chem"
    assert family_of("courtlistener") == "legal"
    assert family_of("wayback_cdx") == "archive"
    print("  ✅ 10 个垂直族归属正确")


def test_dedupe_only_web_general():
    """同族去重只收缩 web_general，垂直族保留多源。"""
    cfg = load_config()
    eng = get_engines(cfg, routable_only=False)
    # web_general 3 个 + 垂直源 → web 去重到 2，垂直源保留
    web_combo = ["byted", "bocha", "duckduckgo", "zhihu", "arxiv"]
    deduped = dedupe_by_family(web_combo, max_per_family=2, spec_lookup=eng)
    web_kept = [e for e in deduped if family_of(e, eng.get(e)) == "web_general"]
    assert len(web_kept) == 2, f"web_general 应保留 2 个: {deduped}"
    assert "zhihu" in deduped and "arxiv" in deduped, "垂直源不应被去重"
    print("  ✅ dedupe_by_family 按族限流生效")


def test_route_keeps_vertical_multi_source():
    """route 层只对 web_general 去重，垂直域保留多源交叉验证。"""
    from route import route_query
    # academic 域应保留多个学术源
    d = route_query("transformer attention paper", mode="auto", depth="fast")
    combo = d.get("engines_combo") or []
    acad = [e for e in combo if family_of(e) == "academic"]
    assert len(acad) >= 2, f"academic 域应保留多源: {combo}"
    # macro 域应保留多个宏观源
    d2 = route_query("美国CPI数据", mode="auto", depth="fast")
    combo2 = d2.get("engines_combo") or []
    macro = [e for e in combo2 if family_of(e) == "finance_macro"]
    assert len(macro) >= 2, f"macro 域应保留多源: {combo2}"
    print(f"  ✅ academic={acad} finance_macro={macro} 多源保留")


def test_route_uses_family_dedup():
    """route 层应用 family 去重后，中文通用查询 combo 不含 3+ 个全网源。"""
    from route import route_query
    d = route_query("最近发生的新闻事件汇总", mode="auto", depth="fast")
    combo = d.get("engines_combo") or []
    web_count = sum(1 for e in combo if family_of(e) == "web_general")
    assert web_count <= 2, f"web_general 族源超过 2 个: {combo}"
    assert combo, "combo 不应为空"
    print(f"  ✅ route combo={combo} web_general={web_count} (≤2)")


if __name__ == "__main__":
    for fn in (
        test_all_engines_have_family,
        test_zhihu_global_is_web_general,
        test_vertical_families_correct,
        test_dedupe_only_web_general,
        test_route_keeps_vertical_multi_source,
        test_route_uses_family_dedup,
    ):
        fn()
    print("\n🎉 能力族测试全部通过")
