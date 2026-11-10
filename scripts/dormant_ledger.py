#!/usr/bin/env python3
"""dormant_ledger — 位置性休眠台账真源与对账（从 test_new_source_reachability 迁入）。

台账原先是测试文件里的私有字典：判定逻辑只在全量 pytest 红，接引擎/调域
combo 的人不跑全量就看不见。本模块把它迁回生产侧，作为唯一真源：

- DORMANT_ALLOWLIST：engine → 为什么允许它位置性休眠（一句话，供下一批
  接线决策用）。接线后休眠解除必须撤账，撤账保留注释记录来龙去脉。
- dormant_engines()：位置性休眠盘点——引擎在它出现的**每一个**域里都落
  预算外（位次 > budget，且非 primary、不在加槽表、非语言 must_keep 族）。
- check_dormant_ledger()：双向对账（新增未登记 + 台账过期），被
  sync_backends.check() 与 test_new_source_reachability 双消费——测试不再
  自带第二套判定。

台账条目与盘点口径的背景细节（语言绑定源、加槽 cap、域预算）见各条目注释。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from config import load_config  # noqa: E402

DORMANT_ALLOWLIST: dict[str, str] = {
    # engine → 为什么允许它位置性休眠（一句话，供下一批接线决策用）
    # 2026-09-21：world_news 域接入 14 个本地语言新闻源。该族是**语言绑定**
    # 的——每个源服务一种语言，运行时由 route._filter_lang_bound_family 按
    # 查询语言把不匹配的移出预算窗口，再由 _lang_must_keep 把匹配的那个保进
    # 窗口（实测：ko → [anysearch, yna]、es → [anysearch, elpais]、ar →
    # [anysearch, aljazeera]）。本检查只看 combo 位次、不含语言维度，故除
    # 前两位（yna@2 / tass@3，位次在 budget 内）外一律判为位置性休眠——
    # 这是检查口径与语言绑定机制的不匹配，不是死源。
    "aljazeera": "world_news 位次 4：语言绑定源，按查询语言选入预算窗口（静态检查不含语言维度）",
    "antara": "world_news 位次 12：语言绑定源，按查询语言选入预算窗口",
    "ct24": "world_news 位次 15：语言绑定源，按查询语言选入预算窗口",
    "elpais": "world_news 位次 5：语言绑定源，按查询语言选入预算窗口",
    "faz": "world_news 位次 8：语言绑定源，按查询语言选入预算窗口",
    "folha": "world_news 位次 6：语言绑定源，按查询语言选入预算窗口",
    "hurriyet": "world_news 位次 13：语言绑定源，按查询语言选入预算窗口",
    "lefigaro": "world_news 位次 7：语言绑定源，按查询语言选入预算窗口",
    "matichon": "world_news 位次 10：语言绑定源，按查询语言选入预算窗口",
    "nhk": "world_news 位次 9：语言绑定源，按查询语言选入预算窗口",
    "vnpress": "world_news 位次 11：语言绑定源，按查询语言选入预算窗口",
    "ynet": "world_news 位次 14：语言绑定源，按查询语言选入预算窗口",
    "obis": "待接线：species_search 位次 4（iplant 接入后顺延；既有债务，非本次引入）",
    "cleveland": "art_museum 同质重复备份（与 artic 同能力，故意不加槽）",
    "cn_ai_news": "待接线：chinese_tech_deep 位次 4",
    "frankfurter": "fx_query 位次 4：2026-10-21 接入 cfets 后顺延；ECB 参考价与 cfets 口径重叠，deep/恢复链可达",
    # 2026-10-06：macro_data 位次 6（fast 档 budget=2，位次 5+ 不参与自动
    # 路由）。汇率主路径由 fx_rate（位次 5，同窗口外但历史更久）承担；
    # frankfurter 的价值口径是 ECB 官方参考价，走 macro_data 零结果恢复链
    # （test_domain_zero_result_recovery 锁定 fb[:4] 含它）与 deep/research。
    #
    # 2026-10-16：fx_rate / frankfurter 从本台账**撤账**——新建 fx_query 域
    # （汇率先行域，位于 macro_data 之前）把 fx_rate 提到位次 1、frankfurter
    # 提到位次 3，auto/balanced 预算窗（budget=3）内可达，「人民币汇率/中间价」
    # 类查询不再需要经 macro_data 的恢复链兜底。macro_data 两个位次上的休眠
    # 由 fx_query 的接线覆盖，故两条登记一并删除。
    # 2026-09-16：academic 域接入 openreview/biorxiv 后，原第 3 位的 crossref
    # 被推到第 5 位因而休眠。这与 dblp/europepmc 是同一笔账：academic 的
    # combo 有 8 个源，而 fast 档 budget=2（must_keep 腾位后实际只留 2 个），
    # 排在 3 位之后一律不参与自动路由，只在 deep/research 不截断时才跑。
    # 若要恢复 crossref 的自动可见性，需给它加槽（_VERTICAL_NEW_SOURCE）或
    # 调高 academic 域预算——两者都会挤掉 openreview/biorxiv，属权衡而非纯收益。
    "crossref": "待接线：academic 位次 5（fast 档 budget=2，位次 3+ 不参与自动路由）",
    # 2026-10-14（7ca2f22）：osv/cisa_kev 从 security_search combo 移出并改为
    # explicit_only（本地插件按需提供），crt_sh 回到位次 3（进 auto/balanced 预算窗），
    # 故 cisa_kev 与 crt_sh 均从休眠台账撤账。
    "dblp": "待接线：academic 位次 7",
    "docker_hub": "待接线：package_search 位次 4",
    "europepmc": "待接线：academic 位次 6；tech_deep 位次 5",
    "eurostat": "待接线：macro_data 位次 4",
    # 2026-09-20：weather_query 域快源前置——weather_cn（国内城市实况两步
    # 直连 0.46s）提到位次 3，weather（CLI 子进程 wttr/open-meteo 双源
    # 1.5-4.3s）顺延到 4。fast 档 budget=2 下 weather 不参与自动路由；
    # auto/balanced（budget 3，含 TF-IDF 注入的 open_meteo）仍可达，海外
    # 城市 wttr 语义无替代源，保留在本域 + fallback 链/显式 --engine 触达。
    "weather": "weather_query 位次 4（快源前置让位 weather_cn）：海外城市仍主用，fast 档靠 fallback 链兜底",
    "fxtwitter": "social 域位次 6：社交引擎另有专用通道，待核实是否真休眠",
    "keenable": "待接线：english_tech 位次 4",
    "jikan": "anime_encyclopedia 位次 4：上游 /anime?q= 检索端点持续 504，"
             "spec enabled=false 暂停启用；恢复后改回 true 并接入加槽表",
    # 2026-09-19：local_pubmed 从本台账**撤销**——它此前不只是「位置性休眠」，
    # 而是根本不工作（请求被注入 format=json 而 NCBI 返回 400；output_map 又把
    # title/url/summary 全指向 pmid，且 esearch 的 idlist 是字符串数组、解析器
    # 对非 dict 条目直接跳过 → 实测稳定 0 条）。本次改为两段式 builder 并接进
    # academic 加槽表后已能实际产出结果，故不再登记休眠。
    # 详见 engines_builders_batch9._build_pubmed_engine 与
    # tests/test_pubmed_local_0919.py。
    "nasa_images": "图源（astro_space@4 / earth_science@5）：另有图片意图通道，待核实",
    # 2026-11-08（60f168e）图像准入后位次性休眠（60f168e 落地时漏登，本次收口补账）：
    # vam@art_museum@5、wallhaven@image_search@4，均超 fast/auto 预算窗，
    # 与 nasa_images 同语义：另有图片意图通道触达
    "vam": "图源（art_museum@5）：另有图片意图通道，待核实",
    "wallhaven": "图源（image_search@4）：另有图片意图通道，待核实",
    "parallel": "待接线：chinese_tech_deep 位次 5",
    "reddit": "social 域位次 7：社交引擎另有专用通道，待核实是否真休眠",
    "twitter": "social 域位次 5：社交引擎另有专用通道，待核实是否真休眠",
    "weibo": "social 域位次 9：社交引擎另有专用通道，待核实是否真休眠",
    "xiaohongshu": "social 域位次 8：社交引擎另有专用通道，待核实是否真休眠",
    "you": "待接线：news_realtime 位次 6",
    "zhihu_hot_app": "待接线：hot_trending 位次 6",
    # 2026-09-28（9044621）：360/神马新接入 chinese_general 尾部（位次 8/9，
    # 预算外）。挂观察期：首周熔断/配额/反爬数据达线后，再评估进
    # route._VERTICAL_NEW_SOURCE 加槽通电，或确认与既有 CN 源同质后裁撤。
    # 2026-11-10（d00df55）dbpedia/mojeek 接入后位次性休眠，补登记：
    # dbpedia@entity_search@5（budget=3，前四位 baidu_baike/wikidata/wikipedia/
    # zh_wikipedia 已覆盖中文百科与维基数据，DBpedia 的 SPARQL 结构化问答是
    # 差异化能力，deep/研究档与恢复链可达）；mojeek@semantic_discovery@8
    # （combo 15 源，budget=2~3，位次 8 远超窗口；独立索引能力在 exa/anysearch
    # 覆盖不足时由 deep/research 触达）。
    "dbpedia": "entity_search 位次 5：结构化知识图谱源，fast/auto 预算窗外，deep/research 可达",
    # 2026-11-10：core 从加槽表移出（四源争三个槽位，取舍见 route_policy 注释），
    # 位次 8 落在 auto/balanced 预算窗（base 3 + cap 4 = 7）之外。它与
    # local_pubmed 同属「开放获取全文」层，能力重叠，故让位给独特性更高的
    # cnii（本域唯一亚洲学术源）；deep/research 与恢复链仍可达。
    "core": "academic 位次 8：让位 cnii（加槽 cap 取舍），与 local_pubmed 能力重叠，deep/research 可达",
    # mojeek 原登记在此（semantic_discovery 位次 8），2026-11-10 前移到第 3 位
    # 后落进基础预算，已撤账——保留注释记录这笔账的来龙去脉。
    "so": "chinese_general 位次 8：新增观察期（9044621），待可用性数据",
    "shenma": "chinese_general 位次 9：新增观察期（9044621），待可用性数据",
    # 2026-10-05：批量登记 33 个位置性休眠引擎。共性根因——各域 combo
    # 声明的源数远超 fast/auto 档预算窗口（budget=2~3），位次 3+ 的源不参与
    # 自动路由，只在 deep/research 不截断时才跑。逐域登记如下：
    # academic 域（combo 15 源，budget=2~3，位次 12+ 一律休眠）
    # cnii 原登记在此（academic 位次 15），2026-11-10 前移到第 7 位落进加槽
    # 射程（base 3 + cap 4 = 7），已撤账——保留注释记录这笔账的来龙去脉。
    "doaj": "academic 位次 12（combo 15 源，budget=2~3，位次 12+ 不参与自动路由）",
    "figshare": "academic 位次 13（combo 15 源，budget=2~3，位次 12+ 不参与自动路由）",
    "hal": "academic 位次 14（combo 15 源，budget=2~3，位次 12+ 不参与自动路由）",
    # semantic_discovery 域（combo 15 源，budget=3，位次 4+ 一律休眠）
    # 2026-11-10：mwmbl/mojeek 前移到 2/3 位后，其余源位次全部顺延。本检查
    # 只看基础预算（不含加槽），故位次 4+ 全休眠；生产路径的加槽额度（extra=4）
    # 覆盖到位次 7，与本检查口径不同——这正是既有口径，不因本次前移改变。
    "lieu": "semantic_discovery 位次 5（budget=3，位次 4+ 不参与自动路由）",
    "marginalia": "semantic_discovery 位次 6（budget=3，位次 4+ 不参与自动路由）",
    "searchmysite": "semantic_discovery 位次 7（budget=3，位次 4+ 不参与自动路由）",
    "wiby": "semantic_discovery 位次 8（budget=3，位次 4+ 不参与自动路由）",
    "uapi": "semantic_discovery 位次 9（budget=3，位次 4+ 不参与自动路由）",
    # parallel_free 已撤账（2026-10-31）：通用池选源改造后它进入 lead 组合参与
    # 自动路由（评测 fe03 实测它作为 general_search 主源出结果），不再是位置性休眠
    "tavily": "semantic_discovery 位次 11（budget=3+加槽4=7，位次 8+ 不参与自动路由）",
    "firecrawl": "semantic_discovery 位次 12（budget=3+加槽4=7，位次 8+ 不参与自动路由）",
    "brave": "semantic_discovery 位次 13（budget=3+加槽4=7，位次 8+ 不参与自动路由）",
    "felo": "semantic_discovery 位次 14（budget=3+加槽4=7，位次 8+ 不参与自动路由）",
    "searxng": "semantic_discovery 位次 15（budget=3+加槽4=7，位次 8+ 不参与自动路由）",
    # social 域（社交引擎另有专用通道）
    "hatena_bookmark": "social 域位次 10：社交引擎另有专用通道，待核实是否真休眠",
    "qiita": "social 域位次 11：社交引擎另有专用通道，待核实是否真休眠",
    # 其它域（均为位次超预算窗口）
    "devto": "cn_tech_community 位次 5（budget=2~3，位次 4+ 不参与自动路由）",
    "dnb": "book_search 位次 7（budget=2~3，位次 4+ 不参与自动路由）",
    "local_goodreads": "book_search 位次 9（budget=2~3，位次 4+ 不参与自动路由）",
    "ndl": "book_search 位次 8（budget=2~3，位次 4+ 不参与自动路由）",
    "electricity_maps": "energy_grid 位次 4（budget=2~3，位次 4+ 不参与自动路由）",
    "eu_opendata": "law_text 位次 5（budget=2~3，位次 4+ 不参与自动路由）",
    "fr_opendata": "law_text 位次 6（budget=2~3，位次 4+ 不参与自动路由）",
    "gov_policy": "law_text 位次 7（budget=2~3，位次 4+ 不参与自动路由）",
    "europeana": "cn_encyclopedia 位次 4（budget=2~3，位次 4+ 不参与自动路由）",
    "local_imdb": "film_search 位次 7（budget=2~3，位次 4+ 不参与自动路由）",
    "metaso": "chinese_general 位次 10（budget=2~3，位次 4+ 不参与自动路由）",
    "open_meteo": "weather_query 位次 5（budget=2~3，位次 4+ 不参与自动路由）",
    "opensky": "transport_rt 位次 4（budget=2~3，位次 4+ 不参与自动路由）",
    "qq_music": "media_search 位次 6（budget=2~3，位次 4+ 不参与自动路由）",
    "realtime_index": "news_realtime 位次 7（budget=2~3，位次 4+ 不参与自动路由）",
    "seltz": "world_news 位次 16（语言绑定源，按查询语言选入预算窗口）",
    "tatoeba": "dictionary_search 位次 4（budget=2~3，位次 4+ 不参与自动路由）",
    "tencent_kline": "us_stock 位次 6（budget=2~3，位次 4+ 不参与自动路由）",
}

def dormant_engines() -> dict[str, list[tuple[str, int]]]:
    """位置性休眠盘点：引擎在它出现的**每一个**域里都落预算外。"""
    import route
    from engine_policy import combo_budget, get_engine_tier

    budget = combo_budget(mode="auto", depth="balanced", context="search") or 3
    slot = route._VERTICAL_NEW_SOURCE
    lang_keep = {e for v in route._LANG_PREFERRED_ENGINES.values() for e in v}
    appear: dict[str, list[tuple[str, int, bool, bool]]] = {}
    for d in (load_config().get("domains") or []):
        if not isinstance(d, dict):
            continue
        combo = list(d.get("engines_combo") or [])
        if not combo:
            continue
        name = d.get("name")
        for pos, e in enumerate(combo, 1):
            appear.setdefault(e, []).append(
                (name, pos, d.get("primary") == e, e in slot.get(name, ())))
    dormant: dict[str, list[tuple[str, int]]] = {}
    for e, infos in sorted(appear.items()):
        if get_engine_tier(e) == "research_only" or e in lang_keep:
            continue
        if any(pos <= budget or primary or in_slot
               for _, pos, primary, in_slot in infos):
            continue
        dormant[e] = [(dn, pos) for dn, pos, _, _ in infos]
    return dormant


def check_dormant_ledger() -> list[str]:
    """双向对账：新增休眠未登记、台账过期、条目缺原因。返回问题列表。"""
    issues: list[str] = []
    dormant = dormant_engines()
    unknown = sorted(set(dormant) - set(DORMANT_ALLOWLIST))
    stale = sorted(set(DORMANT_ALLOWLIST) - set(dormant))
    if unknown:
        issues.append(
            "新增位置性休眠引擎未登记台账：" + ", ".join(unknown) + "；明细："
            + "; ".join(f"{e}@{dormant[e]}" for e in unknown)
            + "。要么接进 route._VERTICAL_NEW_SOURCE，"
            "要么在 dormant_ledger.DORMANT_ALLOWLIST 写明原因")
    if stale:
        issues.append(
            "台账过期（这些引擎已不休眠，请从 DORMANT_ALLOWLIST 撤账）："
            + ", ".join(stale))
    for e, reason in DORMANT_ALLOWLIST.items():
        if not (reason and reason.strip()):
            issues.append(f"台账条目 {e} 缺原因说明")
    return issues
