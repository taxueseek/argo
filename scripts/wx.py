#!/usr/bin/env python3
"""天气查询脚本（免 Key，纯标准库）。

能力（原生吸纳自外部天气技能，不保留其名称）：
  - 主用 wttr.in（https://wttr.in/{地点}?format=j1，免费公开）
  - 兜底 Open-Meteo（https://api.open-meteo.com，需要坐标）
  - 输入：地点名（城市/机场码/邮编）或 "lat,lon"，或两个位置参数 lat lon

输出：YAML 结果列表（当前天气 1 条 + 未来预报 N 条），供 cli 引擎解析。
字段：title/url/snippet/published_at。

用法：
  python3 scripts/wx.py "Shanghai"
  python3 scripts/wx.py "31.23,121.47" --n 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse as up
import urllib.request
from datetime import date, datetime, timedelta, timezone

_WTTR_BASE = "https://wttr.in/"
_OM_BASE = "https://api.open-meteo.com/v1/forecast"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# WMO 天气代码 → 描述（Open-Meteo）
_WMO = {
    0: "晴", 1: "基本晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇", 51: "毛毛雨", 53: "小雨", 55: "中雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "霰",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


def _fetch(url: str, timeout: float = 8) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


_WEATHER_WORDS = re.compile(
    r"(天气|气温|温度|多少度|预报|怎么样|如何|下雨|降水|雪|晴|今天|明天|后天|当前|现在|"
    r"weather|forecast|temperature|today|tomorrow|now|current)",
    re.IGNORECASE,
)


def _parse_location(args: list[str]) -> str:
    """把位置参数归一为 wttr.in 可用的路径片段。"""
    if len(args) >= 2:
        try:
            lat, lon = float(args[0]), float(args[1])
            return f"{lat},{lon}"
        except ValueError:
            pass
    loc = args[0] if args else "Beijing"
    loc = _WEATHER_WORDS.sub("", loc).strip()
    return loc or "Beijing"


def _wttr(loc: str) -> list[dict]:
    url = f"{_WTTR_BASE}{up.quote(loc)}?format=j1"
    data = json.loads(_fetch(url))
    area = (data.get("nearest_area") or [{}])[0]
    area_name = (area.get("areaName") or [{}])[0].get("value", loc)
    country = (area.get("country") or [{}])[0].get("value", "")
    cur = (data.get("current_condition") or [{}])[0]
    desc = ((cur.get("weatherDesc") or [{}])[0].get("value", "") or "")

    def cond_url():
        return f"{_WTTR_BASE}{up.quote(loc)}"

    rows = []
    temp_c = cur.get("temp_C")
    if temp_c is not None:
        title = f"{area_name} 当前 {temp_c}°C {desc}".strip()
        parts = []
        if cur.get("FeelsLikeC"):
            parts.append(f"体感 {cur['FeelsLikeC']}°C")
        if cur.get("humidity"):
            parts.append(f"湿度 {cur['humidity']}%")
        if cur.get("windspeedKmph"):
            parts.append(f"风速 {cur['windspeedKmph']}km/h {cur.get('winddir16Point', '')}")
        if cur.get("cloudcover"):
            parts.append(f"云量 {cur['cloudcover']}%")
        if cur.get("visibility"):
            parts.append(f"能见度 {cur['visibility']}km")
        if cur.get("observation_time"):
            parts.append(f"观测 {cur['observation_time']}")
        if country:
            parts.append(country)
        rows.append({
            "title": title,
            "url": cond_url(),
            "snippet": " · ".join(p for p in parts if p),
            "published_at": date.today().isoformat(),
        })

    for day in data.get("weather") or []:
        d = day.get("date", "")
        if not d:
            continue
        title = f"{area_name} {d} {day.get('mintempC', '?')}°C~{day.get('maxtempC', '?')}°C"
        noon = next((h for h in (day.get("hourly") or []) if str(h.get("time", "")).startswith(("11", "12"))), None)
        parts = []
        if noon:
            desc2 = ((noon.get("weatherDesc") or [{}])[0].get("value", ""))
            if desc2:
                parts.append(desc2)
            if noon.get("tempC"):
                parts.append(f"{noon['tempC']}°C")
            if noon.get("humidity"):
                parts.append(f"湿度 {noon['humidity']}%")
            if noon.get("windspeedKmph"):
                parts.append(f"风速 {noon['windspeedKmph']}km/h")
        rows.append({
            "title": title,
            "url": cond_url(),
            "snippet": " · ".join(parts) or day.get("weatherDesc", ""),
            "published_at": d,
        })
    return rows


def _open_meteo(loc: str) -> list[dict]:
    try:
        lat, lon = (float(x) for x in loc.split(","))
    except (ValueError, AttributeError):
        return []
    params = {
        "latitude": lat, "longitude": lon, "current_weather": "true",
        "timezone": "auto", "forecast_days": 3,
        "daily": "temperature_2m_max,temperature_2m_min",
    }
    url = f"{_OM_BASE}?{up.urlencode(params)}"
    data = json.loads(_fetch(url))
    cur = data.get("current_weather") or {}
    rows = []
    if cur:
        code = _WMO.get(cur.get("weathercode", -1), "未知")
        rows.append({
            "title": f"{lat:.2f},{lon:.2f} 当前 {cur.get('temperature', '?')}°C {code}",
            "url": f"https://open-meteo.com/",
            "snippet": f"风速 {cur.get('windspeed', '?')}km/h · 风向 {cur.get('winddirection', '?')}° · 观测 {cur.get('time', '')}",
            "published_at": (cur.get("time") or "")[:10],
        })
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    mx = daily.get("temperature_2m_max") or []
    mn = daily.get("temperature_2m_min") or []
    for i, t in enumerate(times[:2]):
        if i >= len(mx) or i >= len(mn):
            break
        rows.append({
            "title": f"{lat:.2f},{lon:.2f} {t} {mn[i]}°C~{mx[i]}°C",
            "url": f"https://open-meteo.com/",
            "snippet": "Open-Meteo 预报",
            "published_at": t,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="天气查询（免 Key，wttr.in 主用 / Open-Meteo 兜底）")
    parser.add_argument("loc", nargs="*", help="地点名，或 lat lon 两个位置参数")
    parser.add_argument("--n", type=int, default=3, help="最多返回条数")
    args = parser.parse_args()

    loc = _parse_location(args.loc)
    rows = []
    try:
        rows = _wttr(loc)
    except Exception:
        rows = _open_meteo(loc)
    if not rows:
        try:
            rows = _open_meteo(loc)
        except Exception:
            rows = []
    if not rows:
        print("No weather data available.", file=sys.stderr)
        return 1

    import yaml
    print(yaml.safe_dump(rows[: max(1, args.n)], allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
