#!/usr/bin/env python3
"""tests/test_zhihu_global.py — 知乎全网搜索引擎接入测试 (v2.5.1)"""

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
    """验证端点是 global_search 而非 zhihu_search"""
    from config import load_config, get_engines
    cfg = load_config()
    engines = get_engines(cfg)
    url = engines['zhihu_global'].get('url', '')
    assert 'global_search' in url, f"端点错误: {url}"
    assert 'zhihu_search' not in url, "不应使用 zhihu_search 端点"
    print(f"✅ 端点正确: {url}")

def test_chinese_general_primary():
    """验证 chinese_general 域 primary 是 zhihu_global"""
    from config import get_domains
    domains = get_domains()
    for d in domains:
        if d.get('name') == 'chinese_general':
            assert d.get('primary') == 'zhihu_global', \
                f"chinese_general primary 应为 zhihu_global, 实际是 {d.get('primary')}"
            assert 'zhihu_global' in d.get('engines_combo', []), \
                "zhihu_global 应在 chinese_general combo 中"
            print("✅ chinese_general 域路由正确")
            return
    assert False, "未找到 chinese_general 域"

def test_zhihu_global_tfidf_profile():
    """验证 domain_profiles.json 含 zhihu_global profile"""
    import json
    from pathlib import Path
    profile_path = Path(__file__).parent.parent / 'backends' / 'domain_profiles.json'
    with open(profile_path) as f:
        profiles = json.load(f)
    assert 'zhihu_global' in profiles, "zhihu_global 不在 domain_profiles 中"
    assert len(profiles['zhihu_global'].get('documents', [])) >= 5, "documents 太少"
    print("✅ zhihu_global TF-IDF profile 已创建")

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
    """验证路由能将含中文查询路由到 zhihu_global"""
    from route import route_query
    # 通用中文查询应命中 chinese_general 域，primary 为 zhihu_global
    result = route_query('Python 最佳实践')
    # chinese_general 域 primary 是 zhihu_global
    assert result['engine'] == 'zhihu_global', \
        f"期望 zhihu_global, 实际 {result['engine']}"
    print(f"✅ 通用中文查询路由到: {result['engine']}")

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
    print("\n🎉 全部 zhihu_global 接入测试通过 (v2.5.1)")
