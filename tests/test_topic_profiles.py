#!/usr/bin/env python3
"""单元测试：topic_research_profiles — 专业域 + 触发词 + 自动推断"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from topic_research_profiles import (
    get_profile,
    list_profiles,
    apply_profile,
    detect_topic_from_query,
    is_deep_research_trigger,
    build_profile_sub_queries,
    list_triggers,
    DEEP_RESEARCH_TRIGGERS,
    RESEARCH_SLASH_COMMANDS,
)

# ── Profile 完整性 ──────────────────────────────────────────────────────────

def test_all_profiles_have_required_keys():
    """每个 profile 必须包含核心字段 + 专业门控"""
    required = {
        "name", "description", "engines_priority", "depth", "sub_queries",
        "max_results", "evidence_weights", "freshness_cutoff_days",
        "quality_gates", "report_sections", "source_grades", "discipline",
    }
    for p in list_profiles():
        key = p["key"]
        profile = get_profile(key)
        missing = required - set(profile.keys())
        assert not missing, f"profile '{key}' 缺少字段: {missing}"
        assert isinstance(profile["engines_priority"], list) and len(profile["engines_priority"]) > 0
        assert profile["depth"] in ("fast", "balanced", "deep")
        assert isinstance(profile["quality_gates"], list) and len(profile["quality_gates"]) >= 1
        assert isinstance(profile["report_sections"], list) and len(profile["report_sections"]) >= 1
        print(f"  ✅ {key} ({profile['name']}): discipline={profile['discipline']}, "
              f"gates={len(profile['quality_gates'])}, depth={profile['depth']}")


# ── 别名解析 ──────────────────────────────────────────────────

def test_aliases_resolve_correctly():
    cases = [
        ("ai", "AI / 大模型"), ("人工智能", "AI / 大模型"), ("大模型", "AI / 大模型"),
        ("investment", "投资理财"), ("投资", "投资理财"), ("理财", "投资理财"),
        ("finance", "金融深度研究"), ("金融", "金融深度研究"), ("研报", "金融深度研究"),
        ("academic", "科研 / 学术"), ("科研", "科研 / 学术"), ("文献", "科研 / 学术"),
        ("tech", "数码科技"), ("数码", "数码科技"), ("硬件", "数码科技"),
        ("tool", "效率工具"), ("工具", "效率工具"), ("效率", "效率工具"),
        ("internet", "互联网行业"), ("互联网", "互联网行业"),
        ("social", "社交舆情"), ("社交", "社交舆情"), ("口碑", "社交舆情"),
    ]
    for alias, expected in cases:
        prof = get_profile(alias)
        assert prof is not None, f"别名 '{alias}' 未解析"
        assert prof["name"] == expected, f"别名 '{alias}' → '{prof['name']}', 期望 '{expected}'"
    print(f"  ✅ 全部 {len(cases)} 个别名正确解析")


def test_unknown_alias_returns_none():
    assert get_profile("nonexistent_topic") is None
    assert get_profile("") is None
    print("  ✅ 未知别名返回 None")


# ── Apply Profile ──────────────────────────────────────────────────────────

def test_apply_profile_merges_correctly():
    profile = get_profile("ai")
    # 显式参数已在 kwargs 中 → 保留
    result = apply_profile(profile, {"sub_queries": 3, "max_results": 5, "depth": "balanced"})
    assert result["sub_queries"] == 3
    assert result["max_results"] == 5
    assert result["depth"] == "balanced"
    assert result["engines_priority"] == profile["engines_priority"]
    assert "quality_gates" in result
    print("  ✅ apply_profile 参数合并正确")


def test_apply_profile_fills_defaults():
    """kwargs 为空时填入 profile 默认"""
    profile = get_profile("finance")
    result = apply_profile(profile, {})
    assert result["depth"] == "deep"
    assert result["sub_queries"] == 5
    assert result["discipline"] == "finance"
    assert len(result["quality_gates"]) >= 3
    print(f"  ✅ finance 默认: depth={result['depth']}, sub={result['sub_queries']}")


def test_apply_profile_respects_explicit_overrides():
    profile = get_profile("tech")
    result = apply_profile(profile, {"sub_queries": 5, "max_results": 10, "depth": "deep"})
    assert result["sub_queries"] == 5
    assert result["max_results"] == 10
    assert result["depth"] == "deep"
    print("  ✅ 显式参数优先")


# ── 引擎与专业域 ──────────────────────────────────────────────────

def test_ai_profile_has_zhihu_and_arxiv():
    profile = get_profile("ai")
    engines = profile["engines_priority"]
    assert "zhihu" in engines
    assert "arxiv" in engines
    print(f"  ✅ AI profile: 引擎={engines}")


def test_investment_profile_has_eastmoney():
    profile = get_profile("investment")
    assert "eastmoney" in profile["engines_priority"]
    print(f"  ✅ 投资 profile: 引擎={profile['engines_priority']}")


def test_academic_profile_prefers_scholarly_engines():
    profile = get_profile("academic")
    engines = profile["engines_priority"]
    assert "arxiv" in engines
    assert any(e in engines for e in ("semantic_scholar", "openalex", "crossref"))
    assert profile["discipline"] == "academic"
    assert any("DOI" in g or "doi" in g.lower() for g in profile["quality_gates"])
    print(f"  ✅ academic: engines={engines[:4]}, gates={len(profile['quality_gates'])}")


def test_finance_profile_ic_style_gates():
    profile = get_profile("finance")
    assert profile["discipline"] == "finance"
    assert profile["depth"] == "deep"
    assert "eastmoney" in profile["engines_priority"]
    gate_text = " ".join(profile["quality_gates"])
    assert "盲区" in gate_text or "未找到" in gate_text
    assert "信源" in gate_text or "来源" in gate_text
    sections = " ".join(profile["report_sections"])
    assert "风险" in sections
    print(f"  ✅ finance: depth={profile['depth']}, sections={profile['report_sections'][:3]}")


def test_social_profile_has_platform_engines():
    profile = get_profile("social")
    engines = profile["engines_priority"]
    for e in ["zhihu", "xiaohongshu", "weibo", "reddit"]:
        assert e in engines
    print(f"  ✅ 社交 profile: 引擎={engines}")


# ── 新鲜度 ──────────────────────────────────────────────────

def test_freshness_cutoffs():
    assert get_profile("ai")["freshness_cutoff_days"] <= 180
    assert get_profile("investment")["freshness_cutoff_days"] <= 30
    assert get_profile("finance")["freshness_cutoff_days"] <= 30
    assert get_profile("academic")["freshness_cutoff_days"] >= 365
    assert get_profile("tech")["freshness_cutoff_days"] >= 90
    print("  ✅ 新鲜度阈值合理")


# ── 触发词与自动推断 ──────────────────────────────────────────────────

def test_deep_research_triggers():
    assert is_deep_research_trigger("请做一份深度研究：固态电池")
    assert is_deep_research_trigger("deep research on solid state battery")
    assert is_deep_research_trigger("需要文献综述 CRISPR")
    assert is_deep_research_trigger("/argo-research 台积电")
    assert not is_deep_research_trigger("搜一下天气")
    assert len(DEEP_RESEARCH_TRIGGERS) >= 8
    assert "/argo-research" in RESEARCH_SLASH_COMMANDS
    from topic_research_profiles import ARGO_MAIN_SLASH, ARGO_SUB_SLASH_COMMANDS
    assert ARGO_MAIN_SLASH == "/argo"
    assert "/argo-search" in ARGO_SUB_SLASH_COMMANDS
    assert "/argo-evidence" in ARGO_SUB_SLASH_COMMANDS
    print(f"  ✅ 触发词 {len(DEEP_RESEARCH_TRIGGERS)} 个, 主命令 {ARGO_MAIN_SLASH}, "
          f"子斜杠 {len(ARGO_SUB_SLASH_COMMANDS)} 个")


def test_detect_topic_from_query():
    assert detect_topic_from_query("CRISPR 文献综述 arxiv") == "academic"
    assert detect_topic_from_query("台积电 一致预期 目标价 分歧") == "finance"
    assert detect_topic_from_query("Claude 大模型 benchmark") == "ai"
    assert detect_topic_from_query("产品口碑 吐槽") == "social"
    assert detect_topic_from_query("完全无关的随机串 xyzabc123") is None
    print("  ✅ 自动选题推断")


def test_build_profile_sub_queries():
    prof = get_profile("academic")
    subs = build_profile_sub_queries("CRISPR off-target", prof, num_sub=3)
    assert len(subs) == 3
    assert all("query" in s and "intent" in s for s in subs)
    assert any("CRISPR" in s["query"] for s in subs)
    print(f"  ✅ 模板子查询: {[s['query'][:40] for s in subs]}")


def test_list_triggers_shape():
    t = list_triggers()
    assert "deep_research_triggers" in t
    assert "slash_commands" in t
    assert "main_slash" in t
    assert t["main_slash"] == "/argo"
    assert "research_slash_commands" in t
    assert "topics" in t
    keys = {p["key"] for p in t["topics"]}
    assert "academic" in keys and "finance" in keys
    print(f"  ✅ list_triggers main={t['main_slash']} topics={sorted(keys)}")


if __name__ == "__main__":
    tests = [
        ("Profile 完整性", test_all_profiles_have_required_keys),
        ("别名解析", test_aliases_resolve_correctly),
        ("未知别名 → None", test_unknown_alias_returns_none),
        ("参数合并", test_apply_profile_merges_correctly),
        ("空 kwargs 填默认", test_apply_profile_fills_defaults),
        ("显式参数优先", test_apply_profile_respects_explicit_overrides),
        ("AI → zhihu+arxiv", test_ai_profile_has_zhihu_and_arxiv),
        ("投资 → eastmoney", test_investment_profile_has_eastmoney),
        ("学术 → 学术引擎+门禁", test_academic_profile_prefers_scholarly_engines),
        ("金融 → IC 门禁", test_finance_profile_ic_style_gates),
        ("社交 → 多平台", test_social_profile_has_platform_engines),
        ("新鲜度阈值", test_freshness_cutoffs),
        ("深度研究触发词", test_deep_research_triggers),
        ("自动选题", test_detect_topic_from_query),
        ("模板子查询", test_build_profile_sub_queries),
        ("list_triggers", test_list_triggers_shape),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            failed += 1
        print()

    print(f"=" * 40)
    print(f"结果: {passed}/{len(tests)} 通过, {failed} 失败")
    sys.exit(0 if failed == 0 else 1)
