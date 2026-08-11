#!/usr/bin/env python3
"""job.py — 求职岗位搜索 v2：职位 + 地区 → 该地区在招岗位。

用法:
  argo job "工艺工程师" --city 成都            # 严格：只留成都相关岗位
  argo job "工艺工程师" --city 新加坡 --all    # 海外（国家/城市词展开 + 自动启用免 key 国际源）
  argo job "会计" --city 苏州 --loose          # 宽松：异地保留但命中置顶
  argo job "焊工" --city 上海 --json
  argo job "remote engineer" --engine free     # 仅免 key 国际源（远程岗位）

第一性原理：求职搜索 = 检索(招聘平台) + 判定(地区) + 呈现(去重排序+时效标记)。
本脚本只做这三件事，其余一律不做。

v2 相对 v1 的修复与扩展（2026-08-11，全部经实测验证）：
  1. 判定升级：三级判定（标题/URL 强命中 → 摘要头部 → 摘要尾部），
     —— 修复 v1「snippet 任意位置含地区词即命中」被公司简介/福利文本污染的假阳性
     （实测 byted 昆山查询 dropped=0 但混入四川大学/南通大学/企查查异地岗位）
  2. 白名单扩展：核心 6 平台 + 扩展域（卓博/鱼泡/中华英才/智通/58/国聘/24365/
     校园就业联盟/应届生求职网/北京高校毕业生/JobsDB/JobStreet）+ 人社局源
  3. URL 后置校验：非白名单 URL 一律剔除（修复 byted site: 混入非白名单域名）
  4. 时效补强：tavily days=90（实测生效）、日期字段提取、>365 天标记过期
  5. 免 key 国际源：remotive / himalayas / jobicy / arbeitnow / greenhouse / ashby
  6. 国内后端 bocha / octen 全量启用

v3（2026-08-11）：
  7. 结构化字段：snippet 提取薪资/学历/经验/公司（零成本），--fetch N 可选详情页补全
  8. 增量监控：--watch 存快照至 data/jobs/，二次运行对比输出新上线/已下架
  9. 指纹去重：标题+公司指纹替代纯 URL 去重（多平台同岗位合并）
 10. 白名单加北京高校毕业生就业信息网（bysjy.com.cn，实测 200）
"""
import argparse, json, os, re, sys, threading, urllib.request
from datetime import date, timedelta
from urllib.parse import quote
from typing import Optional

# ── 平台白名单 ──────────────────────────────────────────────────────────
# 核心平台（逐平台 site: 查询用，带标签）
PLATFORMS = [("zhipin.com", "BOSS直聘"), ("liepin.com", "猎聘"),
             ("zhaopin.com", "智联招聘"), ("51job.com", "前程无忧"),
             ("597.com", "597直聘"), ("jrzp.com", "今日招聘")]
# 扩展域（includeDomains 聚合查询用，bocha/octen 宽查询校验用）
EXTRA = [("jobcn.com", "卓博人才网"), ("yupao.com", "鱼泡直聘"), ("chinahr.com", "中华英才网"),
         ("job5156.com", "智通人才网"), ("58.com", "58同城"), ("iguopin.com", "国聘"),
         ("ncss.cn", "新职业24365"), ("91job.org.cn", "校园就业联盟"),
         ("yingjiesheng.com", "应届生求职网"), ("bysjy.com.cn", "北京高校毕业生就业信息网"),
         ("jobsdb.com", "JobsDB"), ("jobstreet.com", "JobStreet"),
         ("hrss.suzhou.gov.cn", "苏州人社局")]
DOMAINS = [d for d, _ in PLATFORMS] + [d for d, _ in EXTRA]
ALL_LABELS = dict(PLATFORMS + EXTRA)
# 免 key 国际源域名（自返回 URL，天然可信）
FREE_DOMAINS = [("remotive.com", "Remotive"), ("remote.co", "Remote.co"),
                ("himalayas.app", "Himalayas"), ("jobicy.com", "Jobicy"),
                ("arbeitnow.com", "Arbeitnow"),
                ("boards-api.greenhouse.io", "Greenhouse"),
                ("boards.greenhouse.io", "Greenhouse"),
                ("jobs.ashbyhq.com", "Ashby")]
