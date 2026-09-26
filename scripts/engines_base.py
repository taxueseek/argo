#!/usr/bin/env python3
"""engines_base — 公共工具 + cli/http/html 构建器 + 通用解析。"""

from __future__ import annotations

import functools
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from config import load_config, get_engines
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines

logger = logging.getLogger("unified_search.engines")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler(sys.stderr))


# ── 引擎级失败归因寄存器 ─────────────────────────────────────────────────────
# 进程内「引擎 → 最近一次失败归因」。归因必须在失败现场写入——这是唯一能同时
# 拿到引擎名、状态码、响应体的位置。HTML 引擎的反爬命中走「静默返回空」路径，
# 没有任何错误文本可供聚合层事后反推；不在这里寄存，封锁就永远伪装成空结果，
# 被封的引擎会被当成故障累计到熔断里（封错人）。
_FAIL_NOTES: dict[str, dict[str, Any]] = {}
_FAIL_NOTES_LOCK = threading.Lock()

# HTTPError 错误体的读取上限：只用于归因（下游截 400 字符），
# 无限读会把几十 MB 错误页吃进内存并写进磁盘状态
_HTTP_ERROR_BODY_CAP = 64 * 1024

# 归因细节里可能回显凭证（OpenAI 风格 "Incorrect API key: sk-…"），
# 且会持久化到 circuit_breaker.json / 出现在 --list-engines 输出。
# 复用 archive_run 的唯一脱敏实现，避免各写一份规则。
try:
    from archive_run import redact_secrets as _redact_secrets
except Exception:  # pragma: no cover - archive_run 不可用时退化为不脱敏
    def _redact_secrets(text: str) -> str:
        return text


def note_failure(engine: str, category: str, reason: str, detail: str = "") -> None:
    """记录引擎最近一次失败的归因（覆盖式：只留最后一次）。

    detail 在写入前脱敏：这里聚合了引擎名、状态码与**响应体**，是唯一能把
    上游回显的凭证带进持久化状态的位置（会写进 circuit_breaker.json 并出现在
    --list-engines 输出）。脱敏在唯一的写入点做，消费者无需各自处理。
    """
    if not engine:
        return
    with _FAIL_NOTES_LOCK:
        _FAIL_NOTES[engine] = {
            "category": category,
            "reason": reason,
            "detail": _redact_secrets(detail or "")[:200],
            "ts": time.time(),
        }


def pop_failure_note(engine: str) -> dict[str, Any] | None:
    """取走引擎的失败归因（读后即清，避免陈旧归因污染后续查询）。"""
    with _FAIL_NOTES_LOCK:
        return _FAIL_NOTES.pop(engine, None)


def _lang_param(param: str, query: str) -> str:
    """按查询主语言返回引擎语言参数；表在 lang_detect 单来源维护。

    弱信号查询（mixed/other）时注入 lang_pref 的 engine_lang（习惯/系统/中英基线），
    强查询信号仍由 detect_language 主导，不因系统 locale 覆盖。
    """
    try:
        from lang_detect import engine_lang_param, detect_language
        preferred = ""
        try:
            from lang_pref import effective_engine_lang
            preferred = effective_engine_lang(detect_language(query))
        except ImportError:
            pass
        return engine_lang_param(param, query, preferred_lang=preferred)
    except ImportError:
        return ""


def safe_search(fn: Callable) -> Callable:
    """统一错误处理装饰器。所有异常返回 []，细粒度异常先于通用 Exception 匹配。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
        name = fn.__name__.replace("_engine", "").strip("_")
        try:
            return fn(*args, **kwargs)
        except subprocess.TimeoutExpired:
            logger.warning(f"引擎 {name} 超时")
        except FileNotFoundError as e:
            logger.warning(f"引擎 {name} 命令不存在: {e}")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            logger.warning(f"引擎 {name} HTTP 错误: {e}")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"引擎 {name} 解析错误: {e}")
        except Exception as e:
            logger.error(f"引擎 {name} 未预期异常: {type(e).__name__}: {e}", exc_info=True)
        return []
    return wrapper


# ── 引擎内排序分（rank decay）─────────────────────────────────────────────────
# 2026-09-13 修复「硬编码常量 score」：多数 builder 给同一引擎返回的每条结果
# 写死同一个分（如全部 0.7），于是上游 API 自己的相关性排序在进 RRF 前就被
# 抹平了——融合层只剩「引擎内序位」这点信息，且 local_five_dim_rerank 的
# relevance 维度对同引擎结果无区分度。
#
# 约定：builder 返回的 list 顺序 = 上游 API 的相关性顺序（各实现均按 API 原序
# append，未重排）。因此不需要猜上游分数语义，只需把「位置」编码进 score：
#   base  —— 该引擎的质量档位（沿用原常量，保持引擎间相对权重不变）；
#   rank  —— 在本次结果中的 0 基序位。
# 衰减用 1/(1+rank*DECAY)：rank0=base、rank1≈0.97*base、rank4≈0.88*base，
# 单调递减且差距温和（避免位置噪声压过引擎档位差）。
#
# 上游本身给了真实分数的引擎（openalex/crossref 等）不要用本函数——直接透传
# 上游分更准；本函数只服务「原先写死常量」的场景。
RANK_DECAY = 0.03


def rank_score(base: float, rank: int) -> float:
    """把常量基础分 + 结果序位转成单调递减的相关性分（保序、不夸大）。

    base 保持引擎档位（跨引擎相对权重不变），rank 仅在引擎内做温和衰减。
    """
    try:
        b = float(base)
        r = int(rank)
    except (TypeError, ValueError):
        return float(base) if isinstance(base, (int, float)) else 0.7
    # 边界加固：NaN/Inf 会一路传到融合层，而下游最终排序用 abs(score)
    # （search.py 的 merged.sort），NaN 的比较结果恒 False 会让排序退化为
    # 任意序；负分经 abs() 会**反转成最高分**。两者都不是调用方有意为之，
    # 统一夹到 [0, 1] 并让非有限值退化为默认档位。
    if not math.isfinite(b):
        return 0.7
    b = min(max(b, 0.0), 1.0)
    if r <= 0:
        return b
    return round(b / (1.0 + r * RANK_DECAY), 4)


def _run(cmd: list[str], timeout: float = 8, engine_name: str = "?") -> str:
    """执行命令，超时/异常不抛。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        tail = (r.stderr or "").strip()[:200]
        logger.warning(f"引擎 {engine_name} 失败 (rc={r.returncode}): {tail}")
        return r.stdout if r.stdout.strip() else ""
    except subprocess.TimeoutExpired:
        logger.warning(f"引擎 {engine_name} 超时 (>{timeout}s)")
    except FileNotFoundError as e:
        logger.error(f"引擎 {engine_name} CLI 缺失: {e}")
    except Exception as e:
        logger.error(f"引擎 {engine_name} 异常: {type(e).__name__}: {e}")
    return ""


