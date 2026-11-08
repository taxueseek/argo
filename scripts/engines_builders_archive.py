#!/usr/bin/env python3
"""专用构建器：法律文书、专利、公版书与网页存档（CourtListener / 裁判文书 / Gutenberg / Wayback / Google Patents）

自 engines_builders_data.py 按主题拆出（2026-11-08）。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from engines_base import (
    rank_score,
    safe_search, _detect_anti_bot,
    http_open,
)

logger = logging.getLogger("unified_search.engines")

# ── 中国裁判文书网引擎（HTML/JSON 抓取）──────────────────────────────────────────

def _build_wenshu_engine(spec: dict[str, Any]) -> Any:
    """中国裁判文书网（wenshu.court.gov.cn）法律文书检索。

    该站有较强反爬（参数加密），此处走公开列表接口，尽力而为；
    失败或被拦截时显式返回 []（不伪造结果）。
    提取：案号、案由、裁判日期、法院。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        # 公开检索页（列表 HTML）——政府站结构可能变化，解析失败即空
        url = f"https://wenshu.court.gov.cn/website/wenshu/181217BMTKHNT2W0/index.html?q={up.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://wenshu.court.gov.cn/",
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"裁判文书网访问失败: {e}")
            return []
        if _detect_anti_bot(html):
            return []

        results: list[dict[str, Any]] = []
        # 尝试解析嵌入的 JSON 数据块（站点结构变化时降级为空）
        blocks = re.findall(r'\{[^{}]*"caseName"[^{}]*\}', html)
        for b in blocks[:n]:
            try:
                item = json.loads(b)
            except Exception:
                continue
            results.append({
                "title": item.get("caseName", "")[:100],
                "url": "https://wenshu.court.gov.cn/",
                "snippet": (f"案号 {item.get('caseNo', '')} | 案由 {item.get('caseType', '')} | "
                            f"法院 {item.get('court', '')} | 裁判日期 {item.get('judgeDate', '')}")[:300],
                "source": "wenshu",
                "facts": {"case_no": item.get("caseNo", ""), "court": item.get("court", ""),
                          "judge_date": item.get("judgeDate", "")},
            })
        return results
    return _engine

# ── CourtListener 美国判例引擎 ───────────────────────────────────────────────

_COURTLISTENER_API = "https://www.courtlistener.com/api/rest/v4/search/"


