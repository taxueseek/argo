#!/usr/bin/env python3
"""extend_route_labels_by_capability.py — 按域/源/子技能/插件能力扩展标注集。

## 为什么需要它

`build_route_labels.py` 从本机真实查询构建的语料只覆盖 **41/99 个域**——
另外 58 个域从未被真实查询命中过（它们是长尾垂直域，真实查询里出现频率低，
但能力是真实存在的）。`extend_route_labels.py` 加的意图标签只解决了「撞车」
问题，没解决「覆盖」问题。

本脚本按**能力**补全：为每个未覆盖域生成标注用例，判据是「这个域声明的
触发词能不能被路由命中」。

## MECE 拆解：按能力族分组生成

58 个未覆盖域按引擎能力族分组（见脚本内 `_CAPABILITY_GROUPS`），每组一个
生成器。分组依据是 `engine_families.family_of(combo 引擎)`，不是域名字面——
同一个域可能跨多个族（如 `earth_science` 跨 media_book/science_geo/web_general）。

**不重不漏**：每个未覆盖域只进一个组（按主族），每组生成 2-4 条用例。

## 第一性原理：判据是「能力可达」

用例的期望域 = 该域声明的触发词所在域。生成后**实测路由**，命中的才写入
语料（`evidence=capability`），失配的记录到报告里供人工判断——不强行把
失配当标注，因为那会引入假标注。

## 与既有扩展的关系

| 脚本 | 解决什么 | 判据 |
|---|---|---|
| `build_route_labels.py` | 真实查询覆盖 | 本机 usage_log |
| `extend_route_labels.py` | 意图撞车 | 外部反馈 + 意图词 |
| 本脚本 | 能力覆盖 | 域声明的触发词 |

三者互补，互不重叠。

## 用法

    python3 scripts/extend_route_labels_by_capability.py            # 扩展并写回
    python3 scripts/extend_route_labels_by_capability.py --dry-run  # 只统计
    python3 scripts/extend_route_labels_by_capability.py --report   # 打印命中/失配
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

LABELS = ROOT / "tests" / "golden" / "route_labeled.jsonl"

# ── 能力族分组（MECE）────────────────────────────────────────────────────────
# 每组：(组名, [域...], [(查询, 说明), ...])
# 查询构造自各域的触发词，但做了**自然语言化**——不是把正则原样塞进去，
# 而是用该域真实会被问到的形式（判据：这条查询是否真的需要这个域的能力）。

_CAPABILITY_GROUPS: list[tuple[str, list[str], list[tuple[str, str]]]] = [
    # ── 法律/合规（6 域）──
    ("legal", ["us_legal", "japan_law", "kor_law", "legal", "law_text",
               "standards", "wenshu_query"],
     [
         # us_legal 的 patterns 是英文（supreme court / court of appeals），
         # 中文「最高法院」不该命中它——用英文判例词。
         ("supreme court ruling 2026", "us_legal"),
         ("刑法 第二百三十四条 故意伤害罪", "legal"),
         ("法律条文 全文 民法典 一千二百六十条", "law_text"),
         ("国家标准 GB/T 2023 全文", "standards"),
         ("裁判文书 判决书 2024 民事", "wenshu_query"),
         ("日本 民法 条文 日语", "japan_law"),
         ("대한민국 형법 조문", "kor_law"),
     ]),
    # ── 金融垂直（5 域）──
    ("finance", ["sec_filings", "hk_filings", "fx_query", "futures_market",
                 "trade_stats", "crypto_search"],
     [
         ("AAPL 10-K annual report SEC", "sec_filings"),
         # hk_filings 与 sec_filings 是同类域且只差 1 位（13 vs 12），
         # 「招股书」被 sec_filings 的宽 pattern 抢走主位；这里只校验
         # **目标域在命中列表里**（见 build() 的判据），不要求主位。
         ("港股 港交所 披露易 招股书", "hk_filings"),
         ("人民币 美元 中间价 汇率 今日", "fx_query"),
         ("沪铜 期货 今日 行情", "futures_market"),
         ("中国 2024 进出口 贸易额 数据", "trade_stats"),
         ("比特币 以太坊 币价 今日", "crypto_search"),
     ]),
    # ── 代码/依赖（5 域）──
    ("code", ["package_search", "package_intel", "lifecycle_search",
              "web_docs", "stackoverflow_search", "local_code"],
     [
         # package_search 声明了 intent_required（install/add/download 等），
         # 缺意图词的短查询会被 _intent_gate 判为「实体词只是上下文」而让位
         # （实测「crates.io serde 包 版本」落 chinese_general）。用它的
         # 真实意图词才校验到该域能力。
         ("crates.io serde 包 安装", "package_search"),
         ("npm express 最新版本 弃用", "package_intel"),
         ("python 3.9 生命周期 停止支持 时间", "lifecycle_search"),
         ("MDN Web API fetch 文档", "web_docs"),
         ("stackoverflow python async 问题", "stackoverflow_search"),
         ("github repo 本地搜索 代码", "local_code"),
     ]),
    # ── 社区/讨论（4 域）──
    ("community", ["hackernews_search", "v2ex_search", "cn_tech_community",
                   "shopping"],
     [
         ("Hacker News 今日 热门", "hackernews_search"),
         ("v2ex 程序员 讨论", "v2ex_search"),
         ("掘金 前端 技术博客", "cn_tech_community"),
         ("踩坑 避雷 好物 推荐", "shopping"),
     ]),
    # ── 影视/文化（3 域）──
    ("media", ["film_search", "art_museum", "anime_encyclopedia"],
     [
         ("电影 票房 2024 排行", "film_search"),
         ("博物馆 藏品 名画 画家", "art_museum"),
         ("萌娘百科 动漫 新番 声优", "anime_encyclopedia"),
     ]),
    # ── 游戏/预测/标准（3 域）──
    ("vertical_misc", ["game_search", "prediction_market", "rfc_search"],
     [
         ("steam 游戏 3A 推荐", "game_search"),
         ("polymarket 预测市场 事件合约", "prediction_market"),
         ("RFC 791 internet standard", "rfc_search"),
     ]),
    # ── 知识/档案（2 域）──
    ("knowledge", ["entity_search", "web_archive"],
     [
         ("wikidata 知识实体 QID 查询", "entity_search"),
         ("internet archive wayback 网页快照", "web_archive"),
     ]),
    # ── 地球/空间/能源（5 域）──
    ("earth_space", ["astro_space", "earth_science", "soil_agri",
                     "energy_grid", "transport_rt"],
     [
         ("天文 小行星 彗星 观测", "astro_space"),
         ("地震 震级 余震 震中", "earth_science"),
         ("土壤 类型 属性 酸碱度", "soil_agri"),
         ("电网 发电量 发电结构", "energy_grid"),
         ("纽约 共享单车 citi bike 实时", "transport_rt"),
     ]),
    # ── 科学/健康（3 域）──
    ("science", ["species_search", "chem_search", "outbreak_health"],
     [
         ("物种 学名 生物分类 科属种", "species_search"),
         ("化学式 分子量 CAS号 化合物", "chem_search"),
         ("疫情 流行病 传染病 最新", "outbreak_health"),
     ]),
    # ── 专利（1 域）──
    ("patent", ["patent_search"],
     [
         ("专利 权利要求 IPC分类 检索", "patent_search"),
     ]),
    # ── 新闻/快讯（6 域）──
    ("news", ["global_event", "world_news", "intl_news_flash",
              "em_news_search", "cls_telegraph_search", "jin10_flash"],
     [
         ("全球事件 地缘政治 局势 最新", "global_event"),
         ("세계 뉴스 today", "world_news"),
         ("国际新闻 今日 要闻", "intl_news_flash"),
         ("东财资讯 7x24 财经快讯", "em_news_search"),
         ("财联社电报 最新", "cls_telegraph_search"),
         ("金十 财经快讯 7x24", "jin10_flash"),
     ]),
    # ── 热点/热榜（3 域）──
    ("hot", ["hot_trending", "ths_hot_search", "zhihu_hot_list"],
     [
         ("微博热搜 今日 榜单", "hot_trending"),
         ("同花顺 热点板块 涨停原因", "ths_hot_search"),
         ("知乎热榜 今日 热点", "zhihu_hot_list"),
     ]),
    # ── 知乎个人数据（1 域）──
    ("zhihu", ["zhihu_user_data"],
     [
         ("我的知乎 创作 数据 统计", "zhihu_user_data"),
     ]),
    # ── 事实核查（1 域）──
    ("factcheck", ["claim_check"],
     [
         ("辟谣 谣言 不实信息 是真的吗", "claim_check"),
     ]),
    # ── 航空天气（1 域）──
    ("aviation", ["aviation_weather"],
     [
         ("METAR aviation weather 机场", "aviation_weather"),
     ]),
    # ── 本地搜索（6 域，2026-11-04 修复后全部可命中）──
    ("local", ["local_chinese", "local_news", "local_general",
               "local_academic", "local_reference", "local_code"],
     [
         ("百度 site:baidu.com 搜索", "local_chinese"),
         ("本地新闻 bing news", "local_news"),
         ("本地搜索 零成本 free search", "local_general"),
         ("arxiv api semantic scholar 本地", "local_academic"),
         ("wikipedia api 本地 查询", "local_reference"),
         ("github repo 本地搜索 代码", "local_code"),
     ]),
    # ── 车辆数据（1 域）──
    ("vehicle", ["vehicle_data"],
     [
         ("车型 厂商 VIN 查询", "vehicle_data"),
     ]),
]


def _extract_trigger_words(domain_spec: dict[str, Any]) -> list[str]:
    """从域声明里抽触发词（用于校验生成的查询是否真的覆盖该域）。"""
    pats = domain_spec.get("patterns", [])
    if not isinstance(pats, list):
        return []
    out = []
    for p in pats[:3]:
        if isinstance(p, dict):
            p = p.get("match")
        if not isinstance(p, str):
            continue
        # 抽第一个字面 alternation
        m = re.search(r"\(([^()]+)\)", p)
        if m:
            for w in m.group(1).split("|"):
                w = w.strip()
                if w and len(w) >= 2 and not w.startswith("(?"):
                    out.append(w)
    return out


def build(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """为未覆盖域生成用例，实测路由后写入（命中的才写）。"""
    from config import get_domains
    from route import route_query

    doms = {d["name"]: d for d in get_domains()}
    existing = {e["query"] for e in entries}
    covered = {e.get("expect_domain") for e in entries}

    out = list(entries)
    added = 0
    hit_count = 0
    miss_count = 0
    misses: list[str] = []
    covered_now: set[str] = set()

    for group, domains, cases in _CAPABILITY_GROUPS:
        for query, want_domain in cases:
            if query in existing:
                continue
            if want_domain not in doms:
                continue  # 域不存在，跳过
            try:
                r = route_query(query, mode="auto", depth="fast")
            except Exception:
                continue
            got = r.get("domain")
            # 判据是「目标域在命中列表里」，不是「主域 == 目标域」——
            # 同类域（hk_filings/sec_filings、trade_stats/macro_data、
            # em_news_search/cls_telegraph_search）只差 1-10 位，主位被更
            # 靠前的同类域抢走是既有的域顺序设计，不代表该域能力不可达。
            # 主域抢占问题由 test_route_capability_coverage 的 _KNOWN_GAPS
            # 与后续域顺序优化处理，不在能力覆盖的判据里。
            from route_domains import match_domains as _md
            hit_names = {h.get("name") for h in _md(query, get_domains(), max_n=5)}
            if want_domain in hit_names:
                hit_count += 1
                covered_now.add(want_domain)
                out.append({
                    "query": query,
                    "expect_domain": want_domain,
                    "expect_family": None,
                    "evidence": "capability",
                    "quality": {"errors": False, "recovered": False,
                                "count": 0, "occurrences": 1},
                    "source": f"capability:{group}",
                    "intent": None,  # 由 extend_route_labels 补
                })
                added += 1
            else:
                miss_count += 1
                if len(misses) < 30:
                    misses.append(
                        f"  {query[:34]!r:38} want={want_domain:22} got={got}")

    # 补 intent 标签
    try:
        from extend_route_labels import classify_intent
        for e in out:
            if not e.get("intent"):
                e["intent"] = classify_intent(e["query"], e.get("expect_domain"))
    except Exception:
        pass

    stats = {
        "total": len(out),
        "added": added,
        "hit": hit_count,
        "miss": miss_count,
        "newly_covered_domains": len(covered_now),
        "covered_domains_total": len(covered | covered_now),
        "misses": misses,
    }
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="按能力扩展路由标注集")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if not LABELS.is_file():
        print(f"[capability-extend] 语料不存在：{LABELS}", file=sys.stderr)
        return 1
    entries = [json.loads(l) for l in
               LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]

    extended, stats = build(entries)
    print(f"[capability-extend] 语料 {len(entries)} -> {stats['total']} 条"
          f"（新增 {stats['added']}，命中 {stats['hit']}，失配 {stats['miss']}）")
    print(f"[capability-extend] 新覆盖域 {stats['newly_covered_domains']} 个，"
          f"累计覆盖 {stats['covered_domains_total']}/99")

    if args.report and stats["misses"]:
        print("失配样例（生成的查询没路由到目标域，供人工判断是查询不自然还是域规则问题）：")
        for m in stats["misses"]:
            print(m)

    if args.dry_run:
        return 0

    with LABELS.open("w", encoding="utf-8") as f:
        for e in extended:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[capability-extend] 已写回 {LABELS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