def _resolve(template: list[str] | str, query: str, n: int, **extra: Any) -> list[str] | str:
    """替换模板占位符。"""
    if isinstance(template, list):
        return [_resolve(item, query, n, **extra) for item in template]
    s = template.replace("{query}", query).replace("{n}", str(n))
    s = s.replace("{TIMESTAMP}", str(int(time.time())))
    for key, val in extra.items():
        s = s.replace(f"{{{key}}}", str(val))
    if s.startswith("~"):
        s = str(Path.home() / s[1:])
    # env 占位符：缺失时替换为空串而非保留字面量。
    # 保留字面量会把 `Authorization: token {GITHUB_TOKEN}` 原样发出 → 401；
    # 空串 + 调用方过滤空头 = 未配置 key 的引擎自动退化为匿名请求。
    # 经 engine_env 按候选链解析（PLACEHOLDER_ALIASES：ARGO_ 推荐名优先 +
    # 历史兼容名）：os.environ 优先 + ~/.config/argo/env 热读保底
    # （密钥轮换改文件即生效，无需重启）。
    try:
        from engine_env import PLACEHOLDER_ALIASES as _PA, get_env as _get_env
        return re.sub(
            r"\{([A-Z_][A-Z0-9_]*)\}",
            lambda m: _get_env(
                _PA.get(m.group(1), [f"ARGO_{m.group(1)}", m.group(1)]), ""
            ),
            s,
        )
    except ImportError:
        return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.environ.get(m.group(1), ""), s)


_AUTH_PREFIXES = ("Bearer", "token", "Basic", "Key", "Api-Key", "X-Key", "Secret", "Appid")


def _header_meaningful(v: Any) -> bool:
    """头值是否有意义：空值、纯空白、或认证前缀残留（'Bearer '/ 'token ' 后无凭据）都过滤。

    '{GITHUB_TOKEN}' 未配置时 _resolve 得到 'token '（前缀残留非空），
    仅靠非空判断过滤不掉；必须按「认证前缀 + 空格 + 无凭据」精确识别，
    且不能用 strip 预处理（会丢掉区分用的尾随空格）。
    """
    if not v:
        return False
    raw = str(v)
    if not raw.strip():
        return False
    for prefix in _AUTH_PREFIXES:
        marker = prefix + " "
        if raw.lower().startswith(marker.lower()):
            if not raw[len(prefix):].strip():
                return False
    return True


def _get_path(obj: Any, path: str) -> Any:
    """按点分路径取值，支持 list 下标（如 authors.0.name）。空路径返回 obj。"""
    if path in ("", ".", "$", "[]", None):
        return obj
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


def _coerce_field(val: Any, *, max_len: int | None = None) -> str:
    """把 API 字段压成字符串：list 取首项、dict 丢弃、其余 str。"""
    if val is None:
        return ""
    if isinstance(val, list):
        if not val:
            return ""
        val = val[0]
    if isinstance(val, dict):
        return ""
    s = str(val).strip()
    if max_len is not None and len(s) > max_len:
        return s[:max_len]
    return s


def _format_url_template(template: str, item: dict) -> str:
    """支持 {field} 与 {a.b.c} 点分路径的 URL 模板。"""
    def repl(m: re.Match) -> str:
        return _coerce_field(_get_path(item, m.group(1)))
    try:
        return re.sub(r"\{([^}]+)\}", repl, template)
    except Exception:
        return ""


def _extract_items(data: Any, path: str) -> list:
    """从 JSON 按路径提取列表。path 为 . / $ / [] / 空 且 data 为 list 时直接返回。"""
    if path in ("", ".", "$", "[]"):
        return data if isinstance(data, list) else []
    if not isinstance(data, dict):
        return []
    obj = _get_path(data, path)
    if isinstance(obj, list):
        return obj
    # DBLP 等 API 在 h=1 时把 hit 收成对象而非数组
    if isinstance(obj, dict):
        return [obj]
    return []


def _make_field_parser(path: str, fields: dict[str, str], url_template: str | None = None,
                   max_items: int = 10) -> Callable:
    """构造声明式 parser。支持点分路径字段与 url_template（含嵌套占位）。"""
    def parser(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list) and path in ("", ".", "$", "[]"):
            items = data
        elif isinstance(data, dict):
            items = _extract_items(data, path)
        else:
            items = []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            r: dict[str, Any] = {}
            for ok, ik in fields.items():
                if not ik:
                    continue
                raw = _get_path(item, ik) if "." in str(ik) else item.get(ik, "")
                # 兼容顶层点分键名不存在时回退 get
                if raw in ("", None) and "." not in str(ik):
                    raw = item.get(ik, "")
                coerced = _coerce_field(raw, max_len=300 if ok == "snippet" else 500)
                if coerced:
                    r[ok] = coerced
            if url_template and (not r.get("url") or not str(r["url"]).startswith(("http://", "https://"))):
                built = _format_url_template(url_template, item)
                if built.startswith(("http://", "https://")):
                    r["url"] = built
            if isinstance(r.get("url"), str) and r["url"].startswith("//"):
                r["url"] = "https:" + r["url"]
            if r.get("title") or r.get("url"):
                results.append(r)
        return results[:max_items]
    return parser


def _build_cli_engine(spec: dict[str, Any]) -> Any:
    cmd_template = spec.get("cmd", [])
    search_args = spec.get("search_args", [])
    env_overrides = spec.get("env", {})
    output_format = spec.get("output_format", "")   # "yaml" | ""（JSON/文本自动）
    filter_args = spec.get("filter_args", {})       # {kwarg: [参数模板...]}，kwargs 携带时追加

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
        cmd = _resolve(cmd_template, query, n, mode=mode, **kwargs)
        args = _resolve(search_args, query, n, mode=mode, **kwargs)
        if not cmd:
            return []
        # 跨平台解释器解析：Windows 一般没有 python3 这个可执行名
        if isinstance(cmd, list) and cmd and cmd[0] == "python3":
            import shutil  # 仅 CLI 引擎解析解释器时需要，不进模块级导入链
            py3 = shutil.which("python3") or shutil.which("python") or sys.executable
            cmd[0] = py3
        for key, tmpl in filter_args.items():
            if kwargs.get(key) not in (None, ""):
                args += _resolve(tmpl, query, n, mode=mode, **kwargs)
        env = os.environ.copy()
        env.update(env_overrides)
        return _parse_text_output(_run(cmd + args, timeout=timeout, engine_name=spec.get("_name", "cli")),
                                  spec.get("_name", "cli"), output_format=output_format, n=n)
    return _engine


