#!/usr/bin/env python3
"""extend_route_labels.py — 给路由语料加意图类型标签（外部反馈驱动扩展）。

## 为什么需要它

`build_route_labels.py` 从本机数据构建的语料只有 `expect_domain`（历史路由
输出）。但外部反馈（GitHub Issue #34.1）暴露的是一类**意图撞车**问题：
同一个词在不同意图下属于不同域（`PD-1` 在「市场规模」下是商业分析，在
「蛋白结构域」下是生信）。只看 domain 标签无法覆盖这类场景——需要一层
**意图类型**标签。

## MECE 拆解：意图类型六分类

按「用户想要什么」正交拆分，六类互不重叠、合起来穷尽：

| 类型 | 定义 | 典型域 | 撞车场景 |
|---|---|---|---|
| `factual` | 查事实/定义/数据 | cn_encyclopedia, stock_query, macro_data | 药靶点名 vs 商业名词 |
| `content` | 找内容/攻略/教程 | chinese_general, chinese_tech_deep, english_tech | 「原理」要讲解 vs 要论文 |
| `academic` | 找文献/论文/数据集 | academic, scholar_search, dataset_search | 「论文」要文献 vs 要讲解 |
| `sentiment` | 看舆情/评论/讨论 | social | 「攻略」要内容 vs 「舆情」要监听 |
| `entity` | 查机构/公司/人物 | org_entity, company_search | 公司名 vs 产品名 |
| `navigational` | 找入口/工具/站点 | dsh_plugin_search, skill_search | 找工具 vs 找资料 |

**穷尽性检查**：六类覆盖了「事实/内容/学术/舆情/实体/导航」六个正交维度。
不在这六类里的查询（如「天气」「股价」）归入 `factual`（它们是查事实）。

## 与外部反馈的对应

| 外部反馈 | 撞车场景 | 标签解法 |
|---|---|---|
| Issue #34.1 | PD-1 市场规模 vs 蛋白结构域 | `factual`（商业）vs `factual`（生信）——**同类不同域**，靠域标签区分 |
| v2.10.2 路由收口 | Transformer 原理 vs 论文 | `content` vs `academic`——**跨类**，靠意图标签区分 |
| v2.10.2 路由收口 | 小红书攻略 vs 舆情 | `content` vs `sentiment`——**跨类**，靠意图标签区分 |

## 用法

    python3 scripts/extend_route_labels.py            # 加标签并写回
    python3 scripts/extend_route_labels.py --dry-run  # 只统计不写
    python3 scripts/extend_route_labels.py --report   # 打印撞车场景覆盖
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

# ── 意图类型判定（规则优先，域兜底）──────────────────────────────────────────
# 判据是「查询里出现了什么意图词」，不是「路由到了哪个域」——后者是输出，
# 用它当标签等于把答案抄进题目。

_INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # academic：明确的文献意图词
    ("academic", re.compile(
        r"(论文|文献|综述|survey|review|arxiv|期刊|journal|doi|引用|reference|"
        r"scholar|学术|科研|研究|实验|数据集|dataset|benchmark|评测)", re.I)),
    # sentiment：舆情/讨论意图词
    ("sentiment", re.compile(
        r"(舆情|评论|讨论|网友|吐槽|口碑|评价|反馈|热议|争议|态度|"
        r"sentiment|opinion|review|discussion|complaint)", re.I)),
    # content：内容/教程意图词
    ("content", re.compile(
        r"(教程|攻略|指南|详解|原理|是什么|怎么用|如何|最佳实践|best practice|"
        r"tutorial|guide|how to|example|案例|方案|步骤|方法)", re.I)),
    # entity：机构/公司/人物意图词
    ("entity", re.compile(
        r"(公司|企业|集团|机构|组织|官网|总部|创始人|CEO|董事长|"
        r"company|inc\.|corp\.|ltd\.|gmbh|organization)", re.I)),
    # navigational：找入口/工具意图词
    ("navigational", re.compile(
        r"(插件|技能|工具|安装|下载|marketplace|plugin|skill|tool|install|"
        r"下载|仓库|repo|github|npm|pypi)", re.I)),
]


def classify_intent(query: str, domain: str | None = None) -> str:
    """判定查询的意图类型。规则优先，域兜底。

    域兜底只用于规则没命中时——它是**弱信号**（域是路由输出），但比
    无标签好。兜底映射按域的语义归属，不是按域名字面。
    """
    for name, pat in _INTENT_PATTERNS:
        if pat.search(query):
            return name
    # 域兜底
    _DOMAIN_INTENT = {
        "academic": "academic", "scholar_search": "academic",
        "dataset_search": "academic",
        "social": "sentiment",
        "org_entity": "entity", "company_search": "entity",
        "dsh_plugin_search": "navigational", "skill_search": "navigational",
        "chinese_tech_deep": "content", "english_tech": "content",
        "tech_deep": "content", "code_search": "content",
        "cn_encyclopedia": "factual", "dictionary_search": "factual",
        "stock_query": "factual", "fund_query": "factual",
        "us_stock": "factual", "macro_data": "factual",
        "financial_news": "factual", "weather_query": "factual",
    }
    if domain and domain in _DOMAIN_INTENT:
        return _DOMAIN_INTENT[domain]
    return "factual"  # 默认：查事实


# ── 外部反馈的撞车场景（回归锚）────────────────────────────────────────────

# 来自 GitHub Issue #34.1 与 v2.10.2 路由准确率收口的实测场景。
# 每条：(查询, 期望域, 期望意图类型, 反馈来源)
COLLISION_ANCHORS: list[tuple[str, str, str, str]] = [
    # Issue #34.1：药靶点撞基因名
    ("PD-1 抑制剂 2026 市场规模", "chinese_general", "factual", "Issue#34.1"),
    ("重组胶原蛋白 医美 市场规模", "chinese_general", "factual", "Issue#34.1"),
    ("PD-1 蛋白 结构域 序列", "protein_search", "factual", "Issue#34.1"),
    # v2.10.2：原理 vs 论文
    ("Transformer 模型 原理", "chinese_tech_deep", "content", "v2.10.2"),
    ("Transformer 原理 论文", "academic", "academic", "v2.10.2"),
    ("深度学习 原理", "chinese_tech_deep", "content", "v2.10.2"),
    ("Attention 机制 详解", "chinese_general", "content", "v2.10.2"),
    # v2.10.2：攻略 vs 舆情
    ("小红书 露营 攻略", "chinese_general", "content", "v2.10.2"),
    ("小红书 露营 舆情", "social", "sentiment", "v2.10.2"),
    ("小红书 露营 评论", "social", "sentiment", "v2.10.2"),
    # v2.10.2：技能目录
    ("小红书 技能 排行榜", "redskill_search", "navigational", "v2.10.2"),
    # v2.10.2：行情 vs 财报
    ("贵州茅台股价", "stock_query", "factual", "v2.10.2"),
    ("宁德时代 股价", "stock_query", "factual", "v2.10.2"),
    # v2.10.2：学术检索正例
    ("科研数据集 开放数据 下载", "dataset_search", "academic", "v2.10.2"),
    ("google scholar 学术论文 期刊", "scholar_search", "academic", "v2.10.2"),
]


def extend(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """给每条语料加 `intent` 字段，并追加撞车锚。"""
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        q = e["query"]
        e2 = dict(e)
        e2["intent"] = classify_intent(q, e.get("expect_domain"))
        out[q] = e2

    # 追加撞车锚（不在语料里的才加，避免重复）
    added = 0
    for q, dom, intent, src in COLLISION_ANCHORS:
        if q in out:
            # 已存在：核对意图标签是否一致
            if out[q].get("intent") != intent:
                out[q]["intent"] = intent
                out[q]["intent_source"] = src
            continue
        out[q] = {
            "query": q,
            "expect_domain": dom,
            "expect_family": None,
            "evidence": "feedback",
            "quality": {"errors": False, "recovered": False,
                        "count": 0, "occurrences": 1},
            "source": src,
            "intent": intent,
            "intent_source": src,
        }
        added += 1

    result = [out[k] for k in sorted(out)]
    stats = {
        "total": len(result),
        "added_anchors": added,
        "by_intent": dict(Counter(e.get("intent") for e in result)),
        "by_evidence": dict(Counter(e.get("evidence") for e in result)),
    }
    return result, stats


def _report(entries: list[dict[str, Any]]) -> None:
    """打印撞车场景覆盖情况。"""
    from route import route_query
    from config import get_domains

    live = {d.get("name") for d in get_domains()}
    anchors = [e for e in entries if e.get("evidence") == "feedback"]
    print(f"[extend_route_labels] 撞车锚 {len(anchors)} 条，逐条核验：")
    ok = 0
    for e in anchors:
        try:
            r = route_query(e["query"], mode="auto", depth="fast")
        except Exception:
            continue
        got = r.get("domain")
        want = e["expect_domain"]
        hit = got == want and want in live
        ok += hit
        print(f"  {'OK ' if hit else 'BAD'} {e['query'][:30]!r:34} "
              f"want={want:20} got={got:20} intent={e.get('intent')}")
    print(f"[extend_route_labels] 撞车锚命中 {ok}/{len(anchors)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="给路由语料加意图类型标签")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true", help="打印撞车锚覆盖")
    args = ap.parse_args()

    if not LABELS.is_file():
        print(f"[extend_route_labels] 语料不存在：{LABELS}", file=sys.stderr)
        return 1
    entries = [json.loads(l) for l in
               LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]

    extended, stats = extend(entries)
    print(f"[extend_route_labels] 语料 {len(entries)} -> {stats['total']} 条"
          f"（新增撞车锚 {stats['added_anchors']}）")
    print(f"[extend_route_labels] 意图分布: {stats['by_intent']}")
    print(f"[extend_route_labels] 证据分档: {stats['by_evidence']}")

    if args.report:
        _report(extended)
    if args.dry_run:
        return 0

    with LABELS.open("w", encoding="utf-8") as f:
        for e in extended:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[extend_route_labels] 已写回 {LABELS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
