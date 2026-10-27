#!/usr/bin/env python3
"""matrix_search_eval.py — 多语言 × 引擎族 × 场景 全面测试 harness

沿 loho / regression_p0p1 思路：
  - offline：语言检测、路由矩阵、recovery 检查、引擎族、lang_pref（无网络）
  - live：多语金标 E2E（domain / primary / selection_hit / 污染）

用法：
  python3 scripts/matrix_search_eval.py --offline
  python3 scripts/matrix_search_eval.py --live
  python3 scripts/matrix_search_eval.py --all
  python3 scripts/matrix_search_eval.py --all --report tests/matrix_search_report.json

退出码：有 hard FAIL 则为 1（warn 不计入）。
"""

from __future__ import annotations

if __name__ == "__main__": from argo_paths import isolate_state_dir as _isol; _isol("argo-bench")  # 状态隔离(2026-10-10 方案甲)：真实搜索路径会写生产状态（夹具引擎已实测混入 circuit_breaker/usage_log），须先于 argo 状态模块导入，机制见 isolate_state_dir docstring

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from cli_io import dumps, dumps_pretty

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# 中文专用源：非中文主语言不得入 combo
_CN_ONLY = frozenset({
    "bocha", "byted", "wechat_sogou", "zhihu", "zhihu_hot",
    "baidu_hot", "toutiao_hot", "weibo", "xiaohongshu",
    "sina_quote", "tencent_quote", "em_flow", "eastmoney",
    "cls_telegraph", "jin10", "em_miaoxiang", "em_global_news",
    "baidu_baike", "zh_wikipedia", "local_baidu", "local_sogou",
    "weread", "douban_book", "juejin", "v2ex", "wenshu",
})

# recovery L3 禁止的无关垂直（与 recovery 检查保持一致）
_RECOVERY_POLLUTE = frozenset({
    "pypi", "npm", "crates", "jin10", "cls_telegraph",
    "sina_quote", "finviz", "pubchem", "steam",
})

# 日韩等语言引擎候选
_LANG_ENGINE_CANDIDATES = {
    "ja": ("local_bing",),
    "ko": ("local_bing",),
    "cyrillic": ("local_bing", "anysearch"),
    "thai": ("local_bing", "anysearch"),
    "arabic": ("local_bing", "anysearch"),
    "hebrew": ("local_bing", "anysearch"),
    "greek": ("local_bing", "anysearch"),
    "devanagari": ("local_bing", "anysearch"),
    "en": ("anysearch", "local_bing", "octen", "exa"),
    "zh": ("byted", "bocha", "local_bing", "anysearch", "zhihu_global"),
}


