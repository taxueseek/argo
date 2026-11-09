#!/usr/bin/env python3
"""test_combo_budget_coverage.py — 域组合「预算内可交付」回归检查。

## 守的是什么缺陷

路由为每个垂类域声明 5-6 个引擎，但 `engine_policy.combo_budget(depth=fast)=2`
**只按声明顺序取前 2 个**。于是「垂直专源排前面、通用保底源排尾部」的写法会
把唯一的通用源系统性剪掉。2026-09-12 实测（修复前）：

| 域 | 头部 2 源（= 实际执行） | 实测结果 |
|----|------------------------|----------|
| medical | clinicaltrials / openfda | 0 条 × 3 查询，每次白烧 2.6-3.5s |
| law_text | flk_law / gov_regulations | 0 条（「民法典 第 1062 条」） |
| us_stock | finviz / seeking_alpha | 0 条 |
| security_search | nvd / crt_sh | 0 条 |
| ai_model | models_dev / huggingface | 0 条 |

被剪掉的通用源单跑都有 5-10 条命中（anysearch / exa）。问题不在「源不够」，
而在「预算内的 2 个位置被同类窄源占满」。

## 为什么不做「前 2 位必须异族 / 必须有 web_general」的通用硬检查

试过，判据不成立：`family_of("anysearch") == "web_general"`，于是
`['redskill','anysearch']`（垂直源 + 通用保底，完全健康）与
`['clinicaltrials','openfda']`（两个英文窄源，真的会空手）被判成同一类。
按该规则扫全仓会报出 30 个域，绝大多数是误报（`['baidu_baike','zh_wikipedia']`
对中文查询是好组合）。**静态结构判不出「能否交付」——那需要每引擎的语言/
模态覆盖声明，而配置里 157 个引擎无一声明语言。**

所以本文件只锁两类**有证据**的判据，外加一份显式的债务清单：

  1. `TestComboExecutableWidth`    宽度：组合至少 2 个引擎（预算内能跑两条）；
  2. `TestMeasuredFailuresStayFixed` 具名锚点：实测 0 结果的域不许回退；
  3. `TestDeclarationDebtIsTracked` 债务清单：前 2 位无通用/本地保底的域
     逐个登记。清单是「检查当前已知集合」而非「检查它们都对」——新增域若
     落进来，必须显式决定是改组合还是登记，不允许静默增长。

新增域的处理路径：先跑 `python3 scripts/search.py "<该域典型查询>"` 看能否
交付；交付不了就调 combo 顺序把通用源提到前 2 位。
"""

import os
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# 单源域豁免：这两域语义上只有一个权威源，没有可替代的第二源
_SINGLE_SOURCE_BY_DESIGN = {
    "aviation_weather": "航空天气只有一个权威源，无同类可加",
    "zhihu_user_data": "知乎个人数据只有 zhihu_user 一个入口",
}

