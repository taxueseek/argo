#!/usr/bin/env python3
"""
evidence_loop.py — 证据完整链路（P0）：fetch 后正文吸收分回写 + URL→证据分缓存 + 高后果门控

问题重定义（第一性）：
  Argo 搜索输出的 snippet 级证据分（credibility_fast）是「候选」分，
  Agent 高后果下结论前必须 fetch 正文复核。此前 fetch 结果不回填搜索结果、
  不缓存 URL→证据分、无「该核验哪些」的可编程信号，证据完整链路是开环。

MECE 分工（互不重叠）：
  A. fetch 证据提取  ：从 fetch_v3 结果提取正文级吸收分（extract_fetch_evidence）
  B. URL 证据缓存    ：URL → 正文级证据分，独立于 fetch 正文缓存（get/set_evidence）
  C. 回填            ：搜索结果若已有证据缓存则回填 post_fetch_absorption（backfill_results）
  D. 高后果门控      ：finance/health/legal 等域标记 fetch_required + fetch_suggested（gate_results）
  E. 核验模式        ：显式对 top-k 未核验结果 fetch 并产出 evidence_revision 分布（verify_results）

完整链路：search 输出（建议核验） → Agent fetch → fetch_v3 写证据缓存 → 下次 search 自动回填。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


def _log(message: str) -> None:
    """默认静默的调试出口；仅在真正需要记录时才引入 logging。

    与 search_output._log 同一范式。本模块曾被 search_output 在**每次搜索的
    输出阶段**导入（shape_response → gate_results），模块级 `import logging`
    会把 logging→traceback→dataclasses→inspect→_colorize（实测 21 ms）拖进
    那条路；而全仓没有把 `unified_search` 的 level 调离默认 WARNING，这些
    debug 默认永不产生输出——纯粹是花钱买一个不说话的 logger。
    """
    import logging
    logging.getLogger("unified_search.evidence_loop").debug(message)

# ── 高后果域（finance / health / legal / 事实与安全）──────────────────────────
# 命中这些域时，Agent 在把搜索结果当答案前必须先核验正文。
HIGH_CONSEQUENCE_DOMAINS: frozenset[str] = frozenset({
    # 金融
    "stock_query", "us_stock", "fund_query", "financial_news", "macro_data",
    "crypto_search", "jin10_flash", "cls_telegraph_search", "company_search",
    # 健康
    "medical", "chem_search",
    # 法律
    "legal", "us_legal", "wenshu_query",
    # 事实核查与安全关键
    "fact_check", "aviation_weather",
})

# 证据缓存 TTL：正文级证据分是稳定派生数据，但需跟随正文更新，默认与 fetch 一致
EVIDENCE_DEFAULT_TTL = 3600
EVIDENCE_DOC_TTL = 86400      # docs/reference 长 TTL（正文稳定）
EVIDENCE_NEWS_TTL = 600       # news/realtime 短 TTL（正文常更新）


def is_high_consequence_domain(domain: str | None) -> bool:
    """域是否高后果（finance/health/legal/事实安全）。"""
    return bool(domain and domain in HIGH_CONSEQUENCE_DOMAINS)


# ── A. fetch 证据提取 ──────────────────────────────────────────────────────────

def extract_fetch_evidence(fetch_result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 fetch_v3 结果提取正文级证据分。

    返回 None 表示抓取失败或无正文，不产生证据记录（不污染缓存）。
    """
    if not fetch_result:
        return None
    if not fetch_result.get("success"):
        return None
    content = fetch_result.get("content") or ""
    if not content.strip():
        return None
    title = fetch_result.get("title") or ""
    url = fetch_result.get("url") or ""
    if not url:
        return None

    evidence: dict[str, Any] = {}
    seo_signals: dict[str, Any] = {}
    try:
        from content_signals import (compute_content_quality, score_clickbait,
                                     score_title_body_consistency,
                                     score_template_repetition)
        qual = compute_content_quality(content, title)
        evidence = dict(qual)
        # SEO/低质信号（2026-09-27 新增）：三个信号与 absorption 互补——
        # absorption 量「有没有可抽取的证据块」，这三个量「这些内容是不是
        # 为了被检索而制造的」。标题党/文不对题/模板化都会拉低吸收分，
        # 因为它们是「看起来有内容、实际不可用」的典型形态。
        seo_signals = {
            "clickbait": score_clickbait(title),
            "title_body": score_title_body_consistency(title, content),
            "template": score_template_repetition(content),
        }
    except Exception as e:  # pragma: no cover - 防御降级
        _log(f"compute_content_quality 失败: {type(e).__name__}")

    # 低质折扣：三项都只降不升（乘子 ≤1），且各自设下限，避免单个信号
    # 就抹掉全部吸收分——假阳性代价高于漏放。
    #
    # 权重按 2026-09-27 标定数据重排（tests/golden/lowquality_calibration.json）：
    #   title_body  AUC 0.643 最佳门槛 0.697 最佳F1 0.714 → 主力，折扣最深
    #   clickbait   AUC 0.643 最佳门槛 0.075 最佳F1 0.444 → 辅助，门槛保持 0.5
    #   template    AUC 0.500（=随机）                      → 降为纯旁挂，见下
    #
    # template 的折扣从 ×0.85 撤掉：它在本仓口径下与随机猜测无差别（正常技术
    # 长文 0.833 vs 农场 0.875），却要对每一篇句式平行的正常长文都扣 15%——
    # 那是**确定的伤害换不到的收益**。信号仍照常计算并输出（可观测、未来可
    # 替换为真句法分析），但不再参与折扣计算。若将来接入 POS/依存句法并跑出
    # AUC>0.75，这里是恢复点。
    absorption = evidence.get("absorption_score")
    if absorption is not None and seo_signals:
        discount = 1.0
        cb = seo_signals.get("clickbait") or {}
        tb = seo_signals.get("title_body") or {}
        if cb.get("score", 0) >= 0.5:
            discount *= 0.90                      # 标题党（弱信号）：-10%
        if tb.get("mismatch"):
            # 文不对题按覆盖度线性折扣：coverage=0 → ×0.7，coverage=0.5 → ×0.85
            cov = float(tb.get("coverage") or 0.0)
            discount *= (0.70 + 0.30 * min(cov / 0.5, 1.0))
        absorption = round(max(0.0, float(absorption) * discount), 3)

    return {
        "url": url,
        "absorption": absorption,
        "quality_score": evidence.get("quality_score", fetch_result.get("quality_score")),
        "content_ok": evidence.get("content_ok", fetch_result.get("content_ok")),
        "word_count": evidence.get("word_count", len(content)),
        "evidence_flags": {
            k: bool(evidence.get(k))
            for k in ("has_numbers", "has_definition", "has_comparison",
                      "has_howto", "has_disclose", "is_qa_format")
        },
        "seo_signals": seo_signals or None,
        "page_type": fetch_result.get("page_type"),
        "source_type": fetch_result.get("source_type"),
        "fetch_method": fetch_result.get("fetch_method"),
        "cached": bool(fetch_result.get("cached", False)),
    }


