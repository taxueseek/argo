#!/usr/bin/env python3
"""evidence_grounding.py — 确定性原文摘录锚定、防编造数字门禁与量级单位归一化。

纯确定性证据锚定与量级换算内核（零 LLM 依赖）：
  1. fold_digits：NFKC 折叠全半角数字与千分位分隔符；
  2. check_ungrounded_numbers：≥3 位数字必须在原文中真实存在，拦截「跨页套用模板数字」；
  3. extract_anchored_context：优先在正文（跳过前导元信息）取距离实体最近的数值窗口，
     输出字符 span 与 sha1 摘录哈希，防止页面通用词（如多处价格/评分）张冠李戴；
  4. normalize_magnitude_unit：保守校准中英量级（万/亿/million/billion 等），仅当数值
     原样抄录原文裸数字且邻近量级词与目标列单位不一致时换算，杜绝二次误换算。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

_CJK_MAGNITUDES: dict[str, float] = {
    "千": 1e3,
    "万": 1e4,
    "十万": 1e5,
    "百万": 1e6,
    "千万": 1e7,
    "亿": 1e8,
    "十亿": 1e9,
    "百亿": 1e10,
    "千亿": 1e11,
    "万亿": 1e12,
}

_EN_MAGNITUDES: dict[str, float] = {
    "thousand": 1e3,
    "million": 1e6,
    "millions": 1e6,
    "billion": 1e9,
    "billions": 1e9,
    "trillion": 1e12,
    "trillions": 1e12,
}

_ALL_MAGNITUDES: dict[str, float] = {**_CJK_MAGNITUDES, **_EN_MAGNITUDES}

_NUM_RE = r"[-+]?\d[\d,，]*(?:\.\d+)?"
_UNIT_ALT = (
    "万亿|千亿|百亿|十亿|千万|百万|十万|亿|万|千"
    "|trillions|trillion|billions|billion|millions|million|thousand"
)
_NUM_UNIT_PAIR_RE = re.compile(rf"({_NUM_RE})\s*({_UNIT_ALT})", re.IGNORECASE)
_TARGET_UNIT_RE = re.compile(rf"({_UNIT_ALT})", re.IGNORECASE)
_PREAMBLE_RE = re.compile(r"Markdown Content:\s*")


def fold_digits(text: str) -> str:
    """NFKC 折叠全半角并去除千分位逗号（全/半角）。"""
    folded = unicodedata.normalize("NFKC", text or "")
    return folded.replace(",", "").replace("，", "")


def check_ungrounded_numbers(page_text: str, value: str, *, min_digits: int = 3) -> list[str]:
    """检查 value 中所有 ≥min_digits 位的数字是否真实出现在 page_text 中。

    返回未在原文中出现的数字 token 列表；空列表表示通过。
    仅检 ≥3 位数字，避免日期/序号/小评分产生误报。
    """
    if not value:
        return []
    tokens = [t for t in re.findall(r"\d+", fold_digits(value)) if len(t) >= min_digits]
    if not tokens:
        return []
    folded_page = fold_digits(page_text)
    return [t for t in dict.fromkeys(tokens) if t not in folded_page]


def extract_anchored_context(
    page_text: str,
    value: str,
    *,
    entity: str = "",
    window: int = 120,
) -> dict[str, Any]:
    """在原文中定位距离 entity 最近的 value 窗口，返回 {excerpt, span, excerpt_sha1}。"""
    if not page_text or not value:
        return {"excerpt": "", "span": None, "excerpt_sha1": ""}

    haystack = page_text
    preamble = _PREAMBLE_RE.search(haystack)
    body_start = preamble.end() if preamble else 0

    def _positions(needle: str) -> list[int]:
        if not needle or len(needle.strip()) < 2:
            return []
        exact = [m.start() for m in re.finditer(re.escape(needle), haystack)]
        if exact:
            return exact
        low_h = haystack.lower()
        return [m.start() for m in re.finditer(re.escape(needle.lower()), low_h)]

    def _prefer_body(positions: list[int]) -> list[int]:
        in_body = [p for p in positions if p >= body_start]
        return in_body or positions

    val_pos = _prefer_body(_positions(value))
    if not val_pos:
        # 尝试折叠千分位后的数字在原文中定位
        folded_val = fold_digits(value).strip()
        if folded_val and folded_val != value:
            val_pos = _prefer_body(_positions(folded_val))

    if not val_pos:
        return {"excerpt": "", "span": None, "excerpt_sha1": ""}

    ent_pos = _prefer_body(_positions(entity)) if entity else []
    if ent_pos:
        anchor = min(val_pos, key=lambda v: min(abs(v - e) for e in ent_pos))
    else:
        anchor = val_pos[0]

    start = max(0, anchor - window)
    end = min(len(haystack), anchor + len(value) + window)
    excerpt = " ".join(haystack[start:end].split())
    sha1 = f"sha1:{hashlib.sha1(excerpt.encode('utf-8')).hexdigest()}"
    return {
        "excerpt": excerpt,
        "span": [start, end],
        "excerpt_sha1": sha1,
    }


def _parse_float(raw: Any) -> float | None:
    try:
        return float(fold_digits(str(raw)).strip())
    except (TypeError, ValueError):
        return None


def _format_num(val: float) -> str:
    if abs(val - round(val)) < 1e-9:
        return str(int(round(val)))
    return f"{val:.6f}".rstrip("0").rstrip(".")


def normalize_magnitude_unit(
    value: str | float | int,
    excerpt: str,
    target_unit: str,
) -> dict[str, Any]:
    """保守量级换算：把原样抄录的源数字按 target_unit 换算。

    例：excerpt="$131 billion", value="131", target_unit="亿美元" → normalized="1310"。
    若 value 已经换算过（不再等于 excerpt 中的源裸数字），保持原值不变。
    """
    raw_str = str(value).strip()
    num = _parse_float(raw_str)
    if num is None or not excerpt or not target_unit:
        return {"normalized": raw_str, "changed": False, "factor": 1.0}

    tm = _TARGET_UNIT_RE.search(target_unit)
    if not tm:
        return {"normalized": raw_str, "changed": False, "factor": 1.0}
    target_scale = _ALL_MAGNITUDES.get(tm.group(1).lower())
    if not target_scale:
        return {"normalized": raw_str, "changed": False, "factor": 1.0}

    for m in _NUM_UNIT_PAIR_RE.finditer(excerpt):
        src_num = _parse_float(m.group(1))
        src_unit = m.group(2).lower()
        src_scale = _ALL_MAGNITUDES.get(src_unit)
        if src_num is None or not src_scale:
            continue
        # 仅当 value 仍等于源裸数字（未换算）且源/目标量级不同时才做换算
        if abs(src_num - num) <= 1e-6 and abs(src_scale - target_scale) > 1e-6:
            factor = src_scale / target_scale
            scaled = num * factor
            return {
                "normalized": _format_num(scaled),
                "changed": True,
                "factor": factor,
                "source_unit": m.group(2),
                "target_unit": tm.group(1),
            }
    return {"normalized": raw_str, "changed": False, "factor": 1.0}