def _build_http_engine(spec: dict[str, Any]) -> Any:
    """统一 HTTP 引擎构造（GET/POST）。

    GET 请求走 HttpClient（UA 轮换 + Cookie 积累 + 429/503 Retry-After 尊重 +
    指数退避重试 + 重定向跟随 + 域族节流）；POST 走 http_open（与 GET 共用同一
    归因路径；POST 型引擎均为 JSON API，无进程内节流需求）。开关
    ARGO_ENGINE_HTTP_CLIENT=0 可整体回退 GET 到 urllib（小范围试运行/诊断用）。
    spec 显式声明的 max_concurrency / min_interval_ms 覆盖域族默认节流。
    """
    url_template = spec.get("url", "")
    headers = spec.get("headers", {"Content-Type": "application/json"})
    query_param = spec.get("query_param", "q")
    fmt = spec.get("format", "")
    # format 的双重身份：它本来是**解析提示**（_parse_http_payload 用它区分
    # xml/json 分支），但历史上被无条件当成查询参数拼进 URL。绝大多数 API
    # 忽略未知参数所以没暴露问题，直到 local_pubmed 撞上 NCBI E-utilities——
    # 那里 `format` 是**输出格式**参数且不接受 json，注入后同一条 URL 由
    # HTTP 200 变 400（实测），引擎静默返回 0 结果。
    # 默认仍注入（保持 47 个既有引擎行为不变），需要干净的引擎显式声明
    # `format_is_query_param: false` 退出注入。改全局会动到全部 47 个源，
    # 拿大范围风险换一个小修，故按引擎收敛。
    format_is_query_param = spec.get("format_is_query_param", True)
    timeout = spec.get("timeout", 8)
    extra_params = spec.get("extra_params", {})
    output_map = spec.get("output_map", {})
    is_get = spec.get("method", "GET") == "GET"
    body_template = spec.get("body", {})
    eng = spec.get("_name", "")
    # spec 显式节流覆盖（max_concurrency / min_interval_ms），声明即契约
    if spec.get("max_concurrency") is not None or \
            spec.get("min_interval_ms") is not None:
        try:
            from http_client import register_spec_limit
            register_spec_limit(eng,
                                spec.get("max_concurrency"),
                                spec.get("min_interval_ms"))
        except ImportError:
            pass

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        import urllib.parse as up

        if is_get:
            resolved_url = _resolve(url_template, query, n, **kwargs)
            parts: list[str] = []
            if query_param:  # 空字符串表示该 API 不用查询参数名（仅 extra_params）
                parts.append(f"{query_param}={up.quote(query)}")
            if fmt and format_is_query_param:
                parts.append(f"format={up.quote(str(fmt))}")
            for k, v in extra_params.items():
                # 语言参数动态化（v2.7）：按查询主语言覆盖静态 setlang/hl/lang/mkt
                if k in ("setlang", "hl", "lang", "uselang", "mkt"):
                    v = _lang_param(k, query) or v
                parts.append(f"{k}={up.quote(_resolve(str(v), query, n, **kwargs))}")
            if parts:
                separator = "&" if "?" in resolved_url else "?"
                full_url = resolved_url + separator + "&".join(parts)
            else:
                full_url = resolved_url
            resolved_headers = {
                k: v for k, v in (
                    (k, _resolve(v, query, n, **kwargs))
                    for k, v in headers.items()
                ) if _header_meaningful(v)  # 过滤空/认证前缀残留头（未配置的 {ENV} 不发送）
            }
            # GET：HttpClient 渐进增强（UA 轮换/重试/重定向跟随/域族节流）；
            # 失败返回空（与 urllib 失败行为一致，不抛异常）
            raw = _http_get_raw(full_url, resolved_headers, to, engine=eng)
            if raw is None:
                return []
            return _parse_http_payload(raw, fmt, eng, n, output_map, spec)
        else:
            body: dict[str, Any] = {}
            for k, v in body_template.items():
                resolved = _resolve(str(v), query, n, **kwargs)
                if k == "search_depth":
                    body[k] = depth
                elif resolved.lower() == "true":
                    body[k] = True
                elif resolved.lower() == "false":
                    body[k] = False
                else:
                    try:
                        body[k] = int(resolved)
                    except ValueError:
                        try:
                            body[k] = float(resolved)
                        except ValueError:
                            body[k] = resolved
            # 与 GET 路径保持一致：过滤空/认证前缀残留头（未配置的 {ENV} 不发送，
            # POST 型可选密钥引擎如 firecrawl 才能 keyless 直连）
            resolved_headers = {
                k: v for k, v in (
                    (k, _resolve(v, query, n, **kwargs))
                    for k, v in headers.items()
                ) if _header_meaningful(v)
            }
            import urllib.request as _urllib_request
            req = _urllib_request.Request(
                url_template,
                data=json.dumps(body).encode("utf-8"),
                headers=resolved_headers,
            )
            try:
                with http_open(req, timeout=to, engine=eng) as resp:
                    raw = resp.read().decode("utf-8")
            except Exception as e:
                logger.warning(f"HTTP 引擎失败: {e}")
                return []
            return _parse_http_payload(raw, fmt, eng, n, output_map, spec)
    return _engine


def _http_get_raw(url: str, headers: dict, timeout: float,
                  engine: str = "?") -> str | None:
    """GET 原始响应体（HttpClient 渐进增强；ARGO_ENGINE_HTTP_CLIENT=0 回退 urllib）。

    返回响应文本；任何失败返回 None（调用方按「无结果」处理）。
    失败时按状态码写入归因寄存器：限流（429）、封锁（403+拦截特征）、
    网络（连接层）。归因供聚合层区分「引擎坏」与「被挡住」。
    """
    from engine_env import env_flag
    use_client = env_flag("ARGO_ENGINE_HTTP_CLIENT")
    if use_client:
        try:
            from http_client import HttpClient
            # max_retries=0：引擎内不做连接级重试——死源一次超时已耗尽预算，
            # 重试把最坏代价翻倍（OSM 6s 声明实测 11.3s=两次尝试）；重试语义
            # 上移到调度层（hedged race 换引擎 / 熔断降权 / 串行救援链）
            resp = HttpClient(timeout=timeout, max_retries=0, jitter=False).get(
                url, extra_headers=headers, follow_redirects=True, engine=engine,
            )
            status = resp.get("status") or 0
            text = resp.get("text") or ""
            if status == 200 and text:
                return text
            if status >= 400 or status == 0:
                logger.warning(
                    f"HTTP 引擎失败: status={status} {resp.get('error', '')[:120]}"
                )
                _note_http_failure(engine, status,
                                   text or resp.get("error", ""))
                return None
            # 2xx/3xx 无 body：视为失败
            return None
        except Exception as e:
            logger.warning(f"HTTP 引擎失败(HttpClient): {type(e).__name__} {e}")
            note_failure(engine, "network", "exception",
                         f"{type(e).__name__}: {e}")
            return None
    # 回退 urllib（原行为）。出口仍须经 net_proxy——否则设
    # ARGO_ENGINE_HTTP_CLIENT=0 就顺带关掉了代理支持，在必须走代理的环境里
    # 这条保底路径会一直连不上（issue #13 的形态）。
    import http.client  # except 元组求值需要；走到这里 urlopen 已加载过，零成本
    try:
        from net_proxy import open_url as _proxy_open
        import urllib.request as _urllib_request
        req = _urllib_request.Request(url, headers=headers)
        with _proxy_open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except (OSError, http.client.HTTPException):
            pass
        logger.warning(f"HTTP 引擎失败: HTTP {e.code} {engine}")
        _note_http_failure(engine, e.code, body)
        return None
    except Exception as e:
        logger.warning(f"HTTP 引擎失败: {e}")
        note_failure(engine, "network", "exception", str(e))
        return None


def _note_http_failure(engine: str, status: int, body: str) -> None:
    """按状态码 + 响应体特征归因，写入寄存器。

    判定委托给 engine_failure.classify（唯一来源）。此前这里复刻了一份状态码
    分支表，与 classify 在 97/201 个状态码上给出不同答案（503 一边说限流、
    一边说上游改版），同一引擎的归因会随「你看哪个界面」而变。
    """
    try:
        from engine_failure import classify
    except ImportError:  # 模块缺失时退化为最小事实，不猜类别
        note_failure(engine, "unknown", f"http-{status}", (body or "")[:120])
        return
    res = classify(status_code=status, output=(body or "")[:2000])
    note_failure(engine, res["category"], res["reason"],
                 res.get("evidence") or (body or "")[:120])


