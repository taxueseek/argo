#!/usr/bin/env python3
"""engines.py — Unified Search v2 引擎适配层（门面）

配置驱动 + 声明式 output_map 字段提取 + 通用 parser 保底。
实现拆分：
  - engines_base.py      公共工具 / cli / http / html / 通用解析
  - engines_builders.py  专用引擎构建器
本文件仅负责 BUILDERS 注册表与 search() 入口。
"""

from __future__ import annotations

import contextlib
import inspect
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from config import load_config, get_engines
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines

try:
    from single_flight import engine_coalescer
except ImportError:  # pragma: no cover
    def engine_coalescer():  # type: ignore
        class _Noop:
            def run(self, key, fn, **kw):
                return fn()
        return _Noop()

from engines_base import (
    safe_search,
    _build_cli_engine,
    _build_http_engine,
    _build_html_engine,
    _parse_generic,
    _parse_text_output,
    _parse_xml,
    _parse_uapi,
    _parse_semantic_scholar,
    _ensure_engine_source,
    _CUSTOM_JSON_PARSERS,
)
from recovery import strip_structured
from cli_io import dumps

# 对外/测试兼容：专用解析器与 source 纠正
__all__ = [
    "search",
    "available_engines",
    "get_registry",
    "safe_search",
    "_parse_uapi",
    "_parse_semantic_scholar",
    "_ensure_engine_source",
]
from engines_builders import (
    _build_who_don_engine,
    _build_who_gho_engine,
    _build_pubmed_engine,
    _build_core_engine,
    _build_gdacs_engine,
    _build_obis_engine,
    _build_worms_engine,
    _build_energy_charts_engine,
    _build_jolpica_engine,
    _build_openf1_engine,
    _build_openligadb_engine,
    _build_artic_engine,
    _build_cleveland_engine,
    _build_tvmaze_engine,
    _build_jikan_engine,
    _build_deezer_engine,
    _build_listenbrainz_engine,
    _build_egov_law_engine,
    _build_k10plus_engine,
    _build_ror_engine,
    _build_soilgrids_engine,
    _build_noaa_swpc_engine,
    _build_satnogs_engine,
    _build_tle_mirror_engine,
    _build_nhtsa_vpic_engine,
    _build_exa_engine,
    _build_anysearch_engine,
    _build_parallel_engine,
    _build_parallel_free_engine,
    _build_seltz_engine,
    _build_you_engine,
    _build_em_miaoxiang_engine,
    _build_cninfo_engine,
    _build_sina_quote_engine,
    _build_tencent_quote_engine,
    _build_em_flow_engine,
    _build_wechat_sogou_engine,
    _build_hackernews_engine,
    _build_stackoverflow_engine,
    _build_google_scholar_engine,
    _build_v2ex_engine,
    _build_tineye_engine,
    _build_bing_rss_engine,
    _build_ths_hot_engine,
    _build_cls_telegraph_engine,
    _build_em_global_news_engine,
    _build_eastmoney_engine,
    _build_itotii_engine,
    _build_baidu_hot_engine,
    _build_toutiao_hot_engine,
    _build_bilibili_hot_engine,
    _build_zhihu_global_engine,
    _build_zhihu_user_engine,
    _build_bocha_engine,
    _build_bocha_ai_engine,
    _build_std_samr_engine,
    _build_openstd_engine,
    _build_bangumi_engine,
    _build_so_engine,
    _build_shenma_engine,
    _build_qwant_engine,
    _build_ecosia_engine,
    _build_douban_movie_engine,
    _build_zdic_engine,
    _build_iplant_engine,
    _build_people_daily_engine,
    _build_flk_law_engine,
    _build_wikisource_engine,
    _build_weibo_hot_engine,
    _build_douyin_hot_engine,
    _build_csdn_engine,
    _build_wallstreetcn_engine,
    _build_weather_cn_engine,
    _build_google_news_engine,
    _build_met_museum_engine,    _build_open_library_engine,
    _build_weread_engine,
    _build_douban_book_engine,
    _build_fred_engine,
    _build_fx_rate_engine,
    _build_worldbank_engine,
    _build_nbs_stats_engine,
    _build_pubchem_engine,
    _build_eurostat_engine,
    _build_gbif_engine,
    _build_rfc_editor_engine,
    _build_twitter_syndication_engine,
    _build_deps_dev_engine,
    _build_endoflife_engine,
    _build_osv_engine,
    _build_cisa_kev_engine,
    _build_biorxiv_engine,
    _build_un_comtrade_engine,
    _build_uniprot_engine,
    _build_rcsb_pdb_engine,
    _build_courtlistener_engine,
    _build_gutenberg_engine,
    _build_wayback_cdx_engine,
    _build_usgs_engine,
    _build_nasa_cmr_engine,
    _build_free_dictionary_engine,
    _build_baidu_baike_engine,
    _build_pypi_engine,
    _build_clinicaltrials_engine,
    _build_openfda_engine,
    _build_juejin_engine,
    _build_models_dev_engine,
    _build_finviz_engine,
    _build_seeking_alpha_engine,
    _build_qweather_engine,
    _build_wenshu_engine,
    _build_jin10_engine,
    _build_octen_engine,
    _build_imdb_engine,
    _build_thesportsdb_engine,
    _build_itunes_engine,
    _build_opencorporates_engine,
    _build_google_patents_engine,
    _build_marginalia_engine,
    _build_wiby_engine,
    _build_cnii_engine,
    _build_ndl_engine,
    _build_kor_law_engine,
    _build_hatena_bookmark_engine,
    _build_dnb_engine,
    _build_doaj_engine,
    _build_europeana_engine,
    _build_hal_engine,
    _build_eu_opendata_engine,
    _build_open_meteo_engine,
    _build_searchmysite_engine,
    _build_lieu_engine,
    _build_opensky_engine,
    _build_electricity_maps_engine,
    _build_usda_engine,
    _build_tatoeba_engine,
    _build_figshare_engine,
    _build_tencent_kline_engine,
    _build_qq_music_engine,
    _build_github_engine,
    _build_rss_feed_engine,
)
# 批次十（engines_builders_batch10）：engines_builders.py 聚合层暂未转出该模块，
# 先直连其源模块——engines_builders.py 补转出后可并入上方聚合 import。
# zhihu_global 用别名：与上方 engines_builders 转出的 cn 旧版同名，F811 门禁
# 禁止静默重定义；别名让「batch10 取代 cn 版」在注册表处显式可见。
from engines_builders_batch10 import (  # noqa: F401
    _build_zhihu_global_engine as _build_zhihu_global_engine_v2,
)
# 批次十一（engines_builders_batch11）：图源补强。Wikimedia Commons 的
# query.pages 是 pageid 字典，声明式 output_map 取不到字段，必须有 builder。
from engines_builders_batch11 import (  # noqa: F401
    _build_wikimedia_commons_engine,
)



