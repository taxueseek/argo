#!/usr/bin/env python3
"""火车余票查询脚本（免 Key，纯标准库）。

能力（原生吸纳自外部列车查询技能，不保留其名称）：
  - 官方两步接口：GET kyfw.12306.cn/otn/leftTicket/init 取会话 Cookie
    → GET kyfw.12306.cn/otn/leftTicket/queryG 查余票（返回 URL 编码的管道分隔行）
  - 站点表 station_name.js 解析 + 7 天本地缓存
  - 输入：自然语言查询词（"上海到北京" / "北京→上海 后天 G" / "北京 上海"）
  - 输出：YAML 结果列表（车次/出发到达时间/历时/余票），供 cli 引擎解析

用法：
  python3 scripts/train.py "上海到北京"
  python3 scripts/train.py "北京→上海 后天 G" --n 10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse as up
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
_QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/queryG"
_STATION_JS_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
_CACHE_DIR = Path(__file__).resolve().parent / "data"
_CACHE_TTL_SECONDS = 7 * 24 * 3600

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json,text/javascript,*/*",
}

# 12306 余票查询结果每行为管道分隔的 58 字段，字段索引映射（社区整理的稳定结构）。
_F = {
    "trainNo": 2, "trainCode": 3, "fromCode": 6, "toCode": 7,
    "departTime": 8, "arriveTime": 9, "duration": 10, "canBuy": 11, "date": 13,
    "gr": 21, "rw": 23, "rz": 24, "tz": 25, "wz": 26, "yw": 28, "yz": 29,
    "ze": 30, "zy": 31, "swz": 32, "dw": 33,
}

# 席别展示顺序与标签（软卧优先动卧、商务优先特等）
_SEAT_LABELS = [
    ("swz", "商务/特等"), ("zy", "一等"), ("ze", "二等"),
    ("rw", "软卧/动卧"), ("yw", "硬卧"), ("yz", "硬座"), ("wz", "无座"),
]

_DATE_WORD = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_DATE_RE = re.compile(
    r"(今天|明天|后天|大后天|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号])"
)
_TYPE_WORD = {
    "高铁": "G", "G字头": "G", "动车": "D", "D字头": "D",
    "直达": "Z", "Z字头": "Z", "特快": "T", "T字头": "T",
    "快速": "K", "K字头": "K",
}
def _today_cn(days: int = 0) -> date:
    return (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=days)).date()


def _parse_query(q: str) -> dict | None:
    """从自然语言查询词解析 出发站/到达站/日期/车次类型。

    支持："上海到北京"、"北京→上海 后天"、"2026-08-10 北京 上海 高铁"、"从北京到上海 G"。
    返回 dict 或 None（无法解析出起止站）。
    """
    text = (q or "").strip()
    if not text:
        return None

    # 1) 日期词 → date
    travel_date: date | None = None
    m = _DATE_RE.search(text)
    if m:
        tok = m.group(1)
        text = text.replace(tok, " ", 1)
        if tok in _DATE_WORD:
            travel_date = _today_cn(_DATE_WORD[tok])
        else:
            if re.match(r"^\d{4}", tok):
                travel_date = datetime.strptime(
                    tok.replace("/", "-"), "%Y-%m-%d"
                ).date()
            else:
                ymd = re.match(r"(\d{1,2})月(\d{1,2})[日号]", tok)
                if ymd:
                    mm, dd = int(ymd.group(1)), int(ymd.group(2))
                    travel_date = date(_today_cn().year, mm, dd)

    # 2) 车次类型词 → type
    train_type = ""
    m = _TYPE_WORD_RE().search(text)
    if m:
        train_type = _TYPE_WORD[m.group(1)]
        text = text.replace(m.group(1), " ", 1)

    # 3) 去掉「从」前缀，按分隔符拆分起止站
    text = re.sub(r"^从", " ", text.strip())
    parts = re.split(r"到|→|->|=>|至|—|--|\s+", text)
    parts = [p.strip(" 站车票张次列高动直特快快速字头，。！？") for p in parts if p.strip()]
    if len(parts) < 2:
        return None
    from_name, to_name = parts[0], parts[1]

    return {
        "from": from_name,
        "to": to_name,
        "date": (travel_date or _today_cn(1)).isoformat(),
        "type": train_type,
    }


_TYPE_WORD_RE_CACHE = None


def _TYPE_WORD_RE():
    global _TYPE_WORD_RE_CACHE
    if _TYPE_WORD_RE_CACHE is None:
        _TYPE_WORD_RE_CACHE = re.compile(
            "(" + "|".join(re.escape(k) for k in sorted(_TYPE_WORD, key=len, reverse=True)) + ")"
        )
    return _TYPE_WORD_RE_CACHE


def _fetch(url: str, headers: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(url, headers={**_HEADERS, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def _get_cookie(timeout: float = 15) -> str:
    """取 12306 会话 Cookie（多个 Set-Cookie 拼一行）。"""
    with _fetch(_INIT_URL, timeout=timeout) as resp:
        cookies = resp.headers.get_all("Set-Cookie") or []
    return "; ".join(c.split(";")[0] for c in cookies if c)


def _query_api(from_code: str, to_code: str, travel_date: str, cookie: str,
               timeout: float = 15) -> list[list[str]]:
    """调 queryG 接口，返回每行 unquote 后的字段数组。"""
    params = up.urlencode({
        "leftTicketDTO.train_date": travel_date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    })
    url = f"{_QUERY_URL}?{params}"
    with _fetch(url, headers={"Cookie": cookie, "Referer": _INIT_URL}, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    rows = ((payload.get("data") or {}).get("result")) or []
    return [up.unquote(r).split("|") for r in rows]


def _parse_station_data(js: str) -> dict:
    """解析 station_name.js（@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||）。"""
    m = re.search(r"'([^']+)'", js)
    raw = m.group(1) if m else ""

    stations, city_stations = {}, {}
    name_stations, city_codes = {}, {}
    for entry in raw.split("@"):
        parts = entry.split("|")
        if len(parts) < 8:
            continue
        name, code = parts[1], parts[2]
        if not name or not code:
            continue
        city = parts[7] or name
        stations[code] = {"station_name": name, "station_code": code}
        name_stations[name] = {"station_name": name, "station_code": code}
        city_stations.setdefault(city, []).append({"station_name": name, "station_code": code})
        if name == city:
            city_codes[city] = {"station_name": name, "station_code": code}
    return {
        "STATIONS": stations, "NAME_STATIONS": name_stations,
        "CITY_STATIONS": city_stations, "CITY_CODES": city_codes,
    }


def _load_stations(force: bool = False, cache_dir: Path | None = None) -> dict:
    """读取站点表（优先 7 天缓存）。"""
    cache_dir = cache_dir or _CACHE_DIR
    cache_file = cache_dir / "stations.json"
    if not force and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text("utf-8"))
            if int(cached.get("ts", 0)) and \
                    int(cached["ts"]) > (datetime.now().timestamp() - _CACHE_TTL_SECONDS):
                return cached["data"]
        except (ValueError, KeyError, OSError):
            pass
    with _fetch(_STATION_JS_URL, timeout=20) as resp:
        js = resp.read().decode("utf-8", "replace")
    data = _parse_station_data(js)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"ts": int(datetime.now().timestamp()), "data": data}, ensure_ascii=False),
            "utf-8",
        )
    except OSError:
        pass
    return data


def _resolve_station(data: dict, name: str) -> dict | None:
    """把城市/站名解析为 12306 站点码（精确站名 > 城市主站 > 城市首站）。"""
    name = (name or "").strip()
    if not name:
        return None
    if name in data["NAME_STATIONS"]:
        return data["NAME_STATIONS"][name]
    if name in data["CITY_CODES"]:
        return data["CITY_CODES"][name]
    if name in data["CITY_STATIONS"]:
        return data["CITY_STATIONS"][name][0]
    trimmed = re.sub(r"[市站]$", "", name)
    if trimmed in data["NAME_STATIONS"]:
        return data["NAME_STATIONS"][trimmed]
    if trimmed in data["CITY_CODES"]:
        return data["CITY_CODES"][trimmed]
    if trimmed in data["CITY_STATIONS"]:
        return data["CITY_STATIONS"][trimmed][0]
    return None


def _fmt_duration(raw: str) -> str:
    h, _, m = raw.partition(":")
    try:
        hh, mm = int(h), int(m)
    except ValueError:
        return raw
    return f"{hh}h{mm:02d}m" if hh else f"{mm}m"


def _fmt_seat(v: str) -> str:
    v = (v or "").strip()
    if v in ("", "--"):
        return ""
    return v


def _parse_ticket(fields: list[str], data: dict) -> dict:
    v = lambda key: fields[_F[key]] if len(fields) > _F[key] else ""
    from_code, to_code = v("fromCode"), v("toCode")
    stations = data["STATIONS"]
    return {
        "trainCode": v("trainCode"),
        "fromStation": stations.get(from_code, {}).get("station_name", from_code),
        "toStation": stations.get(to_code, {}).get("station_name", to_code),
        "departTime": v("departTime"), "arriveTime": v("arriveTime"),
        "duration": v("duration"), "canBuy": v("canBuy"), "date": v("date"),
        "swz": v("swz"), "tz": v("tz"), "zy": v("zy"), "ze": v("ze"),
        "gr": v("gr"), "rw": v("rw"), "dw": v("dw"),
        "yw": v("yw"), "rz": v("rz"), "yz": v("yz"), "wz": v("wz"),
    }


def _build_rows(tickets: list[dict], limit: int) -> list[dict]:
    rows = []
    for t in tickets[: max(1, limit)]:
        seats = []
        for key, label in _SEAT_LABELS:
            val = _fmt_seat(t.get(key))
            if not val:
                continue
            if key == "swz" and not val and t.get("tz"):
                val = _fmt_seat(t["tz"])
            if key == "rw" and not val and t.get("dw"):
                val = _fmt_seat(t["dw"])
            seats.append(f"{label} {val}")
        status = "可购" if t.get("canBuy") == "Y" else "停售"
        d = (t.get("date") or "")[:8]
        published_at = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        rows.append({
            "title": f"{t['trainCode']} {t['fromStation']} {t['departTime']}→{t['toStation']} {t['arriveTime']}",
            "url": "https://kyfw.12306.cn/otn/leftTicket/init",
            "snippet": " · ".join(["历时 " + _fmt_duration(t.get("duration", ""))] + seats + [status]),
            "published_at": published_at,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="火车余票查询（免 Key，12306 官方接口）")
    parser.add_argument("query", nargs="*", help="查询词，如：上海到北京 / 北京→上海 后天 G")
    parser.add_argument("--n", type=int, default=5, help="最多返回条数")
    args = parser.parse_args()

    q = _parse_query(" ".join(args.query))
    if not q:
        print("无法解析起止站：请用「出发站 到 到达站」的格式，如：上海到北京", file=sys.stderr)
        return 2

    data = _load_stations()
    frm = _resolve_station(data, q["from"])
    to = _resolve_station(data, q["to"])
    if not frm or not to:
        missing = q["from"] if not frm else q["to"]
        print(f"未找到站点：{missing}", file=sys.stderr)
        return 2

    cookie = _get_cookie()
    raw = _query_api(frm["station_code"], to["station_code"], q["date"], cookie)
    tickets = [_parse_ticket(f, data) for f in raw]
    if q["type"]:
        tickets = [t for t in tickets if t["trainCode"].startswith(q["type"])]

    import yaml
    print(yaml.safe_dump(_build_rows(tickets, args.n), allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
