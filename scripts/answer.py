#!/usr/bin/env python3
"""answer.py — 直答端点（Answer 形态）：带引用的合成答案。

吸纳自 2026-09 对标研究：Brave Answers / Perplexity Sonar / Seltz Answer
三家同形态，行业已收紧为「搜索的 Answer 端点」标配。当前通道：
Seltz POST /v1/answer（x-api-key 鉴权，注册赠 20000 次）。

**scope 决定语料，不传默认 news**：上游只支持 news / wikipedia / people /
companies 四个语料。2026-09-16 实测：不传 scope 时技术查询（「RRF 融合算法」）
拿到的是 news 语料里的时政与播客条目，看上去像「引用跑偏」，实为语料选错
而非上游坏了。实测四个语料——companies 查公司概况最好（Apple / 台积电 答得
准且完整）；news 在真新闻查询上可用但覆盖窄；wikipedia 干净；people 不可用
（10 条全 linkedin.com，人名查不到），故不对外推荐。

置信计算方式：上游响应无置信度标量（2026-09-14 实测），故输出诚实计数——
引用条数 + 唯一引用域名数；上游若日后提供 confidence 字段则原样透传。
不造模拟精确度的单一分数。

用法：
  argo answer "query"                     # --json 由 bin/argo 默认注入
  argo answer "query" --scope companies    # 查公司概况
  argo answer "query" --model seltz-pro    # 模型自己发起检索，更慢更贵
  python3 answer.py "query" --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse
from cli_io import dumps
from engine_env import get_env

_SELTZ_ANSWER_URL = "https://api.seltz.ai/v1/answer"
_SELTZ_SCOPES = ("news", "wikipedia", "people", "companies")
_SELTZ_MODELS = ("seltz-base", "seltz-pro")


def _unique_domains(citations: list) -> int:
    hosts = set()
    for c in citations or []:
        if isinstance(c, dict):
            u = str(c.get("url") or "")
        else:
            u = str(c or "")
        if u:
            hosts.add(urlparse(u).hostname or "")
    return len(hosts - {""})


def seltz_answer(query: str, timeout: float = 40.0, *,
                 scope: str | None = None, model: str | None = None) -> tuple[dict | None, str]:
    """调 Seltz Answer；返回 (响应 dict, 错误串)。响应含 answer/citations。

    scope / model 不传就由上游取默认（news / seltz-base）。scope 传错值时上游
    返回 404，故此处先按白名单挡掉，错误信息比 HTTP 404 好读。
    """
    scope = (scope or "").strip().lower()
    model = (model or "").strip().lower()
    if scope and scope not in _SELTZ_SCOPES:
        return None, f"未知 scope {scope!r}（可选：{'/'.join(_SELTZ_SCOPES)}）"
    if model and model not in _SELTZ_MODELS:
        return None, f"未知 model {model!r}（可选：{'/'.join(_SELTZ_MODELS)}）"
    key = get_env(["ARGO_SELTZ_API_KEY", "SELTZ_API_KEY"])
    if not key:
        return None, "SELTZ_API_KEY 未设置（写入 ~/.config/argo/env 后重试）"
    body: dict = {"query": query}
    if scope:
        body["scope"] = scope
    if model:
        body["model"] = model
    try:
        from http_client import HttpClient
        # max_retries=0：合成答案一次计费一次调用，引擎内重试会双倍烧赠额
        client = HttpClient(timeout=timeout, max_retries=0, jitter=False)
        resp = client.post(_SELTZ_ANSWER_URL, body=body, extra_headers={
            "x-api-key": key, "Content-Type": "application/json",
        })
    except Exception as e:
        return None, f"seltz answer 请求失败: {e}"
    if resp.get("status", 0) != 200 or not resp.get("text"):
        return None, f"seltz answer HTTP {resp.get('status')}"
    try:
        data = json.loads(resp["text"])
    except (ValueError, TypeError):
        return None, "seltz answer 响应非 JSON"
    if not isinstance(data, dict) or "answer" not in data:
        return None, f"seltz answer 响应缺 answer 字段: {str(data)[:200]}"
    return data, ""


def main() -> None:
    ap = argparse.ArgumentParser(description="直答：带引用的合成答案（Seltz）")
    ap.add_argument("query", help="要回答的问题")
    ap.add_argument("--json", action="store_true", help="JSON 输出（bin/argo 默认注入）")
    ap.add_argument("--scope", choices=list(_SELTZ_SCOPES), default=None,
                    help="语料：companies 查公司概况最好；news 覆盖窄；"
                         "people 实测不可用。不传由上游取默认 news")
    ap.add_argument("--model", choices=list(_SELTZ_MODELS), default=None,
                    help="seltz-base（默认，一次检索）或 seltz-pro（模型自己发起检索）")
    ap.add_argument("--timeout", type=float, default=40.0)
    args = ap.parse_args()

    t0 = time.monotonic()
    data, err = seltz_answer(args.query, timeout=args.timeout,
                             scope=args.scope, model=args.model)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if err:
        envelope = {"status": "error", "query": args.query, "error": err,
                    "elapsed_ms": elapsed_ms}
        print(dumps(envelope))
        sys.exit(1)

    citations = data.get("citations") or []
    # 上游无置信度标量：诚实计数计算方式；有则透传（前向兼容）
    confidence = {"citations": len(citations),
                  "unique_domains": _unique_domains(citations)}
    if isinstance(data.get("confidence"), (int, float)):
        confidence["upstream"] = data["confidence"]

    envelope = {
        "status": "completed",
        "query": args.query,
        # scope 如实回填实际生效值（未传即上游默认 news），便于排查语料选错
        "scope": args.scope or "news",
        "model": args.model or "seltz-base",
        "answer": str(data.get("answer") or ""),
        "citations": citations,
        "confidence": confidence,
        "engine": "seltz",
        "elapsed_ms": elapsed_ms,
    }
    if args.json:
        print(dumps(envelope))
        return
    # 人类可读：答案正文 + 引用清单
    print(envelope["answer"])
    if citations:
        print("\n引用:")
        for i, c in enumerate(citations, 1):
            if isinstance(c, dict):
                print(f"  [{i}] {str(c.get('url') or '')}")
            else:
                print(f"  [{i}] {c}")
    print(f"\n语料: {envelope['scope']} · 模型: {envelope['model']}")
    print(f"置信口径: {dumps(envelope['confidence'])}")


if __name__ == "__main__":
    main()
