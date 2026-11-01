#!/usr/bin/env python3
"""numeric_verify.py — 数值印证切片：摘要里的关键数字，打开原文确认。

问题重定义（第一性，2026-11-01 实测）：
  搜索摘要里带着价格/百分比这类「关键数字」，且互相冲突。同一 query
  「iPhone 17 Pro Max 官方价格」的 top-8 里：博彩 SEO 毒内容（挂医院域名）
  说「Pro Max 5999 元起」，财联社说系列起价 5999，苹果官网摘要 RMB 11,999。
  融合排序对数字无感，把 5999 混进 Pro Max 的答案只差一次引用。

  外部佐证：X 帖（@qi9098，2026-10-31）把同一条经验写成铁律——「搜索返回的
  摘要只当线索，价格、日期这些关键数字要打开原页面确认」。本模块把它做成
  确定性切片：正则检出关键数字 → fetch 原文 → 数字比对 → 冲突者降权。

设计取舍（与 evidence_loop.reorder_by_evidence 同一哲学）：
  ① 只降不升。印证只覆盖 top-k，加分等于奖励「恰好被抓取」；降权表达的
     「原文对不上摘要的数字」是真实信号。
  ② 拿不到正文不惩罚（unfetchable 与 verify 的 pending 同语义）——不伪造。
  ③ 比对用「数字 token 集」不用子串：摘要数字的规范化形态在页面数字
     token 集里即算命中。宁漏杀不误伤：partial/confirmed 不惩罚。
  ④ v1 范围 = 价格（货币符号前缀 / 「N元」后缀）+ 百分比 + 完整日期。
     「N万/N亿」记数法不抽——「2.5万」vs「25000」的换算噪音会制造假冲突，
     收益撑不起风险（留作有标注数据后的扩展点）。

降权语义（reorder_by_numeric）：
    missing   摘要有数字、原文一个都没有 → ×(1-weight)         强信号
    partial   部分命中                        → ×(1-weight/2)    弱信号
    confirmed 全部命中                        → 不动
    unfetchable fetch 失败/超时                → 不动
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

# 数字 token：页面侧与摘要侧统一用同一套抽取+规范化，比对才是同构的。
_NUM_TOKEN_RE = re.compile(r"\d[\d,，]*(?:\.\d+)?")
# 价格：货币符号/代码前缀（$1,299 / ¥5999 / RMB 11,999 / US$799）
_PRICE_PREFIX_RE = re.compile(
    r"(?:US\$|[$€£¥￥]|RMB|CNY|USD|EUR|GBP|JPY)\s?(\d[\d,，]*(?:\.\d+)?)")
# 价格：中文元后缀（5999元 / 1,299 块钱），不吞「万/亿」记数法（见模块头④）
_PRICE_SUFFIX_RE = re.compile(
    r"(\d[\d,，]*(?:\.\d+)?)\s?(?:元|块钱)")
# 百分比（37.5% / 37.5％）
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?[%％]")
# 日期：完整「2025年9月10日」「2025-09-10」与无年「9月10日」
_DATE_FULL_RE = re.compile(r"(?:(20\d{2})\s?[年\-/.]\s?)?(\d{1,2})\s?月\s?(\d{1,2})\s?[日号]?")
_DATE_ISO_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")

# 结果字段上 verdict 的规范值
VERDICTS = ("confirmed", "partial", "missing", "unfetchable")


def _norm_num(s: str) -> str:
    """数字字符串规范化：去千分位逗号（全半角），保留小数点。"""
    return s.replace(",", "").replace("，", "").strip()


def extract_critical_numbers(text: str) -> list[dict[str, str]]:
    """从摘要/标题抽关键数字。返回 [{kind, raw, norm}]，按出现顺序去重。"""
    if not text:
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, raw: str, norm: str) -> None:
        key = (kind, norm)
        if norm and key not in seen:
            seen.add(key)
            out.append({"kind": kind, "raw": raw, "norm": norm})

    for m in _PRICE_PREFIX_RE.finditer(text):
        _add("price", m.group(0), _norm_num(m.group(1)))
    for m in _PRICE_SUFFIX_RE.finditer(text):
        _add("price", m.group(0), _norm_num(m.group(1)))
    for m in _PCT_RE.finditer(text):
        _add("pct", m.group(0), _norm_num(m.group(1)))
    for m in _DATE_ISO_RE.finditer(text):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        _add("date", m.group(0), f"{y}-{int(mo):02d}-{int(d):02d}")
    for m in _DATE_FULL_RE.finditer(text):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
            continue  # 「12 组 3 通道」这类数字+量词不是日期
        norm = f"{int(mo)}月{int(d)}日" + (f"{y}年" if y else "")
        _add("date", m.group(0), norm)
    return out


def _page_number_tokens(page: str) -> set[str]:
    """页面文本的数字 token 集（同样去千分位），供 token 级比对。"""
    return {_norm_num(t) for t in _NUM_TOKEN_RE.findall(page or "")}


def _page_date_hit(norm: str, page: str) -> bool:
    """日期命中：规范化形态（如 9月10日2025年 / 2025-09-10）任一子串出现。"""
    if not norm:
        return False
    # 提取年/月日成分，允许页面用不同分隔符写同一天
    y = re.search(r"(20\d{2})年", norm)
    md = re.search(r"(\d{1,2})月(\d{1,2})日", norm)
    cands: list[str] = [norm]
    if y and md:
        yy, mo, d = y.group(1), int(md.group(1)), int(md.group(2))
        cands += [f"{yy}-{mo:02d}-{d:02d}", f"{yy}/{mo:02d}/{d:02d}",
                  f"{yy}年{mo}月{d}日", f"{yy}-{mo}-{d}"]
    if md:
        cands.append(md.group(0))
    return any(c in page for c in cands)


def _verdict_for(nums: list[dict[str, str]], page: str) -> tuple[str, list[str], list[str]]:
    """比对一组摘要数字与页面文本 → (verdict, hits, misses)。"""
    tokens = _page_number_tokens(page)
    hits: list[str] = []
    misses: list[str] = []
    for n in nums:
        if n["kind"] == "date":
            hit = _page_date_hit(n["norm"], page)
        else:
            hit = n["norm"] in tokens
        (hits if hit else misses).append(n["raw"])
    if not misses:
        return "confirmed", hits, misses
    if hits:
        return "partial", hits, misses
    return "missing", hits, misses


def numeric_verify_results(results: list[dict[str, Any]],
                           query: str,
                           top_k: int = 3,
                           fetch_fn: Callable[..., dict[str, Any]] | None = None,
                           max_chars: int = 8000,
                           timeout: float = 4.0) -> dict[str, Any]:
    """对 top_k 条「摘要含关键数字」的结果 fetch 原文，比对数字并回填字段。

    与 evidence_loop.verify_results 同构但不混用：verify 量「正文吸收分」，
    本切片量「摘要数字与原文是否对得上」。选择面从 results 头部起扫，只取
    摘要含关键数字且非 SERP 的条目（数字才值得付 RTT）。

    回填（原地，不改排序——排序见 reorder_by_numeric）：
      r["numeric_verification"] = {verdict, checked, hits, misses, fetch_method}

    返回：
      - verified: [{url, title, verdict, hits, misses, fetch_method}]
      - summary: {checked, confirmed, partial, missing, unfetchable}
      - pending: 未取到正文的 URL
    """
    # 守卫：top_k 非正数按默认 3（与 verify_results 同款，避免反向切片）
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 3
    if top_k <= 0:
        top_k = 3

    if fetch_fn is None:
        from fetch_v3 import fetch_v3

        def _fast_fetch(url: str, max_chars: int, timeout: float) -> dict:
            # 核验快道：与 verify_results 一致，关浏览器兜底、8s deadline 硬顶
            return fetch_v3(url, max_chars=max_chars, timeout=timeout,
                            use_browser_fallback=False, deadline_s=8.0)
        fetch_fn = _fast_fetch

    # 选样：头部起扫，只收「摘要含关键数字」的非 SERP 条目
    targets: list[tuple[dict, list[dict[str, str]]]] = []
    for r in results:
        if len(targets) >= top_k:
            break
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        try:
            from serp_guard import is_serp_or_jump_url
            if is_serp_or_jump_url(url):
                continue
        except Exception:  # pragma: no cover
            pass
        nums = extract_critical_numbers(
            f"{r.get('title') or ''} {r.get('snippet') or r.get('summary') or ''}")
        if not nums:
            continue
        targets.append((r, nums))

    def _fetch_one(item: tuple[dict, list[dict[str, str]]]):
        # 只返回载荷 (page, method)：item 本身（r, nums）由 run_bounded 的
        # finished 原样带回，调用方用 (*item, value) 拼回三元组——worker 再
        # 捎一份 item 会造成双层嵌套（2026-11-01 实测踩中：并行路径
        # page, method = val 炸 ValueError，单条串行路径测不出来）。
        r, _nums = item
        url = r.get("url") or ""
        try:
            fr = fetch_fn(url, max_chars=max_chars, timeout=timeout)
        except Exception:  # pragma: no cover
            return None
        page = (fr.get("content") or "") if isinstance(fr, dict) else ""
        method = fr.get("fetch_method") if isinstance(fr, dict) else None
        return page, method

    # 墙钟预算与 verify 同一开关（ARGO_VERIFY_BUDGET_S，默认 5s）：到点未回的
    # URL 记 pending，语义与 fetch 失败一致
    from evidence_loop import _verify_budget_s
    budget_s = _verify_budget_s()

    outcomes: list[tuple[dict, list, tuple[str, str] | None]] = []
    pending: list[str] = []
    if not targets:
        pass
    elif budget_s > 0 and len(targets) > 1:
        from bounded_run import TaskError, run_bounded
        finished, unfinished = run_bounded(
            targets, _fetch_one, budget_s, max_workers=min(len(targets), 3))
        for item, value in finished:
            if isinstance(value, TaskError):
                pending.append(item[0].get("url") or "")
                continue
            outcomes.append((*item, value))
        for item in unfinished:
            pending.append(item[0].get("url") or "")
    else:
        for item in targets:
            val = _fetch_one(item)
            if val is None:
                pending.append(item[0].get("url") or "")
            else:
                outcomes.append((*item, val))

    verified: list[dict[str, Any]] = []
    counts = {"confirmed": 0, "partial": 0, "missing": 0, "unfetchable": 0}
    for r, nums, val in outcomes:
        page, method = val
        if not page or len(page.strip()) < 100:
            # 正文过短：无数可比，不伪造 verdict（unfetchable，不惩罚）
            verdict, hits, misses = "unfetchable", [], [n["raw"] for n in nums]
        else:
            verdict, hits, misses = _verdict_for(nums, page)
        counts[verdict] += 1
        r["numeric_verification"] = {
            "verdict": verdict,
            "checked": [n["raw"] for n in nums],
            "hits": hits,
            "misses": misses,
            "fetch_method": method,
        }
        verified.append({
            "url": r.get("url") or "",
            "title": (r.get("title") or "")[:120],
            "verdict": verdict,
            "hits": hits,
            "misses": misses,
            "fetch_method": method,
        })
    for url in pending:
        counts["unfetchable"] += 1

    return {
        "query": query,
        "verified": verified,
        "summary": {"checked": len(verified), **counts},
        "pending": pending,
    }


def reorder_by_numeric(results: list[dict[str, Any]],
                       weight: float = 0.35) -> dict[str, Any]:
    """把数值印证的冲突信号写回排序（只降不升，中性=不动）。

    missing  → ×(1-weight)        摘要数字在原文里一个都没有：文不对题/毒内容
    partial  → ×(1-weight/2)      部分对不上：可能是改版/语境差异，弱惩罚
    其余（confirmed/unfetchable/未核验）不动——未核验条目不参与比较，避免
    「没验证的因为没被降权而反超」的采样偏差（与 reorder_by_evidence 同款）。
    """
    if not results:
        return {"reordered": False, "adjusted": [], "moved": 0}
    try:
        weight = max(0.0, min(1.0, float(weight)))
    except (TypeError, ValueError):
        weight = 0.35

    adjusted: list[dict[str, Any]] = []
    old_order = [(r.get("url") or "") for r in results]
    for r in results:
        if not isinstance(r, dict):
            continue
        nv = r.get("numeric_verification") or {}
        verdict = nv.get("verdict")
        if verdict not in ("missing", "partial"):
            continue
        try:
            base = float(r.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        factor = 1.0 - weight if verdict == "missing" else 1.0 - weight / 2.0
        new_score = base * factor
        if new_score == base:
            continue
        r["score"] = round(new_score, 4)
        adjusted.append({
            "url": r.get("url") or "",
            "verdict": verdict,
            "pre_score": round(base, 4),
            "post_score": round(new_score, 4),
        })
    if not adjusted:
        return {"reordered": False, "adjusted": [], "moved": 0}
    results.sort(key=lambda r: r.get("score", 0) or 0, reverse=True)
    new_order = [(r.get("url") or "") for r in results]
    moved = sum(1 for i, u in enumerate(new_order)
                if i < len(old_order) and u != old_order[i])
    return {"reordered": True, "adjusted": adjusted, "moved": moved}


def maybe_verify_numbers(results: list[dict[str, Any]],
                         query: str,
                         top_k: int = 3,
                         fetch_fn: Callable[..., dict[str, Any]] | None = None,
                         ) -> Optional[dict[str, Any]]:
    """便利入口：无数字可核时返回 None（调用方不落任何键，输出逐位不变）。"""
    has_nums = any(
        isinstance(r, dict) and extract_critical_numbers(
            f"{r.get('title') or ''} {r.get('snippet') or r.get('summary') or ''}")
        for r in (results or [])[:max(top_k, 1) * 3]
    )
    if not has_nums:
        return None
    return numeric_verify_results(results, query, top_k=top_k, fetch_fn=fetch_fn)
