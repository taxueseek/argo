#!/usr/bin/env python3
"""
topic_research_profiles.py — 选题研究配置文件

参照 zhihu-creator 的 ENTITY_DOMAIN_MAP 思路，为常见选题类型预配置
引擎组合、查询模板、分析深度、证据权重、质量门禁与报告结构。

专业域吸收（只内化方法，不外挂整 skill）：
  - super-research：文献综述 / 假设检验等模式的问题框架与交付结构
  - rw-research-router：阶段诊断、证据纪律、不编造 DOI/论文
  - invest-analyst：来源级别、盲区、市场疑问、风险清单、免责

用法：
    argo research "Claude Opus 5" --topic ai
    argo research "CRISPR 脱靶综述" --topic academic
    argo research "台积电估值分歧" --topic finance
"""

from __future__ import annotations

import re
from typing import Any

# ── 深度研究触发词（Agent / 斜杠命令路由用）────────────────────────────────

DEEP_RESEARCH_TRIGGERS: list[str] = [
    # 中文
    "深度研究",
    "深度调研",
    "深度报告",
    "深度分析",
    "全面调研",
    "系统研究",
    "系统综述",
    "文献综述",
    "科研调研",
    "做一份深度",
    "帮我深度",
    "深入研究",
    # 英文
    "deep research",
    "deep-research",
    "thorough research",
    "literature review",
    "systematic review",
]

# 斜杠命令名（与 ~/.claude/commands 对齐）
# 主命令 /argo；日常子技能 /argo-search 等；深度研究见下
ARGO_MAIN_SLASH = "/argo"
ARGO_SUB_SLASH_COMMANDS: list[str] = [
    "/argo-search",
    "/argo-research",
    "/argo-research-academic",
    "/argo-research-finance",
    "/argo-evidence",
    "/argo-clarify",
    "/argo-fetch",
    "/deep-research",  # 深度研究别名
]
RESEARCH_SLASH_COMMANDS: list[str] = [
    "/argo-research",
    "/deep-research",
    "/argo-research-academic",
    "/argo-research-finance",
    "/argo research",
    "/argo academic",
    "/argo finance",
]


# ── 选题类型定义 ──────────────────────────────────────────────────────────────

