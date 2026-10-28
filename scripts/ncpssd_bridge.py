#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NCPSSD（国家哲学社会科学文献中心）检索桥 —— 供 argo 的 CLI 引擎调用。

上游 https://www.ncpssd.cn/searchHandler/search 只吃表单 POST（实测：GET 返回 HTML、
POST 空体+查询串被 WAF 493），argo 的声明式 http 引擎只会发 JSON POST / GET，
故用本桥代发表单请求，再把结果转成 argo 认得的 items 结构。

用法：python ncpssd_bridge.py "唯物史观" -n 10
输出：stdout 一行 JSON（ASCII 转义，避免 Windows 管道默认 GBK 把中文写坏）。
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request

ENDPOINT = "https://www.ncpssd.cn/searchHandler/search"
SORT = "synUpdateType|DESC,date|DESC,ik_subject|DESC,id|DESC"
TAGS = ("IKTE", "IKPYTE", "IKST", "IKET", "IKSE")
DETAIL = "https://www.ncpssd.cn/Literature/articleinfo?type={tab}&id={ident}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.ncpssd.cn/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "；".join(str(v) for v in value if v)
    return _TAG_RE.sub("", str(value)).strip()


def _date(value) -> str:
    m = _DATE_RE.match(str(value or ""))
    return m.group(0) if m else ""


def _expression(word: str) -> str:
    return "(" + " OR ".join(f'{tag}="{word}"' for tag in TAGS) + ")"


def fetch(word: str, count: int, timeout: float = 20.0) -> list[dict]:
    body = urllib.parse.urlencode({
        "search": _expression(word),
        "pageNum": "1",
        "pageSize": str(max(1, min(count, 30))),
        "sort": SORT,
        "sType": "0",
        "ajaxKeys": "",
        "customShowCondition": word,
    }).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    rows = ((payload.get("data") or {}).get("rows")) or []
    items: list[dict] = []
    for row in rows[:count]:
        if not isinstance(row, dict):
            continue
        title = _clean(row.get("title")) or _clean(row.get("ik_title"))
        if not title:
            continue
        ident = _clean(row.get("id") or row.get("data_id"))
        tab = _clean(row.get("tab_name")) or "journalArticle"
        detail = DETAIL.format(tab=tab, ident=ident) if ident else ""
        pdf = _clean(row.get("pdfurl"))
        volume = _clean(row.get("volumn"))
        issue = _clean(row.get("num"))
        pages = ""
        if _clean(row.get("beginpage")):
            pages = _clean(row.get("beginpage")) + "-" + _clean(row.get("endpage")) + "页"
        meta = " · ".join(part for part in (
            _clean(row.get("creator")) or _clean(row.get("creator_auto")),
            _clean(row.get("cbw_name")) or _clean(row.get("bookname")),
            "".join(x for x in (_clean(row.get("years")), volume and f"第{volume}卷", issue and f"第{issue}期") if x),
            pages,
            _clean(row.get("type")),
        ) if part)
        abstract = _clean(row.get("remark")) or _clean(row.get("ik_remark"))
        snippet = "｜".join(part for part in (
            f"详情 {detail}" if (pdf and detail) else "",
            meta,
            abstract,
        ) if part)
        items.append({
            "title": title,
            "url": pdf or detail,
            "snippet": snippet,
            "published_at": _date(row.get("date")),
        })
    return items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NCPSSD 检索（argo CLI 桥）")
    parser.add_argument("query")
    parser.add_argument("-n", "--count", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        items = fetch(args.query.strip(), args.count)
    except Exception as exc:  # 上游异常也要给 argo 一个可解析的空结果
        print(json.dumps({"items": [], "error": f"{type(exc).__name__}: {exc}"[:200]},
                         ensure_ascii=True))
        return 1
    print(json.dumps({"items": items}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
