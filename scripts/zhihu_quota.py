#!/usr/bin/env python3
"""zhihu_quota.py — 知乎开放平台额度查询（官方 `/api/v1/quota`）。

为什么需要（2026-10-21）：知乎各能力的每日免费额度是**账号级**的，同一账号的
多个 Access Secret 共用；本地按引擎计数会与真实额度漂移。官方额度查询不消耗
业务额度，是唯一权威来源。本模块只做读取与缓存，不参与路由决策（路由仍用
quota.py 的本地计数），避免每次搜索多一次外呼。

用法：
  argo quota            # 人读表
  argo quota --json     # 机器读
  argo quota --refresh  # 跳过 10 分钟缓存
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

from cli_io import dumps
from engine_env import get_env
from engines_http import UA
from engines_base import http_open

API_URL = "https://developer.zhihu.com/api/v1/quota"
CACHE_TTL_S = 600

# APIID → argo 引擎名（与 config.yaml / quota_profiles.json 对齐）
API_TO_ENGINE = {
    "global_search": "zhihu_global",
    "zhihu_search": "zhihu",
    "hot_list": "zhihu_hot",
    "question_discovery": "zhihu_questions",
    "zhida_openai": "zhida",
    "tools": "zhihu_tools",
    "knowledge": "zhihu_kb",
    "user_data": "zhihu_user",
    "creator": "zhihu_creator",
}


def _cache_path():
    import argo_paths
    return argo_paths.state_path("zhihu_quota.json")


def _secret() -> str:
    return get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])


def _load_cache() -> dict | None:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if time.time() - float(raw.get("ts") or 0) > CACHE_TTL_S:
        return None
    return raw


def _save_cache(items: list[dict]) -> None:
    try:
        import argo_paths
        argo_paths.atomic_write_json(_cache_path(),
                                     {"ts": time.time(), "items": items})
    except Exception:
        pass


def fetch_quota(*, refresh: bool = False, timeout: float = 8.0) -> dict:
    """返回 {ok, items, error, cached}；永不抛异常（观测层不阻断主路径）。"""
    if not refresh:
        cached = _load_cache()
        if cached is not None:
            return {"ok": True, "items": cached.get("items") or [], "cached": True}
    secret = _secret()
    if not secret:
        return {"ok": False, "items": [], "cached": False,
                "error": "缺少 ZHIHU_ACCESS_SECRET"}
    req = urllib.request.Request(API_URL, headers={
        "Authorization": f"Bearer {secret}",
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    try:
        with http_open(req, timeout=timeout, engine="zhihu_quota") as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        return {"ok": False, "items": [], "cached": False,
                "error": f"{type(exc).__name__}: {exc}"}
    if data.get("Code") not in (0, None):
        return {"ok": False, "items": [], "cached": False,
                "error": f"Code={data.get('Code')} {data.get('Message') or ''}"}
    items = []
    for row in data.get("Data") or []:
        if not isinstance(row, dict):
            continue
        api_id = str(row.get("APIID") or "")
        items.append({
            "api_id": api_id,
            "engine": API_TO_ENGINE.get(api_id, api_id),
            "name": row.get("APIName") or api_id,
            "total": row.get("TotalQuota"),
            "used": row.get("TotalUsed"),
            "remaining": row.get("RemainingQuota"),
        })
    _save_cache(items)
    return {"ok": True, "items": items, "cached": False}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="argo quota",
                                 description="知乎开放平台当日额度（官方 API）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    out = fetch_quota(refresh=args.refresh)
    if args.json:
        print(dumps(out))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(f"额度查询失败：{out.get('error')}")
        return 1
    print("知乎开放平台当日额度（北京时间 00:00 重置）")
    print(f"{'引擎':<18}{'额度项':<20}{'总额':>8}{'已用':>8}{'剩余':>8}")
    for row in out["items"]:
        print(f"{row['engine']:<18}{row['name']:<20}"
              f"{str(row['total']):>8}{str(row['used']):>8}{str(row['remaining']):>8}")
    if out.get("cached"):
        print("（10 分钟内缓存；--refresh 强制刷新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