@contextmanager
def http_open(req: Any, timeout: float = 10.0, engine: str = ""):
    """执行单次 HTTP 请求并在失败现场归因——直连 `urllib.request.urlopen` 的替代。

    背景：手写引擎构建器里曾有近百处直连 `urllib.request.urlopen`。HTTP 失败
    只留下一行 warning 就变成空结果，既不写归因寄存器，也不出现在
    `--list-engines --detail`——博查 AI Search 端点 `403 套餐额度不足` 就是这样
    长期显示 ready 的。归因必须在失败现场做：这里同时拿得到引擎名、状态码与
    响应体，聚合层事后无从反推。

    与 urlopen 的关系是「等价替换」而非「另起一条路」：
      - 成功路径行为完全一致：同样的响应对象、同样的字节流（gbk 等非 UTF-8
        站点仍由调用方手工解码）、同样的异常传播。
      - 失败时先归因再原样抛出，不吞异常、不改各站点既有的 except 分支——
        `except HTTPError` / `except Exception` 的语义都保持原样。
      - HTTPError 的响应体被归因读走后回挂一份 `BytesIO`，调用方 `e.read()`
        仍能拿到错误体（博查的 `_bocha_http_error` 依赖它）。

    归因分类委托 `_note_http_failure`（唯一来源=engine_failure.classify）：
    403+额度文案→rate_limited、403+登录文案→auth、429→rate_limited 等。
    engine 传空串时不写寄存器（测试直调 builder 未标引擎名时保持惰性）。
    """
    if isinstance(req, str):
        import urllib.request as _urllib_request
        req = _urllib_request.Request(req)
    # 出口调度（issue #13）：统一走 net_proxy.open_url——代理解析（argo 级
    # rules / ARGO_PROXY / config url + 标准环境变量）的唯一来源。此前这里
    # 自带一份 opener 拼装，与 fetch/job 等处的 urlopen 各写一份，于是 issue
    # #13 只修了本函数覆盖的引擎路径，其余出口仍然直接调用。
    import http.client  # except 元组求值需要；走到这里 urlopen 已加载过，零成本
    from net_proxy import open_url
    try:
        resp = open_url(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body_bytes = b""
        try:
            # 限长：错误页可能几十 MB，而调用方只用前 400 字符归因。
            # 无上限会把整页读进内存，并经归因持久化进磁盘状态文件。
            body_bytes = e.read(_HTTP_ERROR_BODY_CAP) or b""
        except (OSError, http.client.HTTPException):
            pass
        logger.warning(f"HTTP 引擎失败: HTTP {e.code} {engine}")
        _note_http_failure(engine, e.code,
                           body_bytes.decode("utf-8", errors="replace")[:400])
        _restore_error_body(e, body_bytes)
        raise
    except urllib.error.URLError as e:
        logger.warning(f"HTTP 引擎失败: {e}")
        note_failure(engine, "network", "urlopen", str(e)[:160])
        raise
    except OSError as e:
        logger.warning(f"HTTP 引擎失败: {e}")
        note_failure(engine, "network", "exception",
                     f"{type(e).__name__}: {e}")
        raise
    except Exception as e:
        # http.client.HTTPException（BadStatusLine / IncompleteRead 等）与
        # ValueError 既不是 URLError 也不是 OSError，不兜这一层就会穿透归因，
        # 与该函数的契约不符，也比 _http_get_raw 的保底更窄。归因后原样抛出。
        logger.warning(f"HTTP 引擎失败: {type(e).__name__} {e}")
        note_failure(engine, "network", "exception",
                     f"{type(e).__name__}: {e}")
        raise
    with resp:
        yield resp


def _restore_error_body(e: urllib.error.HTTPError, body: bytes) -> None:
    """把归因时读走的错误体还回 HTTPError，调用方 `e.read()` 仍可用。

    HTTPError 是 tempfile._TemporaryFileWrapper 的子孙，`read` 等文件方法在
    首次访问时被绑定并缓存进实例 __dict__（见 _TemporaryFileWrapper.__getattr__）。
    只改 `fp` / `file` 不改缓存，之后 `e.read()` 仍打到已耗尽的旧流上。所以这里
    连同缓存一起重置。全部包在 try 里：这是尽力而为的兼容层，任何一步失败都不
    应影响原异常的抛出。
    """
    try:
        restored = io.BytesIO(body)
        e.fp = restored
        e.file = restored
        for attr in ("read", "readline", "readlines", "seek", "tell",
                     "close", "__iter__"):
            e.__dict__.pop(attr, None)
    except AttributeError:
        pass


def _envelope_error(data: Any) -> str:
    """提取 HTTP 200 响应体里的业务错误（火山 ResponseMetadata.Error / 知乎
    顶层 Code/Message 风格等）。

    byted 免费配额耗尽（10406 Free quota exhausted）曾以 HTTP 200 + 空
    WebResults 静默通过；zhihu 20001 Authorization failed 同理——错误藏在
    200 响应体里，调用侧把「配额用完/鉴权失败」当「没结果」。
    """
    if not isinstance(data, dict):
        return ""
    rm = data.get("ResponseMetadata")
    if isinstance(rm, dict):
        e = rm.get("Error")
        if isinstance(e, dict) and (e.get("Code") or e.get("Message")):
            return f"{e.get('Code')}: {e.get('Message')}"
    # 顶层 Code/Message 封套（知乎 zhihu_search 等）：Code 非 0 且有 Message。
    # 成功码白名单含数值 200（部分 API 用 HTTP 语义的 200 表示成功）；
    # Code=0 无 Message 时不算错误（纯 Code 字段的成功封套）
    code = data.get("Code")
    msg = data.get("Message") or data.get("message")
    if msg and code not in (0, None, "", "0", "OK", "ok", "success", 200):
        return f"{code}: {msg}"
    for key in ("error", "Error"):
        e = data.get(key)
        if isinstance(e, dict):
            code = e.get("Code") or e.get("code") or ""
            msg = e.get("Message") or e.get("message") or ""
            if code or msg:
                return f"{code}: {msg}" if code and msg else str(msg or code)
        elif isinstance(e, str) and e:
            return e
    return ""


def _normalize_epoch(value: Any) -> str:
    """epoch 秒/毫秒 → `YYYY-MM-DD`，非 epoch 原样返回。

    技能目录源的 updatedAt 单位不统一：SkillsMP 给秒（1786285295），ClawHub
    给毫秒（1789594554485），相差 1000 倍。原样映射等于把一串数字当
    published_at 交出去，既读不出日期也无法与别的源比较。

    按量级判别而非按源声明：1e9~1e11 是秒（2001~5138 年），1e11~1e14 是
    毫秒，两位差三个数量级，不存在擦边区间。范围外的值不猜，原样返回——
    猜错会把一个可疑值变成看起来正常的值，比留着更糟。
    """
    text = str(value).strip()
    if not text.isdigit():
        return text
    num = int(text)
    if 1_000_000_000 <= num < 100_000_000_000:
        seconds = num
    elif 100_000_000_000 <= num < 100_000_000_000_000:
        seconds = num / 1000
    else:
        return text
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return text


def _parse_http_payload(raw: str, fmt: str, eng: str, n: int,
                        output_map: dict, spec: dict) -> list[dict[str, Any]]:
    """HTTP 引擎响应体解析（GET/POST 共用）。"""
    if fmt == "xml":
        return _parse_xml(raw, eng)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"HTTP 引擎解析失败: {eng} 非 JSON/XML 响应")
        return []
    # 业务错误封套优先于条数提取：强封套错误（ResponseMetadata.Error、顶层
    # error 对象）表示本次调用失败，返回结果必然为空或不可信
    env_err = _envelope_error(data)
    if env_err:
        logger.warning(f"HTTP 引擎业务错误: {eng} {env_err[:120]}")
        return [{"error": f"{eng} {env_err}", "source": eng}]
    limit = max(1, int(n or 5))
    # 专用 JSON 解析器优先（DDG Instant Answer / UAPI / Semantic Scholar 等）
    custom = _CUSTOM_JSON_PARSERS.get(eng)
    if custom:
        return _ensure_engine_source(custom(data), eng)[:limit]
    if output_map:
        items_path = output_map.get("items", "")
        # 根节点即为数组时（HF / dev.to / polymarket），items 用 "."
        if isinstance(data, list) and not items_path:
            items_path = "."
        parsed = _make_field_parser(items_path, {
            "title": output_map.get("item_title", "title"),
            "url": output_map.get("item_url", "url"),
            "snippet": output_map.get("item_summary", "snippet"),
            "source": output_map.get("item_source", "source"),
            "published_at": output_map.get("item_published_at", "published_at"),
            # 可验证出处：技能目录源给的是市场页（skillUrl/canonicalUrl），
            # 光有它只能再开一次浏览器。上游仓目录（SkillsMP 的 githubUrl）
            # 或安装引用（ClawHub 的 install.reference）才是能核对、能安装的
            # 那个地址，与市场页分开存。
            "upstream": output_map.get("item_upstream", ""),
            # 可选图片字段：图源（nasa_images 等）声明 item_image 后，结果里多出
            # image_url / image_license，供「搜到图 → 直接看图」用。路径须指向
            # **字符串**（如 links.0.href）——指向 dict 会被 _coerce_field 丢成空串
            # （NASA 的 links.0 是 {href, rel, render, ...}）。
            "image_url": output_map.get("item_image", ""),
            "image_license": output_map.get("item_image_license", ""),
        }, url_template=output_map.get("url_template"))(data)
        # spec 级授权常量：授权不随条目变化的源（如 NASA 公版）在 spec 上写一次
        _lic = spec.get("image_license")
        for r in parsed:
            r.setdefault("source", eng)
            if r.get("published_at"):
                r["published_at"] = _normalize_epoch(r["published_at"])
            if _lic and r.get("image_url"):
                r.setdefault("image_license", _lic)
            if isinstance(r.get("snippet"), str) and len(r["snippet"]) > 300:
                r["snippet"] = r["snippet"][:300]
        # preserve_source（声明式 spec）：保留 API 返回的真实来源标注
        return _ensure_engine_source(
            parsed, eng, preserve=bool(spec.get("preserve_source"))
        )[:limit]
    if isinstance(data, list):
        return _ensure_engine_source(
            _parse_generic({"results": data}, eng, limit), eng
        )[:limit]
    return _ensure_engine_source(_parse_generic(data, eng, limit), eng)[:limit]