class _LazyLogger:
    """延迟创建 logger，避免 import logging 的重量级 import 链（traceback→dataclasses→inspect）。"""
    _logger = None

    def __getattr__(self, name: str):
        if _LazyLogger._logger is None:
            import logging
            _LazyLogger._logger = logging.getLogger("unified_search.engines")
            if not _LazyLogger._logger.handlers:
                _LazyLogger._logger.setLevel(logging.WARNING)
                _LazyLogger._logger.addHandler(logging.StreamHandler(sys.stderr))
        return getattr(_LazyLogger._logger, name)


logger = _LazyLogger()


# 「未知引擎」警告按名字每进程只报一次。
# 有界（2026-09-29）：键可能来自用户/路由输入，长驻 MCP server 收到大量
# 非常规引擎名时，无界集合会随进程寿命线性增长。超限即整体清空——去重是
# best-effort，丢一轮去重只多几行重复警告，远比无界增长便宜。
_UNKNOWN_ENGINE_WARNED_MAX = 256
_unknown_engine_warned: set[str] = set()


def _warn_unknown_engine(engine: str) -> None:
    """未注册引擎的日志警告（同名每进程一次）。

    why 去重：同一次调用里，主 combo、次域补充、恢复链会各试一遍同一个名字，
    实测同一条警告重复 3–4 行。重复并不增加信息，只会把真信号淹掉。
    机器可读的那一份不在这里——它走 outcome 的 `status=unknown-engine` + detail，
    最终进响应的 `errors`（见 engine_dispatch 的前置拦截）。
    """
    if engine in _unknown_engine_warned:
        return
    if len(_unknown_engine_warned) >= _UNKNOWN_ENGINE_WARNED_MAX:
        _unknown_engine_warned.clear()
    _unknown_engine_warned.add(engine)
    logger.warning(
        "未知引擎: %s（registry 查无此名：拼写错误，或该源未随本版发布）", engine)