TOPIC_PROFILES: dict[str, dict[str, Any]] = {
    # ── AI / 大模型 ──────────────────────────────────────────────────────
    "ai": {
        "name": "AI / 大模型",
        "description": "AI模型发布、技术评测、行业趋势分析",
        "discipline": "tech",
        "engines_priority": ["zhihu", "arxiv", "anysearch", "semantic_scholar"],
        "vertical_engines": ["arxiv", "semantic_scholar", "openalex", "github"],
        "query_templates": [
            "{query} technical overview architecture",
            "{query} 技术评测 benchmark 对比",
            "{query} 业界评价 局限 风险",
            "{query} paper arxiv related work",
        ],
        "depth": "balanced",
        "sub_queries": 4,
        "max_results": 5,
        "evidence_weights": {
            "has_numbers": 1.2,
            "has_comparison": 1.3,
            "has_definition": 0.8,
            "authority": 0.9,
            "freshness": 1.1,
        },
        "freshness_cutoff_days": 90,
        "source_grades": {
            "primary": ["arxiv", "semantic_scholar", "openalex", "官方博客/技术报告"],
            "secondary": ["知乎高质量回答", "专业媒体评测"],
            "tertiary": ["社媒热帖", "二手转载"],
        },
        "quality_gates": [
            "是否区分官方/论文 vs 二手解读",
            "是否有可核对的数字或基准来源",
            "是否标明时效（模型版本/发布日）",
            "是否列出局限与未证实主张",
        ],
        "report_sections": [
            "问题框架", "关键发现", "对比/基准", "局限与开放问题", "相关信源"
        ],
        "auto_triggers": [
            "大模型", "llm", "gpt", "claude", "gemini", "agent", "transformer",
            "多模态", "rag", "微调", "inference", "推理模型",
        ],
    },

    # ── 投资理财（通用投资选题）──────────────────────────────────────────
    "investment": {
        "name": "投资理财",
        "description": "政策解读、市场分析、投资策略线索收集（非买卖建议）",
        "discipline": "finance",
        "engines_priority": ["eastmoney", "zhihu", "byted", "cls_telegraph"],
        "vertical_engines": [
            "sina_quote", "em_flow", "eastmoney", "fred", "cls_telegraph",
        ],
        "query_templates": [
            "{query} 政策解读 影响分析",
            "{query} 市场影响 数据",
            "{query} 最新动态 财报 公告",
            "{query} 风险 争议 反对意见",
        ],
        "depth": "balanced",
        "sub_queries": 4,
        "max_results": 5,
        "evidence_weights": {
            "has_numbers": 1.3,
            "has_comparison": 1.0,
            "has_definition": 0.7,
            "authority": 1.2,
            "freshness": 1.1,
        },
        "freshness_cutoff_days": 30,
        "source_grades": {
            "primary": ["交易所/公司公告", "监管文件", "官方统计"],
            "secondary": ["券商研报摘要", "权威财经媒体", "eastmoney 行情数据"],
            "tertiary": ["论坛观点", "自媒体解读"],
        },
        "quality_gates": [
            "关键数字是否标注来源与时点",
            "是否区分事实、市场观点与推断",
            "是否写明盲区/未找到的数据",
            "是否包含风险与反对论点",
            "是否避免「买入/卖出」式喊口号（研究包≠投顾建议）",
        ],
        "report_sections": [
            "市场疑问", "关键证据", "多空/分歧", "风险与盲区", "相关信源", "免责声明"
        ],
        "auto_triggers": [
            "投资", "理财", "基金", "股票", "债券", "etf", "估值", "仓位",
            "政策利率", "宏观流动性",
        ],
    },

    # ── 金融深度（IC/事件/共识风格，吸收 invest-analyst）────────────────
    "finance": {
        "name": "金融深度研究",
        "description": "IC 风格深度：市场疑问、证据层级、共识/分歧、风险清单（非投资建议）",
        "discipline": "finance",
        "engines_priority": ["eastmoney", "cls_telegraph", "byted", "zhihu", "anysearch"],
        "vertical_engines": [
            "sina_quote", "tencent_quote", "em_flow", "eastmoney",
            "fred", "worldbank", "fx_rate", "finviz", "cls_telegraph",
        ],
        "query_templates": [
            "{query} 核心投资逻辑 市场疑问",
            "{query} 财报 业绩 指引 一致预期",
            "{query} 行业格局 竞争 份额",
            "{query} 估值 对标 PE PB DCF 分歧",
            "{query} 风险 监管 黑天鹅 反对意见",
            "{query} 券商 目标价 共识",
        ],
        "depth": "deep",
        "sub_queries": 5,
        "max_results": 6,
        "evidence_weights": {
            "has_numbers": 1.4,
            "has_comparison": 1.2,
            "has_definition": 0.6,
            "authority": 1.3,
            "freshness": 1.2,
        },
        "freshness_cutoff_days": 21,
        "source_grades": {
            "一手": ["年报/季报/公告", "监管问询与回复", "官方宏观数据"],
            "权威": ["主流券商研报", "交易所披露", "权威财经社"],
            "参考": ["卖方路演纪要转述", "行业媒体", "社区讨论"],
        },
        "quality_gates": [
            "是否抓住「市场当前最关键疑问」而非堆砌公开摘要",
            "是否标注信源级别（一手/权威/参考）",
            "宏观-行业-公司-估值是否至少覆盖两层且互相校验",
            "是否有多空对抗或共识/分歧",
            "是否有风险清单（区分致命伤 vs 波动）",
            "是否注明盲区；搜不到写「未找到公开数据」而非编造",
            "是否含简短免责（不构成投资建议）",
        ],
        "report_sections": [
            "市场疑问",
            "投资/分析论点（非买卖指令）",
            "证据层（宏观/行业/公司/估值）",
            "共识与分歧",
            "催化剂与风险",
            "盲区与下一步核验",
            "相关信源",
            "免责声明",
        ],
        "auto_triggers": [
            "研报", "ic 报告", "深度研报", "一致预期", "目标价", "事件驱动",
            "电话会", "主题策略", "行业比较", "comps", "dcf", "pe 对标",
            "财报点评", "超预期", "低于预期", "券商观点",
        ],
    },

    # ── 科研 / 学术（吸收 super-research + rw-research-router）────────────
    "academic": {
        "name": "科研 / 学术",
        "description": "文献发现、综述框架、证据纪律；不编造 DOI/论文",
        "discipline": "academic",
        "engines_priority": [
            "arxiv", "semantic_scholar", "openalex", "crossref", "anysearch", "zhihu",
        ],
        "vertical_engines": [
            "arxiv", "semantic_scholar", "openalex", "crossref",
            "europepmc", "pubchem", "clinicaltrials", "uniprot",
        ],
        "query_templates": [
            "{query} systematic review survey taxonomy",
            "{query} arxiv paper method results limitations",
            "{query} related work open questions",
            "{query} 综述 方法 数据集 基准",
            "{query} replication reproducibility threats to validity",
        ],
        "depth": "deep",
        "sub_queries": 5,
        "max_results": 6,
        "evidence_weights": {
            "has_numbers": 1.1,
            "has_comparison": 1.2,
            "has_definition": 1.1,
            "authority": 1.3,
            "freshness": 0.9,
        },
        "freshness_cutoff_days": 730,
        "source_grades": {
            "primary": ["peer-reviewed / preprint with venue", "官方数据集与代码仓"],
            "secondary": ["高质量综述", "领域手册/标准"],
            "tertiary": ["博客解读", "科普转述"],
        },
        "quality_gates": [
            "研究问题是否可检验（什么算回答）",
            "是否区分用户材料 / 公开来源 / 推断 / 未知",
            "是否标注共识 vs 开放问题",
            "是否写威胁效度（偏倚、样本、测量）",
            "是否禁止编造 DOI、论文、期刊要求或运行结果",
            "是否给出可复现的下一步（单一步骤，不一次铺整条链）",
        ],
        "report_sections": [
            "问题框架（Question）",
            "方法与检索策略（Method）",
            "主要发现（Results）",
            "分析与分类（Analysis / Taxonomy）",
            "局限（Limitations）",
            "下一步（Next Steps）",
            "相关信源（References）",
        ],
        # super-research 模式提示（Agent 写作时选用，不强制实验）
        "research_modes": {
            "literature": ["文献", "literature", "综述", "survey", "related work"],
            "hypothesis": ["假设", "hypothesis", "是否导致", "does x cause"],
            "benchmark": ["基准", "benchmark", "对比", "compare against"],
            "reproduction": ["复现", "reproduce", "replicate", "验证论文"],
        },
        "auto_triggers": [
            "论文", "arxiv", "doi", "文献", "综述", "survey", "meta-analysis",
            "科研", "学术", "peer review", "数据集", "replication", "消融",
            "ablation", "hypothesis", "systematic",
        ],
    },

    # ── 数码科技 ──────────────────────────────────────────────────────────
    "tech": {
        "name": "数码科技",
        "description": "产品评测、硬件行情、购买建议",
        "discipline": "tech",
        "engines_priority": ["zhihu", "byted", "anysearch", "v2ex"],
        "query_templates": [
            "{query} 评测 体验",
            "{query} 值得买 对比",
            "{query} 最新行情 价格",
        ],
        "depth": "fast",
        "sub_queries": 2,
        "max_results": 4,
        "evidence_weights": {
            "has_numbers": 1.1,
            "has_comparison": 1.2,
            "has_definition": 0.9,
            "authority": 0.8,
            "freshness": 1.0,
        },
        "freshness_cutoff_days": 180,
        "source_grades": {
            "primary": ["专业评测站", "官方规格"],
            "secondary": ["知乎/V2EX 实测"],
            "tertiary": ["营销软文"],
        },
        "quality_gates": [
            "是否区分规格参数与主观体验",
            "是否有可比价/竞品对照",
        ],
        "report_sections": ["关键发现", "对比要点", "相关信源"],
        "auto_triggers": ["显卡", "手机", "笔记本", "评测", "数码", "硬件"],
    },

    # ── 效率工具 ──────────────────────────────────────────────────────────
    "tool": {
        "name": "效率工具",
        "description": "工具推荐、工作流、技巧分享",
        "discipline": "tool",
        "engines_priority": ["zhihu", "anysearch", "byted", "v2ex"],
        "query_templates": [
            "{query} 推荐 评测",
            "{query} 使用技巧 工作流",
            "{query} 对比 哪个好",
        ],
        "depth": "fast",
        "sub_queries": 2,
        "max_results": 4,
        "evidence_weights": {
            "has_numbers": 0.8,
            "has_comparison": 1.1,
            "has_definition": 1.0,
            "authority": 0.7,
            "freshness": 1.0,
        },
        "freshness_cutoff_days": 365,
        "source_grades": {
            "primary": ["官方文档", "仓库 README"],
            "secondary": ["实操教程", "社区长文"],
            "tertiary": ["广告软文"],
        },
        "quality_gates": [
            "是否说明适用场景与不适用场景",
        ],
        "report_sections": ["关键发现", "工作流要点", "相关信源"],
        "auto_triggers": ["插件", "workflow", "效率工具", "obsidian", "notion"],
    },

    # ── 互联网行业 ────────────────────────────────────────────────────────
    "internet": {
        "name": "互联网行业",
        "description": "商业模式、行业动态、公司分析",
        "discipline": "business",
        "engines_priority": ["zhihu", "byted", "anysearch", "cls_telegraph"],
        "query_templates": [
            "{query} 商业模式 分析",
            "{query} 行业动态 趋势",
            "{query} 竞争格局 前景",
        ],
        "depth": "balanced",
        "sub_queries": 3,
        "max_results": 5,
        "evidence_weights": {
            "has_numbers": 1.0,
            "has_comparison": 1.1,
            "has_definition": 0.9,
            "authority": 1.0,
            "freshness": 1.1,
        },
        "freshness_cutoff_days": 180,
        "source_grades": {
            "primary": ["公司披露", "监管/协会数据"],
            "secondary": ["行业媒体深度"],
            "tertiary": ["传闻"],
        },
        "quality_gates": [
            "是否区分商业事实与叙事炒作",
        ],
        "report_sections": ["关键发现", "竞争与模式", "相关信源"],
        "auto_triggers": ["商业模式", "平台经济", "互联网行业", "用户增长"],
    },

    # ── 社交舆情 ──────────────────────────────────────────────────────────
    "social": {
        "name": "社交舆情",
        "description": "用户口碑、讨论热点、情绪倾向",
        "discipline": "social",
        "engines_priority": ["zhihu", "xiaohongshu", "weibo", "reddit"],
        "query_templates": [
            "{query} 体验分享",
            "{query} 吐槽 评价",
            "{query} 推荐 避坑",
        ],
        "depth": "fast",
        "sub_queries": 2,
        "max_results": 5,
        "evidence_weights": {
            "has_numbers": 0.6,
            "has_comparison": 0.7,
            "has_definition": 0.8,
            "authority": 0.5,
            "freshness": 1.2,
        },
        "freshness_cutoff_days": 30,
        "source_grades": {
            "primary": ["高互动原始 UGC"],
            "secondary": ["聚合舆情"],
            "tertiary": ["营销号"],
        },
        "quality_gates": [
            "是否标注平台与样本局限（非科学抽样）",
            "是否区分情绪与事实主张",
        ],
        "report_sections": ["平台分布", "高频话题", "代表性内容", "相关信源"],
        "auto_triggers": ["口碑", "舆情", "用户评价", "吐槽", "避坑"],
    },
}

