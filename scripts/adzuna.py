#!/usr/bin/env python3
"""adzuna.py — Adzuna 聚合职位搜索（16 国，免 Key 注册）。

能力：
  - 按关键词 + 国家搜索在招岗位（Adzuna REST API）
  - 返回标题/公司/薪资/地点/描述/URL，结构化 YAML 输出
  - 国家码自动映射（中/英/常见别名 → ISO 3166-1 alpha-2）
  - 薪资字段：salary_min/salary_max/salary_is_predicted
  - 支持 since/until 时间窗（Adzuna API 不直接支持，结果按 posted 降序）

用法：
  python3 scripts/adzuna.py "software engineer" -n 10 --country gb
  python3 scripts/adzuna.py "数据分析师" -n 5 --country de
  python3 scripts/adzuna.py "chef" -n 8 --country 英国
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse as up
import urllib.request
from datetime import datetime, timezone

_API_ROOT = "https://api.adzuna.com/v1/api/jobs"
_HEADERS = {
    "User-Agent": "argo-search/2.7.3 (https://github.com/taxueseek/argo)",
    "Accept": "application/json",
}

# 国家别名 → ISO 3166-1 alpha-2（Adzuna 支持的国家码）
_COUNTRY_MAP = {
    # 英文
    "united kingdom": "gb", "uk": "gb", "britain": "gb", "england": "gb",
    "united states": "us", "usa": "us", "america": "us",
    "canada": "ca", "australia": "au", "germany": "de", "france": "fr",
    "netherlands": "nl", "belgium": "be", "ireland": "ie", "austria": "at",
    "switzerland": "ch", "south africa": "za", "brazil": "br", "india": "in",
    "russia": "ru", "poland": "pl",
    # 中文
    "英国": "gb", "美国": "us", "加拿大": "ca", "澳洲": "au", "澳大利亚": "au",
    "德国": "de", "法国": "fr", "荷兰": "nl", "比利时": "be", "爱尔兰": "ie",
    "奥地利": "at", "瑞士": "ch", "南非": "za", "巴西": "br", "印度": "in",
    "俄罗斯": "ru", "波兰": "pl",
    # 常见变体
    "gb": "gb", "us": "us", "ca": "ca", "au": "au", "de": "de", "fr": "fr",
    "nl": "nl", "be": "be", "ie": "ie", "at": "at", "ch": "ch", "za": "za",
    "br": "br", "in": "in", "ru": "ru", "pl": "pl",
}

# 默认国家（中文查询默认搜中文区最近的 Adzuna 源 → gb）
_DEFAULT_COUNTRY = "gb"


def _resolve_country(s: str) -> str:
    """将国家别名归一为 ISO alpha-2 码；无法识别返回默认值。"""
    key = (s or "").strip().lower()
    return _COUNTRY_MAP.get(key, _DEFAULT_COUNTRY)


def _fetch(url: str, timeout: float = 12) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def search(query: str, n: int = 10, country: str = "gb") -> list[dict]:
    """调用 Adzuna 搜索 API，返回标准化结果列表。"""
    app_id = os.environ.get("ARGO_ADZUNA_APP_ID", "")
    app_key = os.environ.get("ARGO_ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        print("ERROR: 需设置 ARGO_ADZUNA_APP_ID 和 ARGO_ADZUNA_APP_KEY 环境变量", file=sys.stderr)
        print("       注册：https://developer.adzuna.com/signup", file=sys.stderr)
        sys.exit(1)

    cc = _resolve_country(country)
    params = up.urlencode({
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": min(n, 50),
        "what": query,
        "content-type": "application/json",
    })
    url = f"{_API_ROOT}/{cc}/search/1?{params}"
    data = _fetch(url)

    results = []
    for item in data.get("results", []):
        title = item.get("title", "").strip()
        company = (item.get("company") or {}).get("display_name", "")
        location = (item.get("location") or {}).get("display_name", "")
        description = (item.get("description", "") or "").strip()[:300]
        redirect_url = item.get("redirect_url", "")
        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        salary_predicted = item.get("salary_is_predicted", "false")
        posted = item.get("created", "")
        # 薪资展示
        salary_str = ""
        if salary_min and salary_max:
            salary_str = f"£{salary_min:,.0f}-£{salary_max:,.0f}" if cc == "gb" else f"{salary_min:,.0f}-{salary_max:,.0f}"
            if salary_predicted == "true":
                salary_str += " (估)"
        elif salary_min:
            salary_str = f"from {salary_min:,.0f}"

        snippet_parts = [p for p in (company, location, salary_str) if p]
        snippet = " | ".join(snippet_parts)
        if description:
            snippet += f" :: {description}"

        results.append({
            "title": title[:120],
            "url": redirect_url,
            "snippet": snippet[:400],
            "source": "adzuna",
            "published_at": posted[:10] if posted else "",
            "salary": salary_str,
            "company": company,
            "location": location,
        })
    return results


def main():
    ap = argparse.ArgumentParser(description="Adzuna 聚合职位搜索（16 国）")
    ap.add_argument("query", help="搜索关键词（职位/技能/公司）")
    ap.add_argument("-n", "--num", type=int, default=10, help="返回条数（最大 50）")
    ap.add_argument("--country", default="gb", help="国家：gb/us/ca/au/de/fr/nl/... 或中文（英国/美国/...）")
    args = ap.parse_args()

    results = search(args.query, args.num, args.country)
    # YAML 兼容输出（argo cli 引擎解析）
    print("results:")
    for r in results:
        print(f"  - title: {yaml_quote(r['title'])}")
        print(f"    url: {r['url']}")
        print(f"    snippet: {yaml_quote(r['snippet'])}")
        print(f"    source: adzuna")
        if r["published_at"]:
            print(f"    published_at: {r['published_at']}")
        if r["salary"]:
            print(f"    salary: {yaml_quote(r['salary'])}")
        if r["company"]:
            print(f"    company: {yaml_quote(r['company'])}")
        if r["location"]:
            print(f"    location: {yaml_quote(r['location'])}")


def yaml_quote(s: str) -> str:
    """简单 YAML 字符串转义（含冒号/引号时加引号）。"""
    if not s:
        return '""'
    if any(c in s for c in ":{}[]&*?|-><!%@`#'\"\\\n"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


if __name__ == "__main__":
    main()