# ── HTML 网页解析引擎 ─────────────────────────────────────────────────────────

def _load_parse_maps() -> dict:
    """加载 parse_maps.yaml（声明式 CSS 选择器映射）。"""
    maps_path = Path(__file__).parent.parent / "sub-skills" / "local-search" / "parse_maps.yaml"
    if not maps_path.exists():
        return {}
    try:
        from yaml_load import load as _yaml_load
        # UTF-8 读取由 yaml_load 内部固定（Windows GBK locale 会读崩 UTF-8 的
        # YAML，与原实现显式 encoding="utf-8" 等价）
        return _yaml_load(maps_path) or {}
    except Exception:
        return {}


def _detect_anti_bot(html: str) -> bool:
    """检测反爬/拦截页面。head 区扫通用标记 + 全文扫高特异性标记。"""
    if not html:
        return True
    if len(html.strip()) < 500:
        return True
    lowered = html.lower()
    # 通用标记只在前 2000 字符（head 区域）检测：captcha/challenge 这类词
    # 出现在正文属合法语义，全文扫会误杀
    anti_bot_head = [
        "captcha", "challenge", "cf-browser-verification",
        "access denied", "rate limit", "too many requests",
        "checking your browser", "ddos-guard", "perimeterx",
    ]
    for marker in anti_bot_head:
        if marker in lowered[:2000]:
            return True
    # 全文级高特异性标记：实测 DDG challenge 页以 HTTP 202 返回且文案不在
    # head 区（首个 challenge 字样在 2600+ 字符处，urllib 回退路径会把这种
    # body 当正常页送进解析），head 扫描必然漏。这张表只收带站点专名/语境
    # 的短语——不带专名的词（如 anomaly，会在讨论异常检测的结果页正文里
    # 合法出现）禁止进表，否则误杀正常页。
    for marker in (
        "bots use duckduckgo",                   # DDG challenge 页实测文案
        "select all squares containing a duck",  # 同上，点选验证题干
    ):
        if marker in lowered:
            return True
    # 如果页面有大量链接且内容充实，判定为正常结果页
    if len(html) > 50000:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            if len(soup.find_all("a")) > 20:
                return False
        except Exception:
            pass
    return False


# SERP 垃圾结果守卫的引擎范围（冻结）：仅覆盖无 API 的网页 SERP 引擎——它们
# 解析上游 HTML 页，最易被缓存 SERP / 无关结果静默命中；API 引擎结果自带结构化
# 契约，不走这张表。判定规则与逃生门（ARGO_SERP_GUARD=0）在 serp_guard 单点。
SERP_GUARD_ENGINES = frozenset({
    "local_bing", "local_google", "local_baidu", "local_sogou",
    "local_yandex", "local_startpage", "local_mojeek", "local_duckduckgo",
})


