#!/usr/bin/env python3
"""专用构建器：影视、音乐播客与体育（IMDb / iTunes / TheSportsDB）

自 engines_builders_data.py 按主题拆出（2026-11-08）。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as _unescape
from typing import Any

from engines_base import (
    rank_score,
    safe_search, http_open,
)

logger = logging.getLogger("unified_search.engines")

# ── IMDb 影视 suggestion API（免认证） ────────────────────────────────────────

_IMDB_SUGGEST = "https://v2.sg.media-imdb.com/suggestion/{first}/{q}.json"
_IMDB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# 影视问句噪声词：整句进 suggestion 常 0 命中（如 "Interstellar film cast"）
_IMDB_STOP = re.compile(
    r"(?i)\b(movie|film|tv\s*show|tv\s*series|cinema|cast|starring|director|directed\s+by|"
    r"screenwriter|written\s+by|actor|actress|producer|box\s*office|trailer|review|"
    r"imdb|豆瓣|电影|电视剧|剧集|影视|影片|导演|主演|演员|编剧|制片|上映|首映|票房|"
    r"影评|奥斯卡|金像奖|金马奖|网剧|美剧|韩剧|日剧|英剧)\b"
)


def _imdb_clean_query(query: str) -> str:
    """去掉影视角色/事实词，保留片名/人名主体。"""
    q = (query or "").strip()
    if not q:
        return ""
    cleaned = _IMDB_STOP.sub(" ", q)
    cleaned = re.sub(r"[?？!！,，.。:：;；]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 清洗后过短则回退原 query（避免只剩空）
    if len(cleaned) < 2:
        return q
    return cleaned


def _build_imdb_engine(spec: dict[str, Any]) -> Any:
    """IMDb 自动补全 suggestion API（v2.sg.media-imdb.com，免认证）。

    路径：/suggestion/{首字符}/{query}.json
    返回电影/剧集/人物，带 tt/nm ID，可拼正式 IMDb URL。
    """
    timeout = spec.get("timeout", 8)
    source_name = spec.get("_name", "imdb")

    def _fetch_suggest(q: str, to: float) -> list[dict[str, Any]]:
        if not q:
            return []
        first = q[0].lower()
        if not (("a" <= first <= "z") or ("0" <= first <= "9")):
            first = "_"
        path_q = urllib.parse.quote(q[:80], safe="")
        url = _IMDB_SUGGEST.format(first=first, q=path_q)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _IMDB_UA, "Accept": "application/json"})
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"IMDb 失败: {e}")
            return []
        return [x for x in (data.get("d") or []) if isinstance(x, dict)]

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        raw = (query or "").strip()
        if not raw:
            return []
        to = _timeout or timeout
        cleaned = _imdb_clean_query(raw)
        # 先试清洗词，空则回退原句
        items = _fetch_suggest(cleaned, to)
        if not items and cleaned != raw:
            items = _fetch_suggest(raw, to)

        # 按标题与查询 token 重叠 + feature 优先排序
        q_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", cleaned or raw)}
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            title = item.get("l") or ""
            iid = item.get("id") or ""
            if not title and not iid:
                continue
            t_low = title.lower()
            t_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", title)}
            overlap = len(q_tokens & t_tokens) / max(len(q_tokens), 1)
            # 精确/前缀加分；tt 影片优先于 nm 人名（片名查询时）
            exact = 1.0 if t_low == (cleaned or raw).lower() else 0.0
            prefix = 0.3 if t_low.startswith((cleaned or raw).lower()[: min(12, len(cleaned or raw))]) else 0.0
            kind_bonus = 0.15 if str(iid).startswith("tt") else (0.05 if str(iid).startswith("nm") else 0.0)
            year = item.get("y") or 0
            try:
                year_n = int(year) if year else 0
            except (TypeError, ValueError):
                year_n = 0
            # 近年作品轻微加分（同名旧片靠后）
            recency = min(max(year_n - 1980, 0), 50) / 500.0
            score = 0.5 + overlap + exact + prefix + kind_bonus + recency
            ranked.append((score, item))
        ranked.sort(key=lambda x: -x[0])

        results = []
        for score, item in ranked[: max(n, 1)]:
            title = item.get("l") or ""
            iid = item.get("id") or ""
            year = item.get("y")
            kind = item.get("q") or item.get("qid") or ""
            cast = item.get("s") or ""
            parts = [p for p in (str(year) if year else "", kind, cast) if p]
            if str(iid).startswith("tt"):
                href = f"https://www.imdb.com/title/{iid}/"
            elif str(iid).startswith("nm"):
                href = f"https://www.imdb.com/name/{iid}/"
            else:
                href = f"https://www.imdb.com/find/?q={urllib.parse.quote(title)}"
            results.append({
                "title": title,
                "url": href,
                "snippet": " · ".join(parts)[:300],
                "source": source_name,
                "score": min(0.95, 0.7 + score * 0.15),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── iTunes 媒体库（中文需 country=cn；专辑问 entity=album；播客问 entity=podcast）──

# 媒体意图词。引擎可被 --engine 直接调用，那时拿不到路由域的上下文，
# 意图只能从查询词本身读——命中播客词即把 media 从 music 切到 podcast。
_PODCAST_HINT_RE = re.compile(r"(?i)(播客|podcast|电台|单集|一期|这期|那期|episode)")
# 单集意图：用户要的是某期节目（带时长），而非节目本体
_PODCAST_EPISODE_RE = re.compile(r"(?i)(单集|一期|这期|那期|episode|第\s*\d+\s*集|#\s*\d+)")
_ALBUM_HINT_RE = re.compile(r"(?i)(专辑|album|discography|唱片)")
# 意图词只用于定实体，须从检索词里剥掉，否则「播客」这类词会污染 term
_MEDIA_INTENT_WORD_RE = re.compile(
    r"(?i)(专辑|新专辑|唱片|音乐|歌曲|单曲|播客|电台|单集|一期|这期|那期|"
    r"album|discography|music|song|songs|single|podcast|podcasts|episode)"
)
# 节目级时长必须二跳取样（见下方 docstring），逐条补会让请求数随结果数膨胀。
# 只补首条：播客名查询通常只有一个正解（实测「张小珺 商业访谈录」Apple 只回 1 条）。
_ITUNES_SHOW_ENRICH_MAX = 1
# 文字稿/feed 拉取的字节上限：预览只需几百字，2MB 足够防超大文件吃内存
_TRANSCRIPT_MAX_BYTES = 2 * 1024 * 1024


def _itunes_date(raw: Any) -> str:
    """Apple 的 releaseDate 形如 2026-09-03T00:00:00Z，裁到日期部分供展示。"""
    s = str(raw or "").strip()
    return s[:10] if len(s) >= 10 else ""


def _transcript_to_text(raw: str, ctype: str) -> str:
    """文字稿原文 → 纯文本。VTT/SRT 去时间轴与样式行，HTML 去 tag。

    Podcasting 2.0 文字稿的 type 值不统一（text/vtt、application/srt、
    裸 text/plain 都见过），按字符串包含判断而非全等。
    """
    if "html" in ctype:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = _unescape(text)
    elif "vtt" in ctype or "srt" in ctype or "plain" in ctype:
        lines: list[str] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if (not s or s == "WEBVTT"
                    or s.startswith(("NOTE", "STYLE", "REGION"))
                    or "-->" in s or s.isdigit()):
                continue
            s = re.sub(r"<[^>]+>", "", s)  # <c> 高亮、<v 说话人> 内联标签
            if s:
                lines.append(s)
        text = " ".join(lines)
    else:
        text = raw
    return re.sub(r"\s+", " ", text).strip()


def _build_itunes_engine(spec: dict[str, Any]) -> Any:
    """iTunes Search API：按语言与意图调 country / media / entity。

    三种媒体形态（播客为 2026-09-15 补入）：
      - 播客节目 media=podcast&entity=podcast → 集数(trackCount)、更新日期、类型
      - 播客单集 media=podcast&entity=podcastEpisode → 时长(trackTimeMillis)、发布日期
      - 音乐 media=music（专辑问 entity=album）

    播客时长只能取自单集：**节目对象上的 trackTimeMillis 不是时长**。实测 12 档
    节目只有 6 档等于最新单集的秒数，另 6 档明显偏小（忽左忽右 3491 而最新单集
    5739），单位与含义上游都未文档化——取信它会静默给出错值。所以节目级的
    「每期多长」走二跳单集取样，见 _recent_episode_duration。
    """
    timeout = spec.get("timeout", 8)
    source_name = spec.get("_name", "itunes")
    engine_tag = spec.get("_name", "")
    base = spec.get("url") or "https://itunes.apple.com/search"
    lookup_base = (base.replace("/search", "/lookup") if "/search" in base
                   else "https://itunes.apple.com/lookup")
    ua_headers = {"User-Agent": "argo-search/2.6 (itunes)", "Accept": "application/json"}

    def _get_json(url: str, to: float) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=ua_headers)
        with http_open(req, timeout=to, engine=engine_tag) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _recent_episode_duration(collection_id: Any, country: str, to: float) -> tuple[int | None, int]:
        """节目近 5 期单集时长的中位数 → (中位分钟, 样本数)。

        取中位数而非最新一期：访谈类单集长度波动大，一期 4 小时长访谈代表不了
        整个节目。任何失败一律退回 (None, 0)，节目本体照常返回——时长是补充
        信息，不该让整条结果缺失。
        """
        import statistics
        if not collection_id:
            return None, 0
        params = {"id": str(collection_id), "entity": "podcastEpisode", "limit": "5"}
        if country:
            params["country"] = country
        try:
            data = _get_json(f"{lookup_base}?{urllib.parse.urlencode(params)}", to)
        except Exception as e:
            logger.debug(f"iTunes 单集取样失败: {e}")
            return None, 0
        mins = []
        for it in data.get("results") or []:
            if not isinstance(it, dict):
                continue
            if str(it.get("wrapperType") or "").lower() != "podcastepisode":
                continue
            ms = it.get("trackTimeMillis")
            if isinstance(ms, (int, float)) and ms > 0:
                mins.append(ms / 60000.0)
        if not mins:
            return None, 0
        return int(round(statistics.median(mins))), len(mins)

    def _show_transcript(feed_url: str, to: float) -> tuple[str, str]:
        """节目官方文字稿（Podcasting 2.0）：feed 里 <podcast:transcript> 标签
        指向创作者自己挂的全文，不是 ASR 猜的——这是播客转录里唯一免费且
        准确的通道（英文生态采纳率高，中文节目普遍没挂）。

        取 feed 中第一份可解析格式的文字稿（最新一期）：搜索对象是「节目」，
        最新一期代表当前内容；历史各期的文字稿同挂在 feed，下游拿着
        transcript_url 可回读全文。任何失败一律 ("", "")，节目本体照常返回。
        application/json（Podcasting 2.0 的机器格式）本版不解析，静默跳过。
        """
        if not feed_url:
            return "", ""
        feed_url = str(feed_url).strip()
        if not feed_url.lower().startswith(("https://", "http://")):
            return "", ""
        try:
            req = urllib.request.Request(feed_url, headers=ua_headers)
            with http_open(req, timeout=to, engine=engine_tag) as resp:
                feed_xml = resp.read(_TRANSCRIPT_MAX_BYTES).decode("utf-8", "replace")
        except Exception as e:
            logger.debug(f"iTunes 文字稿 feed 拉取失败: {e}")
            return "", ""
        ok_types = ("text/vtt", "text/srt", "text/plain", "text/html")
        ok_exts = ("vtt", "srt", "txt", "html", "htm")
        for m in re.finditer(r"<podcast:transcript\b[^>]*>", feed_xml):
            tag = m.group(0)
            # 属性可能是单引号或双引号；type 可能带 ;charset= 参数
            url_m = re.search(r"""\burl=("([^"]+)"|'([^']+)')""", tag)
            if not url_m:
                continue
            raw_url = url_m.group(2) or url_m.group(3)
            type_m = re.search(r"""\btype=("([^"]+)"|'([^']+)')""", tag)
            ctype = (type_m.group(2) or type_m.group(3) if type_m else "").lower()
            ctype = ctype.split(";", 1)[0].strip()
            try:
                parsed = urllib.parse.urlsplit(raw_url)
                if parsed.scheme == "":
                    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(feed_url, raw_url))
                # 文字稿 URL 来自第三方 feed 内容：仅接受 https，防 file://
                # 读本地文件、防内网地址（SSRF）；urlsplit 对畸形输入抛
                # ValueError，一并拦下
                if parsed.scheme != "https" or not parsed.hostname:
                    continue
                t_url = urllib.parse.urlunsplit(parsed)
            except ValueError:
                continue
            if not ctype:
                ext = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path.rsplit("?", 1)[0] else ""
                if ext in ok_exts:
                    ctype = "text/plain" if ext in ("txt", "srt") else f"text/{ext}"
            if ctype not in ok_types:
                continue
            try:
                req = urllib.request.Request(t_url, headers=ua_headers)
                with http_open(req, timeout=to, engine=engine_tag) as resp:
                    raw = resp.read(_TRANSCRIPT_MAX_BYTES).decode("utf-8", "replace")
            except Exception as e:
                logger.debug(f"iTunes 文字稿下载失败: {e}")
                continue  # 这份坏了不放弃整条节目，试下一份候选
            text = _transcript_to_text(raw, ctype)
            if text:
                return t_url, text
        return "", ""

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        to = _timeout or timeout
        want_podcast = bool(_PODCAST_HINT_RE.search(q))
        want_episode = want_podcast and bool(_PODCAST_EPISODE_RE.search(q))
        want_album = bool(_ALBUM_HINT_RE.search(q)) and not want_podcast
        # 去掉意图词，保留实体名
        term = _MEDIA_INTENT_WORD_RE.sub(" ", q)
        term = re.sub(r"\s+", " ", term).strip() or q
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", term))
        country = "cn" if has_zh else ""
        params: dict[str, str] = {
            "term": term,
            "limit": str(min(max(n, 1), 25)),
        }
        if want_podcast:
            params["media"] = "podcast"
            params["entity"] = "podcastEpisode" if want_episode else "podcast"
        else:
            params["media"] = "music"
            if want_album:
                params["entity"] = "album"
        if country:
            params["country"] = country
            params["lang"] = "zh_cn"
        try:
            data = _get_json(f"{base}?{urllib.parse.urlencode(params)}", to)
        except Exception as e:
            logger.warning(f"iTunes 失败: {e}")
            return []
        items = data.get("results") or []
        # 相关度：实体名与 term 重叠。播客单集的标题里不含节目名，故比对
        # 「标题 + 摘要」——摘要首段就是《节目名》，否则搜节目名拿单集会全被滤掉。
        t_keys = {t.lower() for t in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", term)}
        base_score = 0.88 if want_album else 0.85
        out: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            is_episode = want_podcast and (
                str(it.get("wrapperType") or "").lower() == "podcastepisode"
                or (not it.get("wrapperType") and it.get("episodeUrl"))
            )
            if want_podcast and not is_episode:
                title = str(it.get("collectionName") or it.get("trackName") or "").strip()
                href = it.get("collectionViewUrl") or it.get("feedUrl") or ""
                count = it.get("trackCount")
                bits = ["播客"]
                if isinstance(count, int) and count > 0:
                    bits.append(f"{count} 集")
                genre = str(it.get("primaryGenreName") or "").strip()
                if genre:
                    bits.append(genre)
                updated = _itunes_date(it.get("releaseDate"))
                if updated:
                    bits.append(f"更新 {updated}")
                row: dict[str, Any] = {
                    "title": title,
                    "url": str(href or "").strip(),
                    "snippet": " · ".join(bits)[:300],
                    "source": source_name,
                }
                if isinstance(count, int) and count > 0:
                    row["episode_count"] = count
                # published_at 是既有的一等字段（--fields agent 与 --sort 都认它）。
                # 只给播客挂：音乐的 releaseDate 不在本次范围内，而挂上会改变
                # 音乐查询在 --sort newest 下的重排行为（releaseDate≠引擎相关序）。
                _published = str(it.get("releaseDate") or "").strip()
                if _published:
                    row["published_at"] = _published[:64]
            elif is_episode:
                title = str(it.get("trackName") or "").strip()
                show = str(it.get("collectionName") or "").strip()
                bits = [f"《{show}》"] if show else []
                ms = it.get("trackTimeMillis")
                row = {
                    "title": title,
                    "url": str(it.get("trackViewUrl") or it.get("episodeUrl") or "").strip(),
                    "snippet": "",
                    "source": source_name,
                }
                if isinstance(ms, (int, float)) and ms > 0:
                    mins = int(round(ms / 60000.0))
                    row["duration_minutes"] = mins
                    bits.append(f"时长 {mins} 分")
                pub_date = _itunes_date(it.get("releaseDate"))
                if pub_date:
                    bits.append(pub_date)
                _published = str(it.get("releaseDate") or "").strip()
                if _published:
                    row["published_at"] = _published[:64]
                row["snippet"] = " · ".join(bits)[:300]
            else:
                if want_album:
                    title = str(it.get("collectionName") or it.get("trackName") or "").strip()
                    href = it.get("collectionViewUrl") or it.get("trackViewUrl") or ""
                else:
                    title = str(it.get("trackName") or it.get("collectionName") or "").strip()
                    href = it.get("trackViewUrl") or it.get("collectionViewUrl") or ""
                row = {
                    "title": title,
                    "url": str(href or "").strip(),
                    "snippet": str(it.get("artistName") or "").strip()[:300],
                    "source": source_name,
                }
            if not row["title"] or not row["url"]:
                continue
            blob = f"{row['title']} {row['snippet']}".lower()
            if t_keys and not any(k in blob for k in t_keys):
                continue
            row["score"] = rank_score(base_score, len(out))
            out.append(row)
            if len(out) >= n:
                break
        # 节目级时长补在筛选之后：被滤掉的结果不该付二跳的代价
        if want_podcast and not want_episode:
            enriched = 0
            for it in items:
                if enriched >= _ITUNES_SHOW_ENRICH_MAX:
                    break
                if not isinstance(it, dict):
                    continue
                name = str(it.get("collectionName") or "").strip()
                target = next((r for r in out if r["title"] == name), None)
                if target is None or "duration_minutes" in target:
                    continue
                mid, samples = _recent_episode_duration(it.get("collectionId"), country, to)
                if mid is not None:
                    target["duration_minutes"] = mid
                    target["snippet"] = f"{target['snippet']} · 近 {samples} 期中位 {mid} 分"
                    enriched += 1
                # 文字稿与时长共用 enrich 配额（前 1 条节目）：官方全文预览的
                # 信息密度远高于「N 集 · 类型」元数据行，有文字稿时 snippet
                # 拼接全文开头，episode_count/published_at 等结构字段不动
                feed_url = str(it.get("feedUrl") or "").strip()
                if feed_url:
                    t_url, t_prev = _show_transcript(feed_url, to)
                    if t_url:
                        target["transcript_url"] = t_url
                    if t_prev:
                        target["snippet"] = f"{target['snippet']} ｜ {t_prev}"[:300]
                    enriched += 1
        return out
    return _engine


# ── TheSportsDB 体育（免认证 test key=3） ────────────────────────────────────

_SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"
_SPORTSDB_UA = "argo-search/2.6 (unified-search@local)"

# 高频中文球员/球队 → TheSportsDB 英文检索名（公开 API 基本只认拉丁文）
_SPORTS_ZH_ALIASES: dict[str, str] = {
    "梅西": "Messi",
    "C罗": "Ronaldo",
    "C羅": "Ronaldo",
    "罗纳尔多": "Ronaldo",
    "Cristiano": "Ronaldo",
    "内马尔": "Neymar",
    "姆巴佩": "Mbappe",
    "哈兰德": "Haaland",
    # 单姓 Curry 会命中 Eddy Curry；中文「库里」默认指 Stephen Curry
    "库里": "Stephen Curry",
    "斯蒂芬库里": "Stephen Curry",
    "史蒂芬库里": "Stephen Curry",
    "詹姆斯": "LeBron James",
    "勒布朗": "LeBron",
    "杜兰特": "Durant",
    "科比": "Kobe",
    "乔丹": "Jordan",
    "姚明": "Yao Ming",
    "易建联": "Yi Jianlian",
    "皇马": "Real Madrid",
    "巴萨": "Barcelona",
    "曼联": "Manchester United",
    "曼城": "Manchester City",
    "湖人": "Lakers",
    "勇士": "Warriors",
    "凯尔特人": "Celtics",
}


def _sports_expand_aliases(query: str) -> list[str]:
    """中文别名展开为英文检索变体。"""
    out: list[str] = []
    for zh, en in _SPORTS_ZH_ALIASES.items():
        if zh in query:
            if en not in out:
                out.append(en)
    return out


def _build_thesportsdb_engine(spec: dict[str, Any]) -> Any:
    """TheSportsDB 球员/球队/赛事搜索（api key=3 公开测试钥，免认证）。

    并行查 searchplayers / searchteams / searchevents，合并去重。
    """
    timeout = spec.get("timeout", 10)
    source_name = spec.get("_name", "thesportsdb")

    def _fetch(path: str, params: dict[str, str], to: float) -> Any:
        url = f"{_SPORTSDB_BASE}/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"User-Agent": _SPORTSDB_UA, "Accept": "application/json"},
        )
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        to = _timeout or timeout
        # 多词查询（如 "NBA finals MVP 2024"）整句常 0 命中 → 短语 + token 变体
        stop = {
            "finals", "final", "mvp", "vs", "the", "and", "of", "in", "for", "a", "an",
            "winner", "winners", "who", "won", "what", "which", "year", "years",
            "team", "club", "player", "coach", "manager", "roster", "squad",
            "决赛", "半决赛", "总冠军", "联赛", "球员", "俱乐部", "进球", "冠军",
            "球队", "球星", "效力", "夺冠", "卫冕", "助攻", "得分王",
        }
        tokens = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", q)
        # 纯数字年号单独收集，用于 event 年份保持一致，不当主检索词
        # 注意：必须整年捕获；r"(19|20)\d{2}" 的 findall 只会返回 "20"，
        # 导致 "20" in "2016-11-24" 恒真、年份过滤失效。
        years = re.findall(r"\b((?:19|20)\d{2})\b", q)
        meaningful = [t for t in tokens if t.lower() not in stop and not re.fullmatch(r"\d{4}", t)]

        variants: list[str] = []
        # 中文别名优先（梅西→Messi）
        for alias in _sports_expand_aliases(q):
            if alias not in variants:
                variants.append(alias)
        # 双词短语优先（World Cup / LeBron James）
        for i in range(len(meaningful) - 1):
            phrase = f"{meaningful[i]} {meaningful[i + 1]}"
            if phrase not in variants:
                variants.append(phrase)
        # 整句（短查询，非纯中文）
        if len(q) <= 40 and q not in variants and re.search(r"[A-Za-z]", q):
            variants.insert(0, q)
        for t in meaningful:
            # 纯中文 token 对英文 API 无效，跳过（已由别名覆盖）
            if re.fullmatch(r"[\u4e00-\u9fff]+", t):
                continue
            if t not in variants:
                variants.append(t)
            if len(variants) >= 5:
                break
        if not variants:
            variants = [q]

        # 意图：club/team/球队 → 优先 team/player；event 词 → 保留 event
        q_low = q.lower()
        prefer_entity = bool(re.search(
            r"(?i)(team|club|player|coach|球队|球星|俱乐部|效力|球员|roster|squad)", q,
        ))
        prefer_event = bool(re.search(
            r"(?i)(finals?|mvp|cup|championship|winner|won|决赛|总冠军|世界杯|欧冠|季后赛)", q,
        )) and not prefer_entity

        bucket: list[dict[str, Any]] = []
        seen: set[str] = set()

        endpoints = (
            ("searchplayers.php", "p", "player", "strPlayer", "idPlayer",
             lambda x: " · ".join(p for p in (
                 x.get("strTeam") or "",
                 x.get("strSport") or "",
                 x.get("strPosition") or "",
                 x.get("strNationality") or "",
             ) if p)),
            ("searchteams.php", "t", "team", "strTeam", "idTeam",
             lambda x: " · ".join(p for p in (
                 x.get("strSport") or "",
                 x.get("strLeague") or "",
                 x.get("strCountry") or "",
             ) if p)),
            ("searchevents.php", "e", "event", "strEvent", "idEvent",
             lambda x: " · ".join(p for p in (
                 x.get("strLeague") or "",
                 x.get("strSport") or "",
                 x.get("dateEvent") or x.get("strTimestamp") or "",
             ) if p)),
        )
        list_keys = {
            "searchplayers.php": "player",
            "searchteams.php": "teams",
            "searchevents.php": "event",
        }
        url_tpl = {
            "player": "https://www.thesportsdb.com/player/{id}",
            "team": "https://www.thesportsdb.com/team/{id}",
            "event": "https://www.thesportsdb.com/event/{id}",
        }
        score_of = {"player": 0.86, "team": 0.84, "event": 0.82}
        # 实体问优先 player/team；赛事问放宽 event
        if prefer_entity:
            endpoints = endpoints[:2] + endpoints[2:]  # 顺序已是 p,t,e
        elif prefer_event:
            endpoints = (endpoints[2], endpoints[0], endpoints[1])

        q_token_set = {t.lower() for t in meaningful}

        for v in variants:
            for path, param_k, kind, title_k, id_k, snip_fn in endpoints:
                try:
                    data = _fetch(path, {param_k: v}, to)
                except Exception as e:
                    logger.warning(f"TheSportsDB {path} 失败: {e}")
                    continue
                items = data.get(list_keys[path]) if isinstance(data, dict) else None
                if not items:
                    continue
                for x in items[: max(n, 5)]:
                    if not isinstance(x, dict):
                        continue
                    title = x.get(title_k) or ""
                    iid = str(x.get(id_k) or "")
                    key = f"{kind}:{iid or title}"
                    if not title or key in seen:
                        continue
                    # 相关度：标题/摘要 token 与查询重叠；event 尤其严格
                    snip = snip_fn(x) or ""
                    sport = str(x.get("strSport") or "")
                    league = str(x.get("strLeague") or "")
                    blob = f"{title} {snip} {sport} {league}".lower()
                    t_tokens = set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", blob))
                    overlap = len(q_token_set & t_tokens) if q_token_set else 1
                    # 「世界杯/World Cup」默认足球；公开 API 常塞高尔夫/篮网等噪声 → 非足球一律丢
                    world_cup_q = bool(re.search(r"(?i)world\s*cup|世界杯|fifa", q))
                    if world_cup_q:
                        if not re.search(r"(?i)soccer|football|fifa", f"{title} {sport} {league} {snip}"):
                            continue
                    if kind == "event":
                        # 无实质重叠 → 丢弃噪声 event（如 World Cup → World Sand Greens）
                        if q_token_set and overlap == 0:
                            continue
                        # 有年份且 API 给了日期：年份不对直接丢（避免 2022 问到 2016 Golf）
                        date_s = str(x.get("dateEvent") or x.get("strTimestamp") or "")
                        if years and date_s and not any(y in date_s for y in years):
                            continue
                        # 双词短语（如 World Cup）若在标题中，要求更高相关；单 token 过宽
                        phrase_hit = any(
                            len(p.split()) >= 2 and p.lower() in title.lower()
                            for p in variants[:3]
                        )
                        if q_token_set and overlap < 2 and not phrase_hit:
                            continue
                        rel = 0.35 + 0.15 * overlap + (0.2 if phrase_hit else 0)
                        if years and date_s and any(y in date_s for y in years):
                            rel += 0.2
                        if world_cup_q and re.search(r"(?i)soccer|football|fifa", blob):
                            rel += 0.25
                    else:
                        # 赛事问勿用弱 token 沾上球员（Metta World Peace）
                        if prefer_event and overlap < 2 and v.lower() not in title.lower():
                            continue
                        if world_cup_q and kind == "player" and not re.search(
                            r"(?i)soccer|football", f"{sport} {snip}",
                        ):
                            continue
                        # player/team：变体本身已是名，重叠可放宽
                        rel = 0.35 + 0.25 * overlap
                        if v.lower() in title.lower():
                            rel += 0.25
                        # 全名变体（Stephen Curry）精确命中加权
                        if " " in v and v.lower() == title.lower():
                            rel += 0.3
                    seen.add(key)
                    href = url_tpl[kind].format(id=iid) if iid else ""
                    base = score_of.get(kind, 0.8)
                    bucket.append({
                        "title": f"[{kind}] {title}",
                        "url": href,
                        "snippet": (snip_fn(x) or kind)[:300],
                        "source": source_name,
                        "score": min(0.95, base * 0.5 + rel),
                        "_kind": kind,
                        "_rel": rel,
                    })
            # 已有高相关实体则早停
            if sum(1 for r in bucket if r.get("_rel", 0) >= 0.5) >= n:
                break

        kind_rank = {"player": 0, "team": 1, "event": 2}
        if prefer_entity:
            kind_rank = {"player": 0, "team": 0, "event": 3}
        elif prefer_event:
            kind_rank = {"event": 0, "player": 1, "team": 2}

        bucket.sort(key=lambda r: (
            kind_rank.get(r.get("_kind", ""), 9),
            -float(r.get("_rel", 0)),
            -float(r.get("score", 0)),
        ))
        out = []
        for r in bucket[:n]:
            r.pop("_kind", None)
            r.pop("_rel", None)
            out.append(r)
        return out
    return _engine
