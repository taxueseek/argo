#!/usr/bin/env python3
"""semantic_evidence.py — 可选的语义证据层（classifier.dev，默认关闭）。

## 为什么存在

argo 的正文吸收分是**正则密度**（has_numbers / has_definition / …），它测的是
「这段文本长得像不像证据块」，不测「它是否支持这条查询的主张」。2026-09-19 的
实测（reports/classifier-dev-argo/A、B）：密度最高页判 neutral、密度最低页判
supports 0.99，两者近乎反向；而假前提主张（查询预设「降息」、现实是「加息」）
密度分在结构上无法表达，语义判定 5/5 判 contradicts（conf 0.95~1.00）。

## 定位（与热路径解耦）

- **只在显式核验链路调用**（`search --verify` / `research --verify`），不进搜索
  热路径：热命中 78~82ms 且网络为零，一次分类调用 200~680ms，放进热路径是净损失
- **默认关闭**：config.yaml `semantic_evidence.enabled: false`；显式开启用环境变量
  `ARGO_SEMANTIC_EVIDENCE=1`（env 优先于 config）
- **fail-open**：超时 / HTTP 错误 / 429 / 解析失败 / 条数不匹配 → 返回 None，
  调用方行为与未开启时逐位一致（不加字段、不改排序、不改公式）
- **只加并列字段**：`semantic_support` 挂在结果与 verified 条目上，可信度公式
  （selection 0.40 / absorption 0.35 / freshness 0.15 / original 0.10）不动

## 外部服务约束（2026-09-19 实测，见 reports/classifier-dev-argo/D）

- 无密钥、HTTP 通道稳定（1000 条 1.83s、顺序严格保真）；CLI 与 MCP 通道当前不可用
  （npm/PyPI 404、MCP tools/call 返回 not_found），因此只走 HTTP
- 必须带真实 User-Agent（python urllib 默认 UA 被边缘 403）——复用 HttpClient 的
  UA 轮换与 429 退避
- 标签集必须显式含「none of these」：服务永远返回给定标签之一，不加会把无关文本
  硬塞进某个标签
- 同输入置信度有 ±0.08 漂移（标签稳定），所以只做 ≥0.7 的粗门控；实测 conf ≥0.7
  时严格错误为 0（51 条 entailment 样本）
- 限额按出口 IP：fast 3000 次/分钟、20000 次/天；空串输入会让整批 400，调用前过滤
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("unified_search.semantic_evidence")

DEFAULT_ENDPOINT = "https://classifier.dev"
DEFAULT_TIMEOUT_S = 5.0
# 未声明 transport 时的默认通道。jev = TypeSafe System One（当前唯一可用）；
# classifier.dev 的匿名路径 2026-10-22 起回 403 proxy_requires_payment。
DEFAULT_TRANSPORT = "jev"

# ── JEV / System One 通道（TypeSafe）──────────────────────────────────────
# 官方 API：POST https://api.typesafe.ai/v1/systemone，Bearer 鉴权
# （docs.typesafe.ai/api/system-one）。与 classifier.dev 的 /v1/classify 是
# **两种形状**：那边一次发 N 条文本 + 一组标签；这边一次发一个 state + 若干
# 具名问题，每题独立作答（文档原话：questions are evaluated in parallel and
# in isolation）。所以接过来要做的是形状转换，不是换个 URL。
DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
# key 候选链：argo 推荐名在前；TYPESAFE_API_KEY 是官方 SDK 与 JEV 插件
# （dsh-verbatim-compaction）用的名字，照文档配的人直接可用。
JEV_KEY_ENV = ("ARGO_JEV_API_KEY", "TYPESAFE_API_KEY")
# state 超过这个长度就带长上下文头。实测（2026-10-22 本机）：
#   不带 TypeSafe-Danger-Zone-Long-Context：32K tokens 通过（32350）、
#     40K 中文字（≈40K tokens）回 `max_tokens_exceeded`（400）
#   带上：40K 字（40358 tokens）与 80K 字（80358 tokens）均通过
# 24000 字是保守拐点（正常路径不带头也稳），不是实测上限。
JEV_LONG_CONTEXT_CHARS = 24000
# 并发上限。官方 cookbook（classifying_rag_passages）取 4，理由是端点限流；
# 429/529 要求指数退避，所以不铺大。
JEV_MAX_WORKERS = 4
# 多并发整体截止时间（秒）：实测冷调用在 0.55s 左右，提高到 1.2s（覆盖 p95），防止网络抖动导致的误杀 fail-open
DEFAULT_JEV_DEADLINE_S = 1.2

# 结果级缓存（TTL 7 天）：常见事实与热点二次判定 0ms 响应，API 消耗清零
_JEV_MEMORY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JEV_CACHE_TTL_S = 7 * 86400

# conf ≥ 0.7 才给结论：低于此值的判定不可信（B 实测 4 个严格错误全部 ≤0.67）
SUPPORT_THRESHOLD = 0.7
# 单条文本上限：核验链路取回的正文 ≤8000 字，再高无收益（A 实测 400→7798 字
# 置信度反而从 0.73 升到 0.98，说明长文不劣化，但也没必要超发）
MAX_TEXT_CHARS = 8000

SUPPORT_LABELS = (
    "supports the query",
    "contradicts the query",
    "neutral",
    "none of these",
)
SOURCE_TYPE_LABELS = (
    "primary data or official announcement",
    "secondary reporting",
    "analysis or opinion",
    "social narrative",
    "none of these",
)
SUBSTANTIVE_DISCUSSION_LABELS = (
    "substantive in-depth discussion",
    "passing mention or brief reference",
    "unrelated chatter",
    "none of these",
)
SUBSTANTIVE_DISCUSSION_CRITERIA = {
    "substantive in-depth discussion": "The topic is a primary subject, core explanation, or key focus of the text.",
    "passing mention or brief reference": "The topic is only mentioned in passing, as a metaphor, an offhand remark, or a minor tangent.",
    "unrelated chatter": "The text is casual talk or unrelated discussion where the topic is absent or irrelevant.",
    "none of these": "None of the above criteria fit the text.",
}


def is_local_target(url_or_path: str) -> bool:
    """本地物理隔离屏障：任何本地文件系统路径/私域目标坚决不进 JEV 队列。

    2026-11-11 补 `./` 前缀：相对路径是本机检索最常见的形态
    （`./notes.md`、`./scripts/x.py`），漏了等于私域内容可能外发。
    保留 `http://` 与 `https://` 的判定优先级——带 scheme 的一律不算本地。
    """
    if not url_or_path:
        return False
    u = str(url_or_path).strip()
    low = u.lower()
    if low.startswith(("http://", "https://", "ftp://", "ws://", "wss://")):
        return False
    if u.startswith(("file://", "/", "~", "C:\\", ".\\", "./", "../")):
        return True
    return any(u.startswith(p) for p in ("/Users/", "/home/", "/private/", "\\"))


_RE_HTML_TAGS = re.compile(r"<[^>]+>", re.S)
_RE_DYNAMIC_TIMESTAMPS = re.compile(
    r"\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?|"
    r"\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|秒|分钟|小时|天|周|月|年)\s*(?:ago|前)|"
    r"(?:today|yesterday|just now|刚刚|今天|昨天))\b",
    re.I,
)
_RE_WHITESPACE = re.compile(r"\s+")


def _normalize_cache_text(text: str) -> str:
    """对正文进行稳态清洗以生成稳固的缓存键，消除时间戳/动态元数据导致的键漂移。

    不做长度截断（2026-11-11 修）：此前取前 1200 字符，而实际送入判定的
    正文上限是 MAX_TEXT_CHARS=8000——两段仅在第 1200 字符后不同的文本会
    得到同一个键，从而共享同一个 label/confidence。对语义核验这是正确性
    缺陷（错误结果还会被缓存 TTL 7 天）。稳态清洗只改「动态元数据」
    （HTML/时间戳/空白），不改语义内容，故全文入键是安全的。
    """
    if not text:
        return ""
    # 1. 剥离 HTML 残留
    s = _RE_HTML_TAGS.sub(" ", text)
    # 2. 剥离常见相对/绝对易变时间戳
    s = _RE_DYNAMIC_TIMESTAMPS.sub(" ", s)
    # 3. 规整空白
    s = _RE_WHITESPACE.sub(" ", s).strip()
    return s


def _get_cache_key(text: str, labels: tuple[str, ...] | list[str],
                   instructions: str | None,
                   criteria: dict[str, str] | None = None) -> str:
    import hashlib
    crit_repr = str(sorted((criteria or {}).items())) if criteria else str(sorted(str(l) for l in labels))
    norm_text = _normalize_cache_text(text)
    raw = f"{instructions or ''}::{crit_repr}::{norm_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _should_bypass_cache() -> bool:
    import sys
    if os.environ.get("ARGO_NO_CACHE") == "1":
        return True
    if ("pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get("_TEST_ENABLE_SEMANTIC_CACHE"):
        return True
    return False


def _get_cached_result(key: str) -> Optional[dict[str, Any]]:
    if _should_bypass_cache():
        return None
    cached = _JEV_MEMORY_CACHE.get(key)
    if cached is not None:
        ts, data = cached
        if time.time() - ts < _JEV_CACHE_TTL_S:
            return dict(data)
    try:
        from argo_paths import state_path
        p = Path(state_path("semantic_cache.json"))
        if p.is_file():
            data_map = json.loads(p.read_text(encoding="utf-8"))
            rec = data_map.get(key)
            if rec and isinstance(rec, dict) and time.time() - rec.get("ts", 0) < _JEV_CACHE_TTL_S:
                res = rec.get("result")
                if isinstance(res, dict):
                    _JEV_MEMORY_CACHE[key] = (rec.get("ts", time.time()), res)
                    return dict(res)
    except Exception:
        pass
    return None


def _set_cached_result(key: str, result: dict[str, Any]) -> None:
    if _should_bypass_cache():
        return
    now = time.time()
    _JEV_MEMORY_CACHE[key] = (now, result)
    try:
        from argo_paths import state_path
        p = Path(state_path("semantic_cache.json"))
        data_map = {}
        if p.is_file():
            try:
                data_map = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data_map = {}
        if len(data_map) > 500:
            data_map.clear()
        data_map[key] = {"ts": now, "result": result}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data_map, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _config() -> dict[str, Any]:
    """读 config.yaml 的 semantic_evidence 段（缺失时用内置默认）。"""
    try:
        from config import load_config
        cfg = load_config().get("semantic_evidence")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # pragma: no cover - 配置不可用按默认关处理
        return {}


def enabled() -> bool:
    """开关判定：环境变量优先，其次 config.yaml；两者都缺省 → 关闭。

    用 engine_env.get_env/env_flag 读（全仓唯一的布尔解析），所以
    `ARGO_SEMANTIC_EVIDENCE=off`、写进 ~/.config/argo/env 的写法都生效。
    """
    try:
        from engine_env import env_flag, get_env
        if get_env("ARGO_SEMANTIC_EVIDENCE").strip():
            return env_flag("ARGO_SEMANTIC_EVIDENCE", default=False)
    except Exception:  # pragma: no cover - 环境层不可用时不冒险开启
        return False
    return bool(_config().get("enabled", False))


def _endpoint() -> str:
    return str(_config().get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")


def _timeout() -> float:
    try:
        return float(_config().get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def jev_api_key() -> str:
    """JEV（TypeSafe）key：os.environ 优先，~/.config/argo/env 热读保底。"""
    try:
        from engine_env import get_env
        return (get_env(list(JEV_KEY_ENV)) or "").strip()
    except Exception:  # pragma: no cover - 环境层不可用时不冒险
        import os
        for _n in JEV_KEY_ENV:
            _v = (os.environ.get(_n) or "").strip()
            if _v:
                return _v
        return ""


def _transport() -> str:
    """通道选择：`jev`（TypeSafe System One）或 `classify`（classifier.dev）。

    **纯配置驱动，不做「按键就绪自动选」**。初版按 jev_api_key() 是否就绪自动
    选，结果是通道取决于本机 ~/.config/argo/env 里有没有那把 key——同一份代码
    在开发机与 CI 上走不同分支，既有测试在开发机上会静默改道（实测：写进 key
    后 test_semantic_evidence 的 5 条 HttpClient 桩里 1 条改走 JEV 路径而挂）。
    机器状态不该改变代码路径。

    未声明时的默认是 jev：classifier.dev 的匿名路径 2026-10-22 起回 403
    proxy_requires_payment，JEV 是当前可用的那条。
    """
    declared = str(_config().get("transport") or "").strip().lower()
    return declared if declared in ("jev", "classify") else DEFAULT_TRANSPORT


def _jev_endpoint() -> str:
    """JEV 端点：**独立于 classify 通道的 endpoint 字段**。

    不共用是刻意的坑修：config.yaml 里 `endpoint` 长期写的是
    https://classifier.dev，JEV 通道若读同一个字段，就会把 System One 的
    请求体发到 classifier.dev，拿回「Invalid API key / Create a workspace
    key at /app/keys」这种指向完全错误方向的 401（2026-10-22 实测踩中）。
    每个通道自带端点，杜绝「换 transport 忘了换 endpoint」。
    """
    return str(_config().get("jev_endpoint") or DEFAULT_JEV_ENDPOINT).rstrip("/")


def _jev_model() -> str:
    return str(_config().get("jev_model") or _config().get("model")
               or DEFAULT_JEV_MODEL)


def _jev_post(path_state: Any, questions: dict[str, Any], *,
              timeout: float) -> Optional[dict[str, Any]]:
    """一次 System One 请求；失败返回 None（fail-open 的全部出口都在这里）。"""
    key = jev_api_key()
    if not key:
        logger.debug("JEV key 未配置（%s），语义层跳过", " / ".join(JEV_KEY_ENV))
        return None
    body = {
        "model": _jev_model(),
        "state": path_state,
        "questions": questions,
    }
    # Content-Type 必须显式给：HttpClient.post 会把 dict 做 json.dumps，
    # 但**从不设 Content-Type**（头只来自浏览器 UA 模板 + extra_headers）。
    # 缺它时 FastAPI 侧把 body 当字符串，回
    #   422 {"loc":["body"],"msg":"Input should be a valid dictionary…"}
    # ——报错指向「请求体不是对象」，而真因是少了一个头（2026-10-22 实测踩中）。
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    # 长上下文要显式带头，否则 state 一过 32K tokens 就 400 max_tokens_exceeded
    if len(json.dumps(path_state, ensure_ascii=False)) > JEV_LONG_CONTEXT_CHARS:
        headers["TypeSafe-Danger-Zone-Long-Context"] = "true"
    try:
        from http_client import HttpClient
        resp = HttpClient(timeout=timeout, max_retries=1, jitter=False).post(
            _jev_endpoint(), body, extra_headers=headers)
        if resp.get("status") != 200:
            logger.debug("JEV 非 200: %s %s", resp.get("status"),
                         str(resp.get("text") or resp.get("error") or "")[:200])
            return None
        return json.loads(resp.get("text") or "{}")
    except Exception as e:  # fail-open：语义层任何问题都不阻断核验
        logger.debug("JEV 请求失败: %s", type(e).__name__)
        return None


def _jev_choice(text: str, labels: tuple[str, ...] | list[str],
                instructions: str | None, timeout: float,
                criteria: dict[str, str] | None = None
                ) -> Optional[dict[str, Any]]:
    """单条文本 → 一个 Choice 问题（带本地结果级 Hash 缓存，命中 0ms）。

    **每条一个请求**，不是「多段一个 state、每段一问」。理由在官方文档里：
    jaggedness 那篇明写「state 里塞与决策无关的细节会降低准确率（context
    rot）」，而判第 i 段时其余各段就是无关细节；cookbook 的
    classifying_rag_passages 也是「One request per passage」。（我们试过 3 段
    合一，结果全对，但样本太小，不拿它赌准确率。）
    """
    cache_key = _get_cache_key(text, labels, instructions, criteria=criteria)
    cached = _get_cached_result(cache_key)
    if cached is not None:
        return cached

    crit_map = criteria or {str(lab): str(lab) for lab in labels}
    out = _jev_post(text, {
        "cls": {
            "type": "choice",
            "instructions": instructions or "Classify the state into one of the criteria.",
            # criteria 的**值**是描述：jaggedness 说 instructions 与 criteria
            # 不一致时模型会困惑，所以未显式给描述时两边用同一句话。
            "criteria": crit_map,
        },
    }, timeout=timeout)
    if not isinstance(out, dict):
        return None
    ans = (out.get("answers") or {}).get("cls")
    if not isinstance(ans, dict) or not ans.get("choice"):
        if isinstance(ans, dict):
            logger.debug("JEV 答案缺 choice: %s", str(ans)[:120])
        return None
    res = {
        "label": str(ans.get("choice")),
        "confidence": ans.get("confidence"),
        "scores": ans.get("probabilities"),
    }
    _set_cached_result(cache_key, res)
    return res


def _classify_jev(texts: list[str], labels: tuple[str, ...] | list[str], *,
                  instructions: str | None, criteria: dict[str, str] | None = None,
                  keep: list[tuple[int, str]],
                  timeout: float) -> Optional[list[dict[str, Any]]]:
    """JEV 通道：每条文本一个请求，并发 4，带总体时间硬截止预算。

    任一条失败就在该位留 None（与 classifier.dev 通道「整批失败即 None」
    不同）：一次核验有几条正文，坏一条不该把其余判定的价值也丢掉。全部失败
    时仍返回 None，调用方行为与未开启时一致。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    texts_kept = [t[:MAX_TEXT_CHARS] for _i, t in keep]
    deadline_s = DEFAULT_JEV_DEADLINE_S
    start_t = time.time()
    results: list[Optional[dict[str, Any]]] = [None] * len(texts_kept)

    with ThreadPoolExecutor(max_workers=min(JEV_MAX_WORKERS, len(texts_kept))) as pool:
        futures = {pool.submit(_jev_choice, t, labels, instructions, timeout, criteria): idx
                   for idx, t in enumerate(texts_kept)}
        try:
            for fut in as_completed(futures, timeout=max(0.1, deadline_s)):
                orig_idx = futures[fut]
                try:
                    results[orig_idx] = fut.result()
                except Exception:
                    results[orig_idx] = None
                if time.time() - start_t >= deadline_s:
                    break
        except TimeoutError:
            pass

    if not any(r for r in results):
        return None
    out: list[Optional[dict[str, Any]]] = [None] * len(texts)
    for (idx, _t), r in zip(keep, results):
        if isinstance(r, dict):
            out[idx] = r
    return out  # type: ignore[return-value]