def _build_courtlistener_engine(spec: dict[str, Any]) -> Any:
    """CourtListener 美国判例检索（courtlistener.com/api/rest/v4，匿名可用）。

    注意 2026-05 起限额骤降（免费档约 5 次/分、125 次/天），qps 已压到 1。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = _COURTLISTENER_API + "?" + urllib.parse.urlencode({
            "q": q, "format": "json", "page_size": min(n, 10), "order_by": "score desc",
        })
        try:
            with http_open(urllib.request.Request(url, headers=_HEADERS), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"CourtListener 失败: {e}")
            return []
        results = []
        for _rk12, x in enumerate((data.get("results") or [])[:n]):
            name = x.get("caseName") or q
            rel = x.get("absolute_url") or ""
            date = (x.get("dateFiled") or "")[:10]
            snip = str(x.get("snippet") or "")[:280]
            body_txt = str(x.get("plain_text") or "")[:280]
            snip = snip or body_txt
            parts = [p for p in (date, snip) if p]
            results.append({
                "title": name,
                "url": f"https://www.courtlistener.com{rel}" if rel else "",
                "snippet": " · ".join(parts)[:300],
                "source": "courtlistener",
                "score": rank_score(0.9, _rk12),
            })
        return results
    return _engine


# ── Project Gutenberg 公版书引擎（Gutendex） ─────────────────────────────────

_GUTENDEX_API = "https://gutendex.com/books"


def _build_gutenberg_engine(spec: dict[str, Any]) -> Any:
    """Project Gutenberg 公版书检索（gutendex.com，免认证）。

    返回书名/作者/语言与 Gutenberg 书目页链接；Gutenberg 提供结构化全文。
    """
    timeout = spec.get("timeout", 15)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = _GUTENDEX_API + "?" + urllib.parse.urlencode({"search": q, "page_size": min(n, 10)})
        try:
            with http_open(urllib.request.Request(url, headers=_HEADERS), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Gutendex 失败: {e}")
            return []
        results = []
        for _rk13, x in enumerate((data.get("results") or [])[:n]):
            title = x.get("title") or q
            gid = x.get("id") or ""
            authors = ", ".join((a.get("name") or "") for a in (x.get("authors") or [])[:2])
            langs = ",".join(x.get("languages") or [])
            fmt = x.get("formats") or {}
            txt = fmt.get("text/plain; charset=us-ascii") or fmt.get("text/html")
            snip = " · ".join(p for p in (authors, langs, "公版书全文") if p)
            _row = {
                "title": title,
                "url": f"https://www.gutenberg.org/ebooks/{gid}" if gid else txt or "",
                "snippet": snip[:300],
                "source": "gutenberg",
                "score": rank_score(0.85, _rk13),
            }
            # 源内全文直出：`/ebooks/{id}` 是下载门户页（实测首段全是 noprint
            # 脚本），不是正文；gutendex 本来就把纯文本 URL 放在 formats 里，
            # 此前只在缺 id 时当保底，正常路径直接丢掉。这里单独带上，供下游
            # 取正文时优先使用（省掉从门户页找下载链这一步）。
            if txt:
                _row["full_text_url"] = txt
            results.append(_row)
        return results
    return _engine


# ── Wayback CDX 已删除内容检索引擎 ───────────────────────────────────────────

_CDX_API = "https://web.archive.org/cdx/search/cdx"


def _build_wayback_cdx_engine(spec: dict[str, Any]) -> Any:
    """Wayback Machine CDX 快照检索（web.archive.org/cdx，免认证）。

    输入域名/URL，返回历史快照时间戳与状态码，是查已删除页面的唯一路径。
    """
    timeout = spec.get("timeout", 20)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip().lower()
        if not q:
            return []
        # 剥离常见协议前缀，CDX 用裸域名/host 查询
        q = re.sub(r"^https?://", "", q).strip()
        if not q:
            return []
        to = _timeout or timeout
        url = _CDX_API + "?" + urllib.parse.urlencode({
            "url": q, "output": "json", "limit": min(n, 10),
            "fl": "timestamp,original,statuscode",
        })
        try:
            with http_open(urllib.request.Request(url, headers=_HEADERS), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Wayback CDX 失败: {e}")
            return []
        rows = data if isinstance(data, list) else []
        # 带 fl 时首行是列名
        if rows and rows[0] and rows[0][0] == "timestamp":
            rows = rows[1:]
        results = []
        for _rk14, r in enumerate(rows[:n]):
            if len(r) < 3:
                continue
            ts, orig, status = r[0], r[1], r[2]
            # CDX 时间戳 YYYYMMDDHHMMSS → 标准 published_at（最早快照语义）
            published_at = ""
            try:
                published_at = datetime.strptime(ts, "%Y%m%d%H%M%S").isoformat(timespec="seconds")
            except ValueError:
                pass
            item = {
                "title": f"快照 {orig}（{ts[:8]}）",
                "url": f"https://web.archive.org/web/{ts}/{orig}",
                "snippet": f"Wayback 快照 {ts} · HTTP {status}"[:300],
                "source": "wayback_cdx",
                "score": rank_score(0.8, _rk14),
            }
            if published_at:
                item["published_at"] = published_at
            results.append(item)
        return results
    return _engine


# ── Google Patents 专利搜索 ──────────────────────────────────────────────────

def _build_google_patents_engine(spec: dict[str, Any]) -> Any:
    """Google Patents 专利搜索（patents.google.com/xhr/query，免认证）。

    技术尽调/竞品分析：专利标题、公开号、公开日期、申请人。
    用 patents.google.com 的公开 XHR 接口，免 key、返回 JSON。
    argo 现有引擎无专利数据源，这是技术尽调的核心空白。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = query.strip()
        # 去常见修饰语
        q2 = re.sub(r"(?i)\b(patent|专利|查询|搜索|检索)\b", " ", q)
        q2 = re.sub(r"\s+", " ", q2).strip() or q
        url = "https://patents.google.com/xhr/query"
        params = {
            "url": f"q={urllib.parse.quote(q2)}",
            "exp": "",
        }
        full_url = url + "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "application/json",
        }
        try:
            with http_open(urllib.request.Request(full_url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        results: list[dict[str, Any]] = []
        try:
            cluster = (data.get("results") or {}).get("cluster") or []
            for c in cluster:
                for r in (c.get("result") or []):
                    if len(results) >= n:
                        break
                    pat = r.get("patent") or {}
                    title = pat.get("title") or ""
                    if not title:
                        continue
                    pub_date = pat.get("publication_date") or ""
                    pub_num = pat.get("publication_number") or ""
                    assignee = (pat.get("assignee") or [{}])[0].get("name") if isinstance(pat.get("assignee"), list) else ""
                    pub_id = pat.get("publication_number") or pub_num or ""
                    results.append({
                        "title": title[:200],
                        "url": f"https://patents.google.com/patent/{pub_id}" if pub_id else "",
                        "snippet": (r.get("snippet") or "")[:300],
                        "source": "google_patents",
                        "score": rank_score(0.8, len(results)),
                        "publication_date": pub_date,
                        "publication_number": pub_num,
                        "assignee": assignee,
                    })
        except Exception:
            pass
        return results
    return _engine