def ttl_for_fetch_result(fetch_result: dict[str, Any]) -> int:
    """按页面类型选证据缓存 TTL（与 fetch_v3 写正文缓存的策略保持一致）。"""
    st = fetch_result.get("source_type") or fetch_result.get("page_type") or ""
    if st in ("news", "realtime"):
        return EVIDENCE_NEWS_TTL
    if st in ("docs", "documentation", "reference"):
        return EVIDENCE_DOC_TTL
    return EVIDENCE_DEFAULT_TTL


# ── B. URL 证据缓存 ────────────────────────────────────────────────────────────

def store_fetch_evidence(url: str, evidence: dict[str, Any],
                         cache: Any | None = None,
                         ttl: int | None = None) -> None:
    """写 URL → 正文级证据分缓存（独立 kind，与 fetch 正文缓存隔离）。"""
    if not evidence:
        return
    try:
        from cache import SearchCache
        c = cache if cache is not None else SearchCache()
        c.set_evidence(url, evidence, ttl=ttl if ttl is not None else EVIDENCE_DEFAULT_TTL)
    except Exception as e:  # pragma: no cover
        _log(f"store_fetch_evidence 失败: {type(e).__name__}")


def lookup_fetch_evidence(url: str, cache: Any | None = None) -> Optional[dict[str, Any]]:
    """读 URL → 正文级证据分缓存。未命中返回 None。"""
    try:
        from cache import SearchCache
        c = cache if cache is not None else SearchCache()
        hit = c.get_evidence(url)
        if hit:
            out = {k: v for k, v in hit.items() if not str(k).startswith("_")}
            return out
    except Exception as e:  # pragma: no cover
        _log(f"lookup_fetch_evidence 失败: {type(e).__name__}")
    return None