def classify(texts: list[str], labels: tuple[str, ...] | list[str], *,
             instructions: str | None = None,
             criteria: dict[str, str] | None = None,
             timeout: float | None = None,
             tier: str | None = None) -> Optional[list[dict[str, Any]]]:
    """一次批量分类。任何异常/非 200/条数不匹配 → None。

    返回按输入顺序排列的 [{label, confidence, scores}]；调用方负责判定阈值。
    空文本会被过滤后再发（空串会让整批 400），返回列表与输入等长、空缺位为 None。

    两个通道的实现分居 _classify_jev（TypeSafe，每条一请求）与下方内联的
    classifier.dev 路径（一次发 N 条）；开关判定见 _transport。
    """
    if not texts or len(labels) < 2:
        return None
    keep = [(i, t) for i, t in enumerate(texts) if (t or "").strip()]
    if not keep:
        return None
    to = timeout or _timeout()
    if _transport() == "jev":
        return _classify_jev(texts, labels, instructions=instructions,
                             criteria=criteria, keep=keep, timeout=to)
    body: dict[str, Any] = {
        "labels": [str(x) for x in labels],
        "inputs": [t[:MAX_TEXT_CHARS] for _i, t in keep],
        "tier": tier or str(_config().get("tier") or "fast"),
    }
    if instructions:
        body["instructions"] = instructions
    try:
        from http_client import HttpClient
        # Content-Type 同 JEV 通道：HttpClient.post 不设它，缺了会被服务端
        # 当字符串解析。这条路径此前只在 403（匿名要付费 key）处止步，没走到
        # body 校验，所以同一个坑没暴露——配了有效 workspace key 就会现形。
        resp = HttpClient(timeout=to, max_retries=1, jitter=False).post(
            _endpoint(), body,
            extra_headers={"Content-Type": "application/json"})
        if resp.get("status") != 200:
            logger.debug(f"语义分类非 200: {resp.get('status')} {resp.get('error')}")
            return None
        data = json.loads(resp.get("text") or "{}")
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(keep):
            logger.debug("语义分类返回条数与输入不匹配，按失败处理")
            return None
    except Exception as e:  # fail-open：语义层任何问题都不阻断核验
        logger.debug(f"语义分类失败: {type(e).__name__}")
        return None
    out: list[Optional[dict[str, Any]]] = [None] * len(texts)
    for (idx, _t), r in zip(keep, results):
        if isinstance(r, dict):
            out[idx] = r
    return out  # type: ignore[return-value]


