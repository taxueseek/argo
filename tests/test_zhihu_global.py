#!/usr/bin/env python3
"""tests/test_zhihu_global.py — 知乎全网搜索引擎接入测试 (v2.6.0)"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

def test_zhihu_global_in_config():
    """验证 zhihu_global 引擎已在 config.yaml 注册"""
    from config import load_config, get_engines
    cfg = load_config()
    engines = get_engines(cfg)
    assert 'zhihu_global' in engines, "zhihu_global 未在 engines 中注册"
    assert engines['zhihu_global'].get('enabled', False), "zhihu_global 未启用"
    print("✅ zhihu_global 引擎已注册并启用")

def test_zhihu_global_url():
    """验证 zhihu_global 使用专用 builder（type=zhihu_global，非通用 http）。

    专用 builder 内部固定调用 global_search 端点，config 无需再声明 url。
    """
    from config import load_config, get_engines
    cfg = load_config()
    engines = get_engines(cfg)
    spec = engines['zhihu_global']
    assert spec.get('type') == 'zhihu_global', f"type 应为 zhihu_global: {spec.get('type')}"
    assert spec.get('family') == 'web_general', "family 应为 web_general（真全网搜索）"
    print("✅ zhihu_global 专用 builder + family 正确")

def test_chinese_general_primary():
    """验证 chinese_general 域：bocha 主源 + zhihu_global 补充源。

    zhihu_global 需 ZHIHU_ACCESS_SECRET（本机已配置，routable）；
    作为中文通用搜索的补充源（combo 内），主源仍为免密钥的 bocha。
    """
    from config import get_domains
    domains = get_domains()
    for d in domains:
        if d.get('name') == 'chinese_general':
            assert d.get('primary') == 'bocha', \
                f"chinese_general primary 应为 bocha, 实际是 {d.get('primary')}"
            assert 'zhihu_global' in d.get('engines_combo', []), \
                "zhihu_global 应在 chinese_general combo 中作补充源"
            print("✅ chinese_general 域路由正确（bocha 主源 + zhihu_global 补充）")
            return
    assert False, "未找到 chinese_general 域"

def test_zhihu_global_tfidf_profile():
    """验证 domain_profiles.json 含 zhihu_global 条目。

    zhihu_global 是 cost_tier=api 的密钥引擎（env 就绪才 routable），
    不参与免密钥场景的 TF-IDF 语义路由，documents 为空是设计使然；
    断言条目存在即可（保证注册表派生一致）。
    """
    import json
    from pathlib import Path
    profile_path = Path(__file__).parent.parent / 'backends' / 'domain_profiles.json'
    with open(profile_path) as f:
        profiles = json.load(f)
    assert 'zhihu_global' in profiles, "zhihu_global 不在 domain_profiles 中"
    # label 应存在（引擎注册表派生的一致性校验）
    assert profiles['zhihu_global'].get('label'), "zhihu_global profile 缺 label"
    print("✅ zhihu_global TF-IDF profile 已注册")

def test_zhihu_global_authority():
    """验证 source_types_cn.json 含 zhihu 权威分"""
    import json
    from pathlib import Path
    auth_path = Path(__file__).parent.parent / 'backends' / 'source_types_cn.json'
    with open(auth_path) as f:
        auth = json.load(f)
    overrides = auth.get('authority_overrides', {})
    assert 'zhihu.com' in overrides or 'www.zhihu.com' in overrides, \
        "zhihu.com 权威分未添加"
    print("✅ zhihu 权威分已添加")

def test_route_zhihu_global():
    """验证中文通用查询的路由决策可用，且 chinese_general combo 遵守族契约。

    chinese_general 配置了 5 个 web_general 源（bocha/byted/zhihu_global/
    wechat_sogou/octen），family 去重裁到前 2 个（bocha/byted），
    zhihu_global 作为第三个 web 源被裁掉属契约语义（同族至多 2 个），
    但仍保留在 engines_fallback 可随时调用，且 site: 语法可精确站内搜索。
    查询可能被更具体的域（chinese_tech_deep/weather 等）截胡，这属正常分层。
    """
    from route import route_query
    result = route_query('Python 最佳实践')
    assert result.get('engine'), f"路由主源为空: {result}"
    combo = result.get('engines_combo') or []
    assert combo, f"路由 combo 不应为空: {result}"
    if result.get('domain') == 'chinese_general':
        web_count = sum(1 for e in combo if e in
                        ("bocha", "byted", "zhihu_global", "wechat_sogou", "octen"))
        assert web_count <= 2, f"chinese_general combo 违反 web 族契约: {combo}"
        assert 'zhihu_global' in (result.get('engines_fallback') or []), \
            "zhihu_global 应保留在 engines_fallback 中可随时调用"
    print(f"✅ 中文技术查询路由到: {result.get('domain')} / {result.get('engine')}")


def test_parse_site_filter_bare_host():
    """site: 裸域名 → Filter host== 表达式。"""
    from engines_builders_cn import _parse_site_filter
    expr, q = _parse_site_filter("site:zhuanlan.zhihu.com 量子计算")
    assert expr == 'host=="zhuanlan.zhihu.com"', expr
    assert q == "量子计算", q
    print(f"✅ 裸域名: {expr!r} / {q!r}")


def test_parse_site_filter_full_url():
    """site: 完整 URL → 剥离 scheme/路径为裸域名（修复 https 当 host 的边角）。"""
    from engines_builders_cn import _parse_site_filter
    expr, q = _parse_site_filter("site:https://blog.csdn.net/xxx/yyy 教程")
    assert expr == 'host=="blog.csdn.net"', expr
    assert q == "教程", q
    print(f"✅ 完整 URL: {expr!r} / {q!r}")


def test_parse_site_filter_host_with_port():
    """host: 带端口写法 → 剥离端口。"""
    from engines_builders_cn import _parse_site_filter
    expr, q = _parse_site_filter("host:blog.csdn.net:8080 关键词")
    assert expr == 'host=="blog.csdn.net"', expr
    assert q == "关键词", q
    print(f"✅ 带端口: {expr!r} / {q!r}")


def test_parse_site_filter_no_site():
    """无 site: 语法 → 原样返回。"""
    from engines_builders_cn import _parse_site_filter
    expr, q = _parse_site_filter("量子计算 最新进展")
    assert expr == "" and q == "量子计算 最新进展", (expr, q)
    print(f"✅ 无站点限定: {q!r}")


def test_parse_site_filter_chinese_value_stripped():
    """site: 后是中文（无效 host）→ 剥离词法片段但不出 Filter。"""
    from engines_builders_cn import _parse_site_filter
    expr, q = _parse_site_filter("site:量子计算 是什么")
    assert expr == "", expr
    assert q == "量子计算 是什么", q
    print(f"✅ 中文值剥离: {q!r}")


def test_parse_site_filter_first_occurrence_only():
    """多处 site: → 只取第一处，第二处保留为查询词。"""
    from engines_builders_cn import _parse_site_filter
    expr, q = _parse_site_filter("site:a.com site:b.com 查询")
    assert expr == 'host=="a.com"', expr
    assert q == "site:b.com 查询", q
    print(f"✅ 多处 site: {expr!r} / {q!r}")

def test_engine_names():
    """验证 _ENGINE_NAMES 包含 zhihu_global"""
    from route import _ENGINE_NAMES
    assert 'zhihu_global' in _ENGINE_NAMES, "zhihu_global 不在 _ENGINE_NAMES 中"
    print("✅ _ENGINE_NAMES 已更新")

if __name__ == '__main__':
    test_zhihu_global_in_config()
    test_zhihu_global_url()
    test_chinese_general_primary()
    test_zhihu_global_tfidf_profile()
    test_zhihu_global_authority()
    test_route_zhihu_global()
    test_engine_names()
    test_parse_site_filter_bare_host()
    test_parse_site_filter_full_url()
    test_parse_site_filter_host_with_port()
    test_parse_site_filter_no_site()
    test_parse_site_filter_chinese_value_stripped()
    test_parse_site_filter_first_occurrence_only()
    print("\n🎉 全部 zhihu_global 接入测试通过 (v2.6.0)")