# 政府人社局域名（地区判定强信任：政府站岗位无异地混淆问题）
GOV_RE = re.compile(r"(hrss|rsj|rlsbj|srs)\.[\w-]+\.gov\.cn")
GOV_LABEL = "人社局/政府"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "data")
STALE_DAYS = 365  # 距今超过该天数的岗位标记过期

# ── 地区索引：查询词 → 展开词集（一次性构建，O(1) 查表）────────────────

_INDEX = None
_COUNTRIES = None


def _clean(name: str) -> str:
    """去行政后缀：北京市→北京、内蒙古自治区→内蒙古、金平苗族瑶族傣族自治县→金平。"""
    name = re.sub(r"(维吾尔自治区|壮族自治区|回族自治区|藏族自治区|蒙古族自治区"
                  r"|特别行政区|自治区|自治州|自治县|自治旗|地区|盟|省|市|区|县|旗|州)$",
                  "", name)
    m = re.search(r"(?:蒙古|维吾尔|哈萨克|柯尔克孜|塔吉克|乌孜别克|俄罗斯|鄂温克|"
                  r"达斡尔|鄂伦春|赫哲|裕固|东乡|保安|撒拉|土族|仡佬|毛南|仫佬|"
                  r"布依|侗|瑶|苗|傣|彝|壮|回|藏|满|白|土家|哈尼|黎|傈僳|佤|畲|"
                  r"高山|拉祜|水|纳西|景颇|羌|布朗|锡伯|阿昌|普米|怒|德昂|京|"
                  r"塔塔尔|独龙|门巴|珞巴|基诺|朝鲜)族", name)
    if m:
        name = name[:m.start()]
    return name


def _build_index() -> dict:
    """省→全省词集；市→该市+区县+省；区县→本身+市+省；海外国家→城市、城市→国家。"""
    idx = {}
    with open(os.path.join(DATA_DIR, "regions_cn.json"), encoding="utf-8") as f:
        regions = json.load(f)
    for prov in regions:
        p = _clean(prov["name"])
        pwords = {p}
        for c in prov.get("children", []):
            c2 = _clean(c["name"])
            cwords = {c2, p} | {_clean(d["name"]) for d in c.get("children", [])}
            idx.setdefault(c2, set()).update(cwords)
            pwords.update(cwords)
            for d in c.get("children", []):
                d2 = _clean(d["name"])
                idx.setdefault(d2, set()).update({d2, c2, p})
        idx[p] = pwords  # 省级覆盖全省
    with open(os.path.join(DATA_DIR, "countries.json"), encoding="utf-8") as f:
        countries = json.load(f)
    for cname, info in countries.items():
        for cty in info["cities"]:
            idx.setdefault(cty, set()).update({cty, cname})
        idx.setdefault(cname, set()).update(info["cities"])
    return idx


def _load_countries() -> dict:
    global _COUNTRIES
    if _COUNTRIES is None:
        with open(os.path.join(DATA_DIR, "countries.json"), encoding="utf-8") as f:
            _COUNTRIES = json.load(f)
    return _COUNTRIES


def region_words(city: str) -> list:
    global _INDEX
    if _INDEX is None:
        _INDEX = _build_index()
    words = set()
    for c in city.split():
        words.update(_INDEX.get(c, {c}))
    # 单字词（如「东区」→「东」）易误判其他地名，过滤
    return [w for w in words if len(w) >= 2]


def is_overseas(city: str) -> bool:
    """city 是否命中海外国家/城市词表（用于自动启用免 key 国际源）。"""
    cdata = _load_countries()
    for c in city.split():
        if c in cdata or any(c in info["cities"] for info in cdata.values()):
            return True
    return False


# 资讯/攻略页特征（非岗位页，strict 模式剔除）
INFO_RE = re.compile(r"工资待遇|就业前景|如何入行|怎么样|薪资待遇|工资多少|工资高吗|"
                    r"(多少钱|什么水平|行情|趋势|新闻|资讯|百科|怎么样\?|盘点|一览|指南)")
# 岗位已关闭/已下线特征（BOSS/智联等平台在页面内标注）
CLOSED_RE = re.compile(r"职位已关闭|已下线|已停招|招聘已结束|暂停招聘|已下架|停止招聘")

# ── 判定：三级命中 ──────────────────────────────────────────────────────
# L1 强命中：标题/URL 含地区词，或政府源域名
# L2 中命中：摘要前 120 字符含地区词（摘要头部一般是职位信息块）
# L3 弱命中：摘要其余位置含地区词（可能是公司简介/福利文本，strict 剔除）
# 0  未命中


