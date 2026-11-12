#!/usr/bin/env python3
"""天气数据源构建器：Open-Meteo 全球天气（免认证）。

从 engines_builders_intl.py 拆出（2026-11-11）：该文件 2026-11-11 补全
Open-Meteo 逐日预报后达 1012 行，越过 tests/test_module_size_gate.py 的
1000 行硬上限。拆分口径按「数据源族」——本文件收天气族，与
engines_builders_data*.py / engines_builders_finance.py 同构。

覆盖：
  open_meteo      全球天气（geocoding + current + daily forecast，免认证）

行身份约定：当前天气与逐日预报共用同一个坐标页，裸 URL 会被 argo 的 URL
去重（deduplicate_by_url 按归一 URL 折叠）压成 1 条。数据行的内容身份放进
URL 查询参数（?day=YYYY-MM-DD），与 train.py 的 _row_url、fred/worldbank
时序引擎同约定。
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from engines_base import safe_search, rank_score
from bounded_run import run_bounded
from engines_http import http_json as _http_json

logger = logging.getLogger("unified_search.engines")


# 地理编码类查询的自然语言噪声：geocoding 只吃地名，「今日北京天气」
# 「北京今天多少度」这类完整问句会让 name= 带上时间词与疑问词，上游 0 候选
# → 引擎静默返回空（wx.py / weather_cn 各有一份同型正则，漏掉任一处该查询
# 在对应源就全军覆没）。只剥「查询表达」不碰地名本身：坐标串原样通过。
_GEO_COORD_RE = re.compile(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?")
_GEO_NOISE_RE = re.compile(
    r"(天气|气温|温度|多少度|预报|怎么样|如何|下雨|降水|雪|晴|阴|"
    r"今[天日晚]|明[天日晚]|后[天日]|昨[天日]|现在|实时|当前|查询|"
    r"的|了|呢|吗|\?|？|，|,|。|\s|"
    # 英文天气词：官方文档的 name 只吃「Location name or postal code」，
    # 不做问句清洗。实测 "New York weather" / "weather in Tokyo" 送进
    # geocoding 全 0 候选（2026-11-12 六种写法复测）。长组合须排在裸词前，
    # 否则 "weather in Tokyo" 的 weather 先被裸词吃掉、只剩 "in Tokyo"
    # 仍然 0 候选。in 只在与天气词成组时剥，不误伤 Innsbruck / Virginia
    # / The Hague 这类含 in 的地名。
    r"(?i:\b(?:weather|forecast|temperature)\s+in\b)|"
    r"(?i:\b(?:weather|forecast|temperature|degrees?)\b))+",
)
# 时间词分两类处理：单词（今天/明天）由 _GEO_NOISE_RE 剥掉，带数字的跨度
# （未来3天）由 _FORECAST_SPAN_RE 单独提取——后者同时决定 forecast_days，
# 剥完就丢了。顺序：先取跨度，再剥单词。
_FORECAST_SPAN_RE = re.compile(r"(?:未来|接下来|今后)?\s*(\d{1,2})\s*天")
# 默认预报天数：无显式跨度词时给 3 天（对齐 wx.py 的 forecast_days=3），
# 让「北京天气」这类裸查询也带逐日预报，而不是只有当前天气一条。
_FORECAST_DEFAULT_DAYS = 3


def forecast_span(query: str) -> int:
    """提取预报天数 → forecast_days（1–16）。无跨度词回落默认值。

    超 Open-Meteo 的 16 天窗口部分夹紧（上游对超范围直接报错，夹紧比报错
    对调用方友好）；0 与非法值回落默认值。
    """
    m = _FORECAST_SPAN_RE.search(query or "")
    if not m:
        return _FORECAST_DEFAULT_DAYS
    days = int(m.group(1))
    if days <= 0:
        return _FORECAST_DEFAULT_DAYS
    return min(days, 16)


def geo_core(query: str) -> str:
    """剥掉天气问句的时间/疑问词，留地名；空则回退原查询（不猜）。"""
    raw = (query or "").strip()
    # 坐标串（lat,lon）不是问句，原样透传——剥了逗号反而毁掉它
    if _GEO_COORD_RE.fullmatch(raw):
        return raw
    # 跨度词（未来3天）先摘：含数字与「天」，_GEO_NOISE_RE 不剥数字，
    # 不先摘会让地名变成「北京未来3天」→ geocode 零候选、引擎静默返回空
    # （2026-11-11 实测「北京未来3天天气」返回 []）。
    raw = _FORECAST_SPAN_RE.sub(" ", raw).strip()
    q = _GEO_NOISE_RE.sub(" ", raw).strip()
    return q or raw


# WMO 天气码 → 中文（与 wx.py 的 _WMO 同源；两处都要改时一起改）
_OM_WMO = {
    0: "晴", 1: "基本晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇", 51: "毛毛雨", 53: "小雨", 55: "中雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "霰",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


def _row_url(base_url: str, day: str = "") -> str:
    """行身份 URL。

    当前天气与逐日预报共用同一个坐标页，裸 URL 会让 argo 的 URL 去重
    （deduplicate_by_url 按归一 URL 折叠）把 12 条压成 1 条（2026-11-11
    实测 returned 12 → deduped 3）。修法走本仓既有约定（train.py 的
    _row_url、fred/worldbank 时序引擎）：数据行的内容身份放进 URL 查询
    参数——url_canon 只删追踪参数不删语义参数。day 标行，空串为当前天气。
    """
    if not day:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}day={day}"


def build_open_meteo_engine(spec: dict[str, Any]) -> Any:
    """Open-Meteo 全球天气（geocoding + current + daily forecast，免认证）。

    查询词视为城市名/地名，先 geocode 取坐标，再取当前天气与逐日预报。
    预报天数由查询里的跨度词决定（「未来3天」→ 3 天），无跨度词默认 3 天。

    取数统一走本模块的 `_http_json`（engines_http 的薄导入），测试只需
    patch.object(weather, "_http_json")，不再有第二套 bind_fetch 入口。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        # 预报天数先取：span 从原查询提取，geo_core 只负责剥地名噪声
        forecast_days = forecast_span(query)
        # geocoding 只吃地名：完整天气问句（「今日北京天气」）先剥时间/疑问词，
        # 否则上游 0 候选、引擎静默返回空
        place_q = geo_core(query)
        try:
            geo = _http_json(
                f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(place_q)}&count=3&language=zh",
                to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Open-Meteo geocode 失败: {e}")
            return []
        # 只取最相关的一个候选：天气查询的语义是「一个地点」，而 Open-Meteo
        # 对中文地名常返回多个同名地（实测「北京」返回 3 个：北京 + 重庆某地
        # + 四川某地，全在中国境内，countryCode 收窄无效）。展开全部候选会让
        # 12 条结果里 9 条是同名地点的重复预报，top-5 全被稀释。
        places = [p for p in (geo.get("results") or [])[:1]
                  if p.get("latitude") is not None and p.get("longitude") is not None]
        if not places:
            return []

        def _fetch_place(item: tuple[int, dict[str, Any]]):
            """单个 place 的当前天气 + 逐日预报；失败返回 None。

            返回结构：(place_rank, [rows])。逐日预报按日展开成多条结果，
            让「未来3天天气」有 3 条可独立排序与引用的记录，而不是塞进
            一条 snippet。
            """
            _rk9, place = item
            lat, lon = place["latitude"], place["longitude"]
            try:
                wx = _http_json(
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                    f"&current_weather=true&forecast_days={forecast_days}"
                    f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                    # 必须用 auto：写死 Asia/Shanghai 时，纽约的「11-11」实际是
                    # 上海日界（前一日 13:00 起），逐日序列整体错一天。auto 由
                    # 坐标决定时区，geocode 返回的 place 已带经纬度。
                    f"&timezone=auto",
                    to, engine=spec.get("_name", ""))
            except Exception:
                return None
            name = place.get("name", "")
            country = place.get("country", "")
            base_url = f"https://open-meteo.com/en/docs?latitude={lat}&longitude={lon}"
            rows = []

            cw = wx.get("current_weather") or {}
            temp = cw.get("temperature")
            windspeed = cw.get("windspeed")
            wcode = cw.get("weathercode")
            wmo = _OM_WMO.get(wcode, f"code{wcode}")
            if temp is not None:
                rows.append({
                    "title": f"{name} · {country} 当前天气",
                    "url": _row_url(base_url),
                    "snippet": f"当前 {temp}°C · {wmo} · 风速 {windspeed}km/h"[:300],
                    "published_at": (cw.get("time") or "")[:10],
                })

            daily = wx.get("daily") or {}
            times = daily.get("time") or []
            tmax = daily.get("temperature_2m_max") or []
            tmin = daily.get("temperature_2m_min") or []
            precip = daily.get("precipitation_probability_max") or []
            # 列式返回：各数组按下标对齐，长度不齐时以最短为准（上游偶尔
            # 在窗口边缘少给一天），逐位校验避免错位。
            for i, day in enumerate(times):
                if i >= len(tmax) or i >= len(tmin):
                    break
                parts = [f"{tmin[i]}°C~{tmax[i]}°C"]
                if i < len(precip) and precip[i] is not None:
                    parts.append(f"降雨概率 {precip[i]}%")
                rows.append({
                    "title": f"{name} · {country} {day} 预报",
                    "url": _row_url(base_url, day),
                    "snippet": " · ".join(parts)[:300],
                    "published_at": day,
                })
            return (_rk9, rows)

        # 多 place 的 forecast 并行（2026-09-29）：串行版在 geocode 返回 3 个
        # 候选时墙钟 = geocode + 3×forecast（实测 9.28s 恒定）；有界并发后
        # = geocode + max(forecast)（实测 3.9-5.1s）。总上限 to+2s ≤ 串行
        # 最坏 n×to；daemon 线程 + 轮询，卡住的 HTTP 读拖不垮止损
        # （bounded_run 文档依据；与 wx.py:252「同坐标多端点并行」同哲学）。
        finished, _unfinished = run_bounded(
            list(enumerate(places)), _fetch_place,
            wait_s=float(to) + 2.0, max_workers=3)
        # 保序 + 打分：place 档位用 rank_score 的 rank（跨候选递减，与并行前
        # 一致）；组内逐日预报再用全局累积 rank 续排，保证整个结果序列严格
        # 递减（同一 place 内 当前 > 第1天 > 第2天…，且下一个 place 的首条
        # 仍低于上一个 place 的末条）。rank 取整数位，避免小数被 int() 截断
        # 抹平组内区分度（RANK_DECAY=0.03，rank 差 1 约 3% 衰减）。
        ordered = sorted(
            (v for _i, v in finished if isinstance(v, tuple)),
            key=lambda t: t[0])
        results: list[dict[str, Any]] = []
        rank_base = 0
        for _rk, rows in ordered:
            for row in rows:
                row["source"] = "open_meteo"
                row["score"] = rank_score(0.7, rank_base)
                results.append(row)
                rank_base += 1
        # 遵守调用方的 n：逐日展开后最多 17 条，调用方只要 5 条时不该被
        # 灌满。保序截断保证留下的是当前天气与最近的几天。
        return results[:max(1, int(n or 1))]
    return _engine
