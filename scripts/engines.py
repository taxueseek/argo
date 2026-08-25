#!/usr/bin/env python3
"""engines.py — Unified Search v2 引擎适配层（门面）

配置驱动 + 声明式 output_map 字段提取 + 通用 parser 兜底。
实现拆分：
  - engines_base.py      公共工具 / cli / http / html / 通用解析
  - engines_builders.py  专用引擎构建器
本文件仅负责 BUILDERS 注册表与 search() 入口。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

try:
    from config import load_config, get_engines
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines

from engines_base import (
    safe_search,
    _build_cli_engine,
    _build_http_engine,
    _build_html_engine,
    _parse_generic,
    _parse_text_output,
    _parse_xml,
    _parse_duckduckgo,
    _parse_uapi,
    _parse_semantic_scholar,
    _ensure_engine_source,
    _CUSTOM_JSON_PARSERS,
)
from recovery import strip_structured

# 对外/测试兼容：专用解析器与 source 纠正
__all__ = [
    "search",
    "available_engines",
    "get_registry",
    "safe_search",
    "_parse_duckduckgo",
    "_parse_uapi",
    "_parse_semantic_scholar",
    "_ensure_engine_source",
]
from engines_builders import (
    _build_exa_engine,
    _build_anysearch_engine,
    _build_parallel_engine,
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
    _build_ths_hot_engine,
    _build_cls_telegraph_engine,
    _build_em_global_news_engine,
    _build_eastmoney_engine,
    _build_itotii_engine,
    _build_baidu_hot_engine,
    _build_toutiao_hot_engine,
    _build_bilibili_hot_engine,
    _build_zhihu_global_engine,
    _build_bocha_engine,
    _build_bocha_ai_engine,
    _build_open_library_engine,
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
    _build_gdelt_engine,
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
)

logger = logging.getLogger("unified_search.engines")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler(sys.stderr))


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
        # 进程内优先（省 subprocess 冷启动）
        try:
            from engines_base import _resolve as _resolve_tpl
            sub_dir = Path(__file__).resolve().parent.parent / "sub-skills" / "local-search"
            if str(sub_dir) not in sys.path:
                # append 而非 insert(0)：避免 sub-skills 顶层模块名
                # （health_check 等）劫持 scripts 下同名模块的解析。
                sys.path.append(str(sub_dir))
            import search_v3
            res = search_v3.search_engines(
                query, engines=None, n=n, timeout=float(timeout),
                max_parallel=5, skip_cache=bool(kwargs.get("skip_cache", False)),
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
    "github": _build_github_engine,
    "google_scholar": _build_google_scholar_engine,
    "v2ex": _build_v2ex_engine,
    "ths_hot": _build_ths_hot_engine,
    "cls_telegraph": _build_cls_telegraph_engine,
    "em_global_news": _build_em_global_news_engine,
    "eastmoney": _build_eastmoney_engine,
    "itotii": _build_itotii_engine,
    "baidu_hot": _build_baidu_hot_engine,
    "toutiao_hot": _build_toutiao_hot_engine,
    "bilibili_hot": _build_bilibili_hot_engine,
    "open_library": _build_open_library_engine,
    "weread": _build_weread_engine,
    "douban_book": _build_douban_book_engine,
    "zhihu_global": _build_zhihu_global_engine,
    "fred": _build_fred_engine,
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
    "you": _build_you_engine,
    "bocha_ai": _build_bocha_ai_engine,
    "imdb": _build_imdb_engine,
    "thesportsdb": _build_thesportsdb_engine,
    "itunes": _build_itunes_engine,
    "gdelt": _build_gdelt_engine,
    "opencorporates": _build_opencorporates_engine,
    "google_patents": _build_google_patents_engine,
    "marginalia": _build_marginalia_engine,
    "wiby": _build_wiby_engine,
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
}

# 语义型引擎：把 query 当自然语言语义检索，不识别平台原生结构化语法
# （from:/repo:/site:/until: 等）。对其剥掉字段只留核心词，避免污染相关性。
# 透传型/垂直源（local_*、social/academic/code、github 等）保持原 query，让平台语法生效。
_SEMANTIC_ENGINES = frozenset({
    "byted", "bocha", "anysearch", "tavily", "exa", "octen",
    "uapi", "searxng", "parallel", "you", "bocha_ai", "local_search",
})

_engine_registry: dict[str, Any] = {}
_engine_registry_loaded = False


def _load_registry():
    global _engine_registry, _engine_registry_loaded
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
        # anysearch 走进程内 builder（原 type=cli subprocess，改为进程内 + HttpClient）
        if name == "anysearch":
            spec["type"] = "anysearch"
        builder = _BUILDERS.get(spec.get("type", "cli"))
        if builder:
            registry[name] = builder(spec)
        else:
            logger.warning(f"未知引擎类型: {spec.get('type')} (引擎 {name})")
    _engine_registry = registry
    _engine_registry_loaded = True


def get_registry() -> dict[str, Any]:
    _load_registry()
    return _engine_registry


def available_engines() -> list[str]:
    return sorted(get_registry().keys())


def search(query: str, engine: str, n: int = 5, timeout: float = 8, depth: str = "fast", mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
    """统一引擎调用入口；失败返回空 list，不抛异常。kwargs 透传到引擎 builder（如 since/until 时间窗）。"""
    registry = get_registry()
    fn = registry.get(engine)
    if not fn:
        logger.warning(f"未知引擎: {engine}")
        return []
    # 语义型引擎不识别平台结构化语法，剥掉字段只留核心词；透传型保持原 query。
    if engine in _SEMANTIC_ENGINES:
        query = strip_structured(query)
    t0 = time.time()
    try:
        results = fn(query, n, timeout, depth=depth, mode=mode, **kwargs)
    except TypeError:
        try:
            results = fn(query, n, timeout)
        except Exception as e:
            logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
            results = []
    except Exception as e:
        logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
        results = []
    elapsed = time.time() - t0
    if results and isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and "error" not in r:
                r["_engine"] = engine
                r["_elapsed"] = round(elapsed, 3)
    return results if isinstance(results, list) else []


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
        print(json.dumps(available_engines(), ensure_ascii=False, indent=2))
        return
    if not args.query:
        parser.error("必须提供 query")
    print(json.dumps(search(args.query, args.engine, args.n, args.timeout), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