# ── C. 回填 ────────────────────────────────────────────────────────────────────

def backfill_results(results: list[dict[str, Any]],
                     cache: Any | None = None) -> list[dict[str, Any]]:
    """对搜索结果回填已核验证据（若 URL 有证据缓存）。

    原地标记（不改排序）：
      - has_fetched_evidence: bool
      - post_fetch_absorption: float | None（正文级吸收分）
      - fetched_evidence: dict（完整证据记录，含 word_count/quality/content_ok）
    """
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        ev = lookup_fetch_evidence(url, cache)
        if ev and ev.get("absorption") is not None:
            r["has_fetched_evidence"] = True
            r["post_fetch_absorption"] = ev.get("absorption")
            r["fetched_evidence"] = {
                k: ev.get(k) for k in (
                    "quality_score", "word_count", "content_ok",
                    "page_type", "fetch_method", "evidence_flags",
                )
            }
    return results


# ── D. 高后果门控 ──────────────────────────────────────────────────────────────

def gate_results(results: list[dict[str, Any]],
                 domain: str | None,
                 cache: Any | None = None) -> dict[str, Any]:
    """证据门控：标记哪些结果建议核验 + 是否高后果域。

    返回 gate 元数据（不修改 results 排序）：
      - fetch_required: bool（高后果域）
      - high_consequence_domain: str | None
      - suggested: 建议核验的 URL 列表
      - verified_count: 已有正文证据的结果数
      - pending_count: 建议核验但尚未核验的结果数
    每条结果原地标记 fetch_suggested（SERP/跳转链与已核验不标记）。
    """
    backfill_results(results, cache)
    verified_count = 0
    suggested: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        serp = bool(r.get("authority_tier") == "serp") or bool(
            r.get("evidence_flags", {}).get("is_serp"))
        if r.get("has_fetched_evidence"):
            verified_count += 1
            r["fetch_suggested"] = False
            continue
        is_serp_url = False
        if url:
            try:
                from serp_guard import is_serp_or_jump_url
                is_serp_url = is_serp_or_jump_url(url)
            except Exception:  # pragma: no cover
                is_serp_url = False
        if serp or is_serp_url or not url:
            r["fetch_suggested"] = False
            continue
        r["fetch_suggested"] = True
        # 源内全文直出优先：gutenberg / e-Gov 这类结果的 `url` 是下载门户或
        # JS 空壳页（实测去标签后取不到正文），而 `full_text_url` 是该源给出
        # 的、可确定性取到正文的端点（公版书纯文本 / 法令全文 XML）。取数建议
        # 据此走直连，省掉「门户页 → 找下载链」或浏览器渲染那几级。
        suggested.append(str(r.get("full_text_url") or url))

    hc = is_high_consequence_domain(domain)
    return {
        "fetch_required": hc,
        "high_consequence_domain": domain if hc else None,
        "suggested": suggested,
        "verified_count": verified_count,
        "pending_count": len(suggested),
    }