class Checker:
    def __init__(self) -> None:
        self.ok = 0
        self.fail = 0
        self.warn = 0
        self.rows: list[dict[str, Any]] = []

    def check(
        self,
        name: str,
        cond: bool,
        detail: str = "",
        *,
        soft: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        if cond:
            self.ok += 1
            status = "PASS"
        elif soft:
            self.warn += 1
            status = "WARN"
        else:
            self.fail += 1
            status = "FAIL"
        row: dict[str, Any] = {
            "name": name,
            "status": status,
            "detail": detail,
        }
        if meta:
            row["meta"] = meta
        self.rows.append(row)
        mark = {"PASS": "✓", "FAIL": "✗", "WARN": "~"}[status]
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        return cond


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE A — 语言检测
# ═══════════════════════════════════════════════════════════════════════════

LANG_DETECT_CASES: list[tuple[str, str, str]] = [
    # (id, query, expect_lang)
    ("ld_zh", "苹果股价最新行情", "zh"),
    ("ld_zh_mix", "Python 异步编程", "zh"),
    ("ld_en", "React hooks tutorial", "en"),
    ("ld_en2", "hello world", "en"),
    ("ld_ja_kana", "アニメ おすすめ", "ja"),
    ("ld_ja_mix", "東京の天気予報", "ja"),
    ("ld_ja_kata", "トヨタ 株価", "ja"),
    ("ld_ko", "한국 영화 추천", "ko"),
    ("ld_ko2", "삼성전자 주가", "ko"),
    ("ld_latin", "café français", "latin"),
    ("ld_cyr", "Как написать скрипт", "cyrillic"),
    ("ld_cyr_mix", "Python курс", "cyrillic"),
    ("ld_thai", "สวัสดีครับ", "thai"),
    ("ld_ar", "مرحبا بالعالم", "arabic"),
    ("ld_he", "שלום עולם", "hebrew"),
    ("ld_el", "Καλημέρα", "greek"),
    ("ld_hi", "नमस्ते दुनिया", "devanagari"),
    # 纯汉字日文地名：当前可能判 zh（假名缺席）— soft 期望仍记为 ja 缺口
    ("ld_ja_kanji_only", "宮崎駿", "ja"),
]


def run_offline_lang(c: Checker) -> None:
    print("\n== offline: language detection ==")
    from lang_detect import detect_language, detect_script, engine_lang_param, language_features

    for cid, q, expect in LANG_DETECT_CASES:
        got = detect_language(q)
        soft = cid == "ld_ja_kanji_only" and got == "zh"
        c.check(
            f"lang:{cid}",
            got == expect,
            detail=f"got={got} expect={expect} q={q!r}",
            soft=soft,
            meta={"query": q, "lang": got, "expect": expect},
        )

    # script 与 primary 同源
    for q, lang, script in [
        ("苹果股价", "zh", "cjk"),
        ("アニメ", "ja", "kana"),
        ("한국", "ko", "hangul"),
        ("hello", "en", "latin"),
        ("Как", "cyrillic", "cyrillic"),
    ]:
        f = language_features(q)
        c.check(
            f"script_align:{lang}",
            f["primary_lang"] == lang and f["script"] == script,
            detail=str(f),
        )

    # 引擎语言参数覆盖非 CJK
    for q, param, expect_sub in [
        ("アニメ", "setlang", "ja"),
        ("한국", "hl", "ko"),
        ("hello world", "setlang", "en"),
        ("苹果", "setlang", "zh"),
        ("Как дела", "setlang", "ru"),
        ("مرحبا", "setlang", "ar"),
        ("שלום", "lang", "he"),
    ]:
        v = engine_lang_param(param, q)
        c.check(
            f"lang_param:{param}:{q[:6]}",
            expect_sub.lower() in (v or "").lower(),
            detail=f"got={v}",
        )


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE B — 路由矩阵（语言 × 场景）
# ═══════════════════════════════════════════════════════════════════════════

# soft=True：已知能力缺口（日韩垂直域、纯汉字日文），记 WARN 不阻断
ROUTE_MATRIX: list[dict[str, Any]] = [
    # ── 金融 / 行情 ──
    {"id": "R_zh_stock", "q": "贵州茅台股价", "lang": "zh", "domain": "stock_query",
     "primary": "sina_quote", "scenario": "finance"},
    {"id": "R_en_usstock", "q": "AAPL stock price", "lang": "en", "domain": "us_stock",
     "primary": "finviz", "scenario": "finance"},
    {"id": "R_zh_macro", "q": "中国 GDP", "lang": "zh", "domain": "macro_data",
     "primary_any": ["worldbank", "nbs_stats", "fred"], "scenario": "macro"},
    {"id": "R_zh_trade", "q": "中国 8541 2023 进出口", "lang": "zh", "domain": "trade_stats",
     "primary": "un_comtrade", "scenario": "macro"},
    {"id": "R_zh_security", "q": "log4j 漏洞 CVSS", "lang": "en", "domain": "security_search",
     "primary": "nvd", "scenario": "tech"},
    {"id": "R_en_cve", "q": "CVE-2021-44228 severity", "lang": "en", "domain": "security_search",
     "primary": "nvd", "scenario": "tech"},
    {"id": "R_zh_lifecycle", "q": "python 生命周期 停止支持", "lang": "zh", "domain": "lifecycle_search",
     "primary": "endoflife", "scenario": "tech"},
    {"id": "R_zh_package_intel", "q": "npm express 弃用", "lang": "en", "domain": "package_intel",
     "primary": "deps_dev", "scenario": "tech"},
    {"id": "R_en_macro", "q": "US CPI", "lang": "en", "domain": "macro_data",
     "primary": "fred", "scenario": "macro"},
    {"id": "R_ja_stock", "q": "トヨタ 株価", "lang": "ja", "domain": None,
     "need_lang_engine": True, "forbid_cn": True, "scenario": "finance", "soft": True},
    {"id": "R_ko_stock", "q": "삼성전자 주가", "lang": "ko", "domain": None,
     "need_lang_engine": True, "forbid_cn": True, "scenario": "finance", "soft": True},

    # ── 影视 ──
    {"id": "R_en_film", "q": "Inception movie director", "lang": "en",
     "domain": "film_search", "primary": "imdb", "scenario": "film"},
    {"id": "R_zh_film", "q": "肖申克的救赎 主演", "lang": "zh",
     "domain": "film_search", "primary": "imdb", "scenario": "film"},
    {"id": "R_en_film2", "q": "Interstellar film cast", "lang": "en",
     "domain": "film_search", "primary": "imdb", "scenario": "film"},
    {"id": "R_ja_film_kana", "q": "宮崎駿 アニメ 映画", "lang": "ja",
     "domain": "film_search", "primary": "imdb", "scenario": "film", "soft": True},
    {"id": "R_ko_film", "q": "기생충 영화 감독", "lang": "ko",
     "domain": "film_search", "primary": "imdb", "scenario": "film", "soft": True},

    # ── 视频（2026-10-27 新增域）──
    # 显式「视频」意图必须落到 video 域；泛化的「视频生成模型」由
    # chinese_tech_deep 接管（见 test_domain_rule_schema 的反例表）。
    {"id": "R_zh_video_recipe", "q": "红烧肉 做法 视频", "lang": "zh",
     "domain": "video", "primary": "bilibili", "scenario": "video"},

    # ── 体育 ──
    {"id": "R_zh_sports", "q": "梅西 俱乐部", "lang": "zh",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_en_sports", "q": "LeBron James team", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_en_sports2", "q": "Cristiano Ronaldo club", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_zh_sports2", "q": "库里 球队", "lang": "zh",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_en_wc", "q": "World Cup 2022 winner", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports"},
    {"id": "R_ja_sports", "q": "メッシ クラブ", "lang": "ja",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports", "soft": True},
    {"id": "R_ko_sports", "q": "손흥민 팀", "lang": "ko",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports", "soft": True},

    # ── 地理 ──
    {"id": "R_zh_geo", "q": "埃菲尔铁塔在哪", "lang": "zh",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo"},
    {"id": "R_en_geo", "q": "where is Eiffel Tower", "lang": "en",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo"},
    {"id": "R_zh_geo2", "q": "黄河 流经省份", "lang": "zh",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo"},
    {"id": "R_ja_geo", "q": "エッフェル塔 どこ", "lang": "ja",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo", "soft": True},

    # ── 组织 ──
    {"id": "R_zh_org", "q": "国务院职能", "lang": "zh",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    {"id": "R_en_org", "q": "NASA founding year", "lang": "en",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    {"id": "R_en_org2", "q": "Apple Inc headquarters", "lang": "en",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    {"id": "R_zh_org2", "q": "清华大学 创办年份", "lang": "zh",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},
    # 中文企业总部不是地理问题（2026-10-22 修：geo_places 的方位词 unless 增补
    # 「总部|母公司|子公司|注册地|办公地」后落到本域；此前落 geo_places，用 OSM+维基
    # 只拿到广汽/新凯来/五粮液等无关结果）
    {"id": "R_zh_org_hq", "q": "华为 总部在哪", "lang": "zh",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org"},

    # ── 企业/工商（company_search）──
    # 2026-10-22 补：此前 ROUTE_MATRIX 无本域用例，导致「中国企业股权/工商信息」
    # 被路由到本域、而该域 combo 只有国际法人库（gleif/opencorporates）+ 百科的
    # 问题长期无人发现——实测「长亭科技 股东 持股比例」返回 5 条同名「XX科技
    # 有限公司」干扰。元宝进 combo 第 1 位后（a5a46ad）才具中文交付能力。
    {"id": "R_zh_company_shareholder", "q": "长亭科技 股东 持股比例", "lang": "zh",
     "domain": "company_search", "primary": "opencorporates", "scenario": "company"},
    {"id": "R_zh_company_regcap", "q": "字节跳动 注册资本 变更", "lang": "zh",
     "domain": "company_search", "primary": "opencorporates", "scenario": "company"},
    # 2026-10-25 补（企业主体查询 MECE 类目）：尽调风险（注销/经营异常）、开票
    # （开票信息/税号）、人查公司（担任法定代表人/名下公司）三类此前落
    # org_entity 的 wikidata 组合——wikidata 对中文企业 7 天空结果率 82%，
    # company_search 的元宝才是交付源。config.yaml 两域词表成对改动
    # （org_entity 让位 unless + company_search 收口 match/unless），改一处
    # 必须同步另一处。企业名/人名一律合成名（对外脱敏纪律，v2.9.8 同款）。
    {"id": "R_zh_company_risk", "q": "晨曦合成科技有限公司 注销了吗", "lang": "zh",
     "domain": "company_search", "primary": "opencorporates", "scenario": "company"},
    {"id": "R_zh_company_invoice", "q": "晨曦合成科技有限公司 开票信息 税号", "lang": "zh",
     "domain": "company_search", "primary": "opencorporates", "scenario": "company"},
    {"id": "R_zh_company_person", "q": "李合成 担任法定代表人的公司", "lang": "zh",
     "domain": "company_search", "primary": "opencorporates", "scenario": "company"},
    # 双向锁（v2.9.8 纪律）：新词表不得误吸非主体查询——办事指南是 howto、
    # 招聘是求职，都不该进尽调域
    {"id": "R_zh_company_not_howto", "q": "公司注销流程 怎么办", "lang": "zh",
     "domain_not": ["company_search", "org_entity"], "scenario": "company"},
    {"id": "R_zh_company_not_job", "q": "晨曦合成科技有限公司 招聘", "lang": "zh",
     "domain_not": ["company_search"], "scenario": "company"},
    # ── 2026-10-25 六族 MECE 扫描（学术/金融财务/法律/医药）──
    # 全部为真实场景类目，企业名/人名保持合成名（脱敏纪律）。每处 config 改动
    # 都有成对的 unless 让位，反例见 tests/test_domain_rule_schema.py。
    {"id": "R_zh_stock_financials", "q": "贵州茅台 2025 年报 营收", "lang": "zh",
     "domain": "stock_query", "primary": "sina_quote", "scenario": "finance"},
    {"id": "R_zh_stock_valuation", "q": "贵州茅台 市盈率 估值", "lang": "zh",
     "domain": "stock_query", "primary": "sina_quote", "scenario": "finance"},
    # zh 财务词形不得落美股域（sec_companyfacts 只认 CIK，A 股全废）
    {"id": "R_zh_usfund_yields_cn", "q": "宁德时代 毛利率 现金流", "lang": "zh",
     "domain_not": ["us_fundamentals"], "scenario": "finance"},
    # 民事实务（劳动/借贷）此前落 chinese_general，flk_law 才是交付源
    {"id": "R_zh_legal_labor", "q": "劳动合同解除 赔偿 N+1", "lang": "zh",
     "domain": "legal", "primary": "anysearch", "scenario": "legal"},
    {"id": "R_zh_legal_lending", "q": "民间借贷 利率 司法保护上限", "lang": "zh",
     "domain": "legal", "primary": "anysearch", "scenario": "legal"},
    # 药品说明书此前被 code_search 的「用法」子串抢走（GitHub/MDN 答不了药）
    {"id": "R_zh_medical_drug_label", "q": "布洛芬 说明书 用法用量", "lang": "zh",
     "domain": "medical", "primary": "clinicaltrials", "scenario": "medical"},
    # 综述语境不被模型目录抢（models_dev 答不了综述）
    {"id": "R_zh_survey_not_modelcard", "q": "大模型 长上下文 综述", "lang": "zh",
     "domain_not": ["ai_model"], "scenario": "academic"},
    # 论文写作 howto / 年报模板 howto 不进学术域与行情域
    {"id": "R_zh_academic_not_howto", "q": "怎么写论文 查重 降重", "lang": "zh",
     "domain_not": ["academic"], "scenario": "academic"},
    {"id": "R_zh_stock_not_howto", "q": "年报 模板 怎么写", "lang": "zh",
     "domain_not": ["stock_query", "us_fundamentals"], "scenario": "finance"},

    # ── 媒体/音乐 ──
    {"id": "R_en_media", "q": "Taylor Swift album", "lang": "en",
     "domain": "media_search", "primary": "itunes", "scenario": "media"},
    {"id": "R_zh_media", "q": "周杰伦 专辑", "lang": "zh",
     "domain": "media_search", "primary": "itunes", "scenario": "media"},

    # ── 天气 ──
    {"id": "R_zh_weather", "q": "北京 天气", "lang": "zh",
     "domain": "weather_query", "scenario": "weather"},
    {"id": "R_en_weather", "q": "weather Tokyo", "lang": "en",
     "domain": "weather_query", "scenario": "weather"},
    {"id": "R_ja_weather", "q": "東京の天気", "lang": "ja",
     "domain": "weather_query", "scenario": "weather", "soft": True},

    # ── 学术 / 化学 / RFC / 代码 ──
    {"id": "R_en_academic", "q": "transformer attention paper", "lang": "en",
     "domain": "academic", "primary_any": ["arxiv", "crossref", "semantic_scholar"],
     "scenario": "academic"},
    {"id": "R_zh_chem", "q": "阿司匹林 分子式", "lang": "zh",
     "domain": "chem_search", "primary": "pubchem", "scenario": "chem"},
    # RFC 9110 字母过少，lang 可能判 other；只硬卡 domain/primary
    {"id": "R_en_rfc", "q": "RFC 9110", "lang": None,
     "domain": "rfc_search", "primary": "rfc_editor", "scenario": "rfc"},
    {"id": "R_en_tech", "q": "React vs Vue", "lang": "en",
     "domain": None,  # english_tech / general
     "forbid": list(_CN_ONLY - {"local_baidu", "local_sogou"}),  # 放宽 local
     "forbid_cn": True, "scenario": "tech"},
    {"id": "R_zh_package", "q": "requests pypi", "lang": "en",
     "domain": "package_search", "primary_any": ["pypi", "npm"], "scenario": "code",
     "soft": True},
    # 面查负例（2026-09-06 diffuse intent guard）：长主题句含生态实体词但无
    # 意图词 → 点查域让位，combo 不得被 pypi/npm/models_dev 锁死。
    {"id": "R_en_pkg_diffuse", "q": "pnpm file: directory dependency no content hash reinstall",
     "lang": "en", "domain": None, "forbid": ["pypi", "npm", "crates", "docker_hub"],
     "scenario": "tech"},
    {"id": "R_en_model_diffuse", "q": "DeepSeek Harness DSH 插件开发",
     # 拉丁实体词主导，语言检测判 en（同 R_zh_package 计算方式）；守卫只检查
     # 域让位：ai_model/models_dev 不得锁死通用开发意图
     "lang": "en", "domain": None, "forbid": ["models_dev", "huggingface"],
     "scenario": "tech"},

    # ── 通用多语 ──
    {"id": "R_ja_gen", "q": "アニメ おすすめ", "lang": "ja",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_ko_gen", "q": "한국 영화 추천", "lang": "ko",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_cyr_gen", "q": "Как написать скрипт", "lang": "cyrillic",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_ar_gen", "q": "أخبار التكنولوجيا", "lang": "arabic",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_th_gen", "q": "อากาศ กรุงเทพ", "lang": "thai",
     "domain": None, "need_lang_engine": True, "forbid_cn": True, "scenario": "general"},
    {"id": "R_en_gen", "q": "hello world", "lang": "en",
     "domain": None, "forbid_cn": True, "scenario": "general"},
    {"id": "R_zh_gen", "q": "如何学习机器学习", "lang": "zh",
     "domain": None, "scenario": "general"},

    # ── 2026-09-02 金标增补：fast 单引擎冗余事故回归钉 ──
    # 事故：英文技术无域查询 fast 保底为 anysearch 单引擎，首引擎上游波动
    # 返回高计数垃圾时早停吞掉次引擎（execution 层守卫见 search.py
    # _query_coverage_ok；此处钉路由层契约：保底 combo 必含 ≥2 个免费通用源）。
    # 2026-10-26 9f50a12 通用层改实测排序后，兜底 combo 由 anysearch 领跑
    # 换成 yuanbao+tavily_free（隔离 A/B p50 773→489ms）——契约不变，引擎
    # 身份随实测排序走，故候选表跟随扩到当前领跑组合（原表断言的 anysearch/
    # local_bing 在新排序下已不是前三位）。
    # 2026-10-27：覆盖地板 + 语言门落地后，英文通用的领队是 parallel_free
    # （tavily 交付 2 条低于地板、yuanbao 只声明 chinese）。契约仍是「前三位
    # ≥2 个通用源」，候选表继续随实测领队更新。
    {"id": "R_en_tech_nodomain", "q": "Crawl4AI pruning content filter extraction",
     "lang": "en", "domain": "general_search",
     "primary_any": ["anysearch", "local_bing", "yuanbao", "tavily_free",
                     "parallel_free"],
     "forbid_cn": True, "scenario": "general"},
    {"id": "R_en_tech_py", "q": "python asyncio tutorial", "lang": "en",
     "domain": "english_tech", "primary_any": ["octen", "anysearch", "local_bing"],
     "forbid_cn": True, "scenario": "general"},
    {"id": "R_zh_food", "q": "附近好吃的本帮菜馆", "lang": "zh",
     "domain": "chinese_general", "primary_any": ["bocha", "anysearch", "local_bing"],
     "scenario": "general"},

    # ── 2026-09-14 负向控制增补（OpenAI eval 视角 test-04）─────────────────
    # 泛查询不得误触发垂直域/垂直引擎——正像「给现有应用加 Tailwind」不该
    # 触发「新建 demo」skill：以下查询均不含垂直意图词，命中任何行情/漏洞/
    # 影视/宏观/论文/生命周期/包情报专用源即判失败（结构化垂直源对泛查询
    # 返回的全是不相关结果）。domain_not 防域误抢，forbid 防引擎漏入。
    {"id": "N_en_cooking", "q": "how to cook pasta carbonara", "lang": "en",
     "domain_not": ["stock_query", "us_stock", "macro_data", "trade_stats",
                    "security_search", "lifecycle_search", "package_intel",
                    "film_search"],
     "forbid": ["sina_quote", "finviz", "eastmoney", "nvd", "imdb", "fred",
                "worldbank", "nbs_stats", "un_comtrade", "endoflife",
                "deps_dev", "openalex", "europepmc"],
     "forbid_cn": True, "scenario": "negative"},
    {"id": "N_zh_cooking", "q": "怎么把排骨炖得软烂", "lang": "zh",
     "domain_not": ["stock_query", "us_stock", "macro_data", "trade_stats",
                    "security_search", "lifecycle_search", "package_intel",
                    "film_search"],
     "forbid": ["sina_quote", "finviz", "eastmoney", "nvd", "imdb", "fred",
                "worldbank", "nbs_stats", "un_comtrade", "endoflife",
                "deps_dev", "openalex", "europepmc"],
     "scenario": "negative"},
    {"id": "N_en_history", "q": "who invented the printing press", "lang": "en",
     "domain_not": ["stock_query", "us_stock", "macro_data", "trade_stats",
                    "security_search", "lifecycle_search", "package_intel",
                    "film_search"],
     "forbid": ["sina_quote", "finviz", "eastmoney", "nvd", "imdb", "fred",
                "worldbank", "nbs_stats", "un_comtrade", "endoflife",
                "deps_dev", "openalex", "europepmc"],
     # 不带 forbid_cn：实体问句合理路由到 org_entity（wikidata/wikipedia 族），
     # 其中 zh_wikipedia 是有实测依据的语言中立成员（lang_capability：
     # 「擅长 zh/en/fr/es…」，MediaWiki 跨语检索），对 en 查询不算误入。
     # 负向门只钉「泛查询不进垂直域/垂直引擎」这条真不变量。
     "scenario": "negative"},
    {"id": "N_en_garden", "q": "how often should I water tomato plants",
     "lang": "en",
     "domain_not": ["stock_query", "us_stock", "macro_data", "trade_stats",
                    "security_search", "lifecycle_search", "package_intel",
                    "film_search"],
     "forbid": ["sina_quote", "finviz", "eastmoney", "nvd", "imdb", "fred",
                "worldbank", "nbs_stats", "un_comtrade", "endoflife",
                "deps_dev", "openalex", "europepmc"],
     "forbid_cn": True, "scenario": "negative"},
    {"id": "N_zh_health", "q": "深蹲膝盖疼怎么恢复", "lang": "zh",
     "domain_not": ["stock_query", "us_stock", "macro_data", "trade_stats",
                    "security_search", "lifecycle_search", "package_intel",
                    "film_search"],
     "forbid": ["sina_quote", "finviz", "eastmoney", "nvd", "imdb", "fred",
                "worldbank", "nbs_stats", "un_comtrade", "endoflife",
                "deps_dev", "openalex", "europepmc"],
     "scenario": "negative"},
    {"id": "N_en_hobby", "q": "best wood for beginner woodworking", "lang": "en",
     "domain_not": ["stock_query", "us_stock", "macro_data", "trade_stats",
                    "security_search", "lifecycle_search", "package_intel",
                    "film_search"],
     "forbid": ["sina_quote", "finviz", "eastmoney", "nvd", "imdb", "fred",
                "worldbank", "nbs_stats", "un_comtrade", "endoflife",
                "deps_dev", "openalex", "europepmc"],
     "forbid_cn": True, "scenario": "negative"},

    # ── 域覆盖（2026-10-22）：补 53 个此前零用例的域 ────────────────────────
    # 零用例 = 路由回归无人挡。company_search 零用例，「中国企业股权/工商信息」
    # 被路由到本域却无中文能力的问题因此潜伏 8 天；geo_places 的「在哪」劫持
    # 企业总部查询同理（两者都是靠人工撞出来的）。lang 取路由实测 primary_lang，
    # 与 _route_ok 的 lang 断言对齐；primary 取域声明的主引擎。
    {"id": "R_ai_model", "q": "models.dev claude 价格 上下文", "lang": "en", "domain": "ai_model", "primary": "models_dev", "scenario": "tech"},
    {"id": "R_aviation_weather", "q": "北京首都机场 航空气象 METAR", "lang": "zh", "domain": "aviation_weather", "primary": "aviation_weather", "scenario": "weather"},
    {"id": "R_chinese_tech_deep", "q": "中文 编程 算法 架构 深度对比", "lang": "zh", "domain": "chinese_tech_deep", "primary": "byted", "scenario": "tech"},
    {"id": "R_claim_check", "q": "辟谣 传言 事实核查", "lang": "zh", "domain": "claim_check", "primary": "factcheck_org", "scenario": "factcheck"},
    {"id": "R_cn_encyclopedia", "q": "长征 百度百科 词条解释", "lang": "zh", "domain": "cn_encyclopedia", "primary": "baidu_baike", "scenario": "media"},
    {"id": "R_cn_tech_community", "q": "掘金 前端文章 技术博客", "lang": "zh", "domain": "cn_tech_community", "primary": "juejin", "scenario": "tech"},
    {"id": "R_code_search", "q": "github 代码 实现 用法", "lang": "zh", "domain": "code_search", "primary": "github", "scenario": "code"},
    {"id": "R_crypto_search", "q": "比特币 以太坊 加密货币 价格", "lang": "zh", "domain": "crypto_search", "primary": "coingecko", "scenario": "finance"},
    {"id": "R_dataset_search", "q": "科研数据集 开放数据 下载", "lang": "zh", "domain": "dataset_search", "primary": "datacite", "scenario": "academic"},
    {"id": "R_dictionary_search", "q": "英文单词 词典 意思", "lang": "zh", "domain": "dictionary_search", "primary": "free_dictionary", "scenario": "media"},
    {"id": "R_earth_science", "q": "地震 震级 余震 震中", "lang": "zh", "domain": "earth_science", "primary": "usgs", "scenario": "science"},
    {"id": "R_em_news_search", "q": "东财 全球资讯 市场", "lang": "zh", "domain": "em_news_search", "primary": "em_global_news", "scenario": "news"},
    {"id": "R_energy_grid", "q": "电网 发电结构 发电量", "lang": "zh", "domain": "energy_grid", "primary": "energy_charts", "scenario": "science"},
    {"id": "R_entity_search", "q": "维基数据 知识图谱 实体查询", "lang": "zh", "domain": "entity_search", "primary": "wikidata", "scenario": "media"},
    {"id": "R_fact_check", "q": "长江 长度 是多少", "lang": "zh", "domain": "fact_check", "primary": "wikipedia", "scenario": "factcheck"},
    {"id": "R_futures_market", "q": "沪铜 期货 行情", "lang": "zh", "domain": "futures_market", "primary": "shfe", "scenario": "finance"},
    {"id": "R_fx_query", "q": "人民币 汇率 中间价", "lang": "zh", "domain": "fx_query", "primary": "fx_rate", "scenario": "finance"},
    {"id": "R_game_search", "q": "steam 游戏推荐 好玩的游戏", "lang": "zh", "domain": "game_search", "primary": "steam", "scenario": "media"},
    {"id": "R_global_event", "q": "国际事件 地缘政治 局势", "lang": "zh", "domain": "global_event", "primary": "google_news", "scenario": "news"},
    {"id": "R_hk_filings", "q": "港股 港交所 披露易 公告", "lang": "zh", "domain": "hk_filings", "primary": "hkex", "scenario": "finance"},
    {"id": "R_hot_trending", "q": "微博热搜 今日热搜", "lang": "zh", "domain": "hot_trending", "primary": "baidu_hot", "scenario": "community"},
    {"id": "R_image_search", "q": "雪景 壁纸 图片", "lang": "zh", "domain": "image_search", "primary": "openverse", "scenario": "media"},
    {"id": "R_intl_news_flash", "q": "国际新闻 外媒 国际要闻", "lang": "zh", "domain": "intl_news_flash", "primary": "guardian_rss", "scenario": "news"},
    {"id": "R_japan_law", "q": "日本 法令 民法 条文", "lang": "zh", "domain": "japan_law", "primary": "egov_law", "scenario": "legal"},
    {"id": "R_kor_law", "q": "韩国 法令 民法 条文", "lang": "zh", "domain": "kor_law", "primary": "kor_law", "scenario": "legal"},
    {"id": "R_law_text", "q": "法条 法律条文 全文", "lang": "zh", "domain": "law_text", "primary": "flk_law", "scenario": "legal"},
    {"id": "R_legal", "q": "刑法 司法解释 判例", "lang": "zh", "domain": "legal", "primary": "anysearch", "scenario": "legal"},
    {"id": "R_meme_slang", "q": "网络梗 流行语 黑话", "lang": "zh", "domain": "meme_slang", "primary": "itotii", "scenario": "media"},
    {"id": "R_ml_models", "q": "huggingface 预训练模型 开源模型", "lang": "zh", "domain": "ml_models", "primary": "huggingface", "scenario": "tech"},
    {"id": "R_news_realtime", "q": "最新新闻 今天发生 突发", "lang": "zh", "domain": "news_realtime", "primary": "byted", "scenario": "news"},
    {"id": "R_outbreak_health", "q": "疫情 暴发 流行病 通报", "lang": "zh", "domain": "outbreak_health", "primary": "who_don", "scenario": "science"},
    {"id": "R_patent_search", "q": "专利 权利要求 技术方案", "lang": "zh", "domain": "patent_search", "primary": "google_patents", "scenario": "academic"},
    {"id": "R_prediction_market", "q": "预测市场 事件合约", "lang": "zh", "domain": "prediction_market", "primary": "polymarket", "scenario": "finance"},
    {"id": "R_protein_search", "q": "蛋白质 基因 氨基酸序列", "lang": "zh", "domain": "protein_search", "primary": "uniprot", "scenario": "academic"},
    {"id": "R_redskill_search", "q": "小红书 技能 排行榜", "lang": "zh", "domain": "redskill_search", "primary": "redskill", "scenario": "community"},
    {"id": "R_scholar_search", "q": "google scholar 学术论文 期刊", "lang": "zh", "domain": "scholar_search", "primary": "google_scholar", "scenario": "academic"},
    {"id": "R_sec_filings", "q": "SEC EDGAR 招股书 财报申报", "lang": "zh", "domain": "sec_filings", "primary": "sec_edgar", "scenario": "finance"},
    {"id": "R_semantic_discovery", "q": "discover landscape ecosystem emerging", "lang": "en", "domain": "semantic_discovery", "primary": "exa", "scenario": "tech"},
    {"id": "R_shopping", "q": "购物 评测 推荐 踩坑", "lang": "zh", "domain": "shopping", "primary": "zhihu", "scenario": "media"},
    {"id": "R_skill_search", "q": "agent skills 市场 目录", "lang": "en", "domain": "skill_search", "primary": "skillsmp", "scenario": "community"},
    {"id": "R_dsh_plugin", "q": "dsh 插件 记忆", "lang": "zh", "domain": "dsh_plugin_search", "primary": "dshopen", "scenario": "community"},  # DSH 插件目录（与 skill_search 语义不同：插件是 npm 包，技能是 SKILL.md）
    {"id": "R_species_search", "q": "物种 学名 生物分类 拉丁名", "lang": "zh", "domain": "species_search", "primary": "gbif", "scenario": "academic"},
    {"id": "R_stackoverflow_search", "q": "stackoverflow 编程问题 代码问题", "lang": "zh", "domain": "stackoverflow_search", "primary": "stackoverflow", "scenario": "code"},
    {"id": "R_standards", "q": "国家标准 国标 标准号", "lang": "zh", "domain": "standards", "primary": "std_samr", "scenario": "academic"},
    {"id": "R_tech_deep", "q": "deep dive 架构 实践", "lang": "zh", "domain": "tech_deep", "primary": "arxiv", "scenario": "tech"},
    # 2026-10-25 约定更新：zh 词形财务查询统一走 stock_query（sina/eastmoney
    # 双地覆盖，与「XX 股价」分流同构）；本域收英文词形与美股语境。代价是
    # zh 美股财务查询损失 sec 结构化精度，换来 A 股企业不再落只认 CIK 的
    # sec_companyfacts 全空——两害取其轻，成对改动注释见 config us_fundamentals。
    # 英文方向的域锁定另见 R_us_fundamentals_en。
    {"id": "R_us_fundamentals", "q": "营收 净利润 财务数据 苹果", "lang": "zh",
     "domain": "stock_query", "primary": "sina_quote", "scenario": "finance"},
    {"id": "R_us_fundamentals_en", "q": "apple annual revenue net income", "lang": "en",
     "domain": "us_fundamentals", "primary": "sec_companyfacts", "scenario": "finance"},
    {"id": "R_us_legal", "q": "supreme court 判例 precedent", "lang": "en", "domain": "us_legal", "primary": "federal_register", "scenario": "legal"},
    {"id": "R_web_archive", "q": "网页快照 历史网页 wayback", "lang": "zh", "domain": "web_archive", "primary": "wikipedia", "scenario": "general"},
    {"id": "R_web_docs", "q": "mozilla developer fetch 属性", "lang": "en", "domain": "web_docs", "primary": "mdn", "scenario": "code"},
    {"id": "R_wechat_search", "q": "微信公众号 文章 搜狗微信", "lang": "zh", "domain": "wechat_search", "primary": "wechat_sogou", "scenario": "community"},
    {"id": "R_world_news", "q": "noticias 国际 新闻", "lang": "zh", "domain": "world_news", "primary": "anysearch", "scenario": "news"},
    {"id": "R_zhihu_content", "q": "知乎 怎么看待 如何评价", "lang": "zh", "domain": "zhihu_content", "primary": "zhihu", "scenario": "media"},
    {"id": "R_zhihu_hot_list", "q": "知乎热榜 知乎热搜", "lang": "zh", "domain": "zhihu_hot_list", "primary": "zhihu_hot", "scenario": "community"},
    {"id": "R_zhihu_user_data", "q": "我的知乎 我的创作 收藏", "lang": "zh", "domain": "zhihu_user_data", "primary": "zhihu_user", "scenario": "community"},

    # ── 域冲突让位修复（2026-10-22）────────────────────────────────────────
    # 这三个域的触发词此前被更宽泛的域覆盖，无法通过自动路由命中（因此曾登记在
    # test_route_matrix_coverage 的 _NO_CASE_DEBT 里）。在宽泛侧加 unless 让位后
    # 各补一条钉住；宽泛侧的真查询锁在 test_domain_pattern_precision 的既有用例。
    {"id": "R_jin10_flash", "q": "金十 7x24 快讯", "lang": "zh", "domain": "jin10_flash", "primary": "jin10", "scenario": "news"},
    {"id": "R_v2ex_search", "q": "v2ex 技术社区", "lang": "zh", "domain": "v2ex_search", "primary": "v2ex", "scenario": "code"},
    {"id": "R_wenshu_query", "q": "裁判文书 判决书", "lang": "zh", "domain": "wenshu_query", "primary": "wenshu", "scenario": "legal"},
]


def _route_ok(case: dict[str, Any], d: dict[str, Any]) -> tuple[bool, str]:
    combo = list(d.get("engines_combo") or [])
    got_d = d.get("domain")
    got_e = d.get("engine")
    feat = d.get("features") or {}
    got_lang = feat.get("primary_lang")
    problems: list[str] = []

    if case.get("lang") is not None and got_lang != case["lang"]:
        # 纯汉字等已知误判允许 soft 外层处理；这里仍记问题
        problems.append(f"lang={got_lang}!={case['lang']}")

    if case.get("domain") is not None and got_d != case["domain"]:
        problems.append(f"domain={got_d}!={case['domain']}")

    # 负向控制（test-04 视角）：泛查询不得命中列出的垂直域
    domain_not = case.get("domain_not")
    if domain_not and got_d in domain_not:
        problems.append(f"domain 误命中: {got_d} in {domain_not}")

    primary = case.get("primary")
    if primary and not (got_e == primary or primary in combo[:2]):
        problems.append(f"primary missing: eng={got_e} combo={combo}")

    primary_any = case.get("primary_any")
    if primary_any and not any(p == got_e or p in combo[:3] for p in primary_any):
        problems.append(f"primary_any miss: eng={got_e} combo={combo}")

    if case.get("need_lang_engine"):
        cands = _LANG_ENGINE_CANDIDATES.get(case.get("lang") or "", ())
        if cands and not any(e in combo for e in cands):
            problems.append(f"no lang engine in {combo}")

    forbid = set(case.get("forbid") or [])
    if case.get("forbid_cn"):
        forbid |= _CN_ONLY
        # 中文主查询允许中文源
        if case.get("lang") == "zh":
            forbid -= _CN_ONLY
    hit_forbid = [e for e in combo if e in forbid]
    if hit_forbid:
        problems.append(f"forbid={hit_forbid}")

    ok = not problems
    detail = (
        f"domain={got_d} eng={got_e} combo={combo} lang={got_lang}"
        + (f" | {'; '.join(problems)}" if problems else "")
    )
    return ok, detail


def run_offline_route(c: Checker) -> None:
    print("\n== offline: route matrix (lang × scenario) ==")
    from route import route_query
    from unittest.mock import MagicMock, patch

    # 离线金标验的是「路由语义」，不是运行时健康——真实熔断/配额状态
    # 会随 live 探测漂移（bocha 曾被连续失败自动禁用致金标误报），
    # 与单测「路由检查须打桩」同一教训。2026-09-07。
    class _AllowAll:
        def allow(self, eng):
            return True, "closed"

        def get_negative(self, *a, **k):
            return None

        def status(self, eng):
            return {"state": "closed"}

        def record_success(self, *a, **k):
            pass

        def record_failure(self, *a, **k):
            pass

        def set_negative(self, *a, **k):
            pass

        def clear_negative(self, *a, **k):
            pass

    by_scenario: dict[str, list[bool]] = {}
    by_lang: dict[str, list[bool]] = {}

    for case in ROUTE_MATRIX:
        with patch("circuit_breaker.get_breaker", return_value=_AllowAll()), \
             patch("quota.get_quota_manager", return_value=MagicMock()):
            d = route_query(case["q"], mode="auto", depth="fast",
                            context="search")
        ok, detail = _route_ok(case, d)
        soft = bool(case.get("soft"))
        c.check(
            f"route:{case['id']}",
            ok,
            detail=detail,
            soft=soft,
            meta={
                "id": case["id"],
                "q": case["q"],
                "scenario": case.get("scenario"),
                "lang": case.get("lang"),
                "domain": d.get("domain"),
                "engine": d.get("engine"),
                "combo": d.get("engines_combo"),
                "primary_lang": (d.get("features") or {}).get("primary_lang"),
            },
        )
        sc = case.get("scenario") or "other"
        lg = case.get("lang") or "other"
        by_scenario.setdefault(sc, []).append(ok)
        by_lang.setdefault(lg, []).append(ok)

    print("\n  -- route coverage by scenario --")
    for sc, vals in sorted(by_scenario.items()):
        rate = sum(vals) / len(vals) if vals else 0
        print(f"     {sc:12} {sum(vals)}/{len(vals)} ({rate:.0%})")
    print("  -- route coverage by lang --")
    for lg, vals in sorted(by_lang.items()):
        rate = sum(vals) / len(vals) if vals else 0
        print(f"     {lg:12} {sum(vals)}/{len(vals)} ({rate:.0%})")


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE C — recovery 检查
# ═══════════════════════════════════════════════════════════════════════════

def run_offline_recovery(c: Checker) -> None:
    print("\n== offline: recovery gates ==")
    from recovery import (
        build_recovery_plan,
        cross_lang_query,
        pick_alternative_engines,
        baseline_cross_query,
    )

    # L3：空 tried 时不得选污染垂直源
    picks = pick_alternative_engines(
        tried=["imdb"],
        engines_fallback=["pypi", "npm", "jin10", "anysearch", "local_bing"],
    )
    c.check(
        "rec_l3_no_pollute_film",
        not any(e in _RECOVERY_POLLUTE for e in picks) and bool(picks),
        detail=str(picks),
    )
    picks2 = pick_alternative_engines(
        tried=["thesportsdb"],
        engines_fallback=["crates", "pypi", "wikipedia", "anysearch"],
    )
    c.check(
        "rec_l3_no_pollute_sports",
        not any(e in _RECOVERY_POLLUTE for e in picks2) and bool(picks2),
        detail=str(picks2),
    )

    # 同族可保留（knowledge）
    picks3 = pick_alternative_engines(
        tried=["wikipedia"],
        engines_fallback=["wikidata", "pypi"],
        max_n=2,
    )
    c.check(
        "rec_l3_same_family_ok",
        "pypi" not in picks3,
        detail=str(picks3),
    )

    # 跨语言
    for q, expect_cross in [
        ("アニメ おすすめ", True),
        ("한국 영화", True),
        ("如何写爬虫", True),
        ("Как дела", True),
        ("hello world", False),
        ("React vs Vue", False),
    ]:
        cq, ce = cross_lang_query(q)
        has = bool(ce)
        c.check(
            f"rec_cross:{q[:12]}",
            has is expect_cross,
            detail=f"engines={ce!r}",
        )

    # plan 含 L4 cross_lang（非 fast）
    plan = build_recovery_plan(
        "アニメ おすすめ", ["anysearch"], ["bocha", "wikipedia"], mode="auto",
    )
    strategies = [s.strategy for s in plan]
    c.check("rec_plan_cross_lang", "cross_lang" in strategies, detail=str(strategies))

    plan_fast = build_recovery_plan(
        "アニメ おすすめ", ["anysearch"], ["bocha"], mode="fast",
    )
    c.check(
        "rec_fast_no_cross",
        "cross_lang" not in [s.strategy for s in plan_fast],
        detail=str([s.strategy for s in plan_fast]),
    )

    # baseline zh
    cq, ce = baseline_cross_query("hello world", primary_lang="en", prefer=["zh", "en"])
    c.check("rec_baseline_zh", bool(ce), detail=str(ce))
    cq2, ce2 = baseline_cross_query("hello world", primary_lang="en", prefer=["en"])
    c.check("rec_baseline_skip", not ce2, detail=str(ce2))


# ═══════════════════════════════════════════════════════════════════════════
# OFFLINE D — 引擎族 + 注册完整性
# ═══════════════════════════════════════════════════════════════════════════

FAMILY_EXPECT: dict[str, str] = {
    "anysearch": "web_general",
    "bocha": "web_general",
    "local_bing": "web_general",
    "arxiv": "academic",
    "github": "code",
    "pypi": "code",
    "sina_quote": "finance_market",
    "finviz": "finance_market",
    "fred": "finance_macro",
    "worldbank": "finance_macro",
    "jin10": "news_flash",
    "zhihu": "social",
    "wikipedia": "knowledge",
    "wikidata": "knowledge",
    "baidu_baike": "knowledge",
    "pubchem": "science_chem",
    "imdb": "media_book",
    "itunes": "media_book",
    "thesportsdb": "sports",
    "local_openstreetmap": "science_geo",
}


def run_offline_families(c: Checker) -> None:
    print("\n== offline: engine families ==")
    from engine_families import family_of, DEFAULT_FAMILY

    for eng, expect in FAMILY_EXPECT.items():
        got = family_of(eng)
        soft = eng == "local_openstreetmap" and got != expect
        c.check(
            f"family:{eng}",
            got == expect,
            detail=f"got={got}",
            soft=soft or (got == DEFAULT_FAMILY and expect != DEFAULT_FAMILY),
        )

    # 关键垂直源不得被误标 web_general（污染风险）
    for eng in ("pypi", "npm", "jin10", "sina_quote", "imdb", "thesportsdb"):
        got = family_of(eng)
        c.check(
            f"family_not_general:{eng}",
            got != "web_general",
            detail=f"got={got}",
        )


def run_offline_lang_pref(c: Checker) -> None:
    print("\n== offline: lang_pref ==")
    from lang_pref import (
        prefer_langs, system_lang, dominant_habit,
        effective_engine_lang, record_query_lang, reset_habit_for_tests,
        BASELINE_LANGS,
    )

    reset_habit_for_tests()
    try:
        c.check("pref_sys_zh", system_lang("zh_CN.UTF-8") == "zh")
        c.check("pref_sys_ja", system_lang("ja_JP") == "ja")
        c.check("pref_sys_ko", system_lang("ko_KR") == "ko")
        c.check("pref_sys_ru", system_lang("ru_RU") == "cyrillic")

        prefs = prefer_langs(
            query_lang="ko", system="zh", habit="ja", use_live_habit=False,
        )
        c.check("pref_query_first", prefs[0] == "ko", detail=str(prefs))
        for b in BASELINE_LANGS:
            c.check(f"pref_baseline:{b}", b in prefs, detail=str(prefs))

        c.check(
            "pref_effective_strong",
            effective_engine_lang("ja", ["zh", "en"]) == "ja",
        )
        c.check(
            "pref_effective_weak",
            effective_engine_lang("mixed", ["ja", "zh", "en"]) == "ja",
        )

        for _ in range(6):
            record_query_lang("ja")
        c.check("pref_habit", dominant_habit() == "ja")
    finally:
        reset_habit_for_tests()


def run_offline_reachability(c: Checker) -> None:
    """可达性门：enabled 引擎必须被 ≥1 分发路径可达，防死源复发。

    五条分发路径：域 combo / 保底清单 / 选题 profiles / 研究 boosts /
    TF-IDF 语义画像（documents 非空）。local_* 视为经 local_search 展开
    可达。不可达引擎逐个 WARN（soft）——anysearch/zhihu_global/uapi 三次
    死源事故的系统性防线。2026-09-07。

    explicit_only 例外（2026-09-12）：引擎声明 `explicit_only: true` 表示
    「设计上不进自动路由，按 --engine / 交接提示显式调用」——例如需密钥的
    付费源、或输入形态特殊（单条推文 URL 走 known-url 交接）的抽取型引擎。
    这类不算死源，但要求声明显式存在：漏声明会继续 WARN，防止把「忘了接线」
    伪装成「有意显式」。声明项单独列出，保持可见。
    """
    print("\n== offline: engine reachability gate ==")
    import json as _json
    from pathlib import Path as _Path
    from config import load_config, get_engines, get_domains
    from topic_research_profiles import list_profiles
    from engine_policy import GENERAL_FREE_FALLBACK
    import research as _research

    specs = get_engines(load_config(), routable_only=False)
    domains = get_domains(load_config())

    reachable: set[str] = set()
    for d in domains:
        reachable |= set(d.get("engines_combo") or [])
    reachable |= set(GENERAL_FREE_FALLBACK)
    for p in list_profiles():
        reachable |= set(p.get("engines") or [])
    reachable |= (set(_research._RESEARCH_EN_BOOSTS)
                  | set(_research._RESEARCH_ACADEMIC_BOOSTS)
                  | set(_research._RESEARCH_JA_KO_BOOSTS))
    reachable |= {"local_search"}

    try:
        dp = _json.loads(
            (_Path(__file__).resolve().parent.parent
             / "backends" / "domain_profiles.json").read_text(encoding="utf-8"))
        reachable |= {k for k, v in dp.items()
                      if isinstance(v, dict) and (v.get("documents") or [])}
    except Exception:
        pass

    declared_explicit = {
        n for n, s in specs.items()
        if isinstance(s, dict) and s.get("enabled", True) and s.get("explicit_only")}
    # 声明与实际必须一致：声明了 explicit_only 却又进了自动分发路径，
    # 说明两边有一边说谎（声明忘了撤，或接线时没看声明）。
    contradictory = sorted(declared_explicit & reachable)
    for n in contradictory:
        c.check(f"reach_explicit_conflict:{n}", False, soft=True,
                detail="声明 explicit_only 却进了自动分发路径——声明与实际不一致")
    explicit_only = sorted(declared_explicit - reachable)
    unreachable = sorted(
        n for n, s in specs.items()
        if isinstance(s, dict) and s.get("enabled", True)
        and n not in reachable and not n.startswith("local_")
        and not s.get("explicit_only"))
    for n in unreachable:
        c.check(f"reach:{n}", False,
                detail="五条分发路径均不可达（死源）——接线或声明 explicit_only",
                soft=True)
    c.check("reach:summary", len(unreachable) == 0,
            detail=(f"死源 {len(unreachable)} 个: {', '.join(unreachable) or '无'}"
                    f"；声明 explicit_only {len(explicit_only)} 个: "
                    f"{', '.join(explicit_only) or '无'}"),
            soft=True)


def run_offline(c: Checker) -> None:
    run_offline_lang(c)
    run_offline_route(c)
    run_offline_recovery(c)
    run_offline_families(c)
    run_offline_lang_pref(c)
    run_offline_reachability(c)


# ═══════════════════════════════════════════════════════════════════════════
# LIVE — 多语 × 场景 E2E
# ═══════════════════════════════════════════════════════════════════════════

# gold: 标题/摘要命中任一即可；pollute: source/title 命中则污染
LIVE_CASES: list[dict[str, Any]] = [
    # film
    {"id": "L_en_film", "q": "Inception movie director", "lang": "en",
     "domain": "film_search", "primary": "imdb", "scenario": "film",
     "gold": ["nolan", "inception", "christopher"], "pollute": ["pypi", "npm", "jin10"]},
    {"id": "L_zh_film", "q": "肖申克的救赎 主演", "lang": "zh",
     "domain": "film_search", "primary": "imdb", "scenario": "film",
     "gold": ["shawshank", "redemption", "肖申克", "freeman", "robbins"],
     "pollute": ["pypi", "npm"]},
    # sports
    {"id": "L_en_sports", "q": "Cristiano Ronaldo club", "lang": "en",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports",
     "gold": ["ronaldo", "cristiano", "al nassr", "portugal"],
     "pollute": ["pypi", "npm", "jin10"]},
    {"id": "L_zh_sports", "q": "库里 球队", "lang": "zh",
     "domain": "sports_search", "primary": "thesportsdb", "scenario": "sports",
     "gold": ["curry", "stephen", "warrior", "库里"],
     "pollute": ["pypi", "npm"]},
    # media
    {"id": "L_zh_media", "q": "周杰伦 专辑", "lang": "zh",
     "domain": "media_search", "primary": "itunes", "scenario": "media",
     # musicbrainz 数据源返回繁体（周杰倫），金标含简繁两形（数据源事实，非引擎缺陷）
     "gold": ["jay", "chou", "周杰伦", "周杰倫", "album", "叶惠美", "范特西"],
     "pollute": ["pypi", "crates"]},
    {"id": "L_en_media", "q": "Taylor Swift album", "lang": "en",
     "domain": "media_search", "primary": "itunes", "scenario": "media",
     "gold": ["taylor", "swift", "album", "midnights", "1989"],
     "pollute": ["pypi"]},
    # org / geo
    {"id": "L_en_org", "q": "NASA founding year", "lang": "en",
     "domain": "org_entity", "primary": "wikidata", "scenario": "org",
     "gold": ["nasa", "1958", "space", "aeronautics"],
     "pollute": ["pypi", "npm"]},
    {"id": "L_en_geo", "q": "where is Eiffel Tower", "lang": "en",
     "domain": "geo_places", "primary": "local_openstreetmap", "scenario": "geo",
     "gold": ["eiffel", "paris", "france", "tower"],
     "pollute": ["pypi"]},
    # finance
    {"id": "L_zh_stock", "q": "贵州茅台股价", "lang": "zh",
     "domain": "stock_query", "primary": "sina_quote", "scenario": "finance",
     "gold": ["茅台", "600519", "股价", "行情", "元"],
     "pollute": ["pypi", "imdb"]},
    # chem / rfc
    {"id": "L_zh_chem", "q": "阿司匹林 分子式", "lang": "zh",
     "domain": "chem_search", "primary": "pubchem", "scenario": "chem",
     "gold": ["aspirin", "c9h8o4", "阿司匹林", "acetylsalicylic", "pubchem"],
     "pollute": ["jin10", "imdb"]},
    # multi-lang general（允许 recovery / 通用源，关注污染与语言）
    {"id": "L_ja_gen", "q": "アニメ おすすめ", "lang": "ja",
     "domain": None, "scenario": "general",
     "gold": ["アニメ", "anime", "おすすめ", "漫画", "作品"],
     "pollute": ["pypi", "npm", "sina_quote", "jin10"],
     "timeout": 25},
    {"id": "L_ko_gen", "q": "한국 영화 추천", "lang": "ko",
     "domain": None, "scenario": "general",
     "gold": ["영화", "movie", "korea", "한국", "film"],
     "pollute": ["pypi", "npm", "sina_quote"],
     "timeout": 25},
    {"id": "L_cyr_gen", "q": "Как написать скрипт python", "lang": "cyrillic",
     "domain": None, "scenario": "general",
     "gold": ["python", "скрипт", "script", "программ"],
     "pollute": ["sina_quote", "jin10", "imdb"],
     "timeout": 25},
]


def _blob_from_results(results: list[dict], used: list, eng: str) -> str:
    parts: list[str] = []
    for x in results:
        for k in ("title", "snippet", "content", "url", "source", "engine"):
            v = x.get(k)
            if v:
                parts.append(str(v))
    parts.extend(str(u) for u in used)
    parts.append(eng or "")
    return " ".join(parts).lower()


def run_live(c: Checker) -> None:
    print("\n== live: multilingual × scenario E2E ==")
    from search import super_search

    live_rows: list[dict[str, Any]] = []
    for case in LIVE_CASES:
        q = case["q"]
        timeout = int(case.get("timeout") or 20)
        t0 = time.perf_counter()
        try:
            r = super_search(
                q, n=5, mode="auto", depth="fast",
                skip_cache=True, timeout=timeout, envelope=False,
            )
        except Exception as exc:  # noqa: BLE001
            ms = int((time.perf_counter() - t0) * 1000)
            c.check(f"live:{case['id']}", False, detail=f"exc={exc} ms={ms}")
            live_rows.append({"id": case["id"], "q": q, "error": str(exc), "ms": ms})
            continue

        ms = int((time.perf_counter() - t0) * 1000)
        results = r.get("results") or []
        used = list(r.get("engines_used") or [])
        eng = (r.get("engine") or "").lower()
        domain_got = r.get("domain")
        combo = r.get("engines_combo") or r.get("engines") or []
        recovery = r.get("recovery")
        blob = _blob_from_results(results, used, eng)

        dom_ok = case.get("domain") is None or domain_got == case["domain"]
        primary = case.get("primary")
        eng_ok = True
        if primary:
            eng_ok = (
                eng == primary
                or primary in used
                or primary in (combo or [])
                or any(
                    primary in str(x.get("source") or x.get("engine") or "").lower()
                    for x in results
                )
            )

        gold = [g.lower() for g in case.get("gold") or []]
        gold_matched = next((g for g in gold if g in blob), "")
        hit = bool(gold_matched) if gold else len(results) >= 1

        pollute_keys = [p.lower() for p in case.get("pollute") or []]
        # 污染：结果 source/engine 命中垂直脏源
        pollute_hit: list[str] = []
        for x in results:
            src = f"{x.get('source') or ''} {x.get('engine') or ''}".lower()
            for p in pollute_keys:
                if p in src and p not in pollute_hit:
                    pollute_hit.append(p)
        for u in used:
            ul = str(u).lower()
            for p in pollute_keys:
                if p == ul and p not in pollute_hit:
                    # 若仅作为 recovery 未进 results 可不算污染；仅 results 源算
                    pass

        # 严格：top results 的 source 不得为 pollute
        pollute_ok = not pollute_hit

        soft = case.get("scenario") == "general" and case.get("lang") in (
            "ja", "ko", "cyrillic",
        )
        overall = dom_ok and eng_ok and hit and pollute_ok and len(results) >= 1
        # general 多语：domain 不强求；eng_ok 默认 true
        if case.get("domain") is None:
            overall = hit and pollute_ok and len(results) >= 1

        detail = (
            f"dom={domain_got} eng={eng} n={len(results)} ms={ms} "
            f"hit={hit} gold={gold_matched!r} pollute={pollute_hit} "
            f"used={used} rec={recovery}"
        )
        c.check(
            f"live:{case['id']}",
            overall,
            detail=detail,
            soft=soft and not pollute_ok is False and not overall and hit is False,
            meta={
                "id": case["id"],
                "q": q,
                "scenario": case.get("scenario"),
                "lang": case.get("lang"),
                "dom_ok": dom_ok,
                "eng_ok": eng_ok,
                "hit": hit,
                "pollute": pollute_hit,
                "domain": domain_got,
                "engine": eng,
                "n": len(results),
                "ms": ms,
                "gold_matched": gold_matched,
                "top1": (results[0].get("title") if results else None),
                "sources": [
                    str(x.get("source") or x.get("engine") or "") for x in results[:5]
                ],
                "recovery": recovery,
            },
        )
        live_rows.append({
            "id": case["id"],
            "q": q,
            "dom_ok": dom_ok,
            "eng_ok": eng_ok,
            "hit": hit,
            "pollute": pollute_hit,
            "domain": domain_got,
            "engine": eng,
            "n": len(results),
            "ms": ms,
            "gold_matched": gold_matched,
            "top1": (results[0].get("title") if results else None),
            "recovery": recovery,
        })

    # live 汇总
    if live_rows:
        n = len(live_rows)
        with_dom = [r for r in live_rows if "dom_ok" in r]
        if with_dom:
            print(
                f"\n  live summary: n={n} "
                f"domain_acc={sum(1 for r in with_dom if r.get('dom_ok'))/len(with_dom):.0%} "
                f"hit={sum(1 for r in with_dom if r.get('hit'))/len(with_dom):.0%} "
                f"pollute_n={sum(1 for r in with_dom if r.get('pollute'))}"
            )
    c._live_rows = live_rows  # type: ignore[attr-defined]


def _summarize(c: Checker) -> dict[str, Any]:
    by_status: dict[str, int] = {"PASS": 0, "FAIL": 0, "WARN": 0}
    by_section: dict[str, dict[str, int]] = {}
    for row in c.rows:
        st = row["status"]
        by_status[st] = by_status.get(st, 0) + 1
        sec = row["name"].split(":")[0]
        by_section.setdefault(sec, {"PASS": 0, "FAIL": 0, "WARN": 0})
        by_section[sec][st] = by_section[sec].get(st, 0) + 1

    route_meta = [
        r.get("meta") for r in c.rows
        if r["name"].startswith("route:") and r.get("meta")
    ]
    scenario_stats: dict[str, dict[str, int]] = {}
    lang_stats: dict[str, dict[str, int]] = {}
    for m in route_meta:
        if not m:
            continue
        sc = m.get("scenario") or "other"
        lg = m.get("lang") or "other"
        # find status from parent
        scenario_stats.setdefault(sc, {"ok": 0, "n": 0})
        lang_stats.setdefault(lg, {"ok": 0, "n": 0})

    for r in c.rows:
        if not r["name"].startswith("route:"):
            continue
        m = r.get("meta") or {}
        sc = m.get("scenario") or "other"
        lg = m.get("lang") or "other"
        scenario_stats.setdefault(sc, {"ok": 0, "n": 0})
        lang_stats.setdefault(lg, {"ok": 0, "n": 0})
        scenario_stats[sc]["n"] += 1
        lang_stats[lg]["n"] += 1
        if r["status"] == "PASS":
            scenario_stats[sc]["ok"] += 1
            lang_stats[lg]["ok"] += 1

    live_rows = getattr(c, "_live_rows", [])
    live_summary = None
    if live_rows:
        valid = [r for r in live_rows if "dom_ok" in r]
        n = len(valid) or 1
        live_summary = {
            "n": len(valid),
            "domain_acc": sum(1 for r in valid if r.get("dom_ok")) / n,
            "engine_acc": sum(1 for r in valid if r.get("eng_ok")) / n,
            "selection_hit": sum(1 for r in valid if r.get("hit")) / n,
            "domain_and_hit": sum(
                1 for r in valid if r.get("dom_ok") and r.get("hit")
            ) / n,
            "pollute_n": sum(1 for r in valid if r.get("pollute")),
        }

    return {
        "ok": c.ok,
        "fail": c.fail,
        "warn": c.warn,
        "by_status": by_status,
        "by_section": by_section,
        "route_by_scenario": scenario_stats,
        "route_by_lang": lang_stats,
        "live_summary": live_summary,
        "live_rows": live_rows,
    }


def _reexec_with_yaml() -> bool:
    """缺 PyYAML 时改用带依赖的解释器重入；exec 成功则不返回。

    why：config 在无 yaml 时静默回落内置默认引擎集，route 段整片假红
    （2026-10-25 实测：同机无 yaml 的解释器跑出 94 FAIL、带 yaml 的全绿）。
    bin/argo 启动器的 _pick_python 会自动挑解释器，但分享场景里用户会直接
    `python3 scripts/matrix_search_eval.py`——解释器版本不该卡住人。候选序：
    ARGO_PYTHON（与启动器同一权威覆盖）→ PATH 上的 python3.14/3.13/3.15/
    3.12 → python3；每个候选真跑一次 `import yaml` 验证（探测约 100ms，
    评测不是热路径）。已重入过仍缺（_ARGO_MATRIX_REEXEC 标记）则放弃，
    避免套娃。
    """
    import os
    import shutil
    import subprocess

    if os.environ.get("_ARGO_MATRIX_REEXEC") == "1":
        return False
    cands = []
    env_py = os.environ.get("ARGO_PYTHON", "").strip()
    if env_py:
        cands.append(env_py)
    cands += [shutil.which("python3.%d" % m) or "" for m in (14, 13, 15, 12)]
    cands.append(shutil.which("python3") or "")
    for cand in cands:
        if not cand:
            continue
        if os.path.realpath(cand) == os.path.realpath(sys.executable):
            continue  # 当前解释器已证明缺 yaml，跳过
        try:
            probe = subprocess.run([cand, "-c", "import yaml"],
                                   capture_output=True, timeout=30)
        except Exception:
            continue
        if probe.returncode == 0:
            os.environ["_ARGO_MATRIX_REEXEC"] = "1"
            print("[matrix] 当前解释器缺 PyYAML，改用 %s 重入" % cand,
                  file=sys.stderr)
            os.execv(cand, [cand, sys.argv[0], *sys.argv[1:]])
    return False


def main() -> int:
    # PyYAML 缺失时 config 会**静默**回落到内置默认引擎集（垂直域引擎全不在），
    # route 段整片假红（2026-10-25 实测：无 yaml 的解释器跑出 94 FAIL，带 yaml
    # 重跑 197 全绿——错误根因是解释器不是路由）。先自愈换解释器重入
    # （3.15/3.14 等版本差异不再卡用户），换无可换才硬失败退出。
    try:
        import yaml  # noqa: F401
    except ImportError as exc:
        _reexec_with_yaml()
        print(
            f"[matrix] PyYAML 不可用（{exc}）且候选解释器（ARGO_PYTHON 与 PATH "
            "上的 python3.x）均缺：config 将静默降级到内置默认引擎集，route 段"
            "会整片假红。请 pip install pyyaml，或设 ARGO_PYTHON 指向带依赖的"
            "解释器。",
            file=sys.stderr,
        )
        return 2
    ap = argparse.ArgumentParser(description="多语言×引擎×场景 矩阵测试")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument(
        "--report",
        default=str(_ROOT / "tests" / "matrix_search_report.json"),
        help="JSON 报告路径",
    )
    ap.add_argument("--json", action="store_true", help="stdout 打印摘要 JSON")
    args = ap.parse_args()
    if not (args.offline or args.live or args.all):
        args.offline = True

    c = Checker()
    if args.offline or args.all:
        run_offline(c)
    if args.live or args.all:
        run_live(c)

    summary = _summarize(c)
    print(f"\n== summary: {c.ok} PASS / {c.fail} FAIL / {c.warn} WARN ==")
    if summary.get("route_by_scenario"):
        print("  route by scenario:")
        for sc, v in sorted(summary["route_by_scenario"].items()):
            print(f"    {sc:12} {v['ok']}/{v['n']}")
    if summary.get("route_by_lang"):
        print("  route by lang:")
        for lg, v in sorted(summary["route_by_lang"].items()):
            print(f"    {lg:12} {v['ok']}/{v['n']}")
    if summary.get("live_summary"):
        ls = summary["live_summary"]
        print(
            f"  live: n={ls['n']} domain_acc={ls['domain_acc']:.0%} "
            f"hit={ls['selection_hit']:.0%} pollute_n={ls['pollute_n']}"
        )

    report = {
        "summary": summary,
        "rows": c.rows,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(dumps_pretty(report), encoding="utf-8")
    print(f"  report → {report_path}")

    if args.json:
        print(dumps(summary))
    return 1 if c.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
