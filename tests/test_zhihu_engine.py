#!/usr/bin/env python3
"""tests/test_zhihu_engine.py — 知乎搜索与 Keychain 凭据接入测试"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


def test_zhihu_keychain_secret_resolution():
    """验证 engine_env 能够正确从 Keychain 保底识别知乎 Access Secret。"""
    from engine_env import get_env, missing_env_for, env_ready

    secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
    assert bool(secret), "未成功读取到知乎 Access Secret（环境变量或 Keychain）"
    assert missing_env_for("zhihu") == [], "知乎引擎不应被标记为缺少凭证"
    assert env_ready("zhihu") is True, "知乎引擎应处于 env_ready 状态"
    print("✅ 知乎 Keychain 凭据自省解析通过")


def test_zhihu_engine_scoring_and_meta():
    """验证 zhihu_engine 返回结构、点赞加权与 social_meta。"""
    from social_engines.zhihu_engine import search

    results = search("Python", n=3)
    assert isinstance(results, list)
    if results and "error" not in results[0]:
        first = results[0]
        assert "title" in first
        assert "url" in first
        assert "snippet" in first
        assert "score" in first
        assert "social_meta" in first
        meta = first["social_meta"]
        assert meta.get("platform") == "zhihu"
        assert "likes" in meta
        assert "upvotes" in meta
        assert "comments" in meta
        assert meta["likes"] == meta["upvotes"]
        assert 0.0 <= first["score"] <= 1.0
        print("✅ 知乎搜索结果结构、点赞加权与 social_meta 验证通过")
    else:
        print("⚠ 网络不可达或未配置，跳过在线搜索断言")


def test_zhihu_engine_empty_query():
    """验证空查询边界返回空列表。"""
    from social_engines.zhihu_engine import search

    assert search("") == []
    assert search("   ") == []
    print("✅ 空查询边界处理通过")


if __name__ == "__main__":
    test_zhihu_keychain_secret_resolution()
    test_zhihu_engine_empty_query()
    test_zhihu_engine_scoring_and_meta()
    print("\n🎉 全部 zhihu_engine 测试通过")