# 债务清单：前 2 位既无 web_general 也无 local_* 的域。
# 它们是「潜在 0 结果风险」，多数在语言/模态匹配时正常工作，故不设为硬检查；
# 但集合变化必须显式处理，防止新增域静默落入同一形态。
# 2026-09-13（批次九）：earth_science 与 sports_search 已把通用保底源
# （anysearch）提到前 2 位（earth_science: usgs+gdacs+anysearch；
# sports_search: thesportsdb+jolpica+anysearch），不再是「窄源顶 2」，
# 故从债务清单移除——清单失真的话，后续读者会以为这两域仍有 0 结果风险。
# 2026-11-10：code_search 随 grep_app（GitHub 全仓代码检索，34d5dd2）挂入
# 域档位，前 2 位已有通用源，同理由移除。
_NARROW_TOP2_DEBT = {
    "academic", "book_search", "chem_search", "cls_telegraph_search",
    "cn_encyclopedia", "cn_tech_community", "dictionary_search",
    "em_news_search", "entity_search", "financial_news",
    "hot_trending", "jin10_flash", "macro_data", "media_search", "meme_slang",
    "ml_models", "org_entity", "package_search", "protein_search",
    "scholar_search", "social", "species_search",
    "stock_query", "tech_deep", "web_archive", "web_docs", "zhihu_hot_list",
    # ── 2026-09-16 补登记：引擎归类修正后「显现」出来的既有债务 ─────────────
    #
    # 这批域既不是新增的，也不是本次改动引入的问题。此前它们的 combo 前 2 位里
    # 混着 biorxiv/datacite/energy_charts 这类**垂直源**，只因为那些源没被任何
    # 表归类、静默保底成了 web_general，检查器才误判为「有通用保底」。本次把 69
    # 个未归类引擎按「它回答什么问题」正确归族（web_general 91→35）后，这些域
    # 的真实形态——前 2 位全是窄专源——随之显形。
    #
    # 逐一核实过，登记理由是「专源失效时不会空手」，不是任其静默增长：
    #   * 每个域的 fallback 字段都已在自身 combo 内，是明确的第二/第三顺位；
    #   * fast 档实测这些域实际执行 2~3 个源，且全部返回非零结果
    #     （energy_grid 1 条 / art_museum 5 条 / outbreak_health 5 条 /
    #     standards 1 条 / trade_stats 5 条）；
    #   * 这些源是权威一手库（国家电网、NOAA、WHO、国标平台、Met、
    #     公司注册库），其数据不存在「网页搜索保底」的等价物——硬塞 anysearch
    #     进前 2 位反而会拿二手页面挤掉一手数据。
    # 留在清单里的意义：前 2 位一旦变动，仍会被强制显式复核。
    # 2026-10-22：company_search 移除——元宝（family=web_general）已提到 combo 第 1 位，
    # 前 2 位不再是「纯窄源」，本域从债务形态毕业（判据见 _has_general_fallback：
    # local_* 或 family_of(e)=="web_general"；缘由见 config.yaml 该域注释）。
    "anime_encyclopedia", "art_museum", "astro_space",
    "dataset_search", "earth_science", "energy_grid", "film_search",
    "global_event", "outbreak_health", "patent_search", "soil_agri",
    "standards", "transport_rt",
    # ── 2026-10-16：fx_query 建域时登记 ───────────────────────────────────
    #
    # 前 2 位 fx_rate（open.er-api 即期汇率）+ eastmoney（财经资讯站内检索，
    # 「人民币兑美元中间价报 6.72」类快讯的原生来源）都是免费无密钥专源。
    # 不把 anysearch 提前的理由：汇率是答案型查询，二手网页会在精排里挤掉
    # 6.7x 这类一手数字（原 macro_data 路径的实测事故正是通用源兜底交付了
    # 百科词条）。保底有两条明确通道：fallback=anysearch（combo 位次 4）
    # + 零结果恢复链 L3/switch_engine 拉通用源。建域实测两源均非零返回
    # （675ms，含中间价快讯与双向即期汇率）。
    "fx_query",
    # ── 2026-10-21：crypto_search 接入 DeFiLlama 后登记 ──────────────────
    #
    # 前 2 位 coingecko（币价/市值）+ defillama（协议 TVL）都是免 key 的
    # 结构化一手源，没有「网页搜索保底」的等价物：TVL 是链上统计量，二手
    # 页面只会转述且滞后。保底走 fallback=anysearch（combo 位次 3）与零
    # 结果恢复链；两源实测均非零返回（coingecko 45ms / defillama 2.6s）。
    "crypto_search",
}


def _load_domains() -> list[dict]:
    from config import get_domains, load_config
    return get_domains(load_config())


def _top2_of(domain: dict) -> list[str]:
    return (domain.get("engines_combo") or [])[:2]


def _has_general_fallback(combo: list[str]) -> bool:
    from engine_families import family_of
    return any(e.startswith("local_") or family_of(e) == "web_general"
               for e in combo)


@pytest.fixture(scope="module")
def domains():
    return _load_domains()


class TestComboExecutableWidth:
    """预算内的引擎数量必须真的够用。"""

    def test_budget_is_at_least_two(self):
        from engine_policy import combo_budget
        assert combo_budget(mode="auto", depth="fast") >= 2, \
            "fast 预算小于 2 则无交叉验证空间"

    def test_every_domain_declares_at_least_two_engines(self, domains):
        thin = [(d["name"], d.get("engines_combo"))
                for d in domains
                if len(d.get("engines_combo") or []) < 2
                and d["name"] not in _SINGLE_SOURCE_BY_DESIGN]
        assert not thin, (
            "这些域连 2 个引擎都没声明，预算内无从冗余：\n"
            + "\n".join(f"  - {n}: {c}" for n, c in thin)
        )

    def test_single_source_exemptions_are_still_single_source(self, domains):
        """豁免项确实还只有一个源——加了第二个源就该从名单删掉。"""
        by_name = {d["name"]: d for d in domains}
        stale = [n for n in _SINGLE_SOURCE_BY_DESIGN
                 if len(by_name[n].get("engines_combo") or []) >= 2]
        assert not stale, f"这些域已有第二源，豁免应删除: {stale}"