def judge(item: dict, words: list) -> tuple:
    """返回 (level, reason)。words 为空时全部 L1（无地区约束）。

    政府源（hrss/rsj 人社局）：title 含词 → L1；否则按常规 title/snippet 判定，
    但 snippet 命中只给 L2 不降 L3（政府站信息价值高，避免被严格模式误剔）。
    """
    if not words:
        return 1, "无地区约束"
    url = item.get("url", "")
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    is_gov = bool(GOV_RE.search(url))
    tl = any(w in title for w in words)
    sh = any(w in snippet[:120] for w in words)
    st = any(w in snippet[120:] for w in words)
    if tl:
        return 1, "标题命中"
    if sh:
        return 2, "摘要头部命中"
    if st and not is_gov:
        return 3, "摘要尾部命中"
    if is_gov:
        return 2, "政府源"
    return 0, "未命中"


# ── 时效：日期提取 + 过期标记 ───────────────────────────────────────────

_DATE_RE = re.compile(r"(20\d{2})[-年./](\d{1,2})[-月./](\d{1,2})")


def extract_date(item: dict) -> str:
    """从 date 字段 / snippet 提取 YYYY-MM-DD；无则空串。"""
    for key in ("date", "publishedDate", "published_date", "publication_date",
                "created_at", "dateLastCrawled", "datePublished"):
        v = item.get(key)
        m = _DATE_RE.search(str(v or ""))
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = _DATE_RE.search((item.get("snippet") or "")[:200])
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def is_stale(dstr: str) -> bool:
    if not dstr:
        return False
    try:
        d = date.fromisoformat(dstr)
        return (date.today() - d).days > STALE_DAYS
    except ValueError:
        return False


# ── 结构化字段：薪资/学历/经验/公司（snippet 提取，零成本）──────────────

_SALARY_RE = re.compile(
    r"\d+(?:\.\d+)?\s*[-~至到]\s*\d+(?:\.\d+)?\s*(?:[kK万]|元)"  # 区间优先
    r"|\d+(?:\.\d+)?\s*[kK万]"
    r"|\d{4,6}\s*元"
    r"|\d+\s*[-~至到]\s*\d+\s*元")
_EDU_RE = re.compile(r"(硕士|博士|本科|大专|中专|高中|初中|学历不限)")
_EXP_RE = re.compile(r"(经验不限|应届生|\d+\s*[-~至到]\s*\d+\s*年|\d+\s*年(?:以上|及)?经验|\d+\s*年经验)")
_COMPANY_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()]+?(?:股份有限公司|有限责任公司|有限公司|集团|股份|科技|电子|"
    r"精密|机械|实业|咨询|事务所|工作室|医院|学校|大学|研究院|中心|厂|科技股份|半导体))")


def parse_fields(item: dict) -> dict:
    """从 title + snippet 提取结构化字段（详情页不可达时的零成本方案）。"""
    hay = (item.get("title") or "") + " " + (item.get("snippet") or "")[:300]
    fields = {}
    m = _SALARY_RE.search(hay)
    if m:
        fields["salary"] = m.group(0).strip()
    m = _EDU_RE.search(hay)
    if m:
        fields["education"] = m.group(1)
    m = _EXP_RE.search(hay)
    if m:
        fields["experience"] = m.group(1).strip()
    m = _COMPANY_RE.search(item.get("title") or "")
    if m:
        fields["company"] = m.group(1).strip()
    return fields


# ── 指纹去重：标题+公司（多平台同岗位合并）─────────────────────────────

_FP_SALARY = re.compile(
    r"\d+(?:\.\d+)?\s*[kK万元¥￥]|\d{4,6}\s*元|\d+\s*[-~至到]\s*\d+\s*(?:[kK万元¥￥]|元)")
_FP_SPACE = re.compile(r"\s+")


def fingerprint(item: dict) -> str:
    """规范化标题（去薪资/空白/平台标注）+ 公司名 → 指纹。"""
    t = item.get("title") or ""
    t = _FP_SALARY.sub("", t)
    t = _FP_SPACE.sub("", t)
    t = re.sub(r"[/／]月", "", t)  # 去「9000-14000元/月」残留
    t = re.sub(r"【[^】]*】|「[^」]*」", "", t)  # 去平台标注
    company = item.get("company") or item.get("fields", {}).get("company", "") or ""
    return f"{t}|{company}"


