#!/usr/bin/env python3
"""extend_route_labels_full_coverage.py — 把域覆盖补到 99/99（全构造、全精确命中）。

## 为什么需要它

2026-11-05 语料改为全构造用例后（`build_route_labels.py` 的隐私边界），域覆盖从
99/99 掉到 58/99：差的 41 个域此前靠真实查询覆盖，构造用例里没有对应条目，
覆盖门下限被迫从 0.85 降到 0.55，未覆盖域逐个登记在
`test_route_capability_coverage._KNOWN_GAPS` 里「由快照门/人工集覆盖」。

那份缺口登记里有两条假设是**错的**，本脚本逐条证伪：

1. 「平台专属语法无法构造自然用例」——`wechat_search` / `zhihu_content` /
   `skill_search` / `dsh_plugin_search` 的触发词是**普通中文说法**（「公众号
   文章」「知乎 如何评价」「agent skills 技能市场」），不是平台操作符；
2. 「语义过专的域无法构造」——`modal_card` 的「今日油价 / 火车票 车次 /
   手机参数 对比」是日常问法，`medical` 的「临床试验 适应症 不良反应」是
   标准医学问法。

真正需要区分的不是「能不能构造」，而是**构造出的查询路由到哪个域**：
`test_route_labeled_regression` 判的是 `route_query(q).domain == expect_domain`
（精确相等），而旧能力扩展脚本的判据是「目标域出现在 `match_domains` 前 5 名」
——两者差出 4 条既有失配（同族域被兄弟域抢主位）。

## 第一性原理：一条用例 = 一句真会有人这么问的话

判据三选一，全部满足才入库：

1. **自然性**：这句话是真实用户会打的（不是把正则原样塞进去）；
2. **能力必要性**：这句话确实需要该域的能力才能答好（不是「刚好命中」）；
3. **精确命中**：`route_query(q).domain == 该域`（回归门的判据，不是前 5 名）。

第 3 条把「标注」和「实测」分开：标注来自 1+2 的人工判断，实测只用来**剔除**
达不到 3 的候选。达不到的候选不写进语料（宁缺勿滥），由
`--report` 打印出来供人工判断「是查询不自然，还是域规则坏了」。

## 与既有扩展的分工

| 脚本 | 语料来源 | 判据 |
|---|---|---|
| `build_route_labels.py` | 空基底（隐私边界） | —— |
| `extend_route_labels_by_capability.py` | 域触发词自然化 | 目标域在前 5 名 |
| `extend_route_labels_by_language.py` | 各语言特征词 | 期望域精确命中 |
| `extend_route_labels.py` | 外部反馈撞车 | 加意图标签 |
| **本脚本** | 未覆盖域的人工自然问法 | **期望域精确命中** |

## 用法

    python3 scripts/extend_route_labels_full_coverage.py            # 扩展并写回
    python3 scripts/extend_route_labels_full_coverage.py --dry-run  # 只统计
    python3 scripts/extend_route_labels_full_coverage.py --report   # 打印失配候选
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

LABELS = ROOT / "tests" / "golden" / "route_labeled.jsonl"

# ── 未覆盖域的人工问法（MECE：每个域只在这一处出现）──────────────────────────
# 顺序按域名字母序，便于与 config 对账。notes 写「为什么这句话需要该域」，
# 不写「它命中了什么正则」——后者改规则就过期，前者不会。
_CASES: list[tuple[str, str, str]] = [
    ("ai_model", "GPT-4o 上下文窗口 多少",
     "问模型的上下文窗口规格，只有 models.dev 这类模型目录答得了"),
    ("ai_model", "大模型 API 定价 每百万 token 多少钱",
     "问 API 计价口径，需要模型目录的价格字段"),
    ("book_search", "刘慈欣 三体 小说 书评",
     "找书与书评，需要图书源（微信读书/豆瓣读书）"),
    ("book_search", "有什么好书推荐 科幻小说 书单",
     "要书单，百科与新闻都答不了"),
    ("cn_encyclopedia", "量子纠缠 百度百科 词条",
     "点名要百科词条，需要百科源"),
    ("cn_encyclopedia", "什么是碳中和 词条解释",
     "概念定义，走百科点查"),
    ("cls_telegraph_search", "财联社 电报 快讯 央行",
     "点名财联社电报，只有该源有电报原文"),
    ("code_search", "python 快速排序 实现 源码",
     "找实现源码，需要代码托管源"),
    ("code_search", "github 上有没有类似的 开源项目",
     "找同类开源项目，需要 GitHub 检索"),
    ("company_search", "某公司 统一社会信用代码 工商信息",
     "工商登记信息，需要法人主数据/注册库"),
    ("company_search", "查一下这家公司的 法人 注册资本",
     "查法人/注册资本，百科答不了"),
    ("dictionary_search", "ephemeral 这个英文单词什么意思",
     "查英文单词释义，需要词典源"),
    ("dictionary_search", "词典 查询 成语 意思",
     "中文词典查询"),
    ("dsh_plugin_search", "dsh 插件 推荐 有哪些",
     "找 DSH 插件，需要插件目录源"),
    ("dsh_plugin_search", "DSH 插件市场 搜索",
     "点名插件市场"),
    ("em_news_search", "东财资讯 7x24 全球资讯",
     "点名东财资讯，只有该源有 7x24 快讯"),
    ("english_tech", "Rust async performance best practices",
     "英文技术实践，octen/anysearch 主搜"),
    ("english_tech", "Kubernetes tutorial how to",
     "英文教程检索"),
    ("fact_check", "爱因斯坦 是谁",
     "人物事实点查，走 wikipedia/byted"),
    ("fact_check", "光速 是多少",
     "数值事实点查，走 wolframalpha/wikipedia"),
    ("financial_news", "上市公司 财报 研报 公告 汇总",
     "财报/研报/公告三件套，需要财经资讯源"),
    ("financial_news", "央行 降准 政策 解读",
     "货币政策解读，需要财经资讯与研报"),
    ("fund_query", "沪深300 ETF 基金净值 排行",
     "基金净值排行，需要基金源"),
    ("fund_query", "主动权益 基金经理 业绩 排名",
     "基金经理业绩，只有基金源有"),
    ("geo_places", "长江 流经 哪些 省份",
     "地理水系问题，需要 OSM/百科地物"),
    ("geo_places", "上海 位于 哪个 省 地理 位置",
     "行政区位置，百科地理域"),
    ("hk_filings", "香港上市 招股说明书 披露易",
     "港股披露易招股书，只有港交所源有"),
    ("hk_filings", "港股 公告 披露",
     "港股公告披露"),
    ("image_search", "免费 无版权 图片 素材 下载",
     "找可商用图片素材，需要图库源"),
    ("image_search", "找一张 星空 壁纸",
     "壁纸检索"),
    ("law_text", "民法典 一千二百六十条 条文 全文",
     "要法条全文原文，需要法规全文源"),
    ("law_text", "个人信息保护法 全文 条文",
     "要法律全文"),
    ("legal", "故意伤害罪 量刑 标准 刑法",
     "罪名与量刑标准，需要法律专业源"),
    ("legal", "正当防卫 认定 标准 判例",
     "认定标准与判例"),
    ("macro_data", "美国 CPI 通胀 最新 数据",
     "美国宏观时序数据，需要 FRED 类源"),
    ("macro_data", "中国 2024 GDP 增速 统计",
     "中国宏观统计口径"),
    ("medical", "临床试验 适应症 不良反应 记录",
     "临床试验记录，需要 clinicaltrials/OpenFDA"),
    ("medical", "阿司匹林 用法用量 说明书 不良反应",
     "药品说明书与不良反应"),
    ("media_search", "周杰伦 新歌 专辑 歌词",
     "音乐媒体检索，需要曲库源"),
    ("media_search", "有什么好听的 播客 推荐",
     "播客推荐"),
    ("meme_slang", "yyds 是什么梗",
     "网络梗释义，需要梗百科/俚语源"),
    ("meme_slang", "网络流行语 抽象话 什么意思",
     "流行语释义"),
    ("ml_models", "huggingface 预训练模型 权重 下载",
     "模型权重检索，需要 HuggingFace"),
    ("ml_models", "开源 embedding 模型 检索",
     "找开源模型"),
    ("modal_card", "今日油价 92号汽油 多少钱一升",
     "油价模态卡（结构化实时值）"),
    ("modal_card", "火车票 余票 查询 车次",
     "车次余票模态卡"),
    ("modal_card", "手机参数 配置 对比 报价",
     "手机规格模态卡"),
    ("news_realtime", "今天 最新 突发 新闻",
     "实时突发新闻，需要实时新闻源"),
    ("news_realtime", "breaking news latest developments",
     "英文实时新闻"),
    ("org_entity", "外交部 发言人 表态",
     "政府机构实体，需要权威实体源"),
    ("org_entity", "联合国 安理会 决议",
     "国际组织实体"),
    ("security_search", "CVE-2024-3094 漏洞 细节",
     "CVE 漏洞详情，需要 NVD"),
    ("security_search", "这个 npm 包 有没有 已知漏洞",
     "开源依赖漏洞，需要 OSV"),
    ("semantic_discovery", "find me the best emerging AI agent tools 2026",
     "开放式发现类查询，语义引擎（Exa）主场"),
    ("semantic_discovery", "有哪些值得关注的 开源 数据库 项目",
     "行业盘点类发现"),
    ("skill_search", "agent skills 技能市场 搜索",
     "跨生态技能目录，需要 SkillsMP/ClawHub"),
    ("skill_search", "claude skill 目录 找技能",
     "点名技能目录"),
    ("sports_search", "NBA 季后赛 总决赛 比分",
     "赛事比分，需要体育源"),
    ("sports_search", "英超 转会 球员 新闻",
     "转会与球员动态"),
    ("tech_deep", "联邦学习 SOTA 研究进展",
     "技术研究进展（非单篇论文），走 openalex/arxiv 聚合"),
    ("tech_deep", "deep dive research on vector database internals",
     "英文深度技术调研"),
    ("trade_stats", "海关总署 进出口 数据 统计",
     "海关进出口统计，需要贸易统计源"),
    ("us_fundamentals", "AAPL annual revenue net income financials",
     "美股结构化财务（SEC XBRL），英文词形"),
    ("us_stock", "NASDAQ 美股 盘前 行情",
     "美股盘前行情，需要美股源"),
    ("us_stock", "NVDA 盘后 美股 行情",
     "美股盘后行情"),
    ("video", "B站 视频 合集",
     "视频内容检索，需要 B 站源"),
    ("video", "视频 攻略 bilibili",
     "视频攻略"),
    ("wechat_search", "公众号 文章 搜索 深度学习 部署",
     "公众号文章检索，需要微信搜索源"),
    ("wechat_search", "微信公众号 关于 RAG 的文章",
     "点名公众号"),
    ("zhihu_content", "知乎 如何评价 这件事",
     "知乎观点/评价，需要知乎源"),
    ("zhihu_content", "知友 怎么看待 远程办公",
     "知乎答主观点"),
]


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def build(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """为未覆盖域补用例：候选须**精确命中**目标域，否则不入库。"""
    from config import get_domains
    from route import route_query

    doms = {d["name"] for d in get_domains()}
    existing = {e["query"] for e in entries}
    covered = {e.get("expect_domain") for e in entries}

    out = list(entries)
    added = 0
    hit = 0
    misses: list[str] = []
    covered_now: set[str] = set()

    for domain, query, note in _CASES:
        if query in existing:
            continue
        if domain not in doms:
            misses.append(f"  域不存在: {domain}（{query}）")
            continue
        if domain in covered or domain in covered_now:
            continue  # 该域已有用例，不重复补
        try:
            r = route_query(query, mode="auto", depth="fast")
        except Exception as exc:  # 路由异常不写入语料
            misses.append(f"  路由异常 {query!r}: {exc}")
            continue
        got = r.get("domain")
        if got != domain:
            misses.append(f"  {query[:36]!r:40} want={domain:22} got={got}")
            continue
        hit += 1
        covered_now.add(domain)
        out.append({
            "query": query,
            "expect_domain": domain,
            "expect_family": None,
            "evidence": "capability",
            "quality": {"errors": False, "recovered": False,
                        "count": 0, "occurrences": 1},
            "source": "coverage:full",
            "intent": None,  # 由 extend_route_labels 补
            "note": note,
        })
        added += 1

    # 补 intent 标签（与既有扩展同一口径）
    try:
        from extend_route_labels import classify_intent
        for e in out:
            if not e.get("intent"):
                e["intent"] = classify_intent(e["query"], e.get("expect_domain"))
    except Exception:
        pass

    total_covered = sorted((covered | covered_now) & doms)
    stats = {
        "total": len(out),
        "added": added,
        "hit": hit,
        "miss": len(misses),
        "covered_domains_total": len(total_covered),
        "all_domains": len(doms),
        "uncovered": sorted(doms - set(total_covered)),
        "misses": misses,
    }
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="把路由标注集域覆盖补到 99/99")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if not LABELS.is_file():
        print(f"[coverage-extend] 语料不存在：{LABELS}", file=sys.stderr)
        return 1

    entries = _load(LABELS)
    extended, stats = build(entries)
    print(f"[coverage-extend] 语料 {len(entries)} -> {stats['total']} 条"
          f"（新增 {stats['added']}，精确命中 {stats['hit']}，失配候选 {stats['miss']}）")
    print(f"[coverage-extend] 域覆盖 {stats['covered_domains_total']}/"
          f"{stats['all_domains']}")
    if stats["uncovered"]:
        print(f"[coverage-extend] 仍未覆盖：{stats['uncovered']}")
    if args.report and stats["misses"]:
        print("失配候选（人工判断：查询不自然 → 换问法；规则坏了 → 改域规则）：")
        for m in stats["misses"]:
            print(m)

    if args.dry_run:
        return 0

    with LABELS.open("w", encoding="utf-8") as f:
        for e in extended:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[coverage-extend] 已写回 {LABELS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