def _serp_guard_apply(engine_name: str, query: str,
                      results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """守卫接线：命中引擎且整页判垃圾 → 返回 []。

    判垃圾是「结果无关」不是「引擎故障」：不写 note_failure（结果无关不该进
    熔断归因），也不伪装成成功——返回诚实空。serp_guard 导入失败按不拦截处理
    （fail-open）：守卫自身故障不能放大成搜索故障。
    """
    if not results or engine_name not in SERP_GUARD_ENGINES:
        return results
    try:
        from serp_guard import is_junk_serp
    except ImportError:
        return results
    if is_junk_serp(query, results):
        return []
    return results


def _build_html_engine(spec: dict[str, Any]) -> Any:
    """HTML 网页解析引擎：HTTP 抓取 + BeautifulSoup CSS 选择器解析。"""
    url_template = spec.get("url", "")
    # 注意：不设置 Accept-Encoding，让 urllib 自动处理 gzip/deflate
    # 设 Accept-Encoding: br 会导致收到 Brotli 压缩但 urllib 无法解压
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    # 覆盖自定义 headers
    headers.update(spec.get("headers", {}))
    query_param = spec.get("query_param", "q")
    timeout = spec.get("timeout", 8)
    extra_params = spec.get("extra_params", {})
    engine_name = spec.get("_name", "html")
    # spec 显式节流覆盖（max_concurrency / min_interval_ms），声明即契约
    if spec.get("max_concurrency") is not None or \
            spec.get("min_interval_ms") is not None:
        try:
            from http_client import register_spec_limit
            register_spec_limit(engine_name,
                                spec.get("max_concurrency"),
                                spec.get("min_interval_ms"))
        except ImportError:
            pass
    _parse_maps_cache: dict = {}

    def _get_parse_maps() -> dict:
        if not _parse_maps_cache:
            _parse_maps_cache.update(_load_parse_maps())
        return _parse_maps_cache

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        resolved_url = _resolve(url_template, query, n)
        separator = "&" if "?" in resolved_url else "?"
        full_url = f"{resolved_url}{separator}{query_param}={up.quote(query)}"
        for k, v in extra_params.items():
            # 语言参数动态化（v2.7）：按查询主语言覆盖静态 setlang/hl/lang，
            # 让 local_bing/local_google 等对非中文查询返回对应语言结果。
            if k in ("setlang", "hl", "lang", "uselang"):
                v = _lang_param(k, query) or v
            full_url += f"&{k}={up.quote(_resolve(str(v), query, n))}"
        resolved_headers = {
            k: v for k, v in (
                (k, _resolve(v, query, n))
                for k, v in headers.items()
            ) if _header_meaningful(v)  # 过滤空/认证前缀残留头（未配置的 {ENV} 不发送）
        }
        # HTML 引擎同样走 HttpClient 渐进增强（UA 轮换/重定向跟随/重试/节流）
        html = _http_get_raw(full_url, resolved_headers, to, engine=engine_name)
        if html is None:
            return []
        if _detect_anti_bot(html):
            # 命中拦截页：归因为封锁而非空结果。不记这条，封锁会在聚合层
            # 伪装成「无结果」，被封引擎会被当故障累计进熔断。
            note_failure(engine_name, "blocked", "anti-bot-page", full_url)
            return []
        maps = _get_parse_maps()
        html_maps = maps.get("html", {})
        mapping = html_maps.get(engine_name, html_maps.get("default", {}))
        container_sel = mapping.get("container")
        title_sel = mapping.get("title", "h2 a, h3 a")
        url_sel = mapping.get("url", "a")
        snippet_sel = mapping.get("snippet")
        url_attr = mapping.get("url_attr", "href")
        default_score = mapping.get("score", 0.7)
        if not container_sel:
            return []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            containers = soup.select(container_sel)
        except Exception:
            return []
        from bs4 import NavigableString

        def _el_text(el) -> str:
            """提取元素文本。兼容 bs4 4.13+ 将页面文本标记为 TemplateString、
            get_text()/strings() 失效（如萌娘百科搜索结果页）的情况。"""
            if el is None:
                return ""
            if isinstance(el, str):
                return el
            parts = []
            stack = list(el.contents)
            while stack:
                node = stack.pop(0)
                if isinstance(node, NavigableString):
                    parts.append(str(node))
                elif hasattr(node, "contents"):
                    stack = node.contents + stack
            return "".join(parts)

        # 条目页直接命中：MediaWiki/百科类搜索常直接跳转到条目页（如萌娘百科
        # 「初音未来」→ /初音未来(消歧义)），列表选择器必然不命中。
        # 提取主标题 + 正文首段作为单条结果，避免空结果。
        if not containers:
            try:
                heading = (soup.select_one("h1#firstHeading") or soup.select_one("h1"))
                title = ""
                if heading:
                    title = _el_text(heading).strip()[:200]
                if not title and soup.title:
                    # 萌娘百科等页面无 h1（bs4 4.13 下模板文本壳），标题在 <title>
                    title = soup.title.get_text().split(" - ")[0].strip()[:200]
                if title:
                    snippet = ""
                    # 正文首段：mw-parser-output 下第一个有文本的 p
                    for p in soup.select("div.mw-parser-output p")[:5]:
                        t = _el_text(p).strip()
                        if t:
                            snippet = t[:300]
                            break
                    canonical = soup.find("link", rel="canonical")
                    page_url = canonical["href"] if canonical and canonical.get("href") else resolved_url
                    results = [{
                        "title": title,
                        "url": page_url,
                        "snippet": snippet,
                        "score": default_score,
                        "source": engine_name,
                    }]
                    # 条目页直连分支同样过守卫：SERP 引擎落到这个分支说明页面
                    # 不是结果列表（重定向/拦截页），单条「页面标题」恰是垃圾
                    return _serp_guard_apply(engine_name, query, results)
            except Exception:
                pass

        results = []
        for idx, item in enumerate(containers[:n * 2]):
            try:
                title_el = item.select_one(title_sel) if title_sel else None
                url_el = item.select_one(url_sel) if url_sel else None
                snippet_el = item.select_one(snippet_sel) if snippet_sel else None
                title = _el_text(title_el).strip()[:200] if title_el else ""
                url = ""
                if url_el and url_el.has_attr(url_attr):
                    url = url_el[url_attr]
                elif item.has_attr(url_attr):
                    # 容器自身带链接属性（如 <a class="item" href="..."> 自引用结构）
                    url = item[url_attr]
                snippet = _el_text(snippet_el).strip()[:300] if snippet_el else ""
                if not title and not url:
                    continue
                if url and url.startswith("/"):
                    from urllib.parse import urljoin
                    url = urljoin(resolved_url, url)
                score = max(default_score - idx * 0.05, 0.1)
                results.append({"title": title, "url": url, "snippet": snippet, "score": round(score, 3), "source": engine_name})
            except Exception:
                continue
        return _serp_guard_apply(engine_name, query, results[:n])
    return _engine

# ── 通用解析器 ─────────────────────────────────────────────────────────────────

def _parse_text_output(text: str, engine_name: str, output_format: str = "", n: int = 10) -> list[dict[str, Any]]:
    """通用 CLI 文本解析：YAML（声明式）/ JSON / 结构化文本。n 限制返回条数。"""
    if not text or not text.strip():
        return []
    text = text.strip()
    if output_format == "yaml":
        return _parse_yaml_output(text, engine_name, n=n)
    try:
        data = json.loads(text)
        limit = max(1, n)
        if isinstance(data, list):
            out = []
            for i in data[:limit]:
                if not isinstance(i, dict):
                    continue
                r = {"title": i.get("title", ""), "url": i.get("url", ""),
                     "snippet": i.get("snippet", i.get("content", ""))[:300],
                     "source": engine_name}
                if i.get("published_at"):
                    r["published_at"] = str(i["published_at"])[:64]
                out.append(r)
            return out
        if isinstance(data, dict):
            items = data.get("results", data.get("items", data.get("data", [])))
            if isinstance(items, list):
                out = []
                for i in items[:limit]:
                    if not isinstance(i, dict):
                        continue
                    r = {"title": i.get("title", ""), "url": i.get("url", ""),
                         "snippet": i.get("snippet", i.get("content", ""))[:300],
                         "source": engine_name}
                    if i.get("published_at"):
                        r["published_at"] = str(i["published_at"])[:64]
                    out.append(r)
                return out
    except (json.JSONDecodeError, ValueError):
        pass

    results, cur = [], {}
    seen_url = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("### "):
            if cur:
                results.append(cur)
            cur = {"title": re.sub(r'^\d+\.\s*', '', s[4:].strip()), "source": engine_name,
                   "score": max(1.0 - len(results) * 0.1, 0.1)}
            seen_url = False
        elif s.startswith("- **URL**: ") and cur:
            cur["url"] = s[11:].strip()
            seen_url = True
        elif s.startswith("- ") and not s.startswith("- **") and seen_url and cur:
            cur["snippet"] = " ".join(s[2:].strip().split())[:300]
            seen_url = False
    if cur:
        results.append(cur)
    return results[:max(1, n)]


def _parse_yaml_output(text: str, engine_name: str, n: int = 10) -> list[dict[str, Any]]:
    """解析 YAML 输出（结构化 CLI 数据源的默认格式）。

    支持顶层 list，或 dict 携带 results/items/data 列表；
    字段别名：snippet|description；保留 published_at 时间维度。
    """
    try:
        from yaml_load import loads as _yaml_loads
        data = _yaml_loads(text)
    except Exception:
        return []
    if isinstance(data, dict):
        items = data.get("results", data.get("items", data.get("data", [])))
    elif isinstance(data, list):
        items = data
    else:
        return []
    if not isinstance(items, list):
        return []
    results = []
    for i in items:
        if not isinstance(i, dict):
            continue
        title = i.get("title") or i.get("name") or ""
        url = i.get("url") or i.get("link") or ""
        snippet = i.get("snippet") or i.get("description") or i.get("content") or ""
        if not title and not url:
            continue
        r = {
            "title": str(title)[:200],
            "url": str(url),
            "snippet": str(snippet)[:300],
            "source": engine_name,
        }
        published = i.get("published_at")
        if published:
            r["published_at"] = str(published)[:64]
        results.append(r)
    return results[:max(1, n)]


def _parse_xml(text: str, engine_name: str) -> list[dict[str, Any]]:
    """解析 Atom XML（arXiv 等）。"""
    import xml.etree.ElementTree as ET
    results = []
    try:
        root = ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns) or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for entry in entries:
            title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")[:200]
            summary = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")[:300]
            entry_id = entry.findtext("atom:id", "", ns)
            url = entry_id
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    url = link.get("href", url)
                    break
            if title:
                results.append({"title": title, "url": url, "snippet": summary, "source": engine_name})
    except ET.ParseError:
        pass
    return results


