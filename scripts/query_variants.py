#!/usr/bin/env python3
"""
query_variants.py — 无 LLM 查询变体生成（深度研究多路召回）

生成与原查询「真正不同」的多个检索变体，弥补单查询漏检。
策略：
  1. 原查询（恒保留）
  2. 问句化（陈述 → 问题）
  3. 概念扩展（近义/上位概念替换）
  4. 反方观点（找反方/批评视角）
  5. 范围调整（短查询加限定 / 长查询去限定词）
  6. 缩写与全称互换

纯规则实现，无外部依赖，中英双语。
"""

from __future__ import annotations

import re

# 概念扩展映射（不只同义词——相关表述框架）
CONCEPT_MAP = {
    # 技术
    "ai": ["artificial intelligence", "machine learning", "deep learning"],
    "ml": ["machine learning", "statistical learning"],
    "llm": ["large language model", "foundation model"],
    "api": ["interface", "integration", "SDK", "endpoint"],
    "saas": ["software as a service", "cloud software"],
    "devops": ["deployment automation", "CI/CD", "infrastructure as code"],
    "kubernetes": ["k8s", "container orchestration"],
    "docker": ["containerization", "container runtime"],
    "database": ["data store", "persistence layer"],
    "microservices": ["service-oriented architecture", "distributed systems"],
    # 商业
    "roi": ["return on investment", "cost-benefit", "payback period"],
    "kpi": ["key performance indicator", "metric", "benchmark"],
    "b2b": ["business to business", "enterprise sales"],
    "b2c": ["business to consumer", "consumer market"],
    "revenue": ["income", "sales", "top line"],
    "profit": ["margin", "bottom line", "earnings"],
    "strategy": ["approach", "framework", "playbook"],
    "market": ["industry", "sector", "vertical"],
    # 制造
    "manufacturing": ["production", "fabrication", "industrial"],
    "supply chain": ["logistics", "procurement", "sourcing"],
    # 金融
    "stock": ["equity", "shares", "securities"],
    "bond": ["fixed income", "debt instrument", "treasury"],
    "crypto": ["cryptocurrency", "digital assets", "blockchain"],
    "inflation": ["price increases", "purchasing power", "CPI"],
    "interest rate": ["fed funds rate", "monetary policy", "yield"],
    # 中文
    "人工智能": ["AI", "机器学习", "深度学习", "大模型"],
    "大模型": ["大语言模型", "LLM", "基础模型", "AI"],
    "算法": ["模型", "方法", "框架"],
    "机器人": ["自动化", "智能体", "Agent"],
    "股票": ["股价", "行情", "证券", "equity"],
    "基金": ["ETF", "公募", "理财"],
    "区块链": ["加密货币", "分布式账本", "比特币"],
    "云计算": ["云服务", "SaaS", "PaaS"],
    "开源": ["开放源码", "open source"],
    "软件": ["应用程序", "系统", "工具"],
    "硬件": ["芯片", "处理器", "设备"],
}

QUESTION_PREFIXES = [
    "what is", "how does", "why is", "what are the benefits of",
    "what are the risks of", "how to", "what is the future of",
]

OPPOSITION_TRIGGERS = {
    "best": "worst problems with",
    "benefits": "risks drawbacks of",
    "advantages": "disadvantages limitations of",
    "why": "why not criticism of",
    "success": "failure case study",
    "growing": "declining stagnating",
    "popular": "overrated criticism",
    "recommended": "alternatives to avoid",
    "safe": "risks dangers of",
    "cheap": "hidden costs of",
    "easy": "challenges difficulties of",
    "fast": "slow problems with",
    "good": "problems criticism of",
    "pros": "cons drawbacks",
    # 中文反方触发词
    "推荐": "吐槽 避坑 负面评价",
    "优势": "劣势 缺点 局限",
    "好处": "风险 坏处 缺点",
    "为什么": "为什么不 反对 批评",
    "最佳": "最差 问题 坑",
    "安全": "风险 隐患 危险",
    "值得": "不值得 翻车 后悔",
}

# 长查询限定词（范围调整时移除）
QUALIFIERS = {
    "latest", "best", "top", "new", "recent", "current", "modern",
    "2024", "2025", "2026", "today", "now", "ultimate", "complete",
    "comprehensive", "definitive", "essential",
    "最新", "最好", "最热", "当前", "现在", "近期", "最全",
}

# 缩写/全称互换
ACRONYM_MAP = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "nlp": "natural language processing",
    "llm": "large language model",
    "api": "application programming interface",
    "sdk": "software development kit",
    "ui": "user interface",
    "ux": "user experience",
    "db": "database",
    "oss": "open source software",
    "saas": "software as a service",
    "crm": "customer relationship management",
    "erp": "enterprise resource planning",
    "kpi": "key performance indicator",
    "ipo": "initial public offering",
    "cto": "chief technology officer",
    "ceo": "chief executive officer",
}


