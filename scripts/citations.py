#!/usr/bin/env python3
"""citations.py — DOI 元数据获取与四种引用格式化（argo cite 子命令的库层）。

数据源（均免密钥）：
  - Crossref  https://api.crossref.org/works/{doi}
  - OpenAlex  https://api.openalex.org/works/https://doi.org/{doi}

路由：Crossref 优先（卷期页/出版商最全）；OpenAlex 是唯一落点，见 fetch_metadata。
两个 API 事实决定了这里的写法：Crossref polite pool 只要求 UA 带 mailto 联系方式
（不收费、不发密钥）；OpenAlex 完全免 key。

归一化：OpenAlex work 被折成 Crossref message 的形状（from_openalex_work），
四种格式化器只面对一种数据形状（统一 meta 字典）。差异集中在三处，全在
from_openalex_work 消化：作者只有 display_name（姓氏虚词切分见
split_display_name）、venue 可能是预印本仓库（前缀命中即按预印本处理，
见 _preprint_server）、doi 是完整 URL。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

# HTTP 出口统一走 http_open：全仓 urlopen 门禁（test_http_attribution）要求
# 所有出站请求过同一出口——代理解析（net_proxy）与失败归因只在那一处做。
# engine="cite" 让归因寄存器有落点（cite 不是引擎，寄存器按名聚合即可）。
from engines_base import http_open  # noqa: E402

# polite pool 的 UA：项目级联系方式 + 仓库地址。GitHub noreply 形式的项目邮箱，
# 与参考实现（dsh-lit-search）同一惯例——Crossref 只要 UA 里有一个可联系的字符串。
_UA = "argo-cite/1.0 (+https://github.com/taxueseek/argo; mailto:argo-cite@users.noreply.github.com)"
_API_TIMEOUT = 15.0

_CROSSREF_WORKS = "https://api.crossref.org/works/"
_OPENALEX_WORKS = "https://api.openalex.org/works/"

# arXiv 的 DataCite DOI 前缀：Crossref 不收录，命中就直接走 OpenAlex，
# 不发那记注定 404 的 Crossref 请求。
_ARXIV_DOI_PREFIX = "10.48550/"

# OpenAlex 认作预印本仓库的 source.display_name（小写前缀匹配）。这些是仓库
# 不是期刊，命中后 venue 置空、类型按预印本走（[EB/OL] / @misc 变体）。
_PREPRINT_SERVERS = (
    "arxiv", "biorxiv", "medrxiv", "ssrn", "chemrxiv",
    "research square", "preprints.org", "techrxiv",
)

# 姓氏虚词（nobility particles）：属于 family，不属于 given。大小写不敏感。
_PARTICLES = frozenset((
    "de", "van", "von", "der", "den", "di", "da", "del", "della",
    "las", "le", "bin", "ibn",
))

# OpenAlex/Crossref 偶尔在 title/venue 里塞 HTML 实体（"A &amp; B Journal"）。
# 单趟替换：&amp;lt; 解码成 &lt; 而不是 <，不做二次解码。
_ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|#39);")

# 合法 DOI：10. + 4~9 位注册局前缀 + / + 非空白后缀。只做形状校验，不碰网络。
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


class CiteError(Exception):
    """获取/格式化失败。消息给用户看，必须说清是哪一步、哪个 DOI。"""


def decode_entities(s: str) -> str:
    return _ENTITY_RE.sub(lambda m: _ENTITY_MAP[m.group(0)], s)


def split_display_name(name: str) -> dict:
    """OpenAlex display_name（"given family" 顺序）拆成 family/given。

    连续的尾部虚词整体归 family："Diego de Las Casas" → family
    "de Las Casas"、given "Diego"。单词名整体当 family。
    """
    name = name.strip()
    words = [w for w in name.split() if w]
    if len(words) < 2:
        return {"family": name, "given": ""}
    i = len(words) - 1
    while i > 0 and words[i - 1].lower() in _PARTICLES:
        i -= 1
    return {"family": " ".join(words[i:]), "given": " ".join(words[:i])}


def _preprint_server(raw_venue: str) -> str | None:
    """OpenAlex source 是已知预印本仓库时返回规范化仓库名，否则 None。

    arXiv 给固定标签（OpenAlex 名它是 "arXiv (Cornell University)"，直接展示
    会把机构带进 howpublished）；其余仓库保留 display_name 原样。
    """
    v = raw_venue.strip()
    if not v:
        return None
    for p in _PREPRINT_SERVERS:
        if v.lower().startswith(p):
            return "arXiv" if p == "arxiv" else v
    return None


def _from_crossref_message(msg: dict, doi: str) -> dict:
    title = decode_entities(str((msg.get("title") or [""])[0]))
    container = decode_entities(str((msg.get("container-title") or [""])[0]))
    publisher = decode_entities(str(msg.get("publisher") or ""))
    year = ""
    # published 缺失时 issued 兜底：Crossref 契约里 issued 是必有项，
    # published 只有部分记录带（跨年更正、预印本转正等记录二者会不同）。
    for key in ("published", "issued"):
        parts = (msg.get(key) or {}).get("date-parts") or [[None]]
        y = (parts[0] or [None])[0]
        if y:
            year = str(y)
            break
    return {
        "doi": str(msg.get("DOI") or doi),
        "authors": [
            {"family": str(a.get("family") or ""), "given": str(a.get("given") or "")}
            for a in (msg.get("author") or [])
        ],
        "title": title,
        "container": container,
        "year": year,
        "volume": str(msg.get("volume") or ""),
        "issue": str(msg.get("issue") or ""),
        "pages": str(msg.get("page") or ""),
        "publisher": publisher,
        "type": str(msg.get("type") or ""),
        "url": str(msg.get("URL") or f"https://doi.org/{doi}"),
        "preprint_server": None,  # Crossref 路径不会出现 arXiv DOI，见 fetch_metadata
    }


def from_openalex_work(work: dict) -> dict:
    """OpenAlex work → Crossref-message 形状的统一 meta（差异清单见模块头）。"""
    raw_venue = str(((work.get("primary_location") or {}).get("source") or {})
                    .get("display_name") or "")
    server = _preprint_server(raw_venue)
    venue = "" if server else raw_venue
    biblio = work.get("biblio") or {}
    first, last = biblio.get("first_page"), biblio.get("last_page")
    if first and last:
        pages = f"{first}-{last}"
    else:
        pages = str(first or last or "")
    doi = re.sub(r"^https?://doi\.org/", "", str(work.get("doi") or ""), flags=re.I)
    return {
        "doi": doi,
        "authors": [split_display_name(((a.get("author") or {}).get("display_name")) or "")
                    for a in (work.get("authorships") or [])
                    if isinstance(a.get("author") or {}, dict)],
        "title": decode_entities(str(work.get("title") or "")),
        "container": decode_entities(venue),
        "year": str(work.get("publication_year") or ""),
        "volume": str(biblio.get("volume") or ""),
        "issue": str(biblio.get("issue") or ""),
        "pages": pages,
        "publisher": "",  # OpenAlex 的 host 机构要额外查表，参考实现不取，这里保持一致
        # 无 venue 的记录按预印本处理：预印本仓库 venue 已被置空，真正的纯
        # 预印本（无正式出处）也归入此类——两种走同一套 [EB/OL] / @misc 变体。
        "type": "journal-article" if venue else "posted-content",
        "url": f"https://doi.org/{doi}" if doi else "",
        "preprint_server": server,
    }


def _get_json(url: str) -> object:
    """GET 并解析 JSON。失败抛 CiteError：带状态码/原因，不吞、不猜。

    网络层走统一出口 http_open（全仓 urlopen 门禁；代理解析与失败归因
    单点在那条链上）。http_open 是等价替换：成功路径同一响应对象，
    HTTPError 原样透传且错误体可再 read——下方 except 分支语义不变。
    打桩点 = citations.http_open（读取处）。
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with http_open(req, timeout=_API_TIMEOUT, engine="cite") as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
            # 只取首行：上游 404 的错误体可能是整页 HTML，多行会把 CLI 的
            # 错误输出撑成错误屏；状态码 + 首行足够归因。
            detail = body.splitlines()[0][:120] if body else ""
        except Exception:
            detail = ""  # HTTP 错误体读取失败（非 UTF-8 / 连接中断），退回空 detail
        raise CiteError(f"HTTP {e.code}: {detail}".rstrip()) from None
    except urllib.error.URLError as e:
        raise CiteError(f"连接失败: {getattr(e, 'reason', e)}") from None
    except OSError as e:
        raise CiteError(f"连接失败: {e}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise CiteError(f"响应不是有效 JSON: {e}") from None


def _clean_doi(doi: str) -> str:
    d = doi.strip()
    if not _DOI_RE.match(d):
        raise CiteError(f"不是合法 DOI: {doi!r}（应形如 10.1234/abcdef）")
    return d


def fetch_metadata(doi: str) -> dict:
    """DOI → 统一 meta。Crossref 优先，OpenAlex 兜底（见模块头路由说明）。"""
    d = _clean_doi(doi)
    if not d.lower().startswith(_ARXIV_DOI_PREFIX):
        try:
            payload = _get_json(_CROSSREF_WORKS + urllib.parse.quote(d, safe=""))
            msg = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(msg, dict):
                raise CiteError("Crossref 响应缺少 message 字段")
            return _from_crossref_message(msg, d)
        except CiteError:
            pass  # 查无此 DOI / 网络失败都落 OpenAlex：单源失败不代表 DOI 不存在
    try:
        work = _get_json(
            _OPENALEX_WORKS + "https://doi.org/" + urllib.parse.quote(d, safe=""))
    except CiteError as e:
        raise CiteError(f"Crossref 与 OpenAlex 均未查到 {d}（{e}）") from None
    if not isinstance(work, dict):
        raise CiteError(f"OpenAlex 响应不是对象: {d}")
    meta = from_openalex_work(work)
    if not meta["doi"]:
        meta["doi"] = d  # OpenAlex 个别记录缺 doi 字段，用请求值兜底
    return meta


# GB/T 7714 的文献类型标识。未知类型回落 J（期刊）是行业惯例里最安全的默认。
_GBT_TYPE_MARKS = {
    "journal-article": "J", "proceedings-article": "C", "monograph": "M",
    "dissertation": "D", "posted-content": "EB",
}

_BIBTEX_TYPES = {"journal-article": "article", "proceedings-article": "inproceedings"}


def _short_name(n: dict) -> str:
    """GB/T 7714 作者形态："Family G"（取 given 首字母，无句点）。"""
    return f"{n['family']} {n['given'][:1]}".strip()


def _authors_gbt(meta: dict) -> str:
    ns = meta["authors"]
    # 过滤空名（OpenAlex 个别 authorship 缺 display_name），避免产出 ", ," 碎片
    head = ", ".join(s for s in (_short_name(n) for n in ns[:3]) if s)
    return f"{head}, et al" if len(ns) > 3 else head


def _authors_apa(meta: dict) -> str:
    def fmt(n: dict) -> str:
        initials = " ".join(w[0] + "." for w in n["given"].split() if w)
        return ", ".join(p for p in (n["family"], initials) if p)
    ns = meta["authors"]
    head = ", ".join(fmt(n) for n in ns[:3])
    return f"{head}, et al." if len(ns) > 3 else head


def _bibtex_key(meta: dict, used_keys: set | None) -> str:
    """「第一作者姓+年份」键：非法字符剥掉，批内重复按 a/b/c 递增。"""
    family = meta["authors"][0]["family"] if meta["authors"] else ""
    base = re.sub(r"[^a-z0-9]", "", family.lower()) or "unknown"
    base += str(meta.get("year") or "")
    key = base
    if used_keys is not None:
        i = 0
        while key in used_keys:
            i += 1
            # 同族键超过 26 个时字母翻倍（aa/aaa）：只会出现在病态批里，兜住即可
            key = base + "abcdefghijklmnopqrstuvwxyz"[(i - 1) % 26] * ((i - 1) // 26 + 1)
        used_keys.add(key)
    return key


def format_citation(meta: dict, style: str, used_keys: set | None = None) -> str:
    """统一 meta → 引用条目。纯函数（used_keys 传入时承担批内 BibTeX 键去重）。

    style：gbt7714 | gbt7714n（顺序编码制，即带 [1] 序号）| apa | bibtex。
    """
    doi = meta.get("doi") or ""
    title = meta.get("title") or ""
    venue = meta.get("container") or meta.get("publisher") or ""
    year = str(meta.get("year") or "")

    if style == "gbt7714n":
        return "[1] " + format_citation(meta, "gbt7714")
    if style == "gbt7714":
        authors = _authors_gbt(meta)
        if meta.get("type") == "posted-content" and not venue:
            # 纯预印本走电子资源形态；有年份时按 GB/T 7714 附加更新日期
            y = f" ({year})." if year else ""
            head = f"{authors}. " if authors else ""
            return f"{head}{title}[EB/OL].{y} https://doi.org/{doi}."
        mark = _GBT_TYPE_MARKS.get(meta.get("type") or "", "J")
        vol = "".join(p for p in (meta.get("volume") or "",
                                  f"({meta['issue']})" if meta.get("issue") else "") if p)
        tail = ", ".join(p for p in (venue, year, vol) if p)
        head = f"{authors}. " if authors else ""
        return f"{head}{title}[{mark}]. {tail}."
    if style == "apa":
        authors = _authors_apa(meta)
        head = f"{authors} " if authors else ""
        parts = [
            f"{head}({year or 'n.d.'}). {title}.",
            f"{venue}." if venue else "",
            f"https://doi.org/{doi}" if doi else "",
        ]
        return " ".join(p for p in parts if p)
    if style == "bibtex":
        ns = meta["authors"]
        entry = _BIBTEX_TYPES.get(meta.get("type") or "", "misc")
        lines = [
            f"  author = {{{' and '.join(', '.join(p for p in (n['family'], n['given']) if p) for n in ns)}}}",
            f"  title = {{{title}}}",
        ]
        if venue:
            lines.append(f"  {'journal' if entry == 'article' else 'booktitle'} = {{{venue}}}")
        if year:
            lines.append(f"  year = {{{year}}}")
        if meta.get("volume"):
            lines.append(f"  volume = {{{meta['volume']}}}")
        if meta.get("issue"):
            lines.append(f"  number = {{{meta['issue']}}}")
        if meta.get("type") == "posted-content" and not venue:
            lines.append(f"  howpublished = {{{meta.get('preprint_server') or 'arXiv'} preprint}}")
        if doi:
            lines.append(f"  doi = {{{doi}}}")
        return f"@{entry}{{{_bibtex_key(meta, used_keys)},\n" + ",\n".join(lines) + "\n}"
    raise CiteError(f"未知引用格式: {style!r}（可选 gbt7714/gbt7714n/apa/bibtex）")