def is_registered(engine: str) -> bool:
    """该名字在本版 registry 里是否存在。

    唯一判据就是 `get_registry()`，与 `run_engine` 内部的 `registry.get()` 同源：
    调用方（engine_dispatch 的前置拦截）必须与执行层看到同一个注册表，各写一份
    `in` 判断迟早漂移。
    """
    return engine in get_registry()


def unknown_requested_engines(engine_override: str) -> list[str]:
    """显式点名的引擎里，本版 registry 查无的那些（按输入顺序，去重）。

    why：`--engine nope` 此前产出
    `status=completed / errors=[] / engines_used=['nope'] / count=0`——调用方
    （Agent）据此判定「网上没有这个信息」并停止追问，而真相是这个名字在本版
    registry 里根本不存在（拼写错、或该源未随本版发布）。它与「缺 API Key 的
    静默 no-results」是同一根因，处置也应一致：变成可行动的 error。

    **为什么判在请求层而不是执行层**：执行层收到的引擎名可以是测试替身
    （test_serial_hedge 的 quick_primary、test_race_fast_budget 的 fast_garbage
    都从 dispatch 入口走），在那里按 registry 判「未注册」会误伤替身——实测
    11 个 dispatch 用例集体变红。而「用户点了个不存在的名字」是**请求级**的
    事实，判在请求层才既不误伤、又落在正确的抽象层。

    住在 engines.py 而不是 search.py：判据本体（is_registered）在这里，而
    search.py 已压着 1000 行硬上限（本函数曾在那边，被体积门禁拦下）。这里只做
    「请求串 → 查无的名字」这一层薄封装，不重复 `in` 判断。
    """
    if not engine_override or engine_override == "auto":
        return []
    unknown: list[str] = []
    for raw in str(engine_override).split(","):
        name = raw.strip()
        if name and name not in unknown and not is_registered(name):
            unknown.append(name)
    return unknown


@contextlib.contextmanager
def _sys_path_tmp(path: str):
    """临时把 path 加入 sys.path，退出时移除——只移除自己添加的那一次。

    append 而非 insert(0)：sub-skills 顶层有与 scripts 同名的模块
    （health_check 等），插到前面会劫持 scripts 下同名模块的解析。
    """
    added = path not in sys.path
    if added:
        sys.path.append(path)
    try:
        yield
    finally:
        if added:
            sys.path.remove(path)


def _build_local_search_engine(spec: dict[str, Any]) -> Any:
    """进程内调用 local-search 子技能，避免 subprocess 冷启动（~300-500ms/次）。

    直接 import search_v3.search_engines，复用其智能路由/健康过滤/批量并行，
    输出与 unified-search 一致的 schema。安全降级：import 失败时回退
    原 subprocess 调用，保证功能不丢。
    """
    import os as _os
    import subprocess as _subprocess

    cmd_template = spec.get("cmd", [])
    search_args = spec.get("search_args", [])

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
        # 回退路径无条件依赖 _resolve：必须放 try 外。放 try 首行时，import 失败
        # 会被下面的 except Exception 吞掉，回退路径读 _resolve_tpl 直接
        # UnboundLocalError——「进程内失败回退 subprocess」的语义被架空
        # （ef4e509 同类：分支内 import 污染同函数后续读取，静态门禁
        # test_local_import_shadows_global 依赖此处不报）。
        from engines_base import _resolve as _resolve_tpl
        # 进程内优先（省 subprocess 冷启动）
        try:
            sub_dir = Path(__file__).resolve().parent.parent / "sub-skills" / "local-search"
            # 临时加路径 import，完事自动清理（助手语义见定义处）
            with _sys_path_tmp(str(sub_dir)):
                import search_v3
            res = search_v3.search_engines(
                query, engines=None, n=n, timeout=float(timeout),
                # None = 读子技能 config 的 max_parallel_engines（此前硬编码 5，
                # config 调并发对主链路径不生效）；预算=单引擎超时，local-first
                # 是「快」语义，墙钟不吃 30s 历史默认。
                max_parallel=None, total_budget=float(timeout),
                skip_cache=bool(kwargs.get("skip_cache", False)),
                mode=mode,
                since=kwargs.get("since"), until=kwargs.get("until"),
                sort=kwargs.get("sort"),
            )
            results = res.get("results") or []
            # 与子进程路径一致：每条带 _engine 标记，source 保持子引擎名
            for r in results:
                if isinstance(r, dict) and "error" not in r:
                    r.setdefault("_engine", r.get("source") or "local_search")
            if results:
                return results
        except Exception:
            pass  # 进程内失败回退 subprocess
        # 回退：subprocess 调用（原行为）
        cmd = _resolve_tpl(cmd_template, query, n, mode=mode)
        args = _resolve_tpl(search_args, query, n, mode=mode)
        if not cmd:
            return []
        env = _os.environ.copy()
        env.update(spec.get("env", {}) or {})
        proc = _subprocess.run(cmd + args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout, env=env)
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return []
        results = data.get("results") or []
        for r in results:
            if isinstance(r, dict) and "error" not in r:
                r.setdefault("_engine", r.get("source") or "local_search")
        return results
    return _engine