def _parse_generic(data: dict[str, Any], engine_name: str = "?",
                    limit: int = 10) -> list[dict[str, Any]]:
    """通用 JSON 解析：自动探测常见列表路径与字段别名。

    列表探测（最多 3 层，只走已知键）：
      - 顶层 results/items/data/works/search 等
      - 一层嵌套（data.results / data.value）
      - 二层嵌套（data.webPages.value —— 博查等 AI 搜索 API）

    字段别名：title|name|heading；url|URL|html_url|link|href；
    snippet|summary|content|description|abstract。
    """
    list_keys = (
        "results", "items", "value", "works", "search", "data",
        "organic", "webPages", "hits", "documents", "entries",
    )

    def _find_items(obj: Any, depth: int = 0) -> list | None:
        if depth > 3 or obj is None:
            return None
        if isinstance(obj, list):
            # 空列表或 dict 元素列表视为结果集；纯标量列表跳过
            if not obj or isinstance(obj[0], dict):
                return obj
            return None
        if not isinstance(obj, dict):
            return None
        for key in list_keys:
            if key not in obj:
                continue
            found = _find_items(obj[key], depth + 1)
            if found is not None:
                return found
        # 浅层扫描嵌套 dict（避免深扫整棵树误抓无关数组）
        if depth < 2:
            for v in obj.values():
                if isinstance(v, dict):
                    found = _find_items(v, depth + 1)
                    if found is not None:
                        return found
        return None

    items = _find_items(data) if isinstance(data, (dict, list)) else None
    if not items or not isinstance(items, list):
        return []

    results: list[dict[str, Any]] = []
    for i in items:
        if not isinstance(i, dict):
            continue
        title = i.get("title") or i.get("name") or i.get("heading") or i.get("Title") or ""
        if isinstance(title, list):
            title = title[0] if title else ""
        url = (
            i.get("url") or i.get("URL") or i.get("html_url")
            or i.get("link") or i.get("href") or ""
        )
        snippet = (
            i.get("snippet") or i.get("summary") or i.get("content")
            or i.get("description") or i.get("abstract") or ""
        )
        if isinstance(snippet, list):
            snippet = snippet[0] if snippet else ""
        score = i.get("score", i.get("relevance_score", 0.5))
        results.append({
            "title": str(title)[:200],
            "url": str(url),
            "snippet": str(snippet)[:300],
            "score": score,
            "source": engine_name,
        })
    return results[:limit]


def _ensure_engine_source(
    results: list[dict[str, Any]] | Any, engine_name: str, preserve: bool = False
) -> list[dict[str, Any]]:
    """纠正结果 source，避免 HTTP 解析器错标（如 uapi→stackoverflow）。

    规则：
      - 空 / generic → 设为引擎名
      - source 既不等于引擎名、也不以「引擎名/」开头 → 纠正为引擎名
      - wigolo_npx 允许保留 wigolo/... 子源标注
      - preserve=True（声明式 spec 的 preserve_source）时保留 API 返回的
        真实来源标注（如聚合资讯引擎的上游发布方），只对空/generic 保底
    """
    if not isinstance(results, list) or not engine_name:
        return results if isinstance(results, list) else []
    for r in results:
        if not isinstance(r, dict) or "error" in r:
            continue
        src = str(r.get("source") or "")
        if engine_name == "wigolo_npx" and src.startswith("wigolo"):
            continue
        if not src or src == "generic":
            r["source"] = engine_name
        elif not preserve and src != engine_name and not src.startswith(engine_name + "/"):
            r["source"] = engine_name
    return results


def _parse_duckduckgo(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 DuckDuckGo Instant Answer API 响应。"""
    if not isinstance(data, dict):
        return []
    results: list[dict[str, Any]] = []

    abstract = data.get("Abstract", "")
    if abstract:
        results.append({
            "title": data.get("Heading", "DuckDuckGo Answer"),
            "url": data.get("AbstractURL", ""),
            "snippet": abstract[:300],
            "source": "duckduckgo",
        })

    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and "Text" in topic:
            results.append({
                "title": topic.get("Text", "")[:100],
                "url": topic.get("FirstURL", ""),
                "snippet": topic.get("Text", "")[:300],
                "source": "duckduckgo",
            })

    return results[:5]


def _parse_uapi(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 UAPI 搜索响应。"""
    if not isinstance(data, dict):
        return []
    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", "")[:300],
            "source": "uapi",
        }
        for r in results if isinstance(r, dict)
    ][:10]


