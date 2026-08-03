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
    complement_refill,
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


# ── 互补回填（去重腾出预算位的兑现）────────────────────────────────────────

def test_refill_all_web_combo():
    """全 web combo 去重后回填互补能力族引擎，且不重排/删减原有 combo。"""
    cfg = load_config()
    eng = get_engines(cfg, routable_only=False)
    combo = ["bocha", "byted", "duckduckgo"]  # 3 个 web_general
    deduped = dedupe_by_family(combo, max_per_family=2, spec_lookup=eng)
    assert len(deduped) == 2, f"应去重到 2: {deduped}"
    filled = complement_refill(deduped, enabled=set(eng), spec_lookup=eng,
                               domain_primary="bocha", max_slots=2)
    assert filled[:2] == deduped, f"回填不得重排/删减原 combo: {filled}"
    fams = {family_of(e, eng.get(e)) for e in filled}
    assert len(fams) >= 2, f"回填后应含互补族: {filled} {fams}"
    # 回填引擎必须与主引擎 coverage 重叠（主题相关信号）
    prim_cov = set((eng.get("bocha") or {}).get("coverage") or [])
    assert prim_cov, "bocha 应有 coverage 标签"
    for e in filled[2:]:
        cov = set((eng.get(e) or {}).get("coverage") or [])
        assert cov & prim_cov, f"{e} 与主引擎 coverage 无重叠: {cov} vs {prim_cov}"
    print(f"  ✅ 全 web combo {deduped} → 回填 {filled[2:]}，族={sorted(fams)}")


def test_refill_skips_when_vertical_present():
    """已有垂直族成员的 combo 不回填（尊重域作者配置）。"""
    eng = {
        "byted": {"priority": 20, "coverage": ["general", "news"]},
        "duckduckgo": {"priority": 15, "coverage": ["general"]},
        "wenshu": {"priority": 61, "coverage": ["legal"], "family": "legal"},
        "arxiv": {"priority": 40, "coverage": ["academic"], "family": "academic"},
    }
    combo = ["byted", "duckduckgo", "wenshu"]  # web + legal
    out = complement_refill(combo, enabled={"arxiv"}, spec_lookup=eng,
                            domain_primary="byted", max_slots=2)
    assert out == combo, "已有垂直成员时不应回填"
    print("  ✅ 垂直族成员已存在时跳过回填")


def test_refill_requires_primary_coverage():
    """主引擎无 coverage 标签时不回填（无主题信号，不猜测）。"""
    eng = {
        "local_search": {"priority": 10},
        "bocha": {"priority": 20, "coverage": ["chinese", "general"]},
        "arxiv": {"priority": 30, "coverage": ["academic"]},
    }
    out = complement_refill(["local_search", "bocha"], enabled={"arxiv"},
                            spec_lookup=eng, domain_primary="local_search")
    assert out == ["local_search", "bocha"], f"不应回填: {out}"
    print("  ✅ 主引擎无 coverage 时保守跳过")


def test_refill_excludes_noise_families():
    """回填排除热榜族（hot_trending 等非查询相关源）。"""
    eng = {
        "bocha": {"priority": 20, "coverage": ["chinese", "general"]},
        "zhihu_hot": {"priority": 90, "coverage": ["chinese", "hot"]},
        "baidu_baike": {"priority": 41, "coverage": ["chinese"]},
    }
    out = complement_refill(["bocha", "byted"], enabled=set(eng), spec_lookup=eng,
                            domain_primary="bocha", max_slots=2)
    assert "zhihu_hot" not in out, "hot_trending 族不应回填"
    assert "baidu_baike" in out, "knowledge 族应回填"
    print(f"  ✅ 噪声族排除，回填 {out[2:]}")


def test_route_refill_deep_mode():
    """deep 模式下全 web 域回填互补族（预算不截断，回填生效）。"""
    from route import route_query
    d = route_query("怎么评价现在的创业环境", mode="auto", depth="deep")
    combo = d.get("engines_combo") or []
    fams = {family_of(e) for e in combo}
    assert len(fams) >= 2, f"deep 模式应含互补族: {combo}"
    print(f"  ✅ deep 路由 {combo} 族={sorted(fams)} 含互补源")


if __name__ == "__main__":
    for fn in (
        test_all_engines_have_family,
        test_zhihu_global_is_web_general,
        test_vertical_families_correct,
        test_dedupe_only_web_general,
        test_route_keeps_vertical_multi_source,
        test_route_uses_family_dedup,
        test_refill_all_web_combo,
        test_refill_skips_when_vertical_present,
        test_refill_requires_primary_coverage,
        test_refill_excludes_noise_families,
        test_route_refill_deep_mode,
    ):
        fn()
    print("\n🎉 能力族测试全部通过")