# ── 增量监控快照（data/jobs/）───────────────────────────────────────────

JOBS_DIR = os.path.join(DATA_DIR, "jobs")


def snapshot_path(query: str, city: str) -> str:
    import hashlib
    h = hashlib.md5(f"{query}|{city}".encode()).hexdigest()[:12]
    return os.path.join(JOBS_DIR, f"{h}.json")


def load_snapshot(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_snapshot(path: str, payload: dict) -> None:
    os.makedirs(JOBS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


# ── HTTP 基础 ───────────────────────────────────────────────────────────


def _post(url: str, body: dict, headers: Optional[dict] = None,
          timeout: int = 20) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _norm(item: dict) -> dict:
    """统一结果项字段，并做白名单后置校验（platform 空 → 剔除）。"""
    url = item.get("url", "")
    platform = ""
    for d, label in ALL_LABELS.items():
        if d in url:
            platform = label
            break
    if not platform:
        for d, label in FREE_DOMAINS:
            if d in url:
                platform = label
                break
    if not platform and not GOV_RE.search(url):
        return None  # 非白名单 URL：剔除
    if not platform:
        platform = GOV_LABEL
    item["platform"] = platform
    item["date"] = extract_date(item)
    return item


# ── 检索后端 ────────────────────────────────────────────────────────────


def _search_exa(q: str, n: int) -> list:
    d = _post("https://api.exa.ai/search",
              {"query": q, "includeDomains": DOMAINS, "numResults": n * 3,
               "type": "auto"},
              {"x-api-key": os.environ["EXA_API_KEY"]})
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("text") or "")[:200],
             "date": r.get("publishedDate", "")}
            for r in d.get("results", [])]


def _search_tavily(q: str, n: int) -> list:
    d = _post("https://api.tavily.com/search",
              {"api_key": os.environ["TAVILY_API_KEY"], "query": q,
               "include_domains": DOMAINS, "max_results": n * 3,
               "search_depth": "basic", "days": 90})
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("content") or "")[:200], "date": ""}
            for r in d.get("results", [])]


def _search_byted(q: str, n: int) -> list:
    out = []
    for domain, label in PLATFORMS:
        try:
            d = _post("https://open.feedcoopapi.com/search_api/web_search",
                      {"Query": f"site:{domain} {q}", "Count": n, "SearchType": "web"},
                      {"Authorization": f"Bearer {os.environ['WEB_SEARCH_API_KEY']}"})
        except Exception:
            continue  # 单平台失败不拖累整体
        if not d or not isinstance(d, dict):
            continue  # 上游结构异常（实测 None）防御
        for r in (d.get("Result") or {}).get("WebResults", [])[:n] or []:
            out.append({"title": r.get("Title", ""), "url": r.get("Url", ""),
                        "snippet": r.get("Snippet", ""), "date": ""})
    return out


def _search_bocha(q: str, n: int) -> list:
    """博查 Web Search：中文宽查询 + 白名单校验（site: 与宽查询均实测严格）。"""
    d = _post("https://api.bochaai.com/v1/web-search",
              {"query": q, "summary": True, "freshness": "oneYear",
               "count": min(n * 3, 50)},
              {"Authorization": f"Bearer {os.environ['BOCHA_API_KEY']}"})
    pages = (d.get("data") or {}).get("webPages") or {}
    return [{"title": i.get("name", "") or i.get("title", ""),
             "url": i.get("url", ""),
             "snippet": (i.get("summary") or i.get("snippet") or "")[:200],
             "date": i.get("datePublished", "") or i.get("dateLastCrawled", "")}
            for i in (pages.get("value") or [])]


def _search_octen(q: str, n: int) -> list:
    """Octen AI 搜索：宽查询 + 白名单校验。"""
    d = _post("https://api.octen.ai/search",
              {"query": q, "count": min(n * 3, 10), "topic": "general",
               "safesearch": "off",
               "highlight": {"enable": True, "max_tokens": 512},
               "full_content": {"enable": False}, "include_images": False},
              {"X-Api-Key": os.environ["OCTEN_API_KEY"]})
    items = (d.get("data") or {}).get("results") or []
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("highlight") or "")[:200], "date": ""}
            for r in items]


# ── 免 key 国际源（远程/海外岗位，公开 JSON API）───────────────────────

