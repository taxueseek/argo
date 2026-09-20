#!/usr/bin/env python3
"""test_new_source_reachability.py — 新专源「声明即可达」检查（2026-09-13 新增）。

## 守的是什么缺陷

批次九新增 25 个免密钥源，全部通过收录检查（health+quality pass）、
`--engine` 显式调用也能出结果，但**日常自动路由里永远轮不到它们**：

  - `combo_budget` 在 auto/balanced 只留 3 个引擎、fast 只留 2 个；
  - 新源声明在 combo 后排（位次 3~6），截断时被整段裁掉。

实测（加检查前的真实输出）：
  「海洋物种观测」→ combo=[gbif, wikipedia]（obis/worms 不在）
  「电视剧 元数据」→ combo=[imdb, douban_movie]（tvmaze 不在）
  「音乐 艺人」   → combo=[itunes, musicbrainz]（deezer/listenbrainz 不在）

这与批次九已修的「准入粘滞 bug」是**同一症状、不同成因**：引擎装好了、
没坏、也没被拉黑，只是路由根本没带上它。任一检查只看单层（health/quality/
blocked）时，这类「跨层结论不一致」不会被发现。

## 判据

对每个「声明了待接入新专源」的域，检查该源落在**本模式预算之内**
（即它确实会参与自动路由）。同时检查既有源不被挤掉——这是用 must_keep
顶位的反例（实测会把 douban_movie/musicbrainz 挤出，属净回归）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 域 → 该域声明待接入的新专源。
# **唯一来源**：直接读 route._VERTICAL_NEW_SOURCE，不再在测试里复制一份。
# 复制版是本检查最大的漏洞——测试锁的是自己抄的表，生产表改了它不知道。
# 旧版更用 `assert f'"{domain}"' in inspect.getsource(route)` 冒充一致性校验：
# 那只是子串匹配，把表整段删掉也能通过（域名字还在注释里）。
def _new_sources() -> dict[str, tuple[str, ...]]:
    import route
    return {k: tuple(v) for k, v in route._VERTICAL_NEW_SOURCE.items()}


# 模块级展开（parametrize 需在收集期取值），来源仍是 route 的来源
NEW_SOURCES: dict[str, tuple[str, ...]] = _new_sources()


def _cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def _domain_combo(name: str) -> list[str]:
    for d in _cfg().get("domains") or []:
        if isinstance(d, dict) and d.get("name") == name:
            return list(d.get("engines_combo") or [])
    return []


def _enabled(name: str) -> set[str]:
    """该域 combo 内 enabled 且非 research_only 的引擎（日常可用集）。"""
    from engine_policy import get_engine_tier
    return {e for e in _domain_combo(name) if get_engine_tier(e) != "research_only"}


@pytest.fixture(scope="module")
def new_sources():
    """模块级 NEW_SOURCES 的镜像，便于检查来源未被清空。"""
    return _new_sources()


class TestNewSourcesReachable:
    """每个新专源必须落在**默认模式**（auto/balanced）的预算内。

    为什么只锁 auto/balanced、不强求 fast：
    fast 的契约是低延迟（`_apply_intent_parallelism` 在该模式下强制串行），
    而 medical/film_search/book_search 的新源声明在位次 6 —— 要塞进 fast 的
    预算 2 需要 4 个加槽，等于把 fast 变成 6 引擎串行，直接违背模式意图。
    故 fast 下允许新源缺席（用户显式要快就给快），
    但**默认模式必须可达**，否则等于「装了没通电」。
    下面另有 test_fast_mode_not_bloated 锁定 fast 不被加槽拖爆。
    """

    @pytest.mark.parametrize("domain", sorted(NEW_SOURCES))
    @pytest.mark.parametrize("mode,depth", [("auto", "balanced"), ("deep", "deep")])
    def test_new_source_within_budget(self, domain, mode, depth):
        from engine_policy import filter_combo_by_policy
        # 注意：这里的 combo 用**声明顺序**，与 route 的加槽额度同计算方式
        # （额度按声明位次算，见 route._apply_policy_with_new_source_slots）。
        # 旧版写成 `if e in _enabled(domain) or True`——`or True` 让 enabled
        # 过滤彻底失效，于是「引擎未启用/已下线」这类问题在本检查下不可见。
        combo = [e for e in _domain_combo(domain) if e in _enabled(domain)]
        # 复刻 route 的加槽计算方式
        pending = [e for e in NEW_SOURCES[domain] if e in combo]
        assert pending, f"{domain} 的声明新源不在 combo 中：{NEW_SOURCES[domain]}"
        from engine_policy import combo_budget
        base = combo_budget(mode=mode, depth=depth, context="search") or len(combo)
        deepest = max(combo.index(e) + 1 for e in pending)
        # 与 route 同计算方式：fast/budget 或 depth=fast 上限 2（串行，加槽直接乘延迟），
        # 其余 4（并行，加槽不显著拖慢）
        cap = 2 if (mode in ("fast", "budget") or depth == "fast") else 4
        extra = min(max(deepest - base, 0), cap)
        kept = filter_combo_by_policy(combo, mode=mode, depth=depth,
                                      context="search", budget_extra=extra)
        for e in pending:
            assert e in kept, (
                f"{domain}/{mode}/{depth}：新专源 {e} 未落在预算内"
                f"（combo={combo} 位次={combo.index(e)} budget={base}+{extra} "
                f"kept={kept}）——引擎装了但路由不带它"
            )


class TestExistingSourcesNotEvicted:
    """加槽必须是「扩容」而非「顶位」：既有可用源不得被挤掉。"""

    @pytest.mark.parametrize("domain", sorted(NEW_SOURCES))
    def test_no_existing_source_lost(self, domain):
        from engine_policy import combo_budget, filter_combo_by_policy
        combo = _domain_combo(domain)
        base = combo_budget(mode="auto", depth="balanced", context="search")
        before = filter_combo_by_policy(combo, mode="auto", depth="balanced",
                                        context="search")
        pending = [e for e in NEW_SOURCES[domain] if e in combo]
        deepest = max(combo.index(e) + 1 for e in pending)
        extra = min(max(deepest - base, 0), 4)
        after = filter_combo_by_policy(combo, mode="auto", depth="balanced",
                                       context="search", budget_extra=extra)
        lost = [e for e in before if e not in after]
        assert not lost, (
            f"{domain}：加槽后既有源被挤掉 {lost}——"
            f"应扩容（extra）而非用 must_keep 顶位"
        )


class TestVerticalKeepMapConsistency:
    """route 的加槽表与 config 声明必须一致（防手改漂移）。"""

    def test_declared_sources_exist_in_config(self):
        for domain, engs in NEW_SOURCES.items():
            combo = _domain_combo(domain)
            assert combo, f"{domain} 在 config 中不存在"
            for e in engs:
                assert e in combo, f"{domain} 声明新源 {e}，但不在其 combo 中"

    def test_route_table_is_the_single_source(self, new_sources):
        """本文件的判据必须来自 route 的来源，且来源非空。

        旧版用 `inspect.getsource(route)` 子串检查冒充一致性校验：把
        `_VERTICAL_NEW_SOURCE` 整段删掉，只要域名字还出现在注释里就照样通过。
        现在直接读来源对象，删表即失败。
        """
        assert new_sources, "route._VERTICAL_NEW_SOURCE 为空——加槽机制被移除"
        assert set(new_sources) == set(NEW_SOURCES)


class TestProbeActuallyRoutesToNewSource:
    """端到端：声明了新专源的域，用典型查询真的要把该源选出来。

    上面的 test_new_source_within_budget 只在**策略函数**层面复刻计算方式，它
    无法发现「域根本没被任何 pattern 选中」（kor_law 的实例：引擎声明齐全、
    能 --engine 单跑，但没有任何 pattern 能把查询送进它所在的域，于是永远
    --engine 才可达）。这一层补的正是「路由真跑一遍」。
    """

    PROBES = {
        "species_search": ("海洋物种",),
        "medical": ("疫情 暴发",),
        "book_search": ("德语 图书 联合目录",),
        "film_search": ("意大利 电视剧",),
        "sports_search": ("F1 积分榜", "德甲 赛程"),
        "org_entity": ("研究机构 标识",),
        "media_search": ("独立音乐 艺人",),
        # 2026-09-16：五个免密钥国内源的端到端探针
        "hot_trending": ("微博热搜", "抖音热榜"),
        "cn_tech_community": ("技术博客",),
        "financial_news": ("财经",),
        "weather_query": ("上海天气",),
        # 2026-09-17：国际新闻实时流 + 声明核验两个新域的端到端探针
        "intl_news_flash": ("今日国际要闻",),
        "claim_check": ("疫苗谣言 是不是真的",),
        # 2026-09-19：academic 域接入 local_pubmed（修复）+ core（新增）。
        # 探针须落在 academic 且能选出新源——auto/balanced 档实测为
        # [arxiv, openreview, biorxiv, local_pubmed, core]。
        # 注意别用中医/生物词：CRISPR、基因编辑等会被 protein_search 域
        # 先截走（那里有 uniprot/pubchem），探针就测不到本域的接线了。
        "academic": ("machine learning survey", "文献综述 论文"),
    }

    @pytest.mark.parametrize("domain", sorted(NEW_SOURCES))
    def test_probe_lands_in_domain_and_selects_new_source(self, domain):
        import route
        probes = self.PROBES.get(domain)
        assert probes, f"{domain} 没有登记端到端探针——新增域必须补一条"
        for q in probes:
            d = route.route_query(q, mode="auto", depth="balanced")
            chosen = list(d.get("engines") or [])
            want = [e for e in NEW_SOURCES[domain] if e in _domain_combo(domain)]
            hit = [e for e in want if e in chosen]
            assert hit, (
                f"查询「{q}」未选中 {domain} 的新专源 {want}："
                f"实际域={d.get('domain')} 引擎={chosen} "
                f"reason={d.get('reason')}——引擎装了但路由不带它"
            )


class TestFastModeNotBloated:
    """fast 模式不得因加槽而膨胀（保障模式契约：低延迟）。"""

    @pytest.mark.parametrize("domain", sorted(NEW_SOURCES))
    def test_fast_combo_stays_small(self, domain):
        from engine_policy import combo_budget, filter_combo_by_policy
        combo = _domain_combo(domain)
        base = combo_budget(mode="fast", depth="fast", context="search")
        pending = [e for e in NEW_SOURCES[domain] if e in combo]
        deepest = max(combo.index(e) + 1 for e in pending)
        cap = 2  # fast 档上限
        extra = min(max(deepest - base, 0), cap)
        kept = filter_combo_by_policy(combo, mode="fast", depth="fast",
                                      context="search", budget_extra=extra)
        assert len(kept) <= base + 2, (
            f"{domain}: fast 模式 combo 被加槽撑到 {len(kept)} 个 "
            f"（base={base}+extra={extra}）——fast 契约是低延迟，串行下会拖慢"
        )


# ── 全仓「位置性休眠」台账（防循环信任）────────────────────────────────────────
# 上面所有判据都以 route._VERTICAL_NEW_SOURCE 为唯一来源——这有个盲区：
# 表里漏掉的源，检查连「该查它」都不知道（jikan/listenbrainz 实测漏网：
# auto 预算 3、声明位次 4/5、不在加槽表 → 日常路由永不可达，而本文件当时
# 全绿）。本节把「哪些引擎落在它出现的每一个域的预算外」变成显式台账：
# 新增休眠引擎必须自觉登记（接线或写明原因），接线后休眠解除必须撤账。
_DORMANT_ALLOWLIST: dict[str, str] = {
    # engine → 为什么允许它位置性休眠（一句话，供下一批接线决策用）
    "cleveland": "art_museum 同质重复备份（与 artic 同能力，故意不加槽）",
    "cn_ai_news": "待接线：chinese_tech_deep 位次 4",
    # 2026-09-16：academic 域接入 openreview/biorxiv 后，原第 3 位的 crossref
    # 被推到第 5 位因而休眠。这与 dblp/europepmc 是同一笔账：academic 的
    # combo 有 8 个源，而 fast 档 budget=2（must_keep 腾位后实际只留 2 个），
    # 排在 3 位之后一律不参与自动路由，只在 deep/research 不截断时才跑。
    # 若要恢复 crossref 的自动可见性，需给它加槽（_VERTICAL_NEW_SOURCE）或
    # 调高 academic 域预算——两者都会挤掉 openreview/biorxiv，属权衡而非纯收益。
    "crossref": "待接线：academic 位次 5（fast 档 budget=2，位次 3+ 不参与自动路由）",
    # 2026-09-16：security_search 接入 osv/cisa_kev 后，crt_sh 由第 3 位降到第 5 位。
    # 与 crossref 同一笔账：该域声明 5 个源而 fast 档 budget=2，位次 3+ 不参与
    # 自动路由。cisa_kev 是本次新接的源，天然排在兜底源之后（见 config 注释：
    # 结构化事实不应挤掉中文召回），故同样登记。
    "cisa_kev": "待接线：security_search 位次 4（fast 档 budget=2，位次 3+ 不参与自动路由）",
    "crt_sh": "待接线：security_search 位次 5（原第 3 位，接入 osv/cisa_kev 后顺延）",
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
    "fx_rate": "待接线：macro_data 位次 5",
    "fxtwitter": "social 域位次 6：社交引擎另有专用通道，待核实是否真休眠",
    "gleif": "待接线：org_entity 位次 6",
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
    "parallel": "待接线：chinese_tech_deep 位次 5",
    "reddit": "social 域位次 7：社交引擎另有专用通道，待核实是否真休眠",
    "twitter": "social 域位次 5：社交引擎另有专用通道，待核实是否真休眠",
    "weibo": "social 域位次 9：社交引擎另有专用通道，待核实是否真休眠",
    "xiaohongshu": "social 域位次 8：社交引擎另有专用通道，待核实是否真休眠",
    "you": "待接线：news_realtime 位次 6",
    "zhihu_hot_app": "待接线：hot_trending 位次 6",
}


def _dormant_engines() -> dict[str, list[tuple[str, int]]]:
    """位置性休眠盘点：引擎在它出现的**每一个**域里都落预算外
    （位次 > budget，且非 primary、不在加槽表、非语言 must_keep 族）。"""
    import route
    from engine_policy import combo_budget, get_engine_tier

    budget = combo_budget(mode="auto", depth="balanced", context="search") or 3
    slot = route._VERTICAL_NEW_SOURCE
    lang_keep = {e for v in route._LANG_PREFERRED_ENGINES.values() for e in v}
    appear: dict[str, list[tuple[str, int, bool, bool]]] = {}
    for d in _cfg().get("domains") or []:
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


class TestDormantEngineLedger:
    """休眠引擎集合必须与台账严格相等（防「装了没通电」静默增长）。"""

    def test_dormant_matches_ledger(self):
        dormant = _dormant_engines()
        unknown = sorted(set(dormant) - set(_DORMANT_ALLOWLIST))
        stale = sorted(set(_DORMANT_ALLOWLIST) - set(dormant))
        assert not unknown, (
            "新增位置性休眠引擎未登记台账：" + ", ".join(unknown) + "；明细："
            + "; ".join(f"{e}@{dormant[e]}" for e in unknown)
            + "。要么接进 route._VERTICAL_NEW_SOURCE，"
            "要么在 _DORMANT_ALLOWLIST 写明原因"
        )
        assert not stale, (
            "台账过期（这些引擎已不休眠，请从 _DORMANT_ALLOWLIST 撤账）："
            + ", ".join(stale)
        )

    def test_ledger_entries_have_reasons(self):
        for e, reason in _DORMANT_ALLOWLIST.items():
            assert reason and reason.strip(), f"台账条目 {e} 缺原因说明"