_BUILDERS = {
    "cli": _build_cli_engine,
    "http": _build_http_engine,
    "html": _build_html_engine,
    "local_search": _build_local_search_engine,
    "exa": _build_exa_engine,
    "anysearch": _build_anysearch_engine,
    "em_miaoxiang": _build_em_miaoxiang_engine,
    "cninfo": _build_cninfo_engine,
    "sina_quote": _build_sina_quote_engine,
    "tencent_quote": _build_tencent_quote_engine,
    "em_flow": _build_em_flow_engine,
    "wechat_sogou": _build_wechat_sogou_engine,
    "hackernews": _build_hackernews_engine,
    "stackoverflow": _build_stackoverflow_engine,
    "std_samr": _build_std_samr_engine,
    "openstd": _build_openstd_engine,
    "bangumi": _build_bangumi_engine,
    "douban_movie": _build_douban_movie_engine,
    "zdic": _build_zdic_engine,
    "iplant": _build_iplant_engine,
    "people_daily": _build_people_daily_engine,
    "flk_law": _build_flk_law_engine,
    "wikisource": _build_wikisource_engine,
    "weibo_hot": _build_weibo_hot_engine,
    "douyin_hot": _build_douyin_hot_engine,
    "csdn": _build_csdn_engine,
    "wallstreetcn": _build_wallstreetcn_engine,
    "weather_cn": _build_weather_cn_engine,
    "google_news": _build_google_news_engine,
    "met_museum": _build_met_museum_engine,
    "github": _build_github_engine,
    "google_scholar": _build_google_scholar_engine,
    "v2ex": _build_v2ex_engine,
    "tineye": _build_tineye_engine,
    "bing_rss": _build_bing_rss_engine,
    "ths_hot": _build_ths_hot_engine,
    "cls_telegraph": _build_cls_telegraph_engine,
    "twitter_syndication": _build_twitter_syndication_engine,
    "deps_dev": _build_deps_dev_engine,
    "endoflife": _build_endoflife_engine,
    "osv": _build_osv_engine,
    "cisa_kev": _build_cisa_kev_engine,
    "biorxiv": _build_biorxiv_engine,
    "un_comtrade": _build_un_comtrade_engine,
    "em_global_news": _build_em_global_news_engine,
    "eastmoney": _build_eastmoney_engine,
    "itotii": _build_itotii_engine,
    "baidu_hot": _build_baidu_hot_engine,
    "toutiao_hot": _build_toutiao_hot_engine,
    "bilibili_hot": _build_bilibili_hot_engine,
    "open_library": _build_open_library_engine,
    "weread": _build_weread_engine,
    "douban_book": _build_douban_book_engine,
    "zhihu_global": _build_zhihu_global_engine_v2,  # 补强版在 batch10（候选池取满 + 错误行动提示；cn 版被取代，cn 冻结在行数门禁上限）
    "zhihu_user": _build_zhihu_user_engine,
    "fred": _build_fred_engine,
    "so": _build_so_engine,
    "shenma": _build_shenma_engine,
    "qwant": _build_qwant_engine,
    "ecosia": _build_ecosia_engine,
    "fx_rate": _build_fx_rate_engine,
    "worldbank": _build_worldbank_engine,
    "nbs_stats": _build_nbs_stats_engine,
    "pubchem": _build_pubchem_engine,
    "eurostat": _build_eurostat_engine,
    "gbif": _build_gbif_engine,
    "rfc_editor": _build_rfc_editor_engine,
    "uniprot": _build_uniprot_engine,
    "rcsb_pdb": _build_rcsb_pdb_engine,
    "courtlistener": _build_courtlistener_engine,
    "gutenberg": _build_gutenberg_engine,
    "wayback_cdx": _build_wayback_cdx_engine,
    "usgs": _build_usgs_engine,
    "nasa_cmr": _build_nasa_cmr_engine,
    "free_dictionary": _build_free_dictionary_engine,
    "baidu_baike": _build_baidu_baike_engine,
    "pypi": _build_pypi_engine,
    "clinicaltrials": _build_clinicaltrials_engine,
    "openfda": _build_openfda_engine,
    "juejin": _build_juejin_engine,
    "models_dev": _build_models_dev_engine,
    "finviz": _build_finviz_engine,
    "seeking_alpha": _build_seeking_alpha_engine,
    "qweather": _build_qweather_engine,
    "wenshu": _build_wenshu_engine,
    "jin10": _build_jin10_engine,
    "octen": _build_octen_engine,
    "bocha": _build_bocha_engine,
    "parallel": _build_parallel_engine,
    "parallel_free": _build_parallel_free_engine,
    "seltz": _build_seltz_engine,
    "you": _build_you_engine,
    "bocha_ai": _build_bocha_ai_engine,
    "imdb": _build_imdb_engine,
    "thesportsdb": _build_thesportsdb_engine,
    "itunes": _build_itunes_engine,
    "opencorporates": _build_opencorporates_engine,
    "google_patents": _build_google_patents_engine,
    "marginalia": _build_marginalia_engine,
    "wiby": _build_wiby_engine,
    "rss_feed": _build_rss_feed_engine,
    "cnii": _build_cnii_engine,
    "ndl": _build_ndl_engine,
    "kor_law": _build_kor_law_engine,
    "hatena_bookmark": _build_hatena_bookmark_engine,
    "dnb": _build_dnb_engine,
    "doaj": _build_doaj_engine,
    "europeana": _build_europeana_engine,
    "hal": _build_hal_engine,
    "eu_opendata": _build_eu_opendata_engine,
    "open_meteo": _build_open_meteo_engine,
    "searchmysite": _build_searchmysite_engine,
    "lieu": _build_lieu_engine,
    "opensky": _build_opensky_engine,
    "electricity_maps": _build_electricity_maps_engine,
    "usda": _build_usda_engine,
    "tatoeba": _build_tatoeba_engine,
    "figshare": _build_figshare_engine,
    "tencent_kline": _build_tencent_kline_engine,
    "qq_music": _build_qq_music_engine,
    "who_don": _build_who_don_engine,
    "who_gho": _build_who_gho_engine,
    "pubmed": _build_pubmed_engine,
    "core": _build_core_engine,
    "gdacs": _build_gdacs_engine,
    "obis": _build_obis_engine,
    "worms": _build_worms_engine,
    "energy_charts": _build_energy_charts_engine,
    "jolpica": _build_jolpica_engine,
    "openf1": _build_openf1_engine,
    "openligadb": _build_openligadb_engine,
    "artic": _build_artic_engine,
    "cleveland": _build_cleveland_engine,
    "wikimedia_commons": _build_wikimedia_commons_engine,
    "tvmaze": _build_tvmaze_engine,
    "jikan": _build_jikan_engine,
    "deezer": _build_deezer_engine,
    "listenbrainz": _build_listenbrainz_engine,
    "egov_law": _build_egov_law_engine,
    "k10plus": _build_k10plus_engine,
    "ror": _build_ror_engine,
    "soilgrids": _build_soilgrids_engine,
    "noaa_swpc": _build_noaa_swpc_engine,
    "satnogs": _build_satnogs_engine,
    "tle_mirror": _build_tle_mirror_engine,
    "nhtsa_vpic": _build_nhtsa_vpic_engine,
}