# ── 缩略名索引 ──────────────────────────────────────────────────────────────

ALIASES: dict[str, str] = {
    # AI
    "ai": "ai",
    "人工智能": "ai",
    "大模型": "ai",
    "llm": "ai",
    # investment
    "investment": "investment",
    "invest": "investment",
    "投资": "investment",
    "理财": "investment",
    # finance deep
    "finance": "finance",
    "金融": "finance",
    "金融深度": "finance",
    "ic": "finance",
    "研报": "finance",
    "深度研报": "finance",
    "equity": "finance",
    # academic
    "academic": "academic",
    "science": "academic",
    "科研": "academic",
    "学术": "academic",
    "文献": "academic",
    "综述": "academic",
    "literature": "academic",
    "paper": "academic",
    "论文": "academic",
    # tech / tool / internet / social
    "tech": "tech",
    "数码": "tech",
    "科技": "tech",
    "硬件": "tech",
    "tool": "tool",
    "工具": "tool",
    "效率": "tool",
    "internet": "internet",
    "互联网": "internet",
    "行业": "internet",
    "social": "social",
    "社交": "social",
    "舆情": "social",
    "口碑": "social",
}


# ── API ──────────────────────────────────────────────────────────────────────

def get_profile(topic: str) -> dict[str, Any] | None:
    """根据 topic 名称或别名查找 profile。"""
    if not topic:
        return None
    key = topic.strip().lower()
    # 中文别名不 lower 二次：先原样、再 lower
    profile_key = ALIASES.get(topic.strip()) or ALIASES.get(key) or key
    return TOPIC_PROFILES.get(profile_key)


