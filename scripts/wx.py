#!/usr/bin/env python3
"""天气查询脚本（免 Key，纯标准库）。

能力：
  - 双源并行：wttr.in（信息全：体感/湿度/云量/能见度）+ Open-Meteo
    （结构化：中文 WMO 码 / 降水概率 / 空气质量），快者先返、结果融合去重
  - 地理编码：Open-Meteo Geocoding API（免 Key），中文/拼音/英文城市名 → 坐标，
    让 Open-Meteo 兜底在任意地名输入下可用（此前仅坐标输入可兜底）
  - 空气质量：Open-Meteo Air Quality API（免 Key），当前 PM2.5/PM10 + 等级
  - 天气描述中文化：wttr.in 英文 desc → 中文（WMO 常见词映射表）
  - 输入：地点名（城市/机场码/邮编）或 "lat,lon"，或两个位置参数 lat lon

输出：YAML 结果列表（当前天气 1 条 + 未来预报 N 条），供 cli 引擎解析。
字段：title/url/snippet/published_at。

数据源许可：wttr.in（Apache 2.0）、Open-Meteo 数据（CC BY 4.0，需署名）、
Open-Meteo Geocoding / Air Quality（同 Open-Meteo）。

用法：
  python3 scripts/wx.py "上海"
  python3 scripts/wx.py "31.23,121.47" --n 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse as up
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

_WTTR_BASE = "https://wttr.in/"
_OM_BASE = "https://api.open-meteo.com/v1/forecast"
_GEO_BASE = "https://geocoding-api.open-meteo.com/v1/search"
_AQI_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"
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

# wttr.in 英文天气描述 → 中文（WMO 常见词，先转小写精确匹配）
_WEATHER_EN_ZH = {
    "sunny": "晴", "clear": "晴", "clear sky": "晴",
    "partly cloudy": "多云", "cloudy": "阴", "overcast": "阴",
    "mist": "薄雾", "fog": "雾", "freezing fog": "冻雾", "haze": "霾",
    "light drizzle": "毛毛雨", "drizzle": "毛毛雨", "heavy drizzle": "强毛毛雨",
    "patchy light drizzle": "局部毛毛雨", "patchy light rain": "局部小雨",
    "patchy rain nearby": "局部阵雨", "patchy rain possible": "局部阵雨",
    "light rain": "小雨", "moderate rain": "中雨", "heavy rain": "大雨",
    "light rain shower": "小阵雨", "moderate or heavy rain shower": "强阵雨",
    "torrential rain shower": "暴雨", "moderate or heavy rain with thunder": "雷雨",
    "patchy light rain with thunder": "雷阵雨", "thundery outbreaks possible": "雷阵雨",
    "thunderstorm": "雷暴",
    "light snow": "小雪", "moderate snow": "中雪", "heavy snow": "大雪",
    "patchy snow nearby": "局部降雪", "light snow showers": "小阵雪",
    "moderate or heavy snow showers": "强阵雪", "blowing snow": "吹雪",
    "blizzard": "暴风雪", "patchy light snow with thunder": "雷阵雪",
    "light freezing rain": "冻雨", "moderate or heavy freezing rain": "强冻雨",
    "patchy freezing drizzle nearby": "局部冻毛毛雨", "freezing drizzle": "冻毛毛雨",
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
    """把位置参数归一为查询可用的片段。"""
    if len(args) >= 2:
        try:
            lat, lon = float(args[0]), float(args[1])
            return f"{lat},{lon}"
        except ValueError:
            pass
    loc = args[0] if args else "Beijing"
    loc = _WEATHER_WORDS.sub("", loc).strip()
    return loc or "Beijing"


def _zh(desc: str) -> str:
    """wttr.in 英文天气描述 → 中文；未命中保留原文。"""
    if not desc:
        return ""
    return _WEATHER_EN_ZH.get(desc.strip().lower(), desc)


def _wttr(loc: str, query: str | None = None) -> list[dict]:
    """wttr.in 主源。标题用查询词（用户输入），避免 nearest_area 返回细分区域名。"""
    q = (query or loc).strip() or loc
    url = f"{_WTTR_BASE}{up.quote(loc)}?format=j1"
    data = json.loads(_fetch(url))
    area = (data.get("nearest_area") or [{}])[0]
    country = (area.get("country") or [{}])[0].get("value", "")
    cur = (data.get("current_condition") or [{}])[0]
    desc = ((cur.get("weatherDesc") or [{}])[0].get("value", "") or "")

    def cond_url():
        return f"{_WTTR_BASE}{up.quote(loc)}"

    rows = []
    temp_c = cur.get("temp_C")
    if temp_c is not None:
        title = f"{q} 当前 {temp_c}°C {_zh(desc)}".strip()
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
        title = f"{q} {d} {day.get('mintempC', '?')}°C~{day.get('maxtempC', '?')}°C"
        noon = next((h for h in (day.get("hourly") or []) if str(h.get("time", "")).startswith(("11", "12"))), None)
        parts = []
        if noon:
            desc2 = ((noon.get("weatherDesc") or [{}])[0].get("value", ""))
            if desc2:
                parts.append(_zh(desc2))
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


def _geocode(loc: str) -> dict | None:
    """Open-Meteo Geocoding：中文/拼音/英文城市名 → {lat, lon, name, country}。"""
    params = {"name": loc, "count": 1, "language": "zh", "format": "json"}
    url = f"{_GEO_BASE}?{up.urlencode(params)}"
    try:
        data = json.loads(_fetch(url, timeout=6))
    except Exception:
        return None
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    return {
        "lat": r.get("latitude"), "lon": r.get("longitude"),
        "name": r.get("name") or loc, "country": r.get("country") or "",
    }


def _aqi_level(pm25: float | None) -> str:
    """PM2.5 日均浓度 → 中国标准等级（ug/m³）。"""
    if pm25 is None:
        return ""
    if pm25 < 35:
        return "优"
    if pm25 < 75:
        return "良"
    if pm25 < 115:
        return "轻度污染"
    if pm25 < 150:
        return "中度污染"
    if pm25 < 250:
        return "重度污染"
    return "严重污染"


def _aqi(lat: float, lon: float) -> str | None:
    """Open-Meteo Air Quality：当前 PM2.5/PM10 + 等级。失败不阻塞预报。"""
    params = {"latitude": lat, "longitude": lon,
              "current": "pm10,pm2_5", "timezone": "auto"}
    url = f"{_AQI_BASE}?{up.urlencode(params)}"
    try:
        data = json.loads(_fetch(url, timeout=6))
        cur = data.get("current") or {}
    except Exception:
        return None
    pm25 = cur.get("pm2_5")
    pm10 = cur.get("pm10")
    if pm25 is None and pm10 is None:
        return None
    parts = []
    if pm25 is not None:
        parts.append(f"PM2.5 {pm25:.1f}µg/m³")
    if pm10 is not None:
        parts.append(f"PM10 {pm10:.1f}µg/m³")
    level = _aqi_level(pm25)
    if level:
        parts.append(level)
    return " · ".join(parts)


def _open_meteo(loc: str) -> list[dict]:
    """Open-Meteo 兜底源：坐标或地名（自动地理编码）。含降水概率与空气质量。"""
    try:
        lat, lon = (float(x) for x in loc.split(","))
        label = loc
    except (ValueError, AttributeError):
        place = _geocode(loc)
        if not place:
            return []
        lat, lon = place["lat"], place["lon"]
        label = f"{place['name']}{(' ' + place['country']) if place.get('country') else ''}"
    params = {
        "latitude": lat, "longitude": lon, "current_weather": "true",
        "timezone": "auto", "forecast_days": 3,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }
    url = f"{_OM_BASE}?{up.urlencode(params)}"
    data = json.loads(_fetch(url))
    cur = data.get("current_weather") or {}
    rows = []
    if cur:
        code = _WMO.get(cur.get("weathercode", -1), "未知")
        row = {
            "title": f"{label} 当前 {cur.get('temperature', '?')}°C {code}",
            "url": "https://open-meteo.com/",
            "snippet": f"风速 {cur.get('windspeed', '?')}km/h · 风向 {cur.get('winddirection', '?')}° · 观测 {cur.get('time', '')}",
            "published_at": (cur.get("time") or "")[:10],
        }
        aqi = _aqi(lat, lon)
        if aqi:
            row["snippet"] += f" · {aqi}"
        rows.append(row)
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    mx = daily.get("temperature_2m_max") or []
    mn = daily.get("temperature_2m_min") or []
    pp = daily.get("precipitation_probability_max") or []
    for i, t in enumerate(times[:2]):
        if i >= len(mx) or i >= len(mn):
            break
        parts = [f"{mn[i]}°C~{mx[i]}°C"]
        if i < len(pp) and pp[i] is not None:
            parts.append(f"降雨概率 {pp[i]}%")
        rows.append({
            "title": f"{label} {t} {' · '.join(parts)}",
            "url": "https://open-meteo.com/",
            "snippet": "Open-Meteo 预报",
            "published_at": t,
        })
    return rows


def _safe(fn, *args, **kwargs):
    """包装：异常转 (空列表, 错误信息)，保证并行线程内不抛。"""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001
        return [], str(e)


def _merge(wttr_rows: list[dict], om_rows: list[dict]) -> list[dict]:
    """融合去重：当前天气 wttr 优先（信息全），预报按日期 OM 优先（含降水概率）。"""
    cur = None
    if wttr_rows:
        cur = wttr_rows[0]
    elif om_rows:
        cur = om_rows[0]
    by_date: dict[str, dict] = {}
    if om_rows:
        for r in om_rows[1:]:
            by_date.setdefault(r.get("published_at", ""), r)
    if wttr_rows:
        for r in wttr_rows[1:]:
            by_date.setdefault(r.get("published_at", ""), r)
    rows = [cur] if cur else []
    rows += [by_date[d] for d in sorted(d for d in by_date if d)]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="天气查询（免 Key，wttr.in + Open-Meteo 双源并行）")
    parser.add_argument("loc", nargs="*", help="地点名，或 lat lon 两个位置参数")
    parser.add_argument("--n", type=int, default=3, help="最多返回条数")
    args = parser.parse_args()

    loc = _parse_location(args.loc)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_w = ex.submit(_safe, _wttr, loc, loc)
        f_o = ex.submit(_safe, _open_meteo, loc)
        wttr_rows, w_err = f_w.result()
        om_rows, o_err = f_o.result()

    errors = [e for e in (w_err, o_err) if e]
    rows = _merge(wttr_rows, om_rows)
    if not rows:
        detail = f" ({'; '.join(errors)})" if errors else ""
        print(f"No weather data available.{detail}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"wx: {e}", file=sys.stderr)

    import yaml
    print(yaml.safe_dump(rows[: max(1, args.n)], allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