class TestMeasuredFailuresStayFixed:
    """2026-09-12 实测 0 结果的 5 个域，通用保底源必须留在预算内。

    这是全文件最硬的一条：每项都对应一次真实复现的 0 结果事故。
    """

    CASES = {
        "medical": "anysearch",
        "law_text": "anysearch",
        "security_search": "anysearch",
        "ai_model": "anysearch",
        "us_stock": "exa",
    }

    def test_fixed_domains_keep_their_fallback_in_budget(self, domains):
        by_name = {d["name"]: d for d in domains}
        missing = []
        for dom, eng in self.CASES.items():
            d = by_name.get(dom)
            assert d is not None, f"域 {dom} 不存在"
            top = _top2_of(d)
            if eng not in top:
                missing.append(f"{dom}: 前 2 位 {top} 缺 {eng}")
        assert not missing, (
            "已知 0 结果事故域回退（combo_budget=2 会剪掉尾部源）：\n"
            + "\n".join(missing)
        )

    def test_fixed_domains_declare_the_fallback_at_all(self, domains):
        """保底源连声明都没有就更谈不上执行。"""
        by_name = {d["name"]: d for d in domains}
        missing = [f"{d}: 未声明 {e}" for d, e in self.CASES.items()
                   if e not in (by_name[d].get("engines_combo") or [])]
        assert not missing, "\n".join(missing)


class TestDeclarationDebtIsTracked:
    """前 2 位缺通用保底的域：集合变化必须显式处理，不得静默增长。"""

    def test_debt_set_is_unchanged(self, domains):
        current = {d["name"] for d in domains
                   if len(_top2_of(d)) >= 2 and not _has_general_fallback(_top2_of(d))}
        new = sorted(current - _NARROW_TOP2_DEBT)
        fixed = sorted(_NARROW_TOP2_DEBT - current)
        assert not new, (
            "新增域的前 2 位既无通用源也无本地源，专源失效时必然 0 结果。\n"
            "要么把通用源提到前 2 位，要么登记进 _NARROW_TOP2_DEBT 并说明"
            "为何它在语言/模态匹配下不会空手：\n  " + "\n  ".join(new)
        )
        assert not fixed, (
            "这些域已具备通用兜底，应从 _NARROW_TOP2_DEBT 删除以免清单失真：\n  "
            + "\n  ".join(fixed)
        )

    def test_debt_entries_are_real_domains(self, domains):
        known = {d["name"] for d in domains}
        unknown = sorted(_NARROW_TOP2_DEBT - known)
        assert not unknown, f"债务清单里的域已不存在: {unknown}"


class TestFlkArticleSpaceNormalization:
    """flk_law 条文号空格归一化（实测：带空格「民法典 第 1062 条」返回 0 条，
    压掉「第 N 条」内部空格后返回 8-10 条）。"""

    def test_space_inside_article_number_is_collapsed(self):
        from engines_builders_cn import _FLK_ARTICLE_SPACE_RE
        for src, want in [
            ("民法典 第 1062 条", "民法典 第1062条"),
            ("第 1062 条", "第1062条"),
            ("劳动法 第 39 条", "劳动法 第39条"),
            ("第1062条", "第1062条"),
        ]:
            got = _FLK_ARTICLE_SPACE_RE.sub(r"第\1条", src)
            assert got == want, f"{src!r} → {got!r}，期望 {want!r}"

    def test_normalization_leaves_plain_queries_untouched(self):
        """不含条文号的查询必须原样通过（法名、术语里没有「第…条」形态）。"""
        from engines_builders_cn import _FLK_ARTICLE_SPACE_RE
        for src in ("民法典", "行政处罚法", "个人信息保护 数据安全", "劳动法 第39条"):
            assert _FLK_ARTICLE_SPACE_RE.sub(r"第\1条", src) == src


class TestStdSamrTagStripping:
    """std_samr 的 C_STD_CODE 也带 <sacinfo> 高亮，旧实现只剥了 C_C_NAME，
    把 XML 标签直接印进了结果标题（实测：
    '<sacinfo>GB</sacinfo>/<sacinfo>T</sacinfo> <sacinfo>45577</sacinfo>-2025 …'）。"""

    def test_std_code_tags_are_stripped(self):
        from engines_builders_cn import _SACINFO_TAG_RE
        raw = "<sacinfo>GB</sacinfo>/<sacinfo>T</sacinfo> <sacinfo>45577</sacinfo>-2025"
        assert _SACINFO_TAG_RE.sub("", raw).strip() == "GB/T 45577-2025"

    def test_produced_title_has_no_tags(self):
        from engines_builders_cn import _SACINFO_TAG_RE
        code = _SACINFO_TAG_RE.sub(
            "", "<sacinfo>GB</sacinfo>/<sacinfo>T</sacinfo> <sacinfo>45577</sacinfo>-2025"
        ).strip()
        name = _SACINFO_TAG_RE.sub("", "数据安全技术 数据安全风险评估方法").strip()
        title = f"{code} {name}".strip()
        assert "<sacinfo>" not in title and "</sacinfo>" not in title
        assert title.startswith("GB/T 45577-2025 数据安全技术")