def _parse_semantic_scholar(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Semantic Scholar API 响应。"""
    if not isinstance(data, dict):
        return []
    papers = data.get("data") or []
    if not isinstance(papers, list):
        return []
    results: list[dict[str, Any]] = []

    for paper in papers:
        if not isinstance(paper, dict):
            continue

        title = paper.get("title", "")
        abstract = paper.get("abstract", "") or ""
        citation_count = paper.get("citationCount", 0) or 0

        pdf_info = paper.get("openAccessPdf", {})
        url = pdf_info.get("url", "") if isinstance(pdf_info, dict) else ""

        authors = paper.get("authors", [])
        author_names = [a.get("name", "") for a in authors if isinstance(a, dict)][:3]
        author_str = ", ".join(author_names)

        snippet = f"{abstract[:200]}"
        if citation_count:
            snippet += f" [引用: {citation_count}]"
        if author_str:
            snippet += f" [作者: {author_str}]"

        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "score": min(1.0, citation_count / 1000) if citation_count else 0.5,
            "source": "semantic_scholar",
        })

    return results[:10]


def _parse_unpaywall(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Unpaywall 响应（单对象根，嵌套 best_oa_location）。

    为什么需要专用解析器：与 doi 同理，响应是**一个对象**而非数组，而声明式
    output_map 只支持数组根（`_extract_items` 对 dict 根返回空）。且「有没有
    合法免费全文」这个答案藏在嵌套字段里（is_oa / best_oa_location.url），
    需要展开成人可读的一条结果。

    本引擎的价值不是「再给一篇论文」，而是回答「这篇论文有没有合法免费全文、
    在哪」——所以把 OA 状态写进标题，让人一眼可判。
    """
    if not isinstance(data, dict):
        return []
    title = str(data.get("title") or "").strip()
    doi = str(data.get("doi") or "").strip()
    if not title:
        return []
    is_oa = data.get("is_oa") is True
    best = data.get("best_oa_location") or {}
    oa_url = ""
    if isinstance(best, dict):
        oa_url = str(best.get("url") or best.get("url_for_pdf") or "").strip()
    # 无 OA 全文时回落到 DOI 落地页：保证 url 非空（下游会过滤掉无 url 的条目，
    # 而那会表现为「引擎明明有记录却是 0 条」的静默空结果）。
    url = oa_url or (f"https://doi.org/{doi}" if doi else "")
    if not url:
        return []
    journal = str(data.get("journal_name") or "").strip()
    year = str(data.get("year") or "").strip()
    host = str(best.get("host_type") or "").strip() if isinstance(best, dict) else ""
    status = ("有合法免费全文" if (is_oa and oa_url)
              else "有 OA 记录但未给出链接" if is_oa
              else "无开放获取版本（需订阅或文献互助）")
    parts = [p for p in (journal, year, host, status) if p]
    return [{
        "title": f"{title} — {status}"[:500],
        "url": url,
        "snippet": " · ".join(parts)[:300],
        "source": "unpaywall",
        "published_at": year,
    }]


def _parse_doi(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 doi.org 内容协商响应（CSL JSON，单对象根）。

    doi.org 配 Accept: application/vnd.citationstyles.csl+json 返回的是
    **一个** CSL JSON 对象而非数组——声明式 output_map 只支持数组根，
    由本解析器包裹成单元素结果。CSL 字段多为嵌套（container-title 是
    list、issued.date-parts 是二维数组），在此展平。
    """
    if not isinstance(data, dict):
        return []
    title = data.get("title") or ""
    if isinstance(title, list):
        title = " ".join(str(t) for t in title)
    title = str(title).strip()
    container = data.get("container-title") or ""
    if isinstance(container, list):
        container = " ".join(str(c) for c in container)
    doi = str(data.get("DOI") or "").strip()
    url = str(data.get("URL") or "").strip() or (f"https://doi.org/{doi}" if doi else "")
    if not title or not url:
        return []
    snippet_parts = [str(p) for p in (container, data.get("publisher") or "") if p]
    out: dict[str, Any] = {
        "title": title[:500],
        "url": url,
        "source": "doi",
    }
    if snippet_parts:
        out["snippet"] = " · ".join(snippet_parts)[:300]
    # CSL issued.date-parts: [[2021, 3, 1]] → "2021-3-1"
    issued = data.get("issued") or {}
    dp = issued.get("date-parts") or [] if isinstance(issued, dict) else []
    if dp and dp[0]:
        out["published_at"] = "-".join(str(p) for p in dp[0] if p)
    authors = data.get("author") or []
    if isinstance(authors, list):
        names = []
        for a in authors[:3]:
            if isinstance(a, dict):
                name = f"{a.get('given', '')} {a.get('family', '')}".strip()
                if name:
                    names.append(name)
        if names:
            out["authors"] = "; ".join(names)
    return [out]


# 引擎名 → 专用 JSON 解析器（无 output_map 时的精确格式）
_CUSTOM_JSON_PARSERS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "duckduckgo": _parse_duckduckgo,
    "uapi": _parse_uapi,
    "semantic_scholar": _parse_semantic_scholar,
    "doi": _parse_doi,
    "unpaywall": _parse_unpaywall,
}




def mcp_error_of(data: Any, source: str = "mcp") -> str | None:
    """检查 MCP JSON-RPC 响应是否含错误，返回错误描述；无错误返回 None。

    ## 为什么需要这个函数（2026-09-10 实测）

    argo 自己**输出** MCP 的 `isError` 约定（见 mcp_handlers.py），但**消费**
    上游 MCP 服务时完全忽略它。实测 anysearch 上游返回：

        HTTP 200
        {"result": {"content": [{"text": "Service temporarily unavailable."}],
                    "isError": true}}

    旧实现只做两件事：① 在文本里找配额关键词（quota/429/rate limit…）
    ② 按 "### N." 解析结果块。而 "Service temporarily unavailable." 不含配额
    词、也没有结果块 → **静默返回空列表**。用户看到「没有结果」，而不是
    「上游不可用」；熔断器也拿不到失败信号，无法降权。

    这类「失败伪装成成功」在本仓已出现多次（V2EX 旧实现产出幻觉、
    缓存软命中跨引擎串味、juejin/qiita 返回热榜、you/parallel 状态说谎）。
    本函数把 MCP 错误判定收紧成唯一来源，供所有 MCP 消费者复用。

    ## 覆盖三类错误

      1. 工具级：`{"result": {"isError": true, ...}}` —— MCP 规范的工具错误
      2. 协议级：`{"error": {"code": ..., "message": ...}}` —— JSON-RPC 错误
      3. 空响应：`result` 缺失或 content 为空且无结构化字段

    业务级错误（如配额耗尽）语义因服务而异，不在此判定——由调用方
    结合 `content` 文本自行处理（见 anysearch 的配额关键词检查）。
    """
    if not isinstance(data, dict):
        return f"{source}: 响应非 JSON 对象"
    # 协议级错误（JSON-RPC 标准）
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or "unknown"
        return f"{source}: JSON-RPC 错误 {msg}"
    if isinstance(err, str) and err:
        return f"{source}: {err}"
    result = data.get("result")
    if not isinstance(result, dict):
        return None  # 交回调用方按业务语义判断（可能是另一种响应形态）
    # 工具级错误（MCP 规范）
    if result.get("isError") is True:
        texts = []
        for item in (result.get("content") or []):
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
            elif isinstance(item, str):
                texts.append(item)
        detail = " ".join(texts).strip()[:200] or "isError=true（无详情）"
        return f"{source}: {detail}"
    return None