def assess_support(query: str, items: list[dict[str, Any]]) -> Optional[dict[str, dict]]:
    """对已抓取的正文批量判定「是否支持查询隐含的主张」。

    items: [{"url": str, "title": str, "text": str}]
    返回 {url: {label, confidence, supports, contradicts}}；未达阈值时
    supports/contradicts 均为 False（原始 label 与 confidence 仍原样保留，
    调用方可自行换阈值）。失败返回 None。
    自动剔除本地文件（is_local_target），严格物理阻断私域外发。
    """
    usable = [it for it in items if (it.get("text") or "").strip() and it.get("url")
              and not is_local_target(it.get("url", ""))]
    if not usable:
        return None
    texts = [f"{it.get('title') or ''}\n{(it.get('text') or '')[:MAX_TEXT_CHARS]}"
             for it in usable]
    instructions = (
        f"Judge whether each passage supports the claim implied by this query: {query!r}. "
        "Answer 'supports the query' when the passage backs the claim, "
        "'contradicts the query' when it states the opposite or a conflicting figure, "
        "'neutral' when it is on-topic but neither supports nor contradicts, "
        "and 'none of these' when it is unrelated to the query."
    )
    results = classify(texts, SUPPORT_LABELS, instructions=instructions)
    if results is None:
        return None
    out: dict[str, dict] = {}
    for it, r in zip(usable, results):
        if not r:
            continue
        label = str(r.get("label") or "")
        conf = r.get("confidence")
        conf_f = float(conf) if isinstance(conf, (int, float)) else 0.0
        out[str(it["url"])] = {
            "label": label,
            "confidence": conf_f,
            "supports": label == "supports the query" and conf_f >= SUPPORT_THRESHOLD,
            "contradicts": label == "contradicts the query" and conf_f >= SUPPORT_THRESHOLD,
        }
    return out or None