def list_profiles() -> list[dict[str, Any]]:
    """列出所有可用 profile。"""
    return [
        {
            "key": key,
            "name": p["name"],
            "description": p["description"],
            "engines": p["engines_priority"],
            "depth": p["depth"],
            "discipline": p.get("discipline", "general"),
        }
        for key, p in TOPIC_PROFILES.items()
    ]


def apply_profile(profile: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """将 profile 参数合并到 research 参数字典中。

    kwargs 中已有的「非默认」值不会被覆盖的语义由调用方保证；
    此处：仅当键不在 kwargs 时写入 profile 默认值。
    """
    merged = dict(kwargs)

    for key in (
        "engines_priority",
        "vertical_engines",
        "depth",
        "sub_queries",
        "max_results",
        "evidence_weights",
        "query_templates",
        "quality_gates",
        "report_sections",
        "source_grades",
        "discipline",
        "freshness_cutoff_days",
        "research_modes",
    ):
        if key not in merged and profile.get(key) is not None:
            merged[key] = profile[key]

    return merged


def is_deep_research_trigger(text: str) -> bool:
    """用户话术是否明确触发深度研究（非日常 SERP）。"""
    if not text:
        return False
    low = text.lower()
    for t in DEEP_RESEARCH_TRIGGERS:
        if t.lower() in low:
            return True
    # 斜杠命令
    for cmd in RESEARCH_SLASH_COMMANDS:
        if cmd in text or cmd.lstrip("/") in text.split()[:1]:
            return True
    return False


def detect_topic_from_query(query: str) -> str | None:
    """从查询文本启发式推断 topic key（显式 --topic 优先于本函数）。

    优先级：academic > finance > investment > ai > social > tech > tool > internet
    （更专业的领域先匹配，避免「研报」落入泛 investment）。
    """
    if not query:
        return None
    q = query.lower()
    # 直接别名整词
    for alias, key in ALIASES.items():
        if len(alias) >= 2 and alias.lower() in q:
            # 短别名如 ic 需词界
            if len(alias) <= 2 and alias.isascii():
                if not re.search(rf"\b{re.escape(alias)}\b", q, re.I):
                    continue
            # 优先返回更高专业度：下面用 ordered scan
            pass

    order = [
        "academic",
        "finance",
        "investment",
        "ai",
        "social",
        "tech",
        "tool",
        "internet",
    ]
    scores: dict[str, int] = {k: 0 for k in order}
    for key in order:
        prof = TOPIC_PROFILES[key]
        for trig in prof.get("auto_triggers") or []:
            if trig.lower() in q:
                scores[key] += 2 if len(trig) >= 4 else 1
        # 别名命中
        for alias, mapped in ALIASES.items():
            if mapped == key and len(alias) >= 2 and alias.lower() in q:
                scores[key] += 1

    best = max(order, key=lambda k: scores[k])
    if scores[best] <= 0:
        return None
    return best


def build_profile_sub_queries(
    query: str,
    profile: dict[str, Any],
    num_sub: int = 4,
) -> list[dict[str, str]]:
    """用 profile.query_templates 生成子查询列表。"""
    templates = profile.get("query_templates") or []
    out: list[dict[str, str]] = []
    for i, tmpl in enumerate(templates):
        if len(out) >= num_sub:
            break
        try:
            q = tmpl.format(query=query)
        except Exception:
            q = f"{query} {tmpl}"
        out.append({
            "query": q,
            "intent": f"{profile.get('name', 'topic')} · 模板{i + 1}",
            "strategy": f"profile_{profile.get('discipline', 'general')}",
        })
    if not out:
        out.append({
            "query": query,
            "intent": "原始查询",
            "strategy": "direct",
        })
    return out[:num_sub]


def profile_meta(profile: dict[str, Any]) -> dict[str, Any]:
    """写入 research 报告的轻量元数据。"""
    return {
        "name": profile.get("name"),
        "discipline": profile.get("discipline"),
        "engines_priority": list(profile.get("engines_priority") or []),
        "vertical_engines": list(profile.get("vertical_engines") or []),
        "quality_gates": list(profile.get("quality_gates") or []),
        "report_sections": list(profile.get("report_sections") or []),
        "source_grades": profile.get("source_grades") or {},
        "freshness_cutoff_days": profile.get("freshness_cutoff_days"),
        "research_modes": profile.get("research_modes"),
    }


def list_triggers() -> dict[str, Any]:
    """供 --topic help / 文档生成。"""
    return {
        "deep_research_triggers": list(DEEP_RESEARCH_TRIGGERS),
        "main_slash": ARGO_MAIN_SLASH,
        "slash_commands": list(ARGO_SUB_SLASH_COMMANDS),
        "research_slash_commands": list(RESEARCH_SLASH_COMMANDS),
        "topics": list_profiles(),
    }
