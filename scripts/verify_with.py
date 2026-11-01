#!/usr/bin/env python3
"""verify_with.py — 主查+核对配对：一个查询类型配一个核对引擎。

问题重定义（第一性，2026-11-01 实测）：
  同一 query 的主查聚合池（中文多源）混着转述、旧闻与 SEO 毒内容；而
  「核对」要的是一张干净的**独立官方簇**。实测「iPhone 17 Pro Max 官方
  价格」：主查 top-8 无一条干净官方直链，`--engine exa` 3 秒返回的 top-5
  全部是 apple.com 官方域（大陆/英/日/港/泰）。主查与核对是两种不同的
  检索意图，不该靠一个融合排序硬捏。

  外部佐证：X 帖（@qi9098，2026-10-31）的路由表把「先用什么查、再用什么
  核对」写成两列——「一般网页、新闻、价格：WebSearch，再用 Exa 核对」。

设计取舍：
  ① 配对规则是「查询类型 → 核对引擎」，不是引擎属性——所以放本模块的
     规则表，不进 engine_registry.yaml（引擎条目描述的是引擎自己）。
  ② 核对结果**独立交付**（verify_with 段），不并入主排序：官方簇的价值
     在「给 Agent 一眼可对的第二意见」，混排反而稀释。
  ③ 默认关（显式 --verify-with 才跑）。auto 档按规则表选引擎，无规则
     命中就明确报 skipped，不静默空转。
  ④ 核对调用走 super_search 的单引擎路径（与 --engine exa 完全同款），
     复用其缓存与告警纪律；失败 fail-open，绝不拖垮主搜索。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import urlparse

# 规则表：查询特征 → 核对引擎。顺序即优先级，首条命中即返回。
# why exa：语义检索强于词面，官方/一手站点召回稳定（10-31 实测 5/5 官方簇），
# 且在本机免密钥可路由；deepseek_search 计费不作默认核对档。
_VERIFY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 价格/购买意图：价格、售价、报价、多少钱、行情价、price
    (re.compile(r"价格|售价|报价|多少钱|卖多少|费用|收费|price|pricing|cost",
                re.IGNORECASE), "exa"),
    # 官方规格/参数核对：参数、配置、规格、对比
    (re.compile(r"参数|配置|规格|spec|specification|对比",
                re.IGNORECASE), "exa"),
)


def pick_verify_engine(query: str, domain: str | None = None) -> str | None:
    """按查询特征选核对引擎；无规则命中返回 None（调用方明确 skipped）。"""
    if not query:
        return None
    for pat, engine in _VERIFY_RULES:
        if pat.search(query):
            return engine
    return None


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:  # pragma: no cover
        return ""


def run_verify_search(query: str, engine: str, n: int = 5,
                      timeout: int = 10) -> dict[str, Any]:
    """单引擎核对检索（与 `--engine <名>` 同一条路径）。失败抛给调用方。"""
    from search import super_search  # 懒加载：search 模块重（~百 ms），仅开启时付
    # include_local 显式关：fast 模式默认并入本机文件命中（file://），而核对
    # 簇要的是「独立 web 官方簇」——实测 file:// 命中混进 exa 结果污染核对段
    # （2026-11-01 端到端实测抓到）。
    out = super_search(query, engine=engine, n=n, mode="fast", quiet=True,
                       timeout=timeout, include_local=False)
    return out.get("results") or []


def attach_verify_with(results: dict[str, Any],
                       query: str,
                       engine: str = "auto",
                       n: int = 5,
                       timeout: int = 10,
                       runner: Callable[..., list[dict]] | None = None,
                       ) -> dict[str, Any]:
    """执行核对并写 results["verify_with"] 段。

    engine="auto"：按规则表选；无命中 → {"engine": None, "skipped": "no_rule"}。
    engine 显式点名：照跑（含未知引擎——错误由执行层 errors 报告，这里不拦）。

    段结构（独立交付，不动主排序）：
      engine        实际使用的核对引擎
      skipped       未执行原因（no_rule / error:…），无则无此键
      elapsed_ms    核对调用墙钟
      results       核对结果（每条附 verify_provenance=True，n 条）
      overlap       与主查的域级重叠 {domains, count}——重叠是「两路独立
                    检索都看到同一域」，是比 URL 级更稳的核对信号
    """
    chosen = engine
    if engine in ("auto", ""):
        chosen = pick_verify_engine(query, results.get("domain"))
        if not chosen:
            return {"engine": None, "skipped": "no_rule"}

    run = runner or run_verify_search
    t0 = time.perf_counter()
    try:
        vres = run(query, chosen, n=n, timeout=timeout)
    except Exception as e:  # fail-open：核对失败不影响主搜索交付
        return {"engine": chosen, "skipped": f"error:{type(e).__name__}",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    for r in vres or []:
        if isinstance(r, dict):
            r["verify_provenance"] = True

    main_domains = {_domain_of(r.get("url") or "")
                    for r in (results.get("results") or []) if isinstance(r, dict)}
    verify_domains = {_domain_of(r.get("url") or "") for r in (vres or [])
                      if isinstance(r, dict)} - {""}
    overlap = sorted(d for d in verify_domains if d in main_domains)

    return {
        "engine": chosen,
        "elapsed_ms": elapsed,
        "results": vres or [],
        "overlap": {"domains": overlap, "count": len(overlap)},
    }
