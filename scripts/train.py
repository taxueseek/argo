#!/usr/bin/env python3
"""火车余票/中转/经停查询脚本（免 Key，纯标准库）。

能力（原生吸纳自外部列车查询技能，不保留其名称）：
  - 余票：官方两步接口 GET leftTicket/init 取会话 Cookie
    → GET leftTicket/queryG 查余票（返回 URL 编码的管道分隔行）
  - 中转：GET lcQuery/init 运行时解析 lc_search_url → lcquery/queryG
    按 result_index 翻页取换乘方案（前段+换乘站+后段）
  - 经停：search.12306.cn 车次号搜 train_no → otn/queryTrainInfo/query
    查全程经停（整条路线压成一行，防输出端截断）
  - 站点表 station_name.js 解析 + 7 天本地缓存
  - 输入：自然语言查询词（"上海到北京" / "北京→上海 后天 G" /
    "北京到拉萨 中转（经西宁）" / "G2 经停"）
  - 输出：YAML 结果列表，行 URL 带查询身份参数（同查询多行不共享裸 URL，
    不会被 argo 的 URL 去重折叠——fred/worldbank 时序引擎同款约定）

用法：
  python3 scripts/train.py "上海到北京"
  python3 scripts/train.py "北京→上海 后天 G" --n 10
  python3 scripts/train.py "北京到拉萨 中转"
  python3 scripts/train.py "G2 经停"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.parse as up
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from net_proxy import open_url  # 出口调度唯一入口（issue #13 同类修复）

_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
_QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/queryG"
_LC_INIT_URL = "https://kyfw.12306.cn/otn/lcQuery/init"
_TRAIN_INFO_URL = "https://kyfw.12306.cn/otn/queryTrainInfo/query"
_SEARCH_API_URL = "https://search.12306.cn/search/v1/train/search"
_STATION_JS_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
_CACHE_TTL_SECONDS = 7 * 24 * 3600


def _cache_dir() -> Path:
    """站点表缓存目录：argo 状态目录下的 train/，**不写进源码树**。

    此前是 `scripts/data/`（源码树内的目录），两个后果都实测过：

    1. 测试污染生产缓存：tests/conftest.py 只隔离了 ARGO_STATE_DIR，而这里
       绕过了它。2026-09-19 一次全量 pytest 把夹具里的 4 个站写进
       scripts/data/stations.json，此后 7 天（TTL）真实查询只认那 4 个站，
       「张家口 到 北京」这类站名全部解析失败——静默降级，没有任何报错。
       （conftest 的注释记着同一类事故：v2ex 节点表曾把假节点写进
       ~/.cache/unified-search/。）
    2. npx / 只读安装下源码树不可写，缓存必须落到可写的位置。

    与 circuit_breaker / admission / health 同惯例：走 argo_paths.state_path()，
    测试会话自动隔离，平台惯例目录也自动生效。
    """
    try:
        import argo_paths
        return argo_paths.state_path("train")
    except Exception:
        # 兜底也不能落回源码树：scripts/data/ 正是「测试污染生产缓存」的事故
        # 现场，兜底回那里等于把刚修掉的坑重新挖开。退到系统临时目录仍然
        # fail-open（火车查询不会整个挂掉），且不再可能被测试写进仓库。
        return Path(tempfile.gettempdir()) / "argo-train-cache"

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


# 查询模式路由：车次号（G/D/C/Z/T/K/L/Y + 1-5 位数字）。前后禁字母数字：
# Python 的 \b 对 CJK 无效（\w 含汉字，「G2经停」在 2 和 经 之间没有词边界）。
_TRAIN_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([GDCZTKLY]\d{1,5})(?![0-9A-Za-z])")
_ROUTE_WORDS = ("经停", "途经", "路过", "停靠", "停站", "时刻")
_TRANSFER_WORDS = ("中转", "换乘")


def _parse_travel_date(text: str) -> tuple[date | None, str]:
    """提取日期词 → (date, 去掉该词后的文本)。三种查询模式共用。"""
    m = _DATE_RE.search(text)
    if not m:
        return None, text
    tok = m.group(1)
    text = text.replace(tok, " ", 1)
    if tok in _DATE_WORD:
        return _today_cn(_DATE_WORD[tok]), text
    if re.match(r"^\d{4}", tok):
        try:
            return datetime.strptime(tok.replace("/", "-"), "%Y-%m-%d").date(), text
        except ValueError:
            return None, text
    ymd = re.match(r"(\d{1,2})月(\d{1,2})[日号]", tok)
    if ymd:
        mm, dd = int(ymd.group(1)), int(ymd.group(2))
        try:
            return date(_today_cn().year, mm, dd), text
        except ValueError:
            return None, text
    return None, text


def _parse_query(q: str) -> dict | None:
    """从自然语言查询词解析 出发站/到达站/日期/车次类型。

    支持："上海到北京"、"北京→上海 后天"、"2026-08-10 北京 上海 高铁"、"从北京到上海 G"。
    返回 dict 或 None（无法解析出起止站）。
    """
    text = (q or "").strip()
    if not text:
        return None

    # 1) 日期词 → date（经停/中转模式共用 _parse_travel_date）
    travel_date, text = _parse_travel_date(text)

    # 2) 车次类型词 → type
    train_type = ""
    m = _TYPE_WORD_RE().search(text)
    if m:
        train_type = _TYPE_WORD[m.group(1)]
        text = text.replace(m.group(1), " ", 1)

    # 3) 去掉「从」前缀，按分隔符拆分起止站
    text = re.sub(r"^从", " ", text.strip())
    # 「N张」是购票数量，不是站名：不摘掉的话「1张 北京 到 上海」的起站会变成
    # 「1张」（实测 2026-09-21）。判据是「独立成词」而不是「后面不接某几个字」：
    # 负向列举永远列不全（实测「买1 张家口 到 北京」会被吃掉张字变成「家口」），
    # 正向断言词尾才是这条规则的真实含义。
    text = re.sub(r"\d{1,2}\s*张(?:票)?(?=\s|$|[，。！？、]|到|→|->|=>|至)", " ", text)
    parts = re.split(r"到|→|->|=>|至|—|--|\s+", text)
    # 只去空白与句读，**不要按字符集剥站名**：站名里含「张/高/次/包/头/站/车/票」
    # 的很多，`strip("站车票张次列高动直特快速字头")` 会把首字当填充字符吃掉——
    # 2026-09-21 实测「张家口 到 北京」→「家口」、「包头 到 北京」→「包」、
    # 「高碑店」→「碑店」、「次渠」→「渠」。站名后缀（北京站/上海市）由
    # _resolve_station 统一剥离（它已有 `[市站]$`），这里不必也不该再剥一遍。
    parts = [p.strip(" \t，。！？、") for p in parts if p.strip()]
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


def _parse_mode(text: str) -> str:
    """查询模式路由：route（经停）/ transfer（中转）/ tickets（余票，默认）。

    车次号 + 经停类词 → route；裸车次号（除车次号只剩「次/车/时刻表」这类
    后缀词）→ route——「G2」「G2次」单独出现时问的只能是这趟车本身；
    含中转/换乘 → transfer；其余走原有余票解析。
    """
    m = _TRAIN_CODE_RE.search(text)
    if m and any(w in text for w in _ROUTE_WORDS):
        return "route"
    if m:
        rest = (text[: m.start()] + text[m.end():]).strip(" \t，。！？、")
        if not rest or re.fullmatch(r"次|车|列车|时刻表|的时刻表", rest):
            return "route"
    if any(w in text for w in _TRANSFER_WORDS):
        return "transfer"
    return "tickets"


# 换乘站提取：「中转（经/停/于/站）X」「在X中转」「经X」三式。捕获用非贪婪 +
# 边界字符收尾（下一位必须是 到/至/空白/标点/结尾，不许跨字扫描）：前瞻里若
# 写 `[\u4e00-\u9fff]*(?:到|至)`，前缀能跳过任意汉字，「中转南京南到上海」会
# 在「南京」处提前满足边界截成两半。提取结果必须过 _resolve_station 认证——
# 解析不出站码的捕获一律弃用，宁可丢中转站也不能把站名误当换乘站。
_MIDDLE_RES = (
    re.compile(r"在([\u4e00-\u9fff]{2,8}?)(?:中转|换乘)"),
    re.compile(r"(?:中转|换乘)(?:经|停|经停|于|站)?([\u4e00-\u9fff]{2,8}?)"
               r"(?=到|至|$|\s|[，。！？、])"),
    re.compile(r"经(?:停|于)?([\u4e00-\u9fff]{2,8}?)"
               r"(?=到|至|$|\s|[，。！？、])"),
)


def _parse_middle(text: str, data: dict) -> tuple[dict | None, str]:
    """从 transfer 查询词提取换乘站 → (站点码 dict, 去掉该片段后的文本)。"""
    for rex in _MIDDLE_RES:
        m = rex.search(text)
        if not m:
            continue
        st = _resolve_station(data, m.group(1))
        if st:
            return st, (text[: m.start()] + " " + text[m.end():])
    return None, text


def _fetch(url: str, headers: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(url, headers={**_HEADERS, **(headers or {})})
    return open_url(req, timeout=timeout)


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
    cache_dir = cache_dir or _cache_dir()
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


def _row_url(route: dict | None, train_code: str) -> str:
    """余票行的行身份 URL。

    余票行没有独立落地页，载体都是同一个 init 查询页；此前每行发裸 URL，
    argo 的 URL 去重（deduplicate_by_url 按归一 URL 折叠）把 10 条车次压成
    1 条（2026-10-06 canary 实测 returned 10 → deduped 1）。修法走本仓
    既有约定（fred/worldbank 等时序引擎）：数据行的内容身份放进 URL 查询
    参数——url_canon 只删追踪参数不删语义参数，search_rank 的
    _distinct_data_rows 亦按「同文档、不同查询」豁免 minhash 近重复。
    参数形态取自 queryG 真实请求，train_code 标行。
    """
    if not route:
        return "https://kyfw.12306.cn/otn/leftTicket/init"
    qs = up.urlencode({
        "leftTicketDTO.train_date": route.get("date", ""),
        "leftTicketDTO.from_station": route.get("from_code", ""),
        "leftTicketDTO.to_station": route.get("to_code", ""),
        "purpose_codes": "ADULT",
        "train_code": train_code,
    })
    return f"https://kyfw.12306.cn/otn/leftTicket/init?{qs}"


def _build_rows(tickets: list[dict], limit: int,
                route: dict | None = None) -> list[dict]:
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
            "url": _row_url(route, t["trainCode"]),
            "snippet": " · ".join(["历时 " + _fmt_duration(t.get("duration", ""))] + seats + [status]),
            "published_at": published_at,
        })
    return rows


def _fmt_minutes(raw: str) -> str:
    try:
        return f"{int(raw)}分"
    except (TypeError, ValueError):
        return ""


def _norm_date(raw) -> str:
    d = (raw or "").strip()
    if re.match(r"^\d{8}$", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _search_train_no(code: str, travel_date: str, cookie: str,
                     timeout: float = 15) -> str | None:
    """车次号（G2）→ 内部编号 train_no。search.12306.cn 关键字搜索接口，
    优先取 station_train_code 精确命中，退而取首条。"""
    params = up.urlencode({"keyword": code, "date": travel_date.replace("-", "")})
    with _fetch(f"{_SEARCH_API_URL}?{params}",
                headers={"Cookie": cookie, "Referer": _INIT_URL}, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    for it in items:
        if isinstance(it, dict) and it.get("station_train_code") == code and it.get("train_no"):
            return it["train_no"]
    for it in items:
        if isinstance(it, dict) and it.get("train_no"):
            return it["train_no"]
    return None


def _route_api(train_no: str, travel_date: str, cookie: str,
               timeout: float = 15) -> list[dict]:
    """train_no + 日期 → 全程经停列表（queryTrainInfo/query，经停站在 data.data）。"""
    params = up.urlencode({
        "leftTicketDTO.train_no": train_no,
        "leftTicketDTO.train_date": travel_date,
        "rand_code": "",
    })
    with _fetch(f"{_TRAIN_INFO_URL}?{params}",
                headers={"Cookie": cookie, "Referer": _INIT_URL}, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    data = payload.get("data") if isinstance(payload, dict) else None
    stations = data.get("data") if isinstance(data, dict) else None
    if not isinstance(stations, list):
        return []
    return [s for s in stations if isinstance(s, dict)]


def _get_lcquery_path(timeout: float = 15) -> str:
    """中转查询路径运行时解析（init 页 JS 变量 var lc_search_url = '...'）。

    12306 会无预告改查询路径（余票侧 CLeftTicketUrl 同款机制，外部实现亦
    靠运行时解析跟随）。解析失败必须报错而不是静默回落旧路径——回落后
    「接口改版」会被伪装成「无中转方案」，比报错难查得多。
    """
    with _fetch(_LC_INIT_URL, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"},
                timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r"var\s+lc_search_url\s*=\s*'(.+?)'", html)
    if not m:
        raise RuntimeError("lc_search_url 解析失败（12306 中转查询路径可能已改版）")
    return m.group(1)


def _transfer_api(from_code: str, to_code: str, middle_code: str, travel_date: str,
                  cookie: str, limit: int = 5, timeout: float = 15):
    """中转换乘查询（lcquery/queryG），按 result_index 翻页取满 limit 条。

    返回 (方案列表, 实际查询路径, 错误消息)。can_query=='N'、单批为空或
    翻到第 5 页（防 can_query 恒 Y 时死循环）即停。
    """
    path = _get_lcquery_path(timeout=timeout)
    plans: list[dict] = []
    result_index = 0
    for _page in range(5):
        params = up.urlencode({
            "train_date": travel_date,
            "from_station_telecode": from_code,
            "to_station_telecode": to_code,
            "middle_station": middle_code,
            "result_index": str(result_index),
            "can_query": "Y",
            "isShowWZ": "N",
            "purpose_codes": "00",
            "channel": "E",
        })
        with _fetch(f"https://kyfw.12306.cn{path}?{params}",
                    headers={"Cookie": cookie, "Referer": _LC_INIT_URL}, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            msg = payload.get("errorMsg") if isinstance(payload, dict) else ""
            return plans, path, (msg or "中转接口返回异常")
        batch = [p for p in (data.get("middleList") or []) if isinstance(p, dict)]
        plans.extend(batch)
        if len(plans) >= limit or data.get("can_query") == "N" or not batch:
            break
        nxt = data.get("result_index")
        result_index = nxt if isinstance(nxt, int) else result_index + len(batch)
    return plans, path, None


def _build_route_row(code: str, stations: list[dict], travel_date: str,
                     train_no: str) -> dict:
    """经停行：整条路线压成**一行**。

    不按站拆行——经停查询的答案是有序完整列表，按站拆行后 argo 输出端
    按 max_results 截断只露前几站，恰好截掉用户要的那一站；一行承载全表
    则不受输出截断影响（snippet 无管线级截断，31 站约 600 字）。
    """
    segs = []
    for s in stations:
        name = (s.get("station_name") or "?").strip() or "?"
        # 12306 的空位可以是 "--" 也可以是 "----"，按纯杠串统一视为缺省
        arrive = (s.get("arrive_time") or "").strip()
        start = (s.get("start_time") or "").strip()
        stop = (s.get("stopover_time") or "").strip()
        arrive = "" if arrive.strip("-") == "" else arrive
        start = "" if start.strip("-") == "" else start
        stop = "" if stop.strip("-") == "" else stop
        if arrive in ("", "--") and start not in ("", "--"):
            t = f"{start}开"
        elif start in ("", "--"):
            t = f"{arrive}到"
        else:
            t = f"{arrive}到/{start}开"
        segs.append(f"{name} {t}" + (f"(停{stop}分)" if stop not in ("", "--") else ""))
    qs = up.urlencode({
        "leftTicketDTO.train_no": train_no,
        "leftTicketDTO.train_date": travel_date,
    })
    return {
        "title": f"{code} 经停站 {travel_date} · 共{len(stations)}站",
        "url": f"{_TRAIN_INFO_URL}?{qs}",
        "snippet": " → ".join(segs),
        "published_at": travel_date,
    }


def _build_transfer_rows(plans: list[dict], limit: int, ctx: dict) -> list[dict]:
    """中转方案行：一行 = 前段车次 + 换乘站 + 后段车次。

    行 URL 带 first_train_no 标行身份——同一次查询的方案行共享查询参数，
    没有 first_train_no 会在 URL 去重里折叠成一条（同 _row_url 的教训）。
    """
    rows = []
    for p in plans[: max(1, limit)]:
        full = [s for s in (p.get("fullList") or []) if isinstance(s, dict)]
        first = full[0] if full else {}
        last = full[-1] if full else {}
        fcode = (first.get("station_train_code") or "?").strip()
        lcode = (last.get("station_train_code") or "?").strip()
        mid = (p.get("middle_station_name") or "").strip() or "中转"
        parts = []
        lishi = _fmt_duration((p.get("all_lishi") or "").strip())
        if lishi:
            parts.append(f"总历时 {lishi}")
        wait = _fmt_minutes((p.get("wait_time") or "").strip())
        parts.append(f"换乘 {mid}" + (f" 等{wait}" if wait else ""))
        if first:
            parts.append(f"{fcode} {first.get('start_time', '')}→"
                         f"{first.get('to_station_name') or mid} {first.get('arrive_time', '')}")
        if last:
            parts.append(f"{lcode} {last.get('from_station_name') or mid} "
                         f"{last.get('start_time', '')}→{last.get('to_station_name', '')} "
                         f"{last.get('arrive_time', '')}")
        seats = []
        for key, label in _SEAT_LABELS:
            v = (last.get(f"{key}_num") or "").strip()
            if v and v != "--":
                seats.append(f"{label} {v}")
        if seats:
            parts.append("末段 " + " · ".join(seats))
        qs = up.urlencode({
            "train_date": ctx.get("date", ""),
            "from_station_telecode": ctx.get("from_code", ""),
            "to_station_telecode": ctx.get("to_code", ""),
            "middle_station": ctx.get("middle_code", ""),
            "first_train_no": p.get("first_train_no") or fcode,
        })
        rows.append({
            "title": (f"{fcode}+{lcode} {p.get('from_station_name', '')}→{mid}"
                      f"→{p.get('end_station_name', '')} "
                      f"{p.get('start_time', '')}→{p.get('arrive_time', '')}"),
            "url": f"https://kyfw.12306.cn{ctx.get('path', '/lcquery/queryG')}?{qs}",
            "snippet": " · ".join(parts),
            "published_at": _norm_date(p.get("train_date")) or ctx.get("date", ""),
        })
    return rows


def _print_rows(rows: list[dict]) -> None:
    import yaml
    print(yaml.safe_dump(rows, allow_unicode=True, sort_keys=False))


def _run_tickets(text: str, n: int) -> int:
    q = _parse_query(text)
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
    code_m = _TRAIN_CODE_RE.search(text)
    if code_m:
        code = code_m.group(1)
        exact = [t for t in tickets if t["trainCode"] == code]
        # 点名车次（"北京到上海 G531"）按精确车次过滤；当日查无此车时
        # 回落全量——点名扑空不该连别的车也不给看
        if exact:
            tickets = exact

    route = {"date": q["date"], "from_code": frm["station_code"],
             "to_code": to["station_code"]}
    _print_rows(_build_rows(tickets, n, route))
    return 0


def _run_route(text: str) -> int:
    m = _TRAIN_CODE_RE.search(text)
    if not m:
        print("经停查询需要车次号，如：G2 经停", file=sys.stderr)
        return 2
    code = m.group(1)
    travel_date, _ = _parse_travel_date(text)
    d = (travel_date or _today_cn(1)).isoformat()

    cookie = _get_cookie()
    train_no = _search_train_no(code, d, cookie)
    if not train_no:
        print(f"未找到 {code} 在 {d} 的车次编号（可能当日无此车）", file=sys.stderr)
        return 2
    stations = _route_api(train_no, d, cookie)
    if not stations:
        print(f"未查到 {code} 的经停信息（{d}）", file=sys.stderr)
        return 2
    _print_rows([_build_route_row(code, stations, d, train_no)])
    return 0


def _run_transfer(text: str, n: int) -> int:
    data = _load_stations()
    middle, text = _parse_middle(text, data)
    text = re.sub(r"中转|换乘", " ", text)

    q = _parse_query(text)
    if not q:
        print("无法解析起止站：请用「出发站 到 到达站 中转（可「经X」指定换乘站）」"
              "的格式，如：北京到拉萨 中转", file=sys.stderr)
        return 2
    frm = _resolve_station(data, q["from"])
    to = _resolve_station(data, q["to"])
    if not frm or not to:
        missing = q["from"] if not frm else q["to"]
        print(f"未找到站点：{missing}", file=sys.stderr)
        return 2

    cookie = _get_cookie()
    plans, path, err = _transfer_api(
        frm["station_code"], to["station_code"],
        (middle or {}).get("station_code", ""), q["date"], cookie, limit=max(n, 5))
    if err:
        print(f"中转查询失败：{err}", file=sys.stderr)
        return 2
    if not plans:
        print("未查到中转方案", file=sys.stderr)
        return 2
    ctx = {"date": q["date"], "path": path,
           "from_code": frm["station_code"], "to_code": to["station_code"],
           "middle_code": (middle or {}).get("station_code", "")}
    _print_rows(_build_transfer_rows(plans, n, ctx))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="火车余票/中转/经停查询（免 Key，12306 官方接口）")
    parser.add_argument("query", nargs="*",
                        help="余票：上海到北京 · 中转：北京到拉萨 中转（可「经西宁」指定换乘站）"
                             " · 经停：G2 经停")
    parser.add_argument("--n", type=int, default=5, help="最多返回条数")
    args = parser.parse_args()

    raw = " ".join(args.query).strip()
    mode = _parse_mode(raw)
    if mode == "route":
        return _run_route(raw)
    if mode == "transfer":
        return _run_transfer(raw, args.n)
    return _run_tickets(raw, args.n)


if __name__ == "__main__":
    sys.exit(main())