def generate_query_variations(original_query: str) -> list[str]:
    """生成 3-5 个真正不同的查询变体（含原查询）。"""
    if not original_query or not original_query.strip():
        return []
    original = original_query.strip()
    variations = [original]
    query_lower = original.lower()
    words = query_lower.split()

    # 策略 2：问句化
    question = _to_question(query_lower, words)
    if question and question.lower() != query_lower:
        variations.append(question)

    # 策略 3：概念扩展
    expanded = _expand_concepts(original, words)
    if expanded and expanded.lower() != query_lower:
        variations.append(expanded)

    # 策略 4：反方观点
    opposing = _opposing_viewpoint(original, query_lower, words)
    if opposing and opposing.lower() != query_lower:
        variations.append(opposing)

    # 策略 5：范围调整
    scoped = _adjust_scope(original, query_lower, words)
    if scoped and scoped.lower() != query_lower:
        variations.append(scoped)

    # 策略 6：缩写/全称互换
    expanded_acronym = _expand_acronym(original, query_lower)
    if expanded_acronym and expanded_acronym.lower() != query_lower:
        variations.append(expanded_acronym)

    # 去重（大小写不敏感）
    seen = set()
    unique = []
    for v in variations:
        key = v.strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)

    return unique[:6]


def _to_question(query: str, words: list[str]) -> str | None:
    """陈述 → 问题形式。"""
    if query.endswith("?") or words[0] in (
        "how", "what", "why", "when", "where", "who", "which", "is", "are",
        "can", "do", "does",
    ):
        return None

    # 动作类 → "how to"
    action_words = {"install", "setup", "configure", "build", "create", "deploy",
                    "fix", "solve", "debug", "optimize", "improve", "migrate"}
    if words[0] in action_words or (len(words) > 1 and words[1] in action_words):
        return f"how to {query}"

    # 名词短语 → "what is ... and how does it work"
    if len(words) <= 4:
        return f"what is {query} and how does it work"

    # 长查询 → "why"
    return f"why {query}"


def _expand_concepts(original: str, words: list[str]) -> str | None:
    """概念扩展：替换为上位/替代概念。"""
    result = original
    expanded = False

    for word in words:
        if word in CONCEPT_MAP:
            alternatives = CONCEPT_MAP[word]
            replacement = alternatives[0]
            result = re.sub(r"\b" + re.escape(word) + r"\b", replacement,
                            result, count=1, flags=re.IGNORECASE)
            expanded = True
            break

    if not expanded:
        for phrase, alternatives in CONCEPT_MAP.items():
            if " " in phrase and phrase.lower() in original.lower():
                result = original.lower().replace(phrase, alternatives[0], 1)
                expanded = True
                break

    return result if expanded else None


def _opposing_viewpoint(original: str, query_lower: str,
                        words: list[str]) -> str | None:
    """反方观点生成。"""
    for trigger, opposition in OPPOSITION_TRIGGERS.items():
        if trigger in words:
            return query_lower.replace(trigger, opposition, 1)

    # 无触发词 → 通用反方框架
    if len(words) >= 2:
        return f"criticism problems with {original}"
    return None


def _adjust_scope(original: str, query_lower: str, words: list[str]) -> str | None:
    """范围调整：短查询加限定，长查询去限定词。"""
    # 短查询 → 加时间/最新限定
    if len(words) <= 2:
        return f"{original} in 2026 latest developments"

    # 长查询 → 去掉限定词（最新/最热等）
    narrowed_words = [w for w in words if w not in QUALIFIERS]
    if len(narrowed_words) < len(words) and len(narrowed_words) >= 2:
        return " ".join(narrowed_words)

    # 深度补充
    if not any(w in words for w in ("research", "study", "analysis", "paper",
                                    "academic", "研究", "综述", "分析", "报告")):
        return f"{original} research analysis"
    return None


def _expand_acronym(original: str, query_lower: str) -> str | None:
    """缩写 → 全称。"""
    for word in query_lower.split():
        clean = word.strip(".,;:()[]")
        if clean in ACRONYM_MAP:
            return re.sub(r"\b" + re.escape(clean) + r"\b",
                          ACRONYM_MAP[clean], original, count=1,
                          flags=re.IGNORECASE)
    return None


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="Argo 查询变体生成（无 LLM 多路召回）")
    p.add_argument("query", nargs="?", default=None, help="原始查询")
    p.add_argument("--stdin", action="store_true", help="从 stdin 读取")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    q = args.query
    if args.stdin:
        q = sys.stdin.read().strip()
    if not q:
        p.error("需要查询或 --stdin")

    variants = generate_query_variations(q)
    if args.json:
        print(json.dumps({"original": q, "variants": variants},
                         ensure_ascii=False, indent=2))
    else:
        print(f"原查询：{q}")
        for i, v in enumerate(variants, 1):
            print(f"  {i}. {v}")