# 语义型引擎：把 query 当自然语言语义检索，不识别平台原生结构化语法
# （from:/repo:/site:/until: 等）。对其剥掉字段只留核心词，避免污染相关性。
# 透传型/垂直源（local_*、social/academic/code、github 等）保持原 query，让平台语法生效。
# local_search 是聚合器（子引擎 local_bing 等透传平台语法），不在语义集内。
_SEMANTIC_ENGINES = frozenset({
    "byted", "bocha", "anysearch", "tavily", "exa", "octen",
    "uapi", "searxng", "parallel", "you", "bocha_ai",
})

# ── 实体型引擎的查询规范化（2026-09-06 live 金标教训）─────────────────────
# 自然语言句直达实体搜索接口会全军覆没：「NASA founding year」wikidata 0 条
# （裸「NASA」7 条）、「where is Eiffel Tower」同型且拖满 11s 超时、
# 「Cristiano Ronaldo club」thesportsdb 抖动放大。接口只吃实体名——分发层
# 统一剥疑问前缀与属性词，保留核心实体。只作用于实体型引擎；通用 web 引擎
# 不动（属性词对全文检索是有用信号）。
_ENTITY_QUERY_ENGINES = frozenset({"wikidata", "thesportsdb", "local_openstreetmap"})
_ENTITY_Q_PREFIX_RE = re.compile(
    r"(?i)^\s*(where\s+(is|are|was|were)|where'?s|who\s+(is|are|was)|who'?s"
    r"|what\s+(is|are)|when\s+(was|is|did)|which\s+\w+\s+(is|was)"
    r"|哪里|在哪[里儿]?|是谁?|是什么|什么时候|哪一[个国年])\s*",
)
# 英文属性词带 \b（空格分词下词边界天然成立）；中文属性词不进此表——
# CJK 字符全是 \w，「周杰伦专辑」「周杰伦的专辑」这类无空格连写永远撞不上
# 词边界（2026-09-06 审查实锤漏剥），改由下方尾部循环剥离。
_ENTITY_ATTR_RE = re.compile(
    r"(?i)\b(founding year|founded|established|headquarters?|located( in)?"
    r"|population|capital|address|club|team|stadium|league|album|discography"
    r"|song|movie|film|director|cast)\b",
)
# 中文属性词（含繁体）：只在查询尾部剥离——中文自然语序是实体名在前、
# 属性词在后；不碰串中，「电影频道」「歌曲排行榜」这类实体名内含属性词，
# 串中剥离会误伤。长词优先，避免「成立年份」被「成立」截断残留「年份」。
_ENTITY_ATTR_TAILS = (
    "成立年份", "成立", "创立", "創立", "总部", "總部", "位置", "球队", "球隊",
    "俱乐部", "俱樂部", "职能", "職能", "职责", "職責", "专辑", "專輯",
    "歌曲", "电影", "電影", "导演", "導演", "主演",
)


