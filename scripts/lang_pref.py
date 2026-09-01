#!/usr/bin/env python3
"""lang_pref.py — 搜索语言偏好：默认中英 + 系统语言 + 使用习惯

像系统软件的「界面语言」与「内容语言」分层一样，搜索侧拆成：

  1. 本轮查询语言（强信号）—— 永远驱动 setlang / 路由 / 引擎选择
  2. 用户使用习惯（滑动窗口）—— 持续使用某语种后提升该语种偏好
  3. 系统 locale —— 冷启动种子（macOS/Linux LANG、LC_ALL）
  4. 内置基线 zh + en —— 永不剔除，中英双轨覆盖全球主流量

原则：
  - 强查询信号（zh/ja/ko/非拉丁等）以查询为准，习惯/系统只作补充与观测
  - 弱信号（mixed / other / 空）才用 prefer_langs[0] 驱动引擎语言参数
  - 中英基线始终在 prefer 列表中，习惯学到 ja 也不会丢掉 zh/en
  - 纯本地、仅 stdlib；状态写 <状态目录>/lang_habit.json（由 argo_paths 派生）
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

# ── 路径 / 常量 ──────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    """状态目录（惰性派生，支持 ARGO_STATE_DIR 覆盖）。"""
    import argo_paths
    return argo_paths.ensure_state_dir()


STATE_DIR = _state_dir()  # 兼容旧引用
STATE_PATH = STATE_DIR / "lang_habit.json"

# 全球默认：中英双语基线（永不剔除）
BASELINE_LANGS: tuple[str, ...] = ("zh", "en")

# 习惯生效阈值：至少 N 条样本且主导语种占比 ≥ 阈值
HABIT_MIN_SAMPLES = 5
HABIT_MIN_RATIO = 0.55
HABIT_WINDOW = 80  # 滑动窗口最大事件数

# 与 lang_detect 对齐的可学习标签（排除 mixed/other 噪声）
_TRACKABLE = frozenset({
    "zh", "en", "ja", "ko", "latin", "cyrillic", "thai",
    "arabic", "hebrew", "greek", "devanagari",
})

# 弱信号：引擎语言参数可回落到偏好语
WEAK_QUERY_LANGS = frozenset({"mixed", "other", ""})

# locale 前缀 → argo 语言标签
_LOCALE_PREFIX: list[tuple[str, str]] = [
    ("zh", "zh"),
    ("ja", "ja"),
    ("ko", "ko"),
    ("en", "en"),
    ("ru", "cyrillic"),
    ("uk", "cyrillic"),
    ("bg", "cyrillic"),
    ("th", "thai"),
    ("ar", "arabic"),
    ("he", "hebrew"),
    ("iw", "hebrew"),
    ("el", "greek"),
    ("hi", "devanagari"),
    ("mr", "devanagari"),
    ("ne", "devanagari"),
    # 西欧带变音：归 latin（与 detect_language 一致）
    ("fr", "latin"),
    ("de", "latin"),
    ("es", "latin"),
    ("it", "latin"),
    ("pt", "latin"),
    ("nl", "latin"),
    ("sv", "latin"),
    ("pl", "latin"),
    ("tr", "latin"),
    ("vi", "latin"),
    ("id", "latin"),
    ("ms", "latin"),
]

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_mtime: float = 0.0


# ── 系统 locale ──────────────────────────────────────────────────────────────

def _raw_locale() -> str:
    """读取进程环境语言标签（不调用 setlocale，避免改全局状态）。"""
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = (os.environ.get(key) or "").strip()
        if val and val not in ("C", "POSIX"):
            return val
    try:
        import locale
        loc = locale.getlocale()
        if loc and loc[0]:
            return loc[0]
    except Exception:
        pass
    return ""


def system_lang(raw: str | None = None) -> str:
    """系统语言 → argo 标签；无法判定时返回空串（不冒充 en）。"""
    s = (raw if raw is not None else _raw_locale()).strip()
    if not s:
        return ""
    # zh_CN.UTF-8 / ja-JP / en_US → 取前缀
    s = s.replace("-", "_")
    m = re.match(r"^([A-Za-z]{2,3})", s)
    if not m:
        return ""
    prefix = m.group(1).lower()
    for p, tag in _LOCALE_PREFIX:
        if prefix == p:
            return tag
    return ""


# ── 习惯持久化 ───────────────────────────────────────────────────────────────

def _empty_state() -> dict[str, Any]:
    return {"version": 1, "events": [], "updated_at": 0.0}


def _load_state() -> dict[str, Any]:
    global _cache, _cache_mtime
    with _lock:
        try:
            if STATE_PATH.exists():
                mtime = STATE_PATH.stat().st_mtime
                if _cache is not None and mtime == _cache_mtime:
                    return _cache
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = _empty_state()
                data.setdefault("events", [])
                data.setdefault("version", 1)
                _cache = data
                _cache_mtime = mtime
                return data
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        _cache = _empty_state()
        _cache_mtime = 0.0
        return _cache


def _save_state(state: dict[str, Any]) -> None:
    global _cache, _cache_mtime
    with _lock:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            state["updated_at"] = time.time()
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(STATE_PATH)
            _cache = state
            _cache_mtime = STATE_PATH.stat().st_mtime
        except OSError:
            pass  # 偏好写失败不阻断搜索


def record_query_lang(lang: str) -> None:
    """记录一次查询主语言（仅可追踪标签；mixed/other 忽略）。"""
    if not lang or lang not in _TRACKABLE:
        return
    state = _load_state()
    events = list(state.get("events") or [])
    events.append({"lang": lang, "ts": time.time()})
    if len(events) > HABIT_WINDOW:
        events = events[-HABIT_WINDOW:]
    state["events"] = events
    _save_state(state)


def dominant_habit(
    min_samples: int = HABIT_MIN_SAMPLES,
    min_ratio: float = HABIT_MIN_RATIO,
    events: list[dict[str, Any]] | None = None,
) -> Optional[str]:
    """滑动窗口主导语种；样本不足或不够集中时返回 None。"""
    if events is None:
        events = list((_load_state().get("events") or []))
    if len(events) < min_samples:
        return None
    counts: dict[str, int] = {}
    for e in events:
        lang = e.get("lang") if isinstance(e, dict) else None
        if lang in _TRACKABLE:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    top_lang, top_n = max(counts.items(), key=lambda x: x[1])
    total = sum(counts.values())
    if total < min_samples or top_n / total < min_ratio:
        return None
    return top_lang


def habit_counts() -> dict[str, int]:
    """调试/观测：各语种计数。"""
    counts: dict[str, int] = {}
    for e in (_load_state().get("events") or []):
        lang = e.get("lang") if isinstance(e, dict) else None
        if lang in _TRACKABLE:
            counts[lang] = counts.get(lang, 0) + 1
    return counts


# ── 偏好解析 ─────────────────────────────────────────────────────────────────

def prefer_langs(
    query_lang: str | None = None,
    *,
    system: str | None = None,
    habit: str | None = None,
    use_live_habit: bool = True,
) -> list[str]:
    """有序语言偏好列表（去重，基线 zh+en 垫底）。

    顺序：本轮强查询语 → 习惯主导语 → 系统语言 → 中英基线。

    Args:
        query_lang: 本轮 detect_language 结果；弱信号不前置。
        system: 注入系统语（测试用）；None 则现场读 locale。
        habit: 注入习惯语（测试用）；None 且 use_live_habit 时读持久化。
        use_live_habit: 是否读取磁盘习惯（单测可关）。
    """
    ordered: list[str] = []

    def _push(tag: str | None) -> None:
        if tag and tag not in ordered and tag in _TRACKABLE:
            ordered.append(tag)

    ql = (query_lang or "").strip()
    # 强查询信号前置；弱信号留给习惯/系统
    if ql and ql not in WEAK_QUERY_LANGS:
        _push(ql)

    if habit is not None:
        _push(habit)
    elif use_live_habit:
        _push(dominant_habit())

    sys_tag = system if system is not None else system_lang()
    _push(sys_tag)

    for b in BASELINE_LANGS:
        _push(b)

    # 极端兜底：若全部过滤空，仍返回中英
    return ordered or list(BASELINE_LANGS)


def effective_engine_lang(query_lang: str, preferred: list[str] | None = None) -> str:
    """决定引擎 setlang/hl 用的语言标签。

    强查询语 → 原样；弱信号 → prefer_langs 首项（默认 en，因基线含 en）。
    """
    ql = (query_lang or "").strip()
    if ql and ql not in WEAK_QUERY_LANGS:
        return ql
    prefs = preferred if preferred is not None else prefer_langs(query_lang=ql)
    for p in prefs:
        if p in _TRACKABLE:
            return p
    return "en"


def is_weak_query_lang(lang: str) -> bool:
    return (lang or "") in WEAK_QUERY_LANGS


def lang_pref_snapshot(query_lang: str | None = None) -> dict[str, Any]:
    """供 search 结果顶层观测字段。"""
    sys_tag = system_lang()
    habit = dominant_habit()
    prefs = prefer_langs(query_lang=query_lang, system=sys_tag, habit=habit,
                         use_live_habit=False)
    return {
        "query_lang": query_lang or "",
        "system_lang": sys_tag,
        "habit_lang": habit or "",
        "prefer_langs": prefs,
        "baseline": list(BASELINE_LANGS),
        "engine_lang": effective_engine_lang(query_lang or "", prefs),
        "habit_counts": habit_counts(),
        "habit_samples": sum(habit_counts().values()),
    }


def reset_habit_for_tests() -> None:
    """测试用：清空内存与磁盘习惯。"""
    global _cache, _cache_mtime
    with _lock:
        _cache = _empty_state()
        _cache_mtime = 0.0
        try:
            if STATE_PATH.exists():
                STATE_PATH.unlink()
        except OSError:
            pass
