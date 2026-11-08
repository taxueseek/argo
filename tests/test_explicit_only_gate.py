#!/usr/bin/env python3
"""test_explicit_only_gate.py — explicit_only 声明必须是路由侧的硬契约。

根因（2026-10-19 定位）：`config._routable_filter` 只过滤 env 黑白名单、
密钥就绪、准入拉黑三项，不过滤 `explicit_only`；而 `engine_status` 的
routable 判定过滤了它，两边代码注释写的却是「同一口径」。于是 route_query
的 `enabled = get_engines(routable_only=True)` 候选池里躺着全部 18 个
explicit_only 引擎，TF-IDF 注入、catch-all 注入、backfill、breaker_filter、
tfidf_lead_combo、通用兜底 `sorted(enabled)[:2]` 全都能选中它们。

此前没爆只因语料空缺：18 个 explicit_only 引擎都不在
backends/domain_profiles.json 的 documents 里，semantic_route 打不到分。
这是数据兜底不是契约兜底——谁给语料补一条词条，auto 档（默认档不做成本
过滤）就会把唯一 cost_tier=paid 的 deepseek_search 并行调起来扣费。
实测（patch semantic_route 打 0.30 高分）：deepseek_search 在真机带 key
时进入 combo（engines=['deepseek_search','anysearch']，reason「TF-IDF
语义路由 → DeepSeek Search」）；ecosia/qwant/tineye/gdelt 等 13 个免 key
显式源在隔离环境同样进入 combo。

本文件锁三件事：
  ① 路由侧：patch 语义路由给 explicit_only 引擎打高分，combo 不得含它
     （强证据主位与弱证据辅源位各一条）；
  ② 候选池侧：get_engines(routable_only=True) 排除全部 explicit_only 引擎；
  ③ 显式路径不受牵连：--engine 指定照常单发（契约只挡自动分发）。

用 tinyfish（免 key 搜索引擎）而不是 deepseek_search 做路由侧样本：
conftest 把 ARGO_ENV_FILE 指向不存在的文件隔离真实密钥，带 key 的引擎在
测试环境被 env_ready 挡掉，会重现 conftest 自己警告的假绿。
"""

import os
import sys
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from route import route_query, TFIDF_STRONG_SCORE  # noqa: E402
from config import get_engines, load_config  # noqa: E402

# 无域 pattern 命中、走 TF-IDF 通用分支的查询（与 test_route_tfidf_weak_score 同型）
_QUERY = "nanjing food recommendations"
# 免 key 的 explicit_only 搜索引擎：测试环境里不被 env_ready 挡掉，
# 能真实走到注入分支（deepseek_search 带 key 时的同型泄漏，见模块 docstring）。
# 原样本 ecosia 已于 2026-11-08 停用（上游 bot 盾 403，禁用引擎被 routable
# 过滤挡住会让本契约测试退化成空转），换同样免 key、enabled 的 tinyfish。
_EXPLICIT_ONLY = "tinyfish"


def test_strong_tfidf_pick_cannot_be_explicit_only():
    """强证据语义推荐也不得把 explicit_only 引擎注入 combo 主位。"""
    strong = TFIDF_STRONG_SCORE + 0.15
    with patch("route.semantic_route",
               return_value=[(_EXPLICIT_ONLY, strong, f"sim={strong}")]):
        out = route_query(_QUERY)
    engines = out["engines"]
    assert engines, "combo 不能为空"
    assert _EXPLICIT_ONLY not in engines, (
        f"explicit_only 引擎进了自动分发 combo 主位：{engines}"
        f"（reason={out.get('reason')}）")
    # 通用保底从配置池判断，不写死 anysearch：2026-10-26 起池子按实测
    # 先验排序，top-2 可能是 tavily_free/parallel_free。
    from route_combo import _configured_general_pool
    assert set(engines) & set(_configured_general_pool() or []), \
        f"通用保底应在场：{engines}"


def test_weak_tfidf_pick_cannot_be_explicit_only():
    """弱证据辅源位同样不得放进 explicit_only 引擎（辅源也真发请求）。"""
    weak = 0.13  # TFIDF_MIN_SCORE(0.12) < weak < TFIDF_STRONG_SCORE(0.15)
    with patch("route.semantic_route",
               return_value=[(_EXPLICIT_ONLY, weak, f"sim={weak}")]):
        out = route_query(_QUERY)
    engines = out["engines"]
    assert _EXPLICIT_ONLY not in engines, (
        f"explicit_only 引擎以辅源身份漏进 combo：{engines}"
        f"（reason={out.get('reason')}）")


def test_routable_pool_excludes_every_explicit_only_engine():
    """候选池收口：routable_only=True 与 engine_status 的 routable 同口径。"""
    routable = set(get_engines(load_config(), routable_only=True))
    explicit = {
        name for name, spec in get_engines(load_config()).items()
        if isinstance(spec, dict) and spec.get("explicit_only")
    }
    assert explicit, "explicit_only 引擎集合不应为空（声明被误删？）"
    leaked = sorted(explicit & routable)
    assert not leaked, (
        f"explicit_only 引擎仍在自动路由候选池里：{leaked}"
        "——_routable_filter 少一道 explicit_only 过滤")


def test_explicit_engine_flag_still_dispatches():
    """契约只挡自动分发：用户显式 --engine 指定照常单发，不过滤。"""
    out = route_query("tinyfish 搜索测试", engine_override=_EXPLICIT_ONLY)
    assert out["engines"] == [_EXPLICIT_ONLY], (
        f"显式指定被误伤：{out['engines']}")
    assert out.get("reason", "").startswith("用户指定"), out.get("reason")
