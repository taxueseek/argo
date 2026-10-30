#!/usr/bin/env python3
"""技能目录构建器 —— explainx 与 skills.sh 两个新源的接入实现。

为什么这两个走 L3 而不是 L1 声明式：它们的核心价值是**安装量**，而 L1 的
`output_map` 只能把单个上游字段映射成 `item_summary`（不支持模板拼接）。
把「安装 216877 次 · anthropics/skills」这样一行说清楚，只能自己拼——
否则用户搜「pdf」看到的是干巴巴的仓库名，判断不出哪个值得装。

两个源的口径不可比（跨源别混用）：
  · explainx  totalInstalls/weeklyInstalls —— 平台统计的安装数
  · skills.sh installs —— Vercel 官方安装遥测（头部技能 21 万+）
Argo 已有的 skillsmp（仓库星数）、clawhub（生态下载量）是另外两套口径。
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from typing import Any

from engines_base import safe_search, http_open, rank_score

# ── 面向英文技能目录（explainx/skills.sh/skillsmp/clawhub）的轻量中英概念映射 ──
_ZH_TO_EN_SKILL_TERMS: tuple[tuple[str, str], ...] = (
    ("架构图", "architecture diagram"),
    ("流程图", "flowchart diagram"),
    ("思维导图", "mindmap"),
    ("时序图", "sequence diagram"),
    ("画图", "diagram"),
    ("可视化", "visualization chart"),
    ("图表", "chart"),
    ("海报", "poster"),
    ("浏览器", "browser"),
    ("自动化", "automation"),
    ("记忆", "memory"),
    ("爬虫", "scraper crawl"),
    ("抓取", "fetch scrape"),
    ("网页", "web"),
    ("公众号", "wechat"),
    ("微信", "wechat"),
    ("视频", "video"),
    ("字幕", "caption subtitle"),
    ("论文", "academic paper"),
    ("学术", "academic"),
    ("文献", "literature citation"),
    ("金融", "finance stock"),
    ("股票", "stock"),
    ("审查", "code review"),
    ("体检", "audit health"),
    ("调试", "debug"),
    ("测试", "testing"),
    ("表格", "spreadsheet excel csv"),
    ("文档", "document"),
    ("技能", "skill"),
    # 通用高频补充（2026-10-31：实测「渲染」查询归一后只剩 remotion，映射盲区）
    ("渲染", "render"),
    ("翻译", "translation translate"),
    ("邮件", "email mail"),
    ("摘要", "summary summarize"),
    ("投资", "invest investing"),
    ("图片", "image"),
    ("下载", "download"),
    ("剪辑", "video editing"),
)


def _normalize_en_catalog_query(query: str) -> str:
    """将含中文的技能检索词对齐为英文目录可命中的英文查询词（纯英文原样返回）。"""
    q = (query or "").strip()
    if not q or q.isascii():
        return q
    en_bits: list[str] = re.findall(r"[A-Za-z0-9_+-]{2,}", q)
    remaining = q
    for zh, en in _ZH_TO_EN_SKILL_TERMS:
        if zh in remaining:
            if en not in en_bits:
                en_bits.append(en)
            remaining = remaining.replace(zh, " ")
    return " ".join(en_bits) if en_bits else q


def _skill_rank_score(rank: int, popularity: Any = 0, is_official: bool = False) -> float:
    """结合上游位次、对数饱和流行度先验 (log10(1+pop)) 与官方标记计算初始分。"""
    try:
        pop = max(0, int(popularity or 0))
    except (TypeError, ValueError):
        pop = 0
    pop_bonus = min(0.14, 0.026 * math.log10(1 + pop)) if pop > 0 else 0.0
    off_bonus = 0.04 if is_official else 0.0
    base = min(0.92, 0.72 + pop_bonus + off_bonus)
    return rank_score(base, rank)

# ── explainx.ai（12075 条，安装量 + 分类 + 官方标记）────────────────────────
_EXPLAINX_URL = "https://explainx.ai/api/skills"

# ── skills.sh（Vercel，57k+，官方安装遥测）──────────────────────────────────
_SKILLS_SH_URL = "https://www.skills.sh/api/search"
_SKILLS_SH_PAGE = "https://skills.sh/"

# ── SkillsMP（200 万+，仓库星数口径）────────────────────────────────────────
_SKILLSMP_URL = "https://skillsmp.com/api/v1/skills/search"

# ── ClawHub（OpenClaw 生态，下载量口径）─────────────────────────────────────
_CLAWHUB_URL = "https://clawhub.ai/api/search"
_CLAWHUB_PAGE = "https://clawhub.ai"


def _normalize_epoch(val: Any) -> str:
    """epoch → ISO8601 日期。上游两种量级：SkillsMP/ClawHub 的 updatedAt 是
    **毫秒**（13 位），skills.sh 系是秒——按位数归一，避免 1000 倍错位。"""
    try:
        n = int(val)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    # 13 位（毫秒）vs 10 位（秒）：毫秒转秒
    if n > 10_000_000_000:
        n //= 1000
    try:
        import time as _t

        return _t.strftime("%Y-%m-%d", _t.gmtime(n))
    except (OverflowError, OSError, ValueError):
        return ""


def _get_json(url: str, timeout: float, engine: str) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "argo-search/1.0", "Accept": "application/json"}
    )
    with http_open(req, timeout=timeout, engine=engine) as resp:
        return json.loads(resp.read())


def _build_explainx_engine(spec: dict[str, Any]) -> Any:
    """explainx.ai —— 带安装量/分类/官方标记的技能目录。

    字段口径（跨源不可比）：totalInstalls/weeklyInstalls 是平台安装统计；
    isOfficial 标记官方出品；category 是平台分类（Productivity/Frontend/…）。
    条目只给 repoOwner/repoName（GitHub 仓），无技能在仓内的子路径，故 URL
    只能到仓级别——这是上游数据的边界，不是取舍。
    """
    timeout = float(spec.get("timeout", 15))

    @safe_search
    def _engine(
        query: str, n: int = 5, _timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = _normalize_en_catalog_query(query)
        if not q:
            return []
        url = f"{_EXPLAINX_URL}?q={urllib.parse.quote(q)}&limit={max(1, n)}"
        data = _get_json(url, to, "explainx")
        skills = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(skills, list):
            return []

        results: list[dict[str, Any]] = []
        for rank, s in enumerate(skills[: max(1, n)]):
            if not isinstance(s, dict):
                continue
            owner = s.get("repoOwner") or ""
            repo = s.get("repoName") or ""
            if not owner or not repo:
                continue
            installs = s.get("totalInstalls") or 0
            weekly = s.get("weeklyInstalls")
            category = s.get("category") or ""
            is_official = bool(s.get("isOfficial"))
            # 安装量是这一源的核心价值，必须出现在摘要里（L1 做不到）
            bits = [f"安装 {installs}"]
            if weekly is not None and weekly != installs:
                bits.append(f"本周 +{weekly}")
            if is_official:
                bits.append("官方")
            summary = " · ".join(bits)
            if category:
                summary = f"{category}｜{summary}"
            results.append(
                {
                    "title": s.get("name") or "",
                    "url": f"https://github.com/{owner}/{repo}",
                    "snippet": summary,
                    "source": "explainx",
                    "score": _skill_rank_score(rank, installs, is_official),
                    # 可验证出处：owner/repo 是「装什么」的权威答案
                    "upstream": f"{owner}/{repo}",
                    "metadata": {
                        "category": category,
                        "totalInstalls": installs,
                        "weeklyInstalls": weekly,
                        "isOfficial": is_official,
                    },
                }
            )
        results.sort(key=lambda r: -r["score"])
        return results

    return _engine


def _build_skillsmp_engine(spec: dict[str, Any]) -> Any:
    """SkillsMP —— 200 万+ 开源技能独立索引（原 L1 声明式，改 L3 补字段）。

    为什么改 L3：上游有 `stars`（源仓库星数）与 `githubUrl`（**能安装**的
    GitHub 目录），而 L1 的 output_map 只能把单字段映射成 summary，透不出
    这两个维度。星数是「这个技能多受欢迎」的唯一硬指标，githubUrl 比市场页
    更接近「装什么」——两者都值得进结果。

    口径：stars 是**源仓库星数**（不是安装量），与 skills.sh 的 Vercel 遥测、
    explainx 的平台统计互不可比。
    """
    timeout = float(spec.get("timeout", 15))

    @safe_search
    def _engine(
        query: str, n: int = 5, _timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = _normalize_en_catalog_query(query)
        if not q:
            return []
        url = (
            f"{_SKILLSMP_URL}?q={urllib.parse.quote(q)}"
            f"&limit={max(1, n)}&sortBy=stars"
        )
        data = _get_json(url, to, "skillsmp")
        skills = (data.get("data") or {}).get("skills") if isinstance(data, dict) else None
        if not isinstance(skills, list):
            return []

        results: list[dict[str, Any]] = []
        for rank, s in enumerate(skills[: max(1, n)]):
            if not isinstance(s, dict):
                continue
            # 优先给「能装」的 GitHub 目录；上游缺失时回落市场页
            github = s.get("githubUrl") or ""
            market = s.get("skillUrl") or ""
            url_final = github or market
            if not url_final:
                continue
            stars = s.get("stars") or 0
            summary = str(s.get("description") or "")
            if stars:
                summary = f"★ {stars}｜{summary}" if summary else f"★ {stars}"
            results.append(
                {
                    "title": s.get("name") or "",
                    "url": url_final,
                    "snippet": summary[:400],
                    "source": "skillsmp",
                    "score": _skill_rank_score(rank, stars, False),
                    "published_at": _normalize_epoch(s.get("updatedAt")),
                    "upstream": github or market,
                    "metadata": {
                        "stars": stars,
                        "author": s.get("author") or "",
                        "contentLanguage": s.get("contentLanguage") or "",
                    },
                }
            )
        results.sort(key=lambda r: -r["score"])
        return results

    return _engine


def _build_clawhub_engine(spec: dict[str, Any]) -> Any:
    """ClawHub —— OpenClaw 技能生态聚合（原 L1 声明式，改 L3 补字段）。

    为什么改 L3：上游有 `downloads`（生态下载量）与 `official`（官方标记），
    L1 透不出。下载量是这一源独有的口径（skills.sh 是 Vercel 遥测、
    SkillsMP 是仓库星数），值得单列。

    口径：downloads 是 ClawHub 生态下载量，与其它源的安装量互不可比。
    """
    timeout = float(spec.get("timeout", 15))

    @safe_search
    def _engine(
        query: str, n: int = 5, _timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = _normalize_en_catalog_query(query)
        if not q:
            return []
        url = f"{_CLAWHUB_URL}?q={urllib.parse.quote(q)}&limit={max(1, n)}"
        data = _get_json(url, to, "clawhub")
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        results: list[dict[str, Any]] = []
        for rank, s in enumerate(items[: max(1, n)]):
            if not isinstance(s, dict):
                continue
            canonical = s.get("canonicalUrl") or ""
            if not canonical:
                continue
            downloads = s.get("downloads") or 0
            official = bool(s.get("official"))
            summary = str(s.get("summary") or "")
            bits = [f"下载 {downloads}"]
            if official:
                bits.append("官方")
            summary = f"{' · '.join(bits)}｜{summary}" if summary else " · ".join(bits)
            results.append(
                {
                    "title": s.get("displayName") or "",
                    "url": f"https://clawhub.ai{canonical}",
                    "snippet": summary[:400],
                    "source": "clawhub",
                    "score": _skill_rank_score(rank, downloads, official),
                    "published_at": _normalize_epoch(s.get("updatedAt")),
                    # install.reference 是 owner/slug 形式的安装引用
                    "upstream": ((s.get("install") or {}).get("reference")) or "",
                    "metadata": {
                        "downloads": downloads,
                        "official": official,
                        "owner": s.get("owner") or "",
                        "slug": s.get("slug") or "",
                        "version": s.get("version") or "",
                    },
                }
            )
        results.sort(key=lambda r: -r["score"])
        return results

    return _engine


def _build_skills_sh_engine(spec: dict[str, Any]) -> Any:
    """skills.sh（Vercel）—— 官方安装遥测口径的技能目录。

    为什么 clawhub 不够：clawhub 声称聚合了 skills.sh 条目，但**聚合时丢了
    installs 字段**（实测 clawhub 返回里只有 title/url/snippet/score）。
    安装量是判断「这个技能有多少人在用」的唯一硬指标，故单列一源。

    字段口径：installs 是 Vercel 官方遥测（头部 pdf 技能 21 万+），与
    explainx 的平台统计、skillsmp 的仓库星数互不可比。
    """
    timeout = float(spec.get("timeout", 15))

    @safe_search
    def _engine(
        query: str, n: int = 5, _timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = _normalize_en_catalog_query(query)
        if not q:
            return []
        url = f"{_SKILLS_SH_URL}?q={urllib.parse.quote(q)}&limit={max(1, n)}"
        data = _get_json(url, to, "skills_sh")
        skills = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(skills, list):
            return []

        results: list[dict[str, Any]] = []
        for rank, s in enumerate(skills[: max(1, n)]):
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or ""
            if not sid:
                continue
            installs = s.get("installs") or 0
            source = s.get("source") or ""
            results.append(
                {
                    "title": s.get("name") or "",
                    # id 是 owner/repo/skill 三段式，直接拼站内页（实测 200）
                    "url": f"{_SKILLS_SH_PAGE}{sid}",
                    "snippet": f"安装 {installs} · {source}",
                    "source": "skills_sh",
                    "score": _skill_rank_score(rank, installs, False),
                    # 可验证出处：三段式 id 指向技能本身（比市场页更精确）
                    "upstream": sid,
                    "metadata": {
                        "installs": installs,
                        "source": source,
                    },
                }
            )
        results.sort(key=lambda r: -r["score"])
        return results

    return _engine