GREENHOUSE_BOARDS = ["asana", "stripe", "airbnb", "reddit", "shopify", "duolingo",
                     "figma", "cloudflare", "datadog", "mongodb", "notion",
                     "instacart", "pinterest", "doordash", "pagerduty", "box",
                     "adobe", "godaddy", "docusign", "dropbox"]

ASHBY_BOARDS = ["notion", "openai", "figma", "linear", "vercel", "airtable",
                "plaid", "ramp", "rippling", "paddle", "perplexity", "midjourney"]


def _search_remotive(q: str, n: int) -> list:
    d = _get(f"https://remotive.com/api/remote-jobs?limit={n * 3}&search={quote(q)}")
    return [{"title": j.get("title", ""), "url": j.get("url", ""),
             "snippet": (j.get("candidate_required_location", "") or "")[:200],
             "location": j.get("candidate_required_location", ""),
             "date": j.get("publication_date", "")}
            for j in d.get("jobs", [])]


def _search_himalayas(q: str, n: int) -> list:
    d = _get(f"https://himalayas.app/jobs/api?limit={n * 3}&query={quote(q)}")
    return [{"title": j.get("title", ""), "url": j.get("url", ""),
             "snippet": (j.get("excerpt") or "")[:200],
             "location": j.get("location", "") or j.get("country", ""),
             "date": ""}
            for j in d.get("jobs", [])]


def _search_jobicy(q: str, n: int) -> list:
    d = _get(f"https://jobicy.com/api/v2/remote-jobs?count={n * 3}&tag={quote(q)}")
    return [{"title": j.get("jobTitle", ""), "url": j.get("url", ""),
             "snippet": (j.get("jobGeo", "") or "")[:200],
             "location": j.get("jobGeo", ""), "date": ""}
            for j in d.get("jobs", [])]


def _search_arbeitnow(q: str, n: int) -> list:
    d = _get(f"https://www.arbeitnow.com/api/job-board-api?search={quote(q)}")
    return [{"title": j.get("title", ""), "url": j.get("url", ""),
             "snippet": (j.get("location", "") or "")[:200],
             "location": j.get("location", ""),
             "date": j.get("created_at", "")}
            for j in d.get("data", [])[:n * 3]]


def _search_greenhouse(q: str, n: int) -> list:
    """Greenhouse ATS 公司直招：遍历内置 boards，标题命中即收。"""
    out = []
    ql = q.lower()
    for board in GREENHOUSE_BOARDS:
        try:
            d = _get(
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?per_page=30&content=true",
                timeout=10)
        except Exception:
            continue
        for j in d.get("jobs", []):
            loc = ""
            lo = j.get("location") or {}
            if isinstance(lo, dict):
                loc = lo.get("name", "")
            elif isinstance(lo, str):
                loc = lo
            hay = (j.get("title", "") + " " + loc).lower()
            if ql in hay or any(w.lower() in hay for w in ql.split()):
                out.append({"title": j.get("title", ""), "url": j.get("absolute_url", ""),
                            "snippet": loc[:200], "location": loc, "date": ""})
        if len(out) >= n * 3:
            break
    return out


def _search_ashby(q: str, n: int) -> list:
    """Ashby ATS 公司直招（免 key，岗位含 compensation 字段）：遍历内置 boards。"""
    out = []
    ql = q.lower()
    for board in ASHBY_BOARDS:
        try:
            d = _get(
                f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true",
                timeout=10)
        except Exception:
            continue
        for j in d.get("jobs", []) or []:
            loc = j.get("location") or ""
            hay = (j.get("title", "") + " " + str(loc)).lower()
            if ql in hay or any(w.lower() in hay for w in ql.split()):
                out.append({"title": j.get("title", ""), "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
                            "snippet": f"{loc} | {'远程' if j.get('isRemote') else '坐班'} | {j.get('employmentType', '')}"[:200],
                            "location": str(loc), "date": (j.get("publishedAt") or "")[:10]})
        if len(out) >= n * 3:
            break
    return out


BACKENDS = {"exa": _search_exa, "tavily": _search_tavily, "byted": _search_byted,
            "bocha": _search_bocha, "octen": _search_octen}
FREE_BACKENDS = {"remotive": _search_remotive, "himalayas": _search_himalayas,
                 "jobicy": _search_jobicy, "arbeitnow": _search_arbeitnow,
                 "greenhouse": _search_greenhouse, "ashby": _search_ashby}
