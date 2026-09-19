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
from typing import Any, Optional

logger = logging.getLogger("unified_search.semantic_evidence")

DEFAULT_ENDPOINT = "https://classifier.dev"
DEFAULT_TIMEOUT_S = 5.0
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


def classify(texts: list[str], labels: tuple[str, ...] | list[str], *,
             instructions: str | None = None,
             timeout: float | None = None,
             tier: str | None = None) -> Optional[list[dict[str, Any]]]:
    """一次批量分类（≤1000 条）。任何异常/非 200/条数不匹配 → None。

    返回按输入顺序排列的 [{label, confidence, scores}]；调用方负责判定阈值。
    空文本会被过滤后再发（空串会让整批 400），返回列表与输入等长、空缺位为 None。
    """
    if not texts or len(labels) < 2:
        return None
    keep = [(i, t) for i, t in enumerate(texts) if (t or "").strip()]
    if not keep:
        return None
    body: dict[str, Any] = {
        "labels": [str(x) for x in labels],
        "inputs": [t[:MAX_TEXT_CHARS] for _i, t in keep],
        "tier": tier or str(_config().get("tier") or "fast"),
    }
    if instructions:
        body["instructions"] = instructions
    try:
        from http_client import HttpClient
        resp = HttpClient(timeout=timeout or _timeout(), max_retries=1,
                          jitter=False).post(_endpoint(), body)
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
    """
    usable = [it for it in items if (it.get("text") or "").strip() and it.get("url")]
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
    """
    usable = [it for it in items if it.get("url") and
              ((it.get("text") or "").strip() or (it.get("title") or "").strip())]
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
        out[str(it["url"])] = {
            "label": label,
            "confidence": float(conf) if isinstance(conf, (int, float)) else 0.0,
        }
    return out or None
