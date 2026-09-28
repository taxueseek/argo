#!/usr/bin/env python3
"""route_bangs.py — DuckDuckGo Bangs 路由解析（!gh react → github 搜 react）。

2026-09-28 从 route.py 拆出：route.py 撞 1000 行体积门禁，而 Bangs 是
自包含的一小块「查表 + 首 token 解析」，与路由主流程只通过一个函数对接，
是门禁意义上最便宜的一刀。纯路由层逻辑：解析出目标引擎后走用户指定
引擎分支，不碰查询改写（那是 query_rewriter.py 的事）。
"""

from __future__ import annotations

# !bang → 引擎名。映射目标必须是 engines.py 已注册的引擎名；写错的键只在
# 搜索时以「未知引擎」暴露——启动路径不加注册表校验（低频热路径零开销），
# 一致性由 tests/test_route_bangs.py 的注册冒烟钉住。
_BANGS_MAP: dict[str, str] = {
    "!g": "local_bing",
    "!gh": "github",
    "!so": "stackoverflow",
    "!w": "wikipedia",
    "!gmaps": "local_openstreetmap",
}
# 注：!yt 不收录——原稿（9044621）映射到 google，而 google 引擎未注册
# （is_registered 实测 False），是带进来的死映射；等真有视频源再按注册名加回。
# 映射表只收已注册目标，由 tests/test_route_bangs.py 的注册冒烟钉住。


def resolve_bangs(query: str) -> tuple[str, str] | None:
    """解析首 token Bang：!gh react → ("github", "react")。

    只处理首 token，大小写不敏感（!GH 等价 !gh）；未收录的 Bang 返回
    None（走正常路由）。裸 Bang（无剩余查询）也解析——引擎点名成立，
    空查询交由调用方按既有引擎分支的空串语义处理。
    """
    if not query.startswith("!"):
        return None
    bang, sep, remainder = query.partition(" ")
    target = _BANGS_MAP.get(bang.lower())
    if not target:
        return None
    return target, (remainder.strip() if sep else "")
