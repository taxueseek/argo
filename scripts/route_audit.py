#!/usr/bin/env python3
"""route_audit.py — 路由误判度量（方案三：先量化，再决定修机制还是修个案）

## 为什么需要它

`SUBSTRING_ACCIDENTS`（26 条）+ `LEGITIMATE_ROUTES`（41 条）是**手工**语料，
误判靠人一个个撞见——2026-11-15 的 4 例（X/小红书/reddit 语境被 academic 与
tech_deep 劫持）就是这么发现的。本脚本补上「度量」这一格：

  1. 真实查询面：从 usage_log 取全部去重查询，跑当前路由，得到「域 × 查询」
     的实际分布，暴露「某个域吃掉了大量不该它的查询」这类结构性偏移。
  2. 语境交叉面：对每个域构造**触发词 × 语境词**的正反对照（语境词取自
     `_CONTEXT_WORDS`：平台/产品/内容/论文/商业…），判定「命中域是否与该
     语境语义一致」，输出可疑清单。
  3. 基线数：`--json` 落盘为可复跑的基线（方案二改打分后重跑对比）。

## 判据（保守，宁可少报）

可疑 = 命中域 != 期望域，且**查询里出现了另一域的强语境词**。不做「域对不对」
的主观判断——只报「语境词与命中域语义冲突」这一可机械验证的形态，人工复核后
才进 SUBSTRING_ACCIDENTS。

用法：
  python3 scripts/route_audit.py                 # 打印摘要
  python3 scripts/route_audit.py --all           # 打印全部可疑
  python3 scripts/route_audit.py --json BASELINE # 落盘基线
  python3 scripts/route_audit.py --compare BASE  # 与基线对比（方案二验收）
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 语境词表：每类是一组「强语境信号」。域与语境冲突时才是可疑。
# 词表刻意保守——只收语义指向明确的词，收宽了会淹没真误判。
#
# 2026-11-15 首轮实测教训：把「平台名」（抖音/小红书/微博…）直接当社交语境，
# 在 500 条真实查询上报出 8 条可疑，人工复核后**全是误报**——它们查的是
# 「平台官方公告 / 推荐算法说明 / 运营规范」，命中 chinese_tech_deep 或
# financial_news 恰恰能抓到官方文档，不是误判。
# 故平台词拆成两类：只有带**讨论/舆情**语义的写法才算社交语境。
_CONTEXT_WORDS: dict[str, list[str]] = {
    # 强社交语境：明确要社区讨论/舆情，不是要官方说明
    "social_platform": [
        "X 上", "X 平台", "twitter 上", "网友", "热议", "舆论", "舆情",
        "怎么看", "评价", "测评", "讨论", "吐槽", "口碑",
    ],
    "product_daily": [
        "使用技巧", "怎么用", "教程", "指南", "官网", "价格", "多少钱",
        "下载", "安装", "注册", "账号", "对比", "哪个好",
    ],
    "academic": [
        "论文", "paper", "综述", "文献", "期刊", "arXiv", "DOI", "预印本",
        "研究进展", "同行评议", "引用",
    ],
    "commercial_market": [
        "市场规模", "市场份额", "销售额", "营收", "市值", "融资", "财报",
        "集采", "医保", "报销", "竞争格局", "龙头",
    ],
    "tech_engineering": [
        "源码分析", "实现原理", "架构设计", "性能优化", "内存安全", "深入研究",
        "底层", "源码", "debug", "报错",
    ],
}

# 域 → 它**应该**匹配的语境（语义一致面）。命中域不含该语境的域即为可疑。
# 只登记有明确语义的域，其余不参与语境检查（避免误报）。
_DOMAIN_CONTEXT: dict[str, tuple[str, ...]] = {
    "academic": ("academic",),
    "social": ("social_platform",),
    "chinese_tech_deep": ("tech_engineering",),
    "english_tech": ("tech_engineering",),
    "ai_model": ("product_daily",),
    "financial_news": ("commercial_market",),
    "stock_query": ("commercial_market",),
    "fund_query": ("commercial_market",),
    "medical": ("commercial_market",),
    "chem_search": ("commercial_market",),
    "protein_search": ("commercial_market",),
    "company_search": ("commercial_market",),
}

# 语境 → 若查询只含该语境词而命中了语义冲突的域，可疑度更高
_STRONG_CONFLICT: dict[str, tuple[str, ...]] = {
    "social_platform": ("academic", "chinese_tech_deep", "english_tech"),
    "academic": ("social",),
}


def _route(query: str) -> dict[str, Any]:
    """跑一次路由，返回 {domain, primary, intents, lang}。

    用 `match_domains(max_n=1, primary_lang=...)`——`route.match_domain` 兼容
    接口不吃 primary_lang，会跳过中文域语言门控，度量结果会与生产不一致。
    """
    from route import extract_features
    from route_domains import match_domains
    feat = extract_features(query)
    lang = feat.get("primary_lang")
    # max_n=3 与生产主路径一致（route.py 的 _domain_hits）：max_n=1 时
    # intent 让位拿不到次域，度量会看不见修复效果。
    hits = match_domains(query, max_n=3, primary_lang=lang,
                         intents=feat.get("intents"))
    dom = hits[0] if hits else None
    return {
        "domain": dom.get("name") if dom else None,
        "primary": dom.get("primary") if dom else None,
        "lang": lang,
        "intents": feat.get("intents") or [],
    }


def _contexts_in(query: str) -> list[str]:
    """查询里出现了哪些语境（小写匹配）。"""
    q = query.lower()
    return [name for name, words in _CONTEXT_WORDS.items()
            if any(w.lower() in q for w in words)]


def _real_queries(limit: int = 0) -> list[str]:
    """从 usage_log 取去重真实查询（带出现次数）。"""
    counts: collections.Counter = collections.Counter()
    pattern = str(Path.home() / ".cache" / "unified-search" / "usage_log" / "*.jsonl")
    for f in glob.glob(pattern):
        try:
            for line in open(f, encoding="utf-8"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("stream") == "query" and d.get("query"):
                    counts[d["query"].strip()] += 1
        except OSError:
            continue
    top = counts.most_common(limit or None)
    return [q for q, _ in top]


def audit(queries: list[str]) -> dict[str, Any]:
    """对一批查询做语境一致性度量。"""
    suspicious: list[dict[str, Any]] = []
    domain_dist: collections.Counter = collections.Counter()
    for q in queries:
        r = _route(q)
        dom = r["domain"]
        domain_dist[dom or "(none)"] += 1
        ctxs = _contexts_in(q)
        if not dom or not ctxs:
            continue
        want_ctxs = _DOMAIN_CONTEXT.get(dom)
        if not want_ctxs:
            continue
        # 语境一致性：命中的域应当至少匹配一个查询语境
        if any(c in want_ctxs for c in ctxs):
            continue
        # 强冲突：查询含 A 语境，却命中了语义冲突的域
        strong = any(
            dom in _STRONG_CONFLICT.get(c, ())
            for c in ctxs
        )
        suspicious.append({
            "query": q, "domain": dom, "contexts": ctxs,
            "strong": strong, "intents": r["intents"],
        })
    return {
        "total": len(queries),
        "domain_distribution": dict(domain_dist.most_common()),
        "suspicious": suspicious,
        "suspicious_count": len(suspicious),
        "strong_count": sum(1 for s in suspicious if s["strong"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="路由误判度量")
    ap.add_argument("--all", action="store_true", help="打印全部可疑条目")
    ap.add_argument("--limit", type=int, default=500,
                    help="真实查询抽样上限（0=全部）")
    ap.add_argument("--json", metavar="BASELINE", help="把结果落盘为基线")
    ap.add_argument("--compare", metavar="BASELINE", help="与基线对比")
    args = ap.parse_args()

    queries = _real_queries(args.limit)
    result = audit(queries)

    print(f"真实查询 {result['total']} 条；命中域分布 Top10：")
    for dom, n in list(result["domain_distribution"].items())[:10]:
        print(f"  {dom:22} {n}")
    print(f"\n语境可疑 {result['suspicious_count']} 条"
          f"（强冲突 {result['strong_count']} 条）")
    for s in result["suspicious"][:(None if args.all else 20)]:
        flag = "强" if s["strong"] else "  "
        print(f"  [{flag}] {s['domain']:20} {s['contexts']} {s['query'][:44]!r}")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n基线已写入 {args.json}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        b = {s["query"]: s["domain"] for s in base["suspicious"]}
        n = {s["query"]: s["domain"] for s in result["suspicious"]}
        fixed = sorted(set(b) - set(n))
        new = sorted(set(n) - set(b))
        print(f"\n与基线 {args.compare} 对比：")
        print(f"  修复 {len(fixed)} 条：{fixed[:10]}")
        print(f"  新增 {len(new)} 条：{new[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
