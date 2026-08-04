#!/usr/bin/env python3
"""
recovery.py — 空结果错误恢复决策树（P0-002）

第一性原理：
  「零结果」不是终态而是可恢复状态。用最小额外成本，按信息增益从高到低
  逐级放宽约束，直到拿到可吸收事实或耗尽策略。

MECE 五级决策树（L1→L5，成本递增）：
  L1 放宽    : 去停用词 / 去引号 / 截断过长尾修饰
  L2 同义    : 小同义表替换（财报↔earnings，股价↔行情，教程↔guide）
  L3 换引擎  : 从 engines_fallback 或通用免费组合取未试引擎
  L4 跨语言  : 非拉丁→通用英源；英文+偏好zh→中文源；中英互译启发式
  L5 结构化  : 输出 recovery 字段告知 Agent 已降级及降级路径

模式约束：
  mode=fast : 最多 L1 + L3，禁止 L4（控制延迟/成本）

设计为「计划生成器」：本模块只产出候选 (query, engines) 与理由，
不直接执行网络请求，由 search.py 用回调执行，保持职责单一。

纯本地，仅 stdlib + 一个 JSON 小表。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_BACKENDS_DIR = Path(__file__).parent.parent / "backends"
_SYNONYMS_PATH = _BACKENDS_DIR / "query_synonyms_cn.json"


# ── 停用词 / 尾修饰 ────────────────────────────────────────────────────────────

_STOPWORDS_CN = {
    "的", "了", "呢", "吧", "啊", "呀", "吗", "哦", "嘛", "些", "个",
    "请问", "怎么", "如何", "一下", "帮我", "我想", "想要", "关于", "有关",
    "求", "跪求", "谁知道", "有没有", "是不是",
}
_STOPWORDS_EN = {"the", "a", "an", "of", "for", "to", "please", "how", "what"}

# 常见「过长尾修饰」：地点/时间/程度等，去掉后主体更聚焦
_TAIL_MODIFIERS = re.compile(
    r"(最新|最全|详细|完整|全面|2020|2021|2022|2023|2024|2025|2026|"
    r"知乎|贴吧|论坛|大全|汇总|排行榜|一览)")

_QUOTE_CHARS = re.compile(r'["\'“”‘’「」『』]')
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")
_EN_WORD = re.compile(r"\b[a-zA-Z]{3,}\b")


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class RecoveryStep:
    """单个恢复尝试。"""

    level: str            # L1 / L2 / L3 / L4
    strategy: str         # relax / synonym / switch_engine / translate
    query: str
    engines: list[str]
    reason: str


@dataclass
class RecoveryResult:
    """恢复执行的最终结构化结论（写入顶层 recovery 字段）。"""

    triggered: bool = False
    recovered: bool = False
    level_used: str | None = None
    strategy_used: str | None = None
    steps_tried: list[dict[str, Any]] = field(default_factory=list)
    final_query: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "recovered": self.recovered,
            "level_used": self.level_used,
            "strategy_used": self.strategy_used,
            "steps_tried": self.steps_tried,
            "final_query": self.final_query,
            "note": self.note,
        }


# ── 同义表加载 ────────────────────────────────────────────────────────────────

_synonyms_cache: dict[str, Any] | None = None


def _load_synonyms() -> dict[str, Any]:
    """加载同义表（缓存）。文件缺失/损坏时返回空表，不抛异常。"""
    global _synonyms_cache
    if _synonyms_cache is not None:
        return _synonyms_cache
    if not _SYNONYMS_PATH.exists():
        _synonyms_cache = {"synonyms": {}, "en_to_cn": {}}
        return _synonyms_cache
    try:
        data = json.loads(_SYNONYMS_PATH.read_text(encoding="utf-8"))
        _synonyms_cache = {
            "synonyms": data.get("synonyms", {}),
            "en_to_cn": data.get("en_to_cn", {}),
        }
    except (json.JSONDecodeError, OSError):
        _synonyms_cache = {"synonyms": {}, "en_to_cn": {}}
    return _synonyms_cache


# ── L1 放宽 ────────────────────────────────────────────────────────────────────

def relax_query(query: str) -> str:
    """L1：去引号 / 去停用词 / 去过长尾修饰。"""
    q = _QUOTE_CHARS.sub(" ", query)
    q = _TAIL_MODIFIERS.sub(" ", q)
    # 去停用词（按 token）
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", q)
    kept = []
    for t in tokens:
        if t.lower() in _STOPWORDS_EN or t in _STOPWORDS_CN:
            continue
        kept.append(t)
    relaxed = " ".join(kept).strip()
    # 兜底：去停用词后不能为空
    return relaxed or re.sub(r"\s+", " ", q).strip() or query


# ── L2 同义 ────────────────────────────────────────────────────────────────────

def synonym_expand(query: str) -> Optional[str]:
    """L2：将查询中命中的词替换为首个同义词。无命中返回 None。"""
    table = _load_synonyms()["synonyms"]
    for term, syns in table.items():
        if term in query and syns:
            return query.replace(term, syns[0])
    return None


# ── L3 换引擎 ──────────────────────────────────────────────────────────────────

_GENERAL_FREE_COMBO = ["anysearch", "duckduckgo", "local_bing", "local_baidu", "wikipedia"]

# 空结果恢复只允许「通用检索 + 百科」；垂直污染源（pypi/npm/jin10 等）一律禁入。
# 例外：已尝试引擎同族可保留（如 tried 含 thesportsdb → 可补 sports 族）。
_RECOVERY_SAFE_FAMILIES = frozenset({"web_general", "knowledge"})


def _engine_family(name: str) -> str:
    try:
        from engine_families import family_of
        return family_of(name)
    except Exception:
        return "web_general"


def _recovery_engine_allowed(eng: str, tried_families: set[str]) -> bool:
    """L3/L4 备选是否安全：通用/百科，或与已试引擎同族。"""
    fam = _engine_family(eng)
    return fam in _RECOVERY_SAFE_FAMILIES or fam in tried_families


def pick_alternative_engines(tried: list[str], engines_fallback: list[str] | None,
                             enabled: set[str] | None = None,
                             max_n: int = 2) -> list[str]:
    """L3：挑选未尝试过的备选引擎。

    优先通用免费组合（anysearch/ddg/bing/wiki），再同族垂直，再路由 fallback。
    禁止 code/行情/快讯等与查询域无关的垂直源污染结果。
    """
    tried_set = set(tried)
    tried_families = {_engine_family(e) for e in tried}
    picks: list[str] = []

    def _try_add(eng: str) -> bool:
        if eng in tried_set or eng in picks:
            return False
        if enabled is not None and eng not in enabled:
            return False
        if not _recovery_engine_allowed(eng, tried_families):
            return False
        picks.append(eng)
        return len(picks) >= max_n

    # 1) 通用免费源优先（避免 fallback 全量 enabled 把 pypi/npm 排到前面）
    for eng in _GENERAL_FREE_COMBO:
        if _try_add(eng):
            return picks

    # 2) 同族垂直（如 knowledge 补 wikidata；sports 补其它体育源）
    for eng in engines_fallback or []:
        if _engine_family(eng) in tried_families and _try_add(eng):
            return picks

    # 3) 其余安全 fallback（仍过 family 门禁）
    for eng in engines_fallback or []:
        if _try_add(eng):
            return picks
    return picks


# ── L4 跨语言回退（原中英互译启发式升级）───────────────────────────────────────

# 非拉丁 / 中文主语言零结果时，用通用源补搜（原文即可，无需真翻译）
_CROSS_LANG_ENGINES = ["duckduckgo", "anysearch", "wikipedia"]
# 中英基线反向：英文主查询零结果且用户偏好含 zh 时，补中文源
_CN_BASELINE_ENGINES = ["local_bing", "anysearch", "duckduckgo"]

_CROSS_LANG_SIGNAL = re.compile(
    r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff\u0e00-\u0e7f"
    r"\u0600-\u06ff\u0590-\u05ff\u0370-\u03ff\u0900-\u097f]"
)


def _is_non_latin_script(query: str) -> bool:
    """是否含非拉丁书写系统字符（日韩/西里尔/泰/阿/希伯来/希腊/天城体等）。"""
    return bool(_CROSS_LANG_SIGNAL.search(query))


def translate_heuristic(query: str) -> Optional[str]:
    """L4：从中文/日韩查询抽英文 token 组新查询；或用同义表做中→英映射。

    非真翻译：仅提取已有英文 token + 同义表英文对照，供英文源二次检索。
    无可用英文信号时返回 None。
    """
    en_tokens = [w for w in _EN_WORD.findall(query)]
    mapped: list[str] = []
    table = _load_synonyms()["synonyms"]
    for term, syns in table.items():
        if term in query:
            for s in syns:
                if s.isascii():
                    mapped.append(s)
                    break
    combined = []
    for t in en_tokens + mapped:
        if t not in combined:
            combined.append(t)
    if not combined:
        return None
    return " ".join(combined)


def cross_lang_query(query: str, primary_lang: str = "") -> tuple[Optional[str], str]:
    """跨语言回退：构造补搜查询与引擎组合。

    规则：
      - 主语言为英文/拉丁且无非拉丁字符 → 无需跨语言回退，返回 (None, "")。
      - 含非拉丁书写系统（或主语言为非拉丁）→ 返回 (query, 通用源组合)，
        用原文在 duckduckgo/anysearch/wikipedia 补搜（无需真翻译）。

    返回 (补搜查询, 引擎列表逗号串)；无需回退时返回 (None, "")。
    """
    if not query:
        return None, ""
    non_latin = _is_non_latin_script(query)
    if primary_lang:
        # 显式主语言：拉丁系且无非拉丁字符才跳过
        if primary_lang in ("en", "latin") and not non_latin:
            return None, ""
        # mixed 纯拉丁也跳过；mixed 含汉字/假名等仍回退
        if primary_lang == "mixed" and not non_latin:
            return None, ""
    elif not non_latin:
        return None, ""
    return query, ",".join(_CROSS_LANG_ENGINES)


def baseline_cross_query(
    query: str,
    primary_lang: str = "",
    prefer: list[str] | None = None,
) -> tuple[Optional[str], str]:
    """中英基线补搜：英文主查询零结果时，若偏好含 zh，用中文源再试原文。

    对称于 cross_lang_query（非拉丁→通用英源）。仅在 prefer 含 zh 时触发，
    避免纯英文市场用户被强塞中文源。
    """
    if not query:
        return None, ""
    lang = primary_lang
    if not lang:
        try:
            from lang_detect import detect_language
            lang = detect_language(query)
        except ImportError:
            lang = "en"
    if lang not in ("en", "latin"):
        return None, ""
    if _is_non_latin_script(query):
        return None, ""
    prefs = prefer
    if prefs is None:
        try:
            from lang_pref import prefer_langs
            prefs = prefer_langs(query_lang=lang)
        except ImportError:
            prefs = ["zh", "en"]
    if "zh" not in (prefs or []):
        return None, ""
    return query, ",".join(_CN_BASELINE_ENGINES)


# ── 计划生成 ──────────────────────────────────────────────────────────────────

def build_recovery_plan(query: str, tried_engines: list[str],
                        engines_fallback: list[str] | None = None,
                        enabled: set[str] | None = None,
                        mode: str = "auto") -> list[RecoveryStep]:
    """构造有序恢复步骤（L1→L5）。

    Args:
        query: 已经失败（零结果）的查询。
        tried_engines: 已尝试的引擎列表。
        engines_fallback: 路由给出的未使用引擎候选。
        enabled: 当前可用引擎集合（用于过滤 L3 候选）。
        mode: 预算模式；fast 仅允许 L1 + L3，禁止 L4。

    Returns:
        RecoveryStep 列表（可能为空）。
    """
    steps: list[RecoveryStep] = []
    base_engines = list(tried_engines) or ["anysearch"]

    # 本轮主语言：供 L4 跨语 / 中英基线判断
    primary_lang = ""
    try:
        from lang_detect import detect_language
        primary_lang = detect_language(query)
    except ImportError:
        pass

    prefer: list[str] = []
    try:
        from lang_pref import prefer_langs
        prefer = prefer_langs(query_lang=primary_lang)
    except ImportError:
        prefer = ["zh", "en"]

    # L1 放宽
    relaxed = relax_query(query)
    if relaxed and relaxed != query:
        steps.append(RecoveryStep(
            level="L1", strategy="relax", query=relaxed, engines=base_engines,
            reason="去停用词/引号/尾修饰后重试"))

    # L2 同义（fast 也允许，成本低）
    syn = synonym_expand(query)
    if syn and syn != query:
        steps.append(RecoveryStep(
            level="L2", strategy="synonym", query=syn, engines=base_engines,
            reason="同义词替换重试"))

    # L3 换引擎
    alt = pick_alternative_engines(tried_engines, engines_fallback, enabled)
    if alt:
        steps.append(RecoveryStep(
            level="L3", strategy="switch_engine", query=relaxed or query,
            engines=alt, reason=f"切换未试引擎: {','.join(alt)}"))

    # L4 跨语言回退（fast 禁止）
    if mode != "fast":
        # 非拉丁 / 中文 → 通用英源补搜
        cross_q, cross_engines = cross_lang_query(query, primary_lang=primary_lang)
        if cross_engines:
            steps.append(RecoveryStep(
                level="L4", strategy="cross_lang",
                query=cross_q or query, engines=cross_engines.split(","),
                reason="跨语言回退：非英文主语言用通用源补搜"))
        # 中英基线反向：英文查询 + 偏好含 zh → 中文源补搜
        base_q, base_engines_s = baseline_cross_query(
            query, primary_lang=primary_lang, prefer=prefer)
        if base_engines_s:
            eng_list = [e for e in base_engines_s.split(",")
                        if enabled is None or e in enabled]
            if not eng_list:
                eng_list = base_engines_s.split(",")
            steps.append(RecoveryStep(
                level="L4", strategy="baseline_zh",
                query=base_q or query, engines=eng_list,
                reason="中英基线：英文主查询零结果，偏好含 zh 时补中文源"))
        # 中英互译启发式（有英文 token / 同义表映射时）
        trans = translate_heuristic(query)
        if trans and trans.lower() != query.lower():
            trans_engines = pick_alternative_engines(
                [], engines_fallback,
                enabled, max_n=2) or ["duckduckgo", "wikipedia"]
            steps.append(RecoveryStep(
                level="L4", strategy="translate", query=trans,
                engines=trans_engines, reason="中英互译启发式重试"))

    # fast 模式：仅保留 L1 + L3
    if mode == "fast":
        steps = [s for s in steps if s.level in ("L1", "L3")]

    return steps


# ── 执行编排（供 search.py 调用）───────────────────────────────────────────────

# recovery 接受结果前的停用词（不含 WHO 等缩写；缩写靠 isupper 保留）
_REC_SIGNAL_STOP = {
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "is", "are", "was", "were",
    "how", "what", "which", "when", "where", "whom", "whose", "why", "who",
    "and", "or", "with", "from", "that", "this", "year", "years", "founded", "founding",
    "headquarters", "please", "latest", "about",
    "请问", "怎么", "如何", "关于", "有关", "年份", "时间", "成立", "创办", "创立",
    "总部", "职能", "简介", "最新", "详细",
}


def _result_has_query_signal(query: str, item: dict[str, Any]) -> bool:
    """恢复结果须与查询有实质 token 重叠，避免 Chegg/无关广告被当成恢复成功。"""
    if not query or not isinstance(item, dict):
        return False
    blob = f"{item.get('title', '')} {item.get('snippet', '')} {item.get('url', '')}".lower()
    if not blob.strip():
        return False
    keys: list[str] = []
    for t in re.findall(r"[A-Z]{2,}|[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}", query):
        if t.isupper() and len(t) >= 2:
            keys.append(t.lower())
        elif t.lower() not in _REC_SIGNAL_STOP:
            keys.append(t.lower())
    if not keys:
        return True  # 无可用信号时不拦
    return any(k in blob for k in keys)


def run_recovery(query: str, tried_engines: list[str],
                 executor: Callable[[str, list[str]], list[dict[str, Any]]],
                 engines_fallback: list[str] | None = None,
                 enabled: set[str] | None = None,
                 mode: str = "auto") -> tuple[list[dict[str, Any]], RecoveryResult]:
    """执行恢复决策树，返回 (结果列表, 结构化 RecoveryResult)。

    executor(query, engines) 必须返回结果 list（可空），不得抛异常。

    Returns:
        (results, recovery_result)：results 为首个非空恢复结果（否则空列表）。
    """
    result = RecoveryResult(triggered=True, final_query=query)
    plan = build_recovery_plan(query, tried_engines, engines_fallback, enabled, mode)

    if not plan:
        result.note = "无可用恢复策略"
        return [], result

    for step in plan:
        try:
            hits = executor(step.query, step.engines)
        except Exception as e:  # 显式记录执行异常，不静默
            result.steps_tried.append({
                "level": step.level, "strategy": step.strategy,
                "query": step.query, "engines": step.engines,
                "outcome": f"executor-error: {type(e).__name__}",
            })
            continue
        goods = [r for r in (hits or [])
                 if isinstance(r, dict) and "error" not in r
                 and _result_has_query_signal(step.query, r)]
        result.steps_tried.append({
            "level": step.level, "strategy": step.strategy,
            "query": step.query, "engines": step.engines,
            "outcome": f"{len(goods)} results",
        })
        if goods:
            result.recovered = True
            result.level_used = step.level
            result.strategy_used = step.strategy
            result.final_query = step.query
            result.note = f"经 {step.level}/{step.strategy} 恢复成功"
            return goods, result

    result.note = "所有恢复策略均无结果"
    return [], result


# ── CLI 测试 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    tests = sys.argv[1:] if len(sys.argv) > 1 else [
        '"贵州茅台" 2025 最新财报 详细',
        "英伟达 财报 请问怎么看",
        "React 教程 入门",
    ]
    for q in tests:
        print(f"原始: {q}")
        print(f"  L1 放宽: {relax_query(q)}")
        print(f"  L2 同义: {synonym_expand(q)}")
        print(f"  L4 译词: {translate_heuristic(q)}")
        plan = build_recovery_plan(q, ["anysearch"], ["duckduckgo", "wikipedia"],
                                   mode="auto")
        for s in plan:
            print(f"  [{s.level}] {s.strategy}: q='{s.query}' engines={s.engines}")
        print("-" * 60)
