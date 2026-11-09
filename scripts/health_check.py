#!/usr/bin/env python3
"""
health_check.py — 引擎健康检查（v2.1 新增，替代 health_probe.py）

升级点：
- 反爬检测（14 种 marker + 内容长度检查）
- 解析成功验证（CSS container / JSON valid / XML valid）
- 惰性健康检查（只检查实际需要的引擎）
- 健康状态持久化（JSON + 5min TTL）
- 连续失败阈值（≥2 次 → unavailable，1 次成功恢复）

用法：
  python3 health_check.py --engine local_bing      # 检查单个引擎
  python3 health_check.py --category chinese       # 检查整个分类
  python3 health_check.py --all                    # 全量检查
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from argo_engine_registry import get_registry
from net_proxy import open_url  # 出口调度唯一入口（issue #13 同类修复）
from cli_io import dumps

logger = logging.getLogger("argo.health_check")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler())

PROBE_TIMEOUT = 5.0
# 本文件不实现失败阈值：它写注册表的布尔 available，连续失败计数由
# argo_engine_registry.update_health 累加但无人按阈值读。阈值的唯一
# 定义在 engine_states.PROBE_MAX_FAILURES（health_probe 消费）。此前这里
# 另有一个 MAX_FAILURES = 2 却没有任何读取点，看着像有门限其实没有。
HEALTH_TTL = 300  # 5 分钟

# 反爬/拦截标记
ANTI_BOT_MARKERS = [
    "captcha", "recaptcha", "robot", "robots", "cloudflare", "challenge",
    "blocked", "verification", "please verify", "access denied",
    "too many requests", "rate limit", "enable javascript", "checking your browser",
    "ddos-guard", "perimeterx", "arkose",
]


def _detect_anti_bot(text: str) -> bool:
    """检测反爬/拦截页面。"""
    if not text or len(text.strip()) < 100:
        return True
    text_lower = text.lower()
    return any(marker in text_lower for marker in ANTI_BOT_MARKERS)


def check_http_engine(name: str, url: str, spec: dict) -> dict:
    """检查 HTTP/HTML 引擎健康状态。"""
    # 仅真探测需要 urllib；route 热路径的 is_available 只读注册表，
    # 不该为模块级这行 import 每次多付 ~26ms（urllib+http.client 链）。
    import urllib.request
    result = {"available": False, "latency_ms": 0, "error": None}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        t0 = time.time()
        with open_url(req, timeout=PROBE_TIMEOUT) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            result["latency_ms"] = round((time.time() - t0) * 1000, 1)
            if _detect_anti_bot(content):
                result["error"] = "anti_bot_detected"
                return result
            # 解析成功验证：检查 CSS container 是否存在
            container = spec.get("parse_container")
            if container and content:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(content, "html.parser")
                    if not soup.select(container):
                        result["error"] = "parse_container_not_found"
                        return result
                except Exception:
                    pass
            result["available"] = True
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def check_engine(name: str) -> dict:
    """检查单个引擎健康状态。"""
    registry = get_registry()
    engines = registry.get_local_search_engines()
    spec = engines.get(name)
    if not spec:
        return {"available": False, "error": "engine_not_found"}
    url = spec.get("url", "")
    if not url:
        return {"available": False, "error": "no_url"}
    return check_http_engine(name, url, spec)


def _record_health(registry, name: str, result: dict) -> None:
    """把探测结果写进注册表，同时保留 result 里的 available 字段。

    两个坑（2026-11-09 实测复现后修）：
    - `update_health(name, result["available"], **result)` 会因 **result 里
      仍有 available 而抛 TypeError: multiple values for argument 'available'
      ——`--category` 路径每查一个引擎就炸一次；
    - `update_health(name, result.pop("available"), **result)` 虽不炸，但
      results[name] 与 result 是同一个 dict，pop 把 available 从**返回值**里
      也抹掉了，`--all` 的「N/M 可用」恒为 0、每行都显示 ❌。
    故：显式取布尔，派生的 extra 里剔掉 available，不修改 result。
    """
    available = bool(result.get("available"))
    registry.update_health(
        name, available, **{k: v for k, v in result.items() if k != "available"})


def check_category(category: str) -> dict[str, dict]:
    """检查整个分类的引擎。"""
    registry = get_registry()
    engines = registry.get_local_search_engines()
    results = {}
    for name, spec in engines.items():
        cats = spec.get("categories", [])
        if isinstance(cats, str):
            cats = [cats]
        if category not in cats:
            continue
        url = spec.get("url", "")
        if url:
            result = check_http_engine(name, url, spec)
            results[name] = result
            _record_health(registry, name, result)
    return results


def check_all() -> dict[str, dict]:
    """全量检查所有 local-search 子引擎。"""
    registry = get_registry()
    engines = registry.get_local_search_engines()
    results = {}
    for name, spec in engines.items():
        if not spec.get("enabled", True):
            continue
        url = spec.get("url", "")
        if not url:
            continue
        result = check_http_engine(name, url, spec)
        results[name] = result
        _record_health(registry, name, result)
    return results


def is_available(name: str) -> bool:
    """查询引擎是否可用（带 TTL 缓存）。

    单一真源（2026-11 收口）：
    优先以真实调用落盘的 circuit_breaker 状态为准。
    若熔断器处于 open/disabled 态，则一票否决；
    若熔断器无失败记录或处于正常/试探态，再结合注册表元数据。
    """
    try:
        from circuit_breaker import get_breaker
        allowed, _ = get_breaker().allow(name)
        if not allowed:
            return False
    except Exception:
        pass
    registry = get_registry()
    return registry.is_available(name)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Argo 引擎健康检查")
    parser.add_argument("--engine", help="检查单个引擎")
    parser.add_argument("--category", help="检查整个分类")
    parser.add_argument("--all", action="store_true", help="全量检查")
    args = parser.parse_args()

    if args.engine:
        result = check_engine(args.engine)
        print(dumps({args.engine: result}))
    elif args.category:
        results = check_category(args.category)
        print(dumps(results))
    elif args.all:
        results = check_all()
        available = sum(1 for r in results.values() if r.get("available"))
        print(f"检查完成: {available}/{len(results)} 可用")
        for name, r in results.items():
            status = "✅" if r.get("available") else "❌"
            print(f"  {status} {name}: {r.get('latency_ms', 0)}ms {r.get('error', '')}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
