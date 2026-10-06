#!/usr/bin/env python3
"""health_check.py — local-search 轻量健康探针

对每个启用引擎发送 canary 查询，检测 HTTP 状态、延迟、解析成功与反爬拦截，
将结果持久化到 ~/.cache/unified-search/local_search_health.json。

判定规则（与设计方案一致）：
- 连续 2 次失败 / 单次延迟 > 8s / HTTP 4xx/5xx → 标记 unavailable
- 成功 1 次即可恢复 available
- 启动时读取缓存（TTL 5 分钟），避免每次查询都发探针
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from engine_registry import EngineRegistry, get_registry, update_availability

# scripts/ 入路径（net_proxy 出口需要；append 不抢占子技能同名模块的优先级）
_ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if _ROOT_SCRIPTS.exists() and str(_ROOT_SCRIPTS) not in sys.path:
    sys.path.append(str(_ROOT_SCRIPTS))

logger = logging.getLogger("local_search.health_check")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler())

# 常见反爬/拦截标记（大小写不敏感）
ANTI_BOT_MARKERS = [
    "captcha", "recaptcha", "robot", "robots", "cloudflare", "challenge",
    "blocked", "verification", "please verify", "access denied",
    "too many requests", "rate limit", "forbidden", "unauthorized",
]

# HTTP 状态码分组
RETRYABLE_STATUS = {429, 503, 502, 504}

# 自适应 TTL 上限（秒）
_MAX_HEALTH_TTL_S = 3600.0


def _effective_ttl(rec: dict[str, Any], base_ttl_s: float) -> float:
    """连续成功 → 探测间隔指数放宽（×2^min(streak,3)，硬上限 60min）。

    探针不是免费的：HTTP canary 是直连上游的真实请求（最坏 8s 墙钟，且
    每 5min 对 baidu/sogou/360 这类激进反爬源发一次固定「test」查询，纯增
    封号风险）。稳定可用的引擎不需要高频确认；任何一次失败 consecutive_ok
    归零，立即回到基准 TTL。
    """
    streak = int(rec.get("consecutive_ok", 0) or 0)
    return min(base_ttl_s * (2 ** min(max(streak, 0), 3)), _MAX_HEALTH_TTL_S)


def _now() -> float:
    return time.time()


def _detect_anti_bot(text: str, status: int | None = None) -> str | None:
    """检测是否命中验证码/拦截页面。"""
    if status == 429:
        return "rate_limited"
    if status and 500 <= status < 600:
        return f"http_{status}"
    lowered = text.lower()
    for marker in ANTI_BOT_MARKERS:
        if marker in lowered:
            return f"anti_bot:{marker}"
    return None


def _build_canary_url(spec: dict[str, Any], query: str, n: int = 1) -> tuple[str, dict[str, str]]:
    """构造 canary 请求的 URL 与 headers。"""
    url = spec.get("url", "")
    qp = spec.get("query_param", "q")
    method = spec.get("method", "GET")
    extra = spec.get("extra_params", {})
    headers = spec.get("headers", {})

    resolved_url = url.replace("{query}", urllib.parse.quote(query)).replace("{n}", str(n))
    if method == "GET":
        params: dict[str, str] = {qp: query}
        for k, v in extra.items():
            params[k] = str(v).replace("{query}", query).replace("{n}", str(n))
        sep = "&" if "?" in resolved_url else "?"
        full_url = f"{resolved_url}{sep}{urllib.parse.urlencode(params)}"
    else:
        full_url = resolved_url

    resolved_headers = {k: str(v).replace("{n}", str(n)) for k, v in headers.items()}
    return full_url, resolved_headers


def _fetch_probe(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    timeout: float = 8,
    user_agent: str = "Mozilla/5.0 (compatible; unified-search-local/1.0.1; +https://local)",
) -> tuple[int | None, float, str, str | None]:
    """发送探针请求，返回 (status, latency_ms, text, fail_reason)。"""
    req_headers = dict(headers)
    if user_agent and "User-Agent" not in req_headers:
        req_headers["User-Agent"] = user_agent
    req_headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req_headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")

    req = urllib.request.Request(url, headers=req_headers, method=method)
    # 出口统一走 net_proxy.open_url（与 search_v3._fetch 同语义）：裸 urlopen
    # 不认 argo 在 config.yaml network.proxy 里配置的 url/rules——「必须经代理
    # 才能出网」的环境里探针全部连不上，所有 HTTP 引擎被判 unavailable，
    # fast/budget 路由随后被静默清空，症状与「源挂了」完全一样（主仓
    # issue #13 同形态，13 个模块已修，这里是漏网的一个）。scripts/ 不在
    # 路径（独立打包形态）时回退裸 urlopen，行为与改动前一致。
    try:
        from net_proxy import open_url
    except ImportError:
        open_url = urllib.request.urlopen  # type: ignore[assignment]
    t0 = time.time()
    try:
        with open_url(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        status = e.code
        text = ""
        try:
            text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
    except urllib.error.URLError as e:
        return None, round((time.time() - t0) * 1000, 2), "", f"url_error:{e.reason}"
    except Exception as e:
        return None, round((time.time() - t0) * 1000, 2), "", f"exception:{type(e).__name__}:{e}"

    elapsed = round((time.time() - t0) * 1000, 2)
    fail_reason = _detect_anti_bot(text, status)
    return status, elapsed, text, fail_reason


def _parse_success(engine_name: str, text: str, fmt: str, registry: EngineRegistry) -> bool:
    """粗略判断解析是否可能成功（通过 parse_maps.yaml 与格式特征）。"""
    if not text:
        return False
    if fmt == "json":
        try:
            json.loads(text)
            return True
        except json.JSONDecodeError:
            return False
    if fmt in ("xml", "rss"):
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(text)
            return True
        except ET.ParseError:
            return False
    # html：检查 parse_maps 中 container 是否出现在文本中
    maps = registry.parse_maps.get("html", {}).get(engine_name, {})
    container = maps.get("container", "")
    if container:
        # 简单把 CSS 选择器转成正则特征
        parts = [p.strip(". ") for p in container.replace(",", " ").split() if p.strip(". ")]
        return any(p in text for p in parts)
    return len(text) > 200


def _check_cli_engine(engine_name: str, spec: dict[str, Any],
                      timeout: float = 8,
                      canary_query: str = "test", n: int = 1) -> dict[str, Any]:
    """CLI 引擎健康探针：跑一次真实 canary 查询，校验「能出结果」。

    2026-10-06 修复假阳性：此前只跑 `ddgs --help`——二进制存在即判
    available。ddgs 9.14.4 移除 bing text 后端、brave/yahoo 对真实查询
    稳定 0 结果后，`--help` 全绿而搜索全灭，死引擎反复混入组合（本地
    兜底链 4.5-8.3s 的结构性成因之一）。探针与搜索同路径（复用
    search_v3._run_cli_engine），canary 拿不到 ≥1 条结果即失败。
    代价：一次真实查询的墙钟（仅健康记录过期时发生，TTL 5min 起）。
    search_v3 不可导入时退化为 --help 探测（fail-open，不崩探针链）。
    """
    cli_cmd = spec.get("cli_command", "")
    if not cli_cmd:
        return {"name": engine_name, "url": "", "status": None,
                "latency_ms": 0, "parse_ok": False, "text_sample": "",
                "fail_reason": "missing_cli_command", "available": False}
    # which 短路：二进制不在 PATH 上时不再白付一次 --help 子进程 spawn
    # （ddgs 冷启实测 0.35s）。与 search_v3._check_cli_available 同判据；
    # shim 损坏（能 which 到但跑不动）仍由下方真实调用暴露。
    if shutil.which(cli_cmd) is None:
        return {"name": engine_name, "url": "", "status": None,
                "latency_ms": 0, "parse_ok": False, "text_sample": "",
                "fail_reason": "cli_not_found", "available": False}
    import subprocess
    t0 = time.time()
    fail_reason = None
    ok = False
    text_sample = ""
    try:
        try:
            from search_v3 import _run_cli_engine  # 同目录，探针与搜索同路径
        except ImportError:
            _run_cli_engine = None  # type: ignore[assignment]
        if _run_cli_engine is not None:
            results, err = _run_cli_engine(spec, canary_query, n,
                                           min(timeout, 8))
            ok = bool(results)
            if not ok:
                fail_reason = (err or "no_results")[:120]
                text_sample = (err or "")[:300]
        else:
            # 退化路径：search_v3 不可用，只验证可执行（fail-open）。
            # 真正的搜索失败由执行层熔断器记录。
            result = subprocess.run(
                [cli_cmd, "--help"],
                capture_output=True, text=True, timeout=min(timeout, 5),
                encoding="utf-8", errors="replace",  # Windows GBK locale 下 UTF-8 输出会解码崩
            )
            ok = result.returncode == 0
            if not ok:
                fail_reason = f"cli_exit_{result.returncode}"
    except subprocess.TimeoutExpired:
        ok, fail_reason = False, "cli_timeout"
    except FileNotFoundError:
        ok, fail_reason = False, "cli_not_found"
    except Exception as e:  # 探针自身失败不判引擎死（fail-open）：记为软失败
        ok, fail_reason = False, f"probe_error:{e}"[:120]
    return {
        "name": engine_name, "url": "", "status": 200 if ok else None,
        "latency_ms": round((time.time() - t0) * 1000, 2),
        "parse_ok": ok, "text_sample": text_sample,
        "fail_reason": fail_reason, "available": ok,
    }


def check_engine(
    engine_name: str,
    registry: EngineRegistry | None = None,
    canary_query: str = "test",
    n: int = 1,
    timeout: float = 8,
) -> dict[str, Any]:
    """对单个引擎执行健康探针，返回状态报告（不直接修改注册表）。"""
    reg = registry or get_registry()
    spec = reg.get_engine(engine_name)
    if spec is None:
        return {"name": engine_name, "available": False, "fail_reason": "not_configured"}
    if not spec.get("enabled", True):
        return {"name": engine_name, "available": False, "fail_reason": "disabled"}

    # cli 引擎无 HTTP 端点，走 CLI 探针而非 URL 探针
    if spec.get("type") == "cli":
        return _check_cli_engine(engine_name, spec, timeout=timeout,
                                 canary_query=canary_query, n=n)

    url, headers = _build_canary_url(spec, canary_query, n)
    method = spec.get("method", "GET")
    status, latency_ms, text, fail_reason = _fetch_probe(url, headers, method=method, timeout=timeout)

    fmt = spec.get("format", "html")
    parse_ok = _parse_success(engine_name, text, fmt, reg) if text else False

    report: dict[str, Any] = {
        "name": engine_name,
        "url": url,
        "status": status,
        "latency_ms": latency_ms,
        "parse_ok": parse_ok,
        "text_sample": text[:300] if text else "",
        "fail_reason": fail_reason,
    }

    # 判定逻辑
    http_failed = status is None or status >= 400
    too_slow = latency_ms > 8000
    blocked = fail_reason is not None
    parse_failed = not parse_ok and not blocked and not http_failed

    if http_failed or too_slow or blocked:
        report["available"] = False
        if not fail_reason:
            if status is None:
                report["fail_reason"] = "network_error"
            else:
                report["fail_reason"] = f"http_{status}"
    elif parse_failed:
        # 解析失败单独记录，但不直接判 unavailable（可能是页面改版）
        report["available"] = True
        report["parse_warning"] = True
    else:
        report["available"] = True

    return report


def apply_threshold(report: dict[str, Any], previous: dict[str, Any] | None = None) -> bool:
    """根据本次报告与上一次状态，应用可用性阈值判定。

    规则：
    - 本次成功 → available = True
    - 本次失败且（连续失败 >=2 或 延迟>8s 或 HTTP 4xx/5xx）→ available = False
    - 其他失败情况继承上一次状态（保守策略）
    """
    available = report.get("available", False)
    if available:
        return True

    status = report.get("status")
    latency_ms = report.get("latency_ms", 0)
    consecutive = previous.get("consecutive_failures", 0) if previous else 0

    http_failed = status is None or (isinstance(status, int) and status >= 400)
    too_slow = latency_ms > 8000
    now_consecutive = consecutive + 1

    if http_failed or too_slow or now_consecutive >= 2:
        return False
    # 单次软性失败，继承之前状态
    return previous.get("available", True) if previous else True


def run_health_check(
    registry: EngineRegistry | None = None,
    canary_query: str = "test",
    n: int = 1,
    timeout: float = 8,
    max_parallel: int = 5,
    ttl_minutes: float = 5,
    engine_names: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """运行一轮健康检查，返回每个引擎的最新报告。

    若缓存 TTL 未过期，则直接返回已有健康状态。
    传入 engine_names 可只检查指定引擎，避免对所有启用引擎全量探针。
    """
    reg = registry or get_registry()
    settings = reg.settings.get("health_check", {})
    canary_query = settings.get("canary_query", canary_query)
    timeout = settings.get("timeout", timeout)
    ttl_minutes = settings.get("ttl_minutes", ttl_minutes)
    max_parallel = settings.get("max_parallel", max_parallel)

    enabled = engine_names if engine_names is not None else reg.list_engines(enabled_only=True)

    now = _now()
    # TTL 检查：在有效期内直接复用缓存。
    # 按引擎名逐一比对（此前 cached.values() 里的 dict 与 enabled 的 str 比较
    # 恒 False → all(空) 恒 True → 健康状态永不刷新），任一引擎记录缺失或
    # 过期即重新探针。TTL 按连续成功自适应放宽（_effective_ttl）。
    cached = reg._health

    def _recent(name: str) -> bool:
        rec = cached.get(name)
        return bool(rec) and (now - rec.get("last_checked", 0)) < _effective_ttl(rec, ttl_minutes * 60)

    all_recent = bool(enabled) and all(_recent(n) for n in enabled)
    if all_recent:
        return {name: dict(h) for name, h in cached.items() if name in enabled}

    reports: dict[str, dict[str, Any]] = {}

    def _probe(name: str) -> tuple[str, dict[str, Any]]:
        return name, check_engine(name, reg, canary_query=canary_query, n=n, timeout=timeout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(enabled), max_parallel)) as ex:
        futures = [ex.submit(_probe, name) for name in enabled]
        for fut in concurrent.futures.as_completed(futures):
            name, report = fut.result()
            reports[name] = report

    # 应用阈值并更新注册表
    for name, report in reports.items():
        previous = reg.get_health(name)
        final_available = apply_threshold(report, previous)
        # available 以位置参数传入，勿再放进 kwargs（此前重复键触发
        # TypeError，被 #⑨ 缓存恒命中掩盖，健康检查从未真正执行）
        update_data = {
            "latency_ms": report.get("latency_ms"),
            "status": report.get("status"),
            "parse_ok": report.get("parse_ok"),
            "fail_reason": report.get("fail_reason"),
        }
        if final_available:
            update_data["success_rate"] = 1.0
        else:
            update_data["success_rate"] = 0.0
        # persist=False：一轮 N 个引擎收成 1 次落盘（此前每引擎全文件重写一次）
        reg.update_availability(name, final_available, persist=False, **update_data)

    if reports:
        reg._save_health()

    return reports


def get_available_engines(
    registry: EngineRegistry | None = None,
    use_cache: bool = True,
    engine_names: list[str] | None = None,
) -> list[str]:
    """获取当前可用引擎列表；若缓存过期则自动执行健康检查。

    通过 engine_names 可只检查指定引擎，避免全量探针。
    """
    reg = registry or get_registry()
    if engine_names is None:
        engine_names = reg.list_engines(enabled_only=True)
    if use_cache:
        now = _now()
        ttl = reg.settings.get("health_check", {}).get("ttl_minutes", 5) * 60
        cached = reg._health

        def _recent(name: str) -> bool:
            rec = cached.get(name)
            # TTL 按连续成功自适应放宽：稳定引擎最多放宽到 60min，
            # 探针墙钟与对反爬源的 canary 流量随之下降一个量级。
            return bool(rec) and (now - rec.get("last_checked", 0)) < _effective_ttl(rec, ttl)

        # 逐引擎名比对新鲜度（此前 dict in list[str] 恒 False → 恒命中，
        # 健康状态永不刷新）。
        # 2026-10-06：只探**过期**的引擎——此前 all() 判全新鲜，任一过期即
        # 全量重探，稳定引擎（TTL 放宽到 60min）被反复拖进探针浪；探针浪
        # 实测 ~2-4.5s（yahoo 慢空），全量重探把它加到每次调用路径上。
        # 注意 engine_names 在上方已保证非空（None → 全量启用列表）。
        stale = [n for n in engine_names if not _recent(n)]
        if not stale:
            return [n for n in engine_names if reg.is_available(n)]
        run_health_check(registry=reg, engine_names=stale)
        return [n for n in engine_names if reg.is_available(n)]
    run_health_check(registry=reg, engine_names=engine_names)
    return [n for n in engine_names if reg.is_available(n)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="local-search 健康检查")
    parser.add_argument("--engine", default=None, help="只检查单个引擎")
    parser.add_argument("--query", default="test", help="canary 查询词")
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--max-parallel", type=int, default=5)
    parser.add_argument("--force", action="store_true", help="忽略 TTL 强制检查")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    reg = get_registry()
    if args.force:
        reg._health.clear()
        reg._save_health()

    if args.engine:
        report = check_engine(args.engine, reg, canary_query=args.query, timeout=args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        reports = run_health_check(reg, canary_query=args.query, timeout=args.timeout, max_parallel=args.max_parallel)
        summary = {
            "total": len(reports),
            "available": sum(1 for r in reports.values() if r.get("available")),
            "unavailable": sum(1 for r in reports.values() if not r.get("available")),
            "reports": reports,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