def _entity_core_query(query: str) -> str:
    """实体搜索接口的查询规范化：剥疑问前缀+属性词，保留核心实体名。"""
    raw = (query or "").strip()
    q = _ENTITY_Q_PREFIX_RE.sub(" ", raw)
    # 中文疑问词多为尾部后置（「清华大学 在哪里」），与英文前缀形态分开剥
    q = re.sub(r"(?i)(在哪里|在哪|哪儿|哪里|是什么|是谁|什么时候|有哪些)\s*[?？]?\s*$",
               " ", q)
    q = _ENTITY_ATTR_RE.sub(" ", q)
    # 空白归一化必须在尾部循环前：前缀/属性词剥除用空格替换，尾空格会让
    # endswith 判空（「清华大学总部 」漏剥总部）
    q = re.sub(r"\s+", " ", q).strip()
    # 中文属性词尾部循环剥 + 「的」后缀收尾：「周杰伦的专辑」→「周杰伦的」
    # →「周杰伦」。剩单字不剥（「美的」是实体，剥成「美」即误伤）
    changed = True
    while changed:
        changed = False
        for tail in _ENTITY_ATTR_TAILS:
            if q.endswith(tail) and len(q) > len(tail) + 1:
                q = q[:-len(tail)].strip()
                changed = True
                break
        if not changed and q.endswith("的") and len(q) > 3:
            q = q[:-1].strip()
            changed = True
    q = re.sub(r"[?？:：，,]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q or raw


_engine_registry: dict[str, Any] = {}
_engine_specs: dict[str, dict[str, Any]] = {}
_engine_registry_loaded = False
_registry_stamp: float | None = None


def _load_registry():
    global _engine_registry, _engine_specs, _engine_registry_loaded, _registry_stamp
    if _engine_registry_loaded:
        return
    cfg = load_config()
    engines = get_engines(cfg)
    registry = {}
    for name, spec in engines.items():
        spec = dict(spec)
        spec["_name"] = name
        # local_search 走进程内 builder（config 里 type=cli，这里显式路由到专用实现）
        if name == "local_search":
            spec["type"] = "local_search"
        # anysearch：type 已在 config.yaml 显式声明（引擎声明来源），
        # 不再运行时硬覆盖；若旧配置缺 type 字段，保底路由到进程内 builder。
        if name == "anysearch" and spec.get("type", "cli") not in _BUILDERS:
            spec["type"] = "anysearch"
        builder = _BUILDERS.get(spec.get("type", "cli"))
        if builder:
            registry[name] = builder(spec)
        else:
            logger.warning(f"未知引擎类型: {spec.get('type')} (引擎 {name})")
    _engine_registry = registry
    # spec 侧表：engine_env 缺 env 检测等需要原始声明（registry 值是闭包）
    global _engine_specs
    _engine_specs = engines
    _engine_registry_loaded = True
    try:
        from config import config_stamp
        _registry_stamp = config_stamp()
    except ImportError:
        _registry_stamp = None


def get_registry() -> dict[str, Any]:
    """引擎注册表。config 变更（综合 mtime 指纹）时自动重建——
    增删引擎/改 qps/换声明等配置无需重启进程。"""
    global _engine_registry, _engine_specs, _engine_registry_loaded, _registry_stamp
    try:
        from config import config_stamp
        stamp = config_stamp()
    except ImportError:
        stamp = _registry_stamp
    if _engine_registry_loaded and _registry_stamp is not None and stamp != _registry_stamp:
        logger.info("config 变更 → 重建引擎注册表")
        _engine_registry = {}
        _engine_specs = {}
        _engine_registry_loaded = False
    _load_registry()
    return _engine_registry


def get_engine_spec(name: str) -> dict[str, Any] | None:
    """返回引擎原始声明（spec），无此引擎返回 None。

    registry 值是构建后的闭包，原始 spec（url/headers/required_env 等）
    存侧表供 engine_env 缺 env 检测等调用方使用。
    """
    get_registry()  # 触发热重建检查，保持侧表与注册表同步
    return _engine_specs.get(name)


def available_engines(routable_only: bool = False) -> list[str]:
    """可实例化的引擎名（有 builder）。

    routable_only=True 时再按「真能用」过滤（密钥齐全、后端依赖就位、
    未被熔断/禁用）——这是与 --list-engines --detail 的 routable 同一判定。

    routable_only 此前没被实现（本函数不收参数），调用侧传进来会抛
    TypeError 并被 except TypeError 吞掉回退到全量——`--list-engines
    --routable-only` 因此静默返回全部引擎，用户以为筛过了。
    """
    names = sorted(get_registry().keys())
    if not routable_only:
        return names
    try:
        from engine_status import list_routable_engine_ids
        routable = set(list_routable_engine_ids())
        return [n for n in names if n in routable]
    except Exception:
        return names


# ── 单飞合并 + 免费引擎结果数桶化 ──
# 场景：MCP 并发请求 / 多轨道同查询 → 同一引擎调用重复打上游。
# 桶化只对免费引擎（cost_factor >= 0.85，与 route 免费判定一致）：n 向上
# snap 到 10/20/50/100，让同查询不同请求数共享一次执行与缓存；付费引擎
# 保持精确 n（按结果计费不得放大）。
_NUM_BUCKETS = (10, 20, 50, 100)


def _free_engine(engine: str) -> bool:
    """是否「免费档」——只有免费档才做 n 桶化。

    此前判据是 cost_factor >= 0.85，而成本表没有 api 档的分支（fallthrough
    到 1.0），于是 api 档（exa/octen/you/parallel/zhihu_global/tavily 这类
    按量计费的源）被当成免费：要 5 条被放大到 10 条，按结果计费的接口直接
    双倍计费。本函数注释一直写着「付费引擎保持精确 n（按结果计费不得放大）」，
    实现却漏了这一档——现在按声明的档位判，不再依赖魔法阈值。
    """
    try:
        from config import cost_tier_of
        return cost_tier_of(engine) == "free"
    except Exception:
        return False


def bucket_n(engine: str, n: int) -> int:
    """免费引擎：n 向上 snap 到缓存友好桶（≤100）；付费引擎原样。"""
    n = max(1, min(int(n), 100))
    if not _free_engine(engine):
        return n
    for b in _NUM_BUCKETS:
        if n <= b:
            return b
    return 100


def _call_key(query: str, engine: str, n: int, kwargs: dict[str, Any]) -> str:
    """单飞 key：同 query+engine+参数指纹才合并（时间窗/域等影响结果参数
    必须进 key，否则不同检索被错误合并）。"""
    import json as _json
    try:
        params = _json.dumps(kwargs, sort_keys=True, ensure_ascii=False,
                             default=str)
    except Exception:
        params = str(sorted(kwargs.items()))
    return f"{engine}|{n}|{query}|{params}"


def search(query: str, engine: str, n: int = 5, timeout: float = 8, depth: str = "fast", mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
    """统一引擎调用入口；失败返回空 list，不抛异常。kwargs 透传到引擎 builder（如 since/until 时间窗）。

    并发同调用（同 query+engine+参数）经进程内单飞合并为一次上游执行；
    免费引擎 n 桶化（见 bucket_n），leader 按桶内最大 n 执行，调用方输出
    层截断到请求数。
    """
    registry = get_registry()
    fn = registry.get(engine)
    if not fn:
        _warn_unknown_engine(engine)
        return []
    # 语义型引擎不识别平台结构化语法，剥掉字段只留核心词；透传型保持原 query。
    if engine in _SEMANTIC_ENGINES:
        query = strip_structured(query)
    elif engine in _ENTITY_QUERY_ENGINES:
        # 实体型引擎：自然语言句（疑问前缀/属性词）会让实体搜索接口空结果
        query = _entity_core_query(query)
    eff_n = bucket_n(engine, n)
    key = _call_key(query, engine, eff_n, kwargs)

    # 引擎自声明 timeout 是该源的硬上限（此前被调用方 timeout 位置传参整体
    # 覆盖成死配置——OSM timeout:6 实测跑出 10s+5s curl 守卫 11.3s）。
    # deep 模式保持原语义：研究场景宁可等，不受 spec 短超时约束。
    spec_to = (_engine_specs or {}).get(engine) or {}
    st = spec_to.get("timeout") if isinstance(spec_to, dict) else None
    call_to = timeout
    if mode != "deep" and isinstance(st, (int, float)) and 0 < st < timeout:
        call_to = float(st)

    def _execute() -> list[dict[str, Any]]:
        t0 = time.time()
        try:
            results = fn(query, eff_n, call_to, depth=depth, mode=mode, **kwargs)
        except Exception as e:
            # TypeError 可能来自引擎内部逻辑错误而非签名不匹配。
            # 用 inspect.signature 确认引擎是否接受 depth/mode 参数，
            # 减少误判：只有引擎函数签名明确不接受这些参数时才回退。
            _retry_simple = False
            if isinstance(e, TypeError):
                try:
                    sig = inspect.signature(fn)
                    params = list(sig.parameters.keys())
                    _retry_simple = not (
                        "depth" in params or "mode" in params or "kwargs" in params
                        or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for _, p in sig.parameters.items()
                        )
                    )
                except (ValueError, TypeError):
                    _retry_simple = False
            if _retry_simple:
                try:
                    results = fn(query, eff_n, timeout)
                except Exception as e2:
                    logger.error(f"引擎 {engine} 回退失败: {type(e2).__name__}: {e2}")
                    results = []
            else:
                logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
                results = []
        elapsed = time.time() - t0
        if results and isinstance(results, list):
            for r in results:
                if isinstance(r, dict) and "error" not in r:
                    r["_engine"] = engine
                    r["_elapsed"] = round(elapsed, 3)
        return results if isinstance(results, list) else []

    try:
        results = engine_coalescer().run(key, _execute)
    except Exception:
        results = []
    results = results or []
    # 桶化后可能多于请求数：调用方按请求 n 截断语义由 execute_search 融合层负责；
    # 此处保留全量（缓存键按 eff_n 存，命中时同样可截）。
    return results

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="引擎适配层调试")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="anysearch")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--timeout", "-t", type=float, default=8)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(dumps(available_engines()))
        return
    if not args.query:
        parser.error("必须提供 query")
    print(dumps(search(args.query, args.engine, args.n, args.timeout)))


if __name__ == "__main__":
    _cli()