def assess_source_types(items: list[dict[str, Any]]) -> Optional[dict[str, dict]]:
    """来源类型分级（研究取证链路用）：一手数据 / 二手报道 / 分析观点 / 社交叙事。

    items: [{"url": str, "title": str, "text": str}]（text 可为摘要）
    自动剔除本地文件（is_local_target），严格物理阻断私域外发。
    """
    usable = [it for it in items if it.get("url") and
              ((it.get("text") or "").strip() or (it.get("title") or "").strip())
              and not is_local_target(it.get("url", ""))]
    if not usable:
        return None
    texts = [f"{it.get('title') or ''}\n{(it.get('text') or '')[:MAX_TEXT_CHARS]}"
             for it in usable]
    instructions = (
        "Classify each source by its nature: 'primary data or official announcement' "
        "for original datasets, filings, official releases; 'secondary reporting' for "
        "news coverage of someone else's work; 'analysis or opinion' for commentary; "
        "'social narrative' for forum/social posts."
    )
    results = classify(texts, SOURCE_TYPE_LABELS, instructions=instructions)
    if results is None:
        return None
    out: dict[str, dict] = {}
    for it, r in zip(usable, results):
        if not r:
            continue
        label = str(r.get("label") or "")
        conf = r.get("confidence")
        conf_f = float(conf) if isinstance(conf, (int, float)) else 0.0
        out[str(it["url"])] = {
            "label": label,
            "confidence": conf_f,
            "is_primary": (label == "primary data or official announcement" and conf_f >= SUPPORT_THRESHOLD),
        }
    return out or None