ALL_BACKENDS = dict(BACKENDS, **FREE_BACKENDS)
DEFAULT_ENGINES = ["exa", "tavily", "byted", "bocha", "octen"]


# ── 详情页结构化补全（--fetch N，走 argo fetch_v3 降级链）──────────────


def _fetch_detail(url: str, timeout: int = 10) -> Optional[dict]:
    """抓详情页提取结构化字段；失败返回 None（静默降级 snippet 字段）。"""
    try:
        from fetch_v3 import fetch_v3
        r = fetch_v3(url, max_chars=3000, timeout=timeout, use_browser_fallback=False)
        if not r or not r.get("success"):
            return None
        content = (r.get("content") or "")[:3000]
        fields = {}
        m = _SALARY_RE.search(content)
        if m:
            fields["salary"] = m.group(0).strip()
        m = _EDU_RE.search(content)
        if m:
            fields["education"] = m.group(1)
        m = _EXP_RE.search(content)
        if m:
            fields["experience"] = m.group(1).strip()
        m = _COMPANY_RE.search((r.get("title") or "") + content[:500])
        if m:
            fields["company"] = m.group(1).strip()
        return fields if fields else None
    except Exception:
        return None


# ── 主流程 ─────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="求职岗位搜索：职位 + 地区 → 该地区在招岗位")
    ap.add_argument("query", help="职位关键词，如：工艺工程师、会计、电工")
    ap.add_argument("--city", default="",
                    help="地区：任意省/市/县（四川、成都、昆山…）或海外国家/城市"
                         "（新加坡、东京、Sydney…），多个用空格分隔")
    ap.add_argument("-n", "--num", type=int, default=5, help="每后端条数")
    ap.add_argument("--engine", default="all",
                    choices=["all", "free"] + sorted(ALL_BACKENDS),
                    help="搜索后端：all = exa+tavily+byted+bocha+octen"
                         "（海外城市自动加 free 免 key 源）；free = 仅免 key 国际源")
    ap.add_argument("--platforms", default="",
                    help="逗号分隔平台: zhipin,liepin,zhaopin,51job,597,jrzp")
    ap.add_argument("--loose", action="store_true",
                    help="宽松：异地岗位也保留（命中置顶）；默认严格过滤")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fetch", type=int, default=0, metavar="N",
                    help="对前 N 条 L1 结果抓详情页补全结构化字段（默认 0 不抓，"
                         "仅用 snippet 提取）")
    ap.add_argument("--watch", action="store_true",
                    help="增量监控：存快照至 data/jobs/，二次运行对比输出新上线/已下架")
    args = ap.parse_args()

    platforms = PLATFORMS
    if args.platforms:
        wanted = {p.strip() for p in args.platforms.split(",")}
        platforms = [(d, l) for d, l in PLATFORMS if d.split(".")[0] in wanted]
    if not platforms:
        sys.exit("未识别的平台，可用: zhipin,liepin,zhaopin,51job,597,jrzp")

    engines = []
    if args.engine == "all":
        engines = list(DEFAULT_ENGINES)
        if is_overseas(args.city):
            engines += list(FREE_BACKENDS)  # 海外自动启用免 key 源
    elif args.engine == "free":
        engines = list(FREE_BACKENDS)
    else:
        engines = [args.engine]

    query = f"{args.query} {args.city}".strip()
    words = region_words(args.city) if args.city else []

    results, errors = [], []

    def run(name: str):
        try:
            fn = ALL_BACKENDS[name]
            for r in fn(query, args.num):
                r["backend"] = name
                results.append(r)
        except Exception as e:
            errors.append(f"{name}: {e}")

    threads = [threading.Thread(target=run, args=(e,)) for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 统一字段 + 白名单后置校验 + 三级判定 + 时效 + 结构化字段
    kept, dropped_url, dropped_region = [], 0, 0
    for r in results:
        nr = _norm(r)
        if nr is None:
            dropped_url += 1  # 非白名单 URL 剔除（修复 byted site: 混入）
            continue
        level, reason = judge(nr, words)
        nr["hit_level"] = level
        nr["hit_reason"] = reason
        nr["stale"] = is_stale(nr["date"])
        nr["fields"] = parse_fields(nr)  # snippet 级结构化（零成本）
        nr["fingerprint"] = fingerprint(nr)
        if level == 0:
            dropped_region += 1
        kept.append(nr)

    strict = bool(words) and not args.loose
    if strict:
        kept = [r for r in kept if r["hit_level"] in (1, 2)]
        # 资讯/攻略页（工资待遇/就业前景/新闻百科）非岗位，剔除
        kept = [r for r in kept if not INFO_RE.search(r["title"])]
        # 平台标注「职位已关闭/已下线」的岗位，剔除
        kept = [r for r in kept if not CLOSED_RE.search(r.get("snippet", ""))]

    # 详情页结构化补全（--fetch N，前 N 条 L1）
    if args.fetch > 0:
        fetched = 0
        for r in sorted(kept, key=lambda x: (x["hit_level"], x.get("date", "")), reverse=True):
            if fetched >= args.fetch:
                break
            if r["hit_level"] != 1:
                continue
            detail = _fetch_detail(r["url"])
            if detail:
                r["fields"].update(detail)
            fetched += 1

    # 指纹去重：多平台同岗位合并（保留级别更高、有日期的）
    seen_fp = {}
    for r in kept:
        fp = r.get("fingerprint") or r["url"]
        prev = seen_fp.get(fp)
        if prev is None:
            seen_fp[fp] = r
            continue
        # 保留 hit_level 高者；同级保留有日期者；再同级保留先到者（后端顺序靠前）
        if (r["hit_level"], r.get("date", "")) > (prev["hit_level"], prev.get("date", "")):
            seen_fp[fp] = r
    unique = list(seen_fp.values())

    # watch 增量对比（对比用快照需在指纹去重前、含全部保留项）
    new_jobs, gone_jobs = [], []
    if args.watch:
        path = snapshot_path(args.query, args.city)
        old = load_snapshot(path)
        old_keys = {j["fingerprint"]: j for j in (old or {}).get("jobs", [])}
        new_keys = {j["fingerprint"]: j for j in unique}
        new_jobs = [j for fp, j in new_keys.items() if fp not in old_keys]
        gone_jobs = [j for fp, j in old_keys.items() if fp not in new_keys]
        save_snapshot(path, {"query": args.query, "city": args.city,
                             "saved_at": date.today().isoformat(),
                             "jobs": [{"fingerprint": j["fingerprint"],
                                        "title": j["title"], "url": j["url"],
                                        "platform": j["platform"],
                                        "date": j.get("date", "")} for j in unique]})
        print(f"\n── 增量对比（{path}）──")
        if old is None:
            print(f"基线建立：{len(unique)} 条岗位已存快照，下次运行输出变化")
        else:
            print(f"新上线 {len(new_jobs)} 条 / 已下架 {len(gone_jobs)} 条（共 {len(unique)} 条在架）")
            for j in new_jobs[:10]:
                print(f"  ＋[{j['platform']}] {j['title'][:48]}")
            for j in gone_jobs[:10]:
                print(f"  －[{j['platform']}] {j['title'][:48]}")
            if len(new_jobs) > 10 or len(gone_jobs) > 10:
                print(f"  … 其余变化请查看快照 {path}")

    # 排序：级别 L1 > L2 > L3 > 0 为主键（稳定排序先按日期降序，级别内日期新→旧），
    # 过期垫底。空日期排最后（byted/tavily 无日期字段）。
    unique.sort(key=lambda r: r.get("date", ""), reverse=True)
    unique.sort(key=lambda r: (r.get("hit_level", 0), r.get("stale", False)))

    if args.json:
        out = {"query": query, "backends": engines,
               "total": len(unique),
               "dropped_url": dropped_url,
               "dropped_region": dropped_region,
               "strict": strict,
               "errors": errors, "results": unique}
        if args.watch:
            out["watch"] = {"new": len(new_jobs), "gone": len(gone_jobs)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        mode = "严格" if strict else "宽松" if args.loose else "全部"
        print(f"查询: {query} | 后端: {','.join(engines)} | {mode} | {len(unique)} 条")
        marks = {1: "●", 2: "◐", 3: "○"}
        for r in unique:
            stale = " [过期]" if r.get("stale") else ""
            mark = marks.get(r.get("hit_level", 0), "○")
            f = r.get("fields", {})
            extra = " | ".join(x for x in (f.get("salary"), f.get("education"),
                                           f.get("experience")) if x)
            print(f"{mark}[{r['platform']}]{stale} {r['title'][:48]}"
                  + (f" {extra}" if extra else ""))
            print(f"  {r['url'][:88]}")


if __name__ == "__main__":
    main()
