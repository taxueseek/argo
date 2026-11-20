#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""围墙花园站点画像：只读登录 Cookie 探针 + 声明式同源搜索 API 预设。

设计原则（唯一真源）：
  1. auth_probe 只声明「登录态标识 Cookie 名称」，绝不读取、打印或持久化 Cookie 值；
     在页面返回 200 推荐壳子时，依靠关键 Cookie 缺失将模糊异常精准翻转为 auth_failed
     + handoff_required。
  2. inpage_api 预设让高频围墙站通过 `ego-search api --preset <site> --query <q>`
     在真实浏览器上下文直取结构化 JSON，替代脆弱的 DOM 解析与人工拼 URL。
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SiteProfile:
    name: str
    domains: tuple[str, ...]
    login_url: str
    cookie_names: tuple[str, ...]
    api_origin: str = ""
    api_url_template: str = ""
    api_default_n: int = 10
    # L0：免登录公开检索范围（供未开 pro-mode 或触发限权/冷却时降级提示）
    public_web_scopes: tuple[str, ...] = ()
    # L1：静默限权（Shadow Restriction）二次懒探针端点与特征码
    health_probe_url: str = ""
    restricted_codes: tuple[int, ...] = ()
    restricted_keywords: tuple[str, ...] = ()


SITE_PROFILES: dict[str, SiteProfile] = {
    "zhihu": SiteProfile(
        name="zhihu",
        domains=("zhihu.com",),
        login_url="https://www.zhihu.com/signin",
        cookie_names=("z_c0",),
        api_origin="https://www.zhihu.com",
        api_url_template="https://www.zhihu.com/api/v4/search_v3?t=general&q={q}&offset=0&limit={n}",
        public_web_scopes=("zhihu.com/question", "zhuanlan.zhihu.com/p"),
        health_probe_url="https://www.zhihu.com/api/v4/me",
        restricted_codes=(40352, 40362),
        restricted_keywords=("账号异常", "安全验证", "请完成验证"),
    ),
    "xiaohongshu": SiteProfile(
        name="xiaohongshu",
        domains=("xiaohongshu.com", "xhslink.com"),
        login_url="https://www.xiaohongshu.com/explore",
        cookie_names=("web_session", "a1"),
        api_origin="https://www.xiaohongshu.com",
        api_url_template="https://www.xiaohongshu.com/search_result?keyword={q}&source=web_search_result_notes",
        public_web_scopes=("xiaohongshu.com/explore", "xiaohongshu.com/discovery/item"),
        health_probe_url="https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
        restricted_codes=(300011, 300012, -100),
        restricted_keywords=("当前账号存在异常", "请切换账号", "账号异常", "安全限制"),
    ),
    "x": SiteProfile(
        name="x",
        domains=("x.com", "twitter.com"),
        login_url="https://x.com/i/flow/login",
        cookie_names=("auth_token", "ct0"),
        public_web_scopes=("x.com", "twitter.com"),
        restricted_codes=(326, 64),
        restricted_keywords=("account is temporarily locked", "suspended", "unusual activity"),
    ),
    "xueqiu": SiteProfile(
        name="xueqiu",
        domains=("xueqiu.com",),
        login_url="https://xueqiu.com/",
        cookie_names=("xq_a_token", "u"),
        api_origin="https://xueqiu.com",
        api_url_template="https://xueqiu.com/query/v1/symbol/search/status.json?q={q}&count={n}&page=1",
        public_web_scopes=("xueqiu.com",),
        health_probe_url="https://xueqiu.com/account/status.json",
        restricted_codes=(400016, 400018),
        restricted_keywords=("访问过于频繁", "账号异常"),
    ),
    "weibo": SiteProfile(
        name="weibo",
        domains=("weibo.com", "s.weibo.com"),
        login_url="https://weibo.com/login.php",
        cookie_names=("SUB", "SUBP"),
        api_origin="https://weibo.com",
        api_url_template="https://weibo.com/ajax/side/search?q={q}",
        public_web_scopes=("weibo.com", "s.weibo.com"),
        health_probe_url="https://weibo.com/ajax/profile/info",
        restricted_codes=(100003, 20003),
        restricted_keywords=("账号异常", "访问频次过高"),
    ),
    "bilibili": SiteProfile(
        name="bilibili",
        domains=("bilibili.com",),
        login_url="https://passport.bilibili.com/login",
        cookie_names=("SESSDATA", "bili_jct"),
        api_origin="https://www.bilibili.com",
        api_url_template="https://api.bilibili.com/x/web-interface/search/all/v2?keyword={q}&page_size={n}",
        public_web_scopes=("bilibili.com/video", "bilibili.com/read"),
        health_probe_url="https://api.bilibili.com/x/web-interface/nav",
        restricted_codes=(-101, -352, -412),
        restricted_keywords=("风控", "请求被拦截", "未登录"),
    ),
}

_ALIASES: dict[str, str] = {
    "twitter": "x",
    "xhs": "xiaohongshu",
    "bili": "bilibili",
}


def get_profile(name: str) -> SiteProfile | None:
    key = (name or "").strip().lower()
    key = _ALIASES.get(key, key)
    return SITE_PROFILES.get(key)


def match_site(url_or_host: str) -> SiteProfile | None:
    """根据完整 URL 或主机名匹配站点画像。"""
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return None
    host = urllib.parse.urlparse(raw).hostname if "://" in raw else raw.split("/", 1)[0]
    host = (host or "").strip(".").lower()
    if not host:
        return None
    for profile in SITE_PROFILES.values():
        for dom in profile.domains:
            if host == dom or host.endswith("." + dom):
                return profile
    return None


def resolve_preset_api(preset: str, query: str, *, n: int = 10) -> dict[str, Any]:
    """把站点预设 + 查询词展开为同源 API URL 与 origin。"""
    profile = get_profile(preset)
    if profile is None:
        valid = ", ".join(sorted(k for k, v in SITE_PROFILES.items() if v.api_url_template))
        raise ValueError(f"未知站点预设 {preset!r}；支持：{valid}")
    if not profile.api_url_template or not profile.api_origin:
        raise ValueError(f"站点 {profile.name} 暂无声明式同源 API 模板，请直接传入 api_url")
    q_enc = urllib.parse.quote(query.strip())
    limit = max(1, min(int(n), 50))
    return {
        "site": profile.name,
        "origin": profile.api_origin,
        "api_url": profile.api_url_template.format(q=q_enc, n=limit),
        "login_url": profile.login_url,
        "cookie_names": list(profile.cookie_names),
    }


def list_api_presets() -> list[str]:
    return sorted(k for k, v in SITE_PROFILES.items() if v.api_url_template)