def assess_substantive_discussion(topic: str, items: list[dict[str, Any]]) -> Optional[dict[str, dict]]:
    """对正文/播客单集转录/长篇段落批量判定「是否对主题进行了实质性深入探讨」。

    用于复杂需求与播客/音视频长文本场景，区分：
    - substantive in-depth discussion: 深入、实质性探讨或核心讲解
    - passing mention or brief reference: 顺带提及、比喻借用或偏题带过
    - unrelated chatter: 闲聊或偏离主题
    自动剔除本地文件（is_local_target），严格物理阻断私域外发。
    """
    usable = [it for it in items if (it.get("text") or "").strip() and it.get("url")
              and not is_local_target(it.get("url", ""))]
    if not usable:
        return None
    texts = [f"{it.get('title') or ''}\n{(it.get('text') or '')[:MAX_TEXT_CHARS]}"
             for it in usable]
    instructions = f"Determine the depth of discussion regarding the topic {topic!r} in the text."
    results = classify(texts, SUBSTANTIVE_DISCUSSION_LABELS,
                       instructions=instructions,
                       criteria=SUBSTANTIVE_DISCUSSION_CRITERIA)
    if results is None:
        return None
    out: dict[str, dict] = {}
    for it, r in zip(usable, results):
        if not r:
            continue
        label = str(r.get("label") or "")
        conf = r.get("confidence")
        conf_f = float(conf) if isinstance(conf, (int, float)) else 0.0
        out[str(it["url"])] = {
            "label": label,
            "confidence": conf_f,
            "is_substantive": (label == "substantive in-depth discussion" and conf_f >= SUPPORT_THRESHOLD),
            "is_passing_mention": (label == "passing mention or brief reference" and conf_f >= SUPPORT_THRESHOLD),
        }
    return out or None