# ── E. 核验模式（显式，不阻塞热路径）────────────────────────────────────────────

def reorder_by_evidence(results: list[dict[str, Any]],
                        pre_scores: dict[str, float] | None = None,
                        weight: float = 0.35) -> dict[str, Any]:
    """把正文级证据分回写进排序（2026-09-27 新增，补上闭环缺口）。

    **为什么需要它**：`verify_results` 抓回正文、算了正文级 absorption，但旧
    实现只把分数写进 `post_fetch_absorption` 字段，**不重排**（调用点在
    `search_cli.py` 的排序之后）。于是「抓取链路已经识别出这是低质正文」这份
    情报到不了排序器——`--verify` 花了 RTT 却只改展示，不改结果顺序。实测
    `argo search "护眼台灯 推荐" --verify 3` 的 rerank_dims 里 authority 一字未变。

    **做法**：只对**已核验**的结果降权（不提升），中性点为 0.5：

        factor = 1 - weight × max(0, (0.5 - 正文质量)) / 0.5
        新分 = 原分 × factor

    三个设计取舍，都是为了避免「验证过的反而吃亏」这类采样偏差：

    ① **只降不升**。verify 只覆盖 top-k，若正文质量好的条目被加分，等于
       奖励「恰好被抓取」——而抓取与否与内容质量无关。降权没有这个问题：
       它表达的是「抓到的证据表明这条不实」，是真实信号。
    ② **0.5 为中性点**。absorption 的分布大致以 0.3-0.6 为主（见
       content_signals.compute_content_quality 的权重），若以「质量本身」
       直接做系数，绝大多数正常内容都会被无端降权（0.4 分的内容 ×0.4）。
       以 0.5 为界则只有明显低质者受罚。
    ③ **未核验条目不参与比较**。它们的分数保持原样，因此不会出现
       「没验证过的因为没被降权而反超」。

    pre_scores：可选的 {url: 原始 score} 快照，用于在返回里给出 delta 分布，
    便于观测「这次核验让谁动了、动了多少」。

    返回：{reordered: bool, adjusted: [...], moved: int}
    """
    if not results:
        return {"reordered": False, "adjusted": [], "moved": 0}
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        weight = 0.35
    weight = max(0.0, min(1.0, weight))

    adjusted: list[dict[str, Any]] = []
    old_order = [(r.get("url") or "") for r in results]

    for r in results:
        if not isinstance(r, dict):
            continue
        # 只处理**真的核验过**的条目：has_fetched_evidence 由 _record_verify 写入，
        # 是「本次或历史抓取确实拿到了正文」的标志。
        if not r.get("has_fetched_evidence"):
            continue
        q = r.get("post_fetch_absorption")
        if q is None:
            q = r.get("absorption")
        try:
            quality = float(q)
        except (TypeError, ValueError):
            continue
        quality = max(0.0, min(1.0, quality))

        try:
            base = float(r.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        # 只降不升：中性点 0.5，低于它才按偏离幅度打折
        shortfall = max(0.0, (0.5 - quality) / 0.5)
        factor = 1.0 - weight * shortfall
        new_score = base * factor
        if new_score == base:
            continue                    # 无变化：不记入 adjusted（避免噪声）
        r["score"] = round(new_score, 4)
        dims = r.get("rerank_dims")
        if isinstance(dims, dict):
            dims["post_fetch_quality"] = round(quality, 3)
        adjusted.append({
            "url": r.get("url") or "",
            "pre_score": round(base, 4),
            "post_score": round(new_score, 4),
            "quality": round(quality, 3),
        })

    if not adjusted:
        return {"reordered": False, "adjusted": [], "moved": 0}

    # 只在确有分数变化时重排；无变化时保持原序（逐位可对拍）
    changed = any(a["pre_score"] != a["post_score"] for a in adjusted)
    if changed:
        # 不加 abs()：分数是「越大越相关」，负数不该被顶到最前。原实现用
        # abs() 纯属冗余（score 恒非负），但它把「负分=最相关」这个反向
        # 语义留在了排序器里——一旦将来有算子产出负分（如「扣分制」改版），
        # 排序会静默倒置。降权只会让分数更小，用普通降序即可。
        results.sort(key=lambda r: r.get("score", 0) or 0, reverse=True)
    new_order = [(r.get("url") or "") for r in results]
    moved = sum(1 for i, u in enumerate(new_order)
                if i < len(old_order) and u != old_order[i])

    if pre_scores is not None:
        for a in adjusted:
            a["delta"] = round(a["post_score"] - a["pre_score"], 4)

    return {"reordered": changed, "adjusted": adjusted, "moved": moved}


def verify_results(results: list[dict[str, Any]],
                   query: str,
                   cache: Any | None = None,
                   fetch_fn: Callable[..., dict[str, Any]] | None = None,
                   top_k: int = 3,
                   max_chars: int = 8000,
                   timeout: float = 4.0) -> dict[str, Any]:
    """对 top_k 未核验结果显式 fetch，回填证据分并产出 evidence_revision 分布。

    不进热路径：调用方（CLI --verify / research --verify）显式触发。
    已核验（有证据缓存）的结果跳过，不重复打网。

    默认走核验快道：核验只需要「正文存在性 + 吸收分」，不需要全文渲染——
    关掉浏览器兜底（Wayback/CDP 最慢两级）并用 8s deadline 硬顶单 URL
    （此前吃 60s 全局默认，冷查询实测 8.7s 全在慢 URL 的降级链上）。
    拿不到正文照旧标 pending，核验语义不变；要完整降级链可显式传 fetch_fn。

    返回：
      - verified: [{url, title, pre_absorption, post_absorption, delta, content_ok, fetch_method}]
      - revision_summary: {n, improved, unchanged, degraded, mean_delta, median_delta}
      - pending: 仍然未核验的 URL（fetch 失败/SERP）
      - skipped_cached: 命中有证据缓存而跳过的 URL 数
    """
    if fetch_fn is None:
        try:
            from fetch_v3 import fetch_v3

            def _verify_fetch(url: str, max_chars: int, timeout: float) -> dict:
                return fetch_v3(url, max_chars=max_chars, timeout=timeout,
                                use_browser_fallback=False, deadline_s=8.0)
            fetch_fn = _verify_fetch
        except ImportError as e:  # pragma: no cover
            return {"error": f"fetch_v3 不可用: {e}", "verified": [],
                    "revision_summary": {}, "pending": [], "skipped_cached": 0}

    # 守卫：top_k 非正数（0 / 负数）按默认 3 处理，避免反向切片（results[:-1]）
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 3
    if top_k <= 0:
        top_k = 3

    verified: list[dict[str, Any]] = []
    pending: list[str] = []
    skipped_cached = 0
    revisions: list[float] = []


    def _record_verify(r: dict, ev: dict, verified: list, revisions: list) -> None:
        """并行/串行共用的核验回填逻辑。"""
        pre = float(r.get("post_fetch_absorption")
                    if r.get("post_fetch_absorption") is not None
                    else r.get("absorption") or 0.0)
        post = float(ev["absorption"] or 0.0)
        delta = round(post - pre, 3)
        revisions.append(delta)
        verified.append({
            "url": r.get("url") or "",
            "title": (r.get("title") or "")[:120],
            "pre_absorption": round(pre, 3),
            "post_absorption": round(post, 3),
            "delta": delta,
            "content_ok": ev.get("content_ok"),
            "fetch_method": ev.get("fetch_method"),
            "word_count": ev.get("word_count"),
        })
        r["has_fetched_evidence"] = True
        r["post_fetch_absorption"] = round(post, 3)
        r["fetch_suggested"] = False

    # 并行 fetch：URL 独立，串行最坏 top_k×timeout
    from concurrent.futures import ThreadPoolExecutor, as_completed
    targets = []
    for r in results[:top_k]:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        # 已核验：跳过，不重复 fetch
        if lookup_fetch_evidence(url, cache) is not None:
            skipped_cached += 1
            continue
        targets.append(r)

    def _fetch_one(r: dict) -> tuple[dict, dict | None, str]:
        url = r.get("url") or ""
        try:
            fr = fetch_fn(url, max_chars=max_chars, timeout=timeout)
        except Exception as e:  # pragma: no cover
            _log(f"verify fetch 异常 {url}: {type(e).__name__}")
            return r, None, ""
        # fetch_fn 可能返回非 dict（上游异常形态），取正文前必须判型，
        # 与 extract_fetch_evidence 的容错口径保持一致
        text = (fr.get("content") or "") if isinstance(fr, dict) else ""
        return r, extract_fetch_evidence(fr), text

    # 语义层（默认关）的输入：正文已在内存，批量一次调用；关闭时不产生任何行为
    scored_inputs: list[dict[str, Any]] = []

    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=min(len(targets), 3)) as ex:
            futures = [ex.submit(_fetch_one, r) for r in targets]
            for fut in as_completed(futures):
                r, ev, text = fut.result()
                if ev is None:
                    pending.append(r.get("url") or "")
                    continue
                # 不再在这里重复写证据：fetch_fn（fetch_v3）成功时已把证据分
                # 并入该 URL 的正文条目。此处再写一遍是同源同刻的双写，
                # 合并存储后还会因为「没有正文条目」而变成静默空操作。
                _record_verify(r, ev, verified, revisions)
                if text.strip():
                    scored_inputs.append({"url": r.get("url") or "",
                                          "title": r.get("title") or "",
                                          "text": text})
    else:
        for r in targets:
            r2, ev, text = _fetch_one(r)
            if ev is None:
                pending.append(r2.get("url") or "")
                continue
            _record_verify(r2, ev, verified, revisions)
            if text.strip():
                scored_inputs.append({"url": r2.get("url") or "",
                                      "title": r2.get("title") or "",
                                      "text": text})

    semantic_meta: dict[str, Any] | None = None
    if scored_inputs:
        try:
            from semantic_evidence import assess_support, enabled as _semantic_enabled
            if _semantic_enabled():
                support = assess_support(query, scored_inputs)
                if support:
                    for item in verified:
                        info = support.get(item.get("url") or "")
                        if info:
                            item["semantic_support"] = info
                    for r in results:
                        info = support.get(r.get("url") or "")
                        if info:
                            r["semantic_support"] = info
                    semantic_meta = {
                        "scored": len(support),
                        "supports": sum(1 for x in support.values() if x["supports"]),
                        "contradicts": sum(1 for x in support.values() if x["contradicts"]),
                    }
        except Exception as e:  # fail-open：语义层不阻断核验
            _log(f"语义证据层失败: {type(e).__name__}")

    summary: dict[str, Any] = {"n": len(revisions)}
    if revisions:
        summary.update({
            "improved": sum(1 for d in revisions if d > 0.02),
            "unchanged": sum(1 for d in revisions if abs(d) <= 0.02),
            "degraded": sum(1 for d in revisions if d < -0.02),
            "mean_delta": round(sum(revisions) / len(revisions), 3),
            "median_delta": round(sorted(revisions)[len(revisions) // 2], 3),
            "max_delta": round(max(revisions), 3),
            "min_delta": round(min(revisions), 3),
        })

    out: dict[str, Any] = {
        "query": query,
        "verified": verified,
        "revision_summary": summary,
        "pending": pending,
        "skipped_cached": skipped_cached,
    }
    # 语义层默认关：关闭时连键都不出现，输出与未接入时逐位一致
    if semantic_meta is not None:
        out["semantic"] = semantic_meta
    return out
