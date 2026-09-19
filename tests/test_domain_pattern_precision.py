#!/usr/bin/env python3
"""test_domain_pattern_precision.py — 域规则精度回归测试（纯本地，不联网）。

背景：config.yaml 的 domains 用**裸子串/无词边界正则**匹配查询，且匹配是
「先命中先赢」（route.py 按 config 列表顺序取第一个命中域，没有任何打分）。
于是短 ASCII 备选会撞进不相干的英文单词、多义中文词会撞进议题句，命中后
主域就锁死垂直引擎组合——而这些组合大多**不含通用保底源**，误命中时整批
结果与查询无关，且早停会把通用引擎一并跳过。

实测（2026-09-19，修复前）：
  「monetary policy tightening 2026」→ art_museum，返回三幅画
  「project manager 职责」          → sports_search，返回 F1 积分榜
  「React Native metro 报错」        → transport_rt，返回纽约共享单车站点
  「single threaded 性能」           → media_search，返回同名歌曲

修复分两类，本文件两类都锁：
  A. 词边界（\\b）——治「短词是别的英文单词的子串」：hn/technique、
     monet/monetary、TLE/settlement、VIN/Kevin、otc/notch、drug/drugstore。
  B. 让位表（route._POINTED_INTENT_RE）——治「整词多义」：天气/气候变化、
     在哪里/在哪里改配置、指南/编程指南。长主题句且无本域意图词时让位。

**两类都必须双向锁**：只锁「误判消失」会让域永久失能而无人察觉，所以每个
修复面都配一条「真查询仍命中」的对照用例。
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from route import route_query  # noqa: E402


def _domain(query):
    return route_query(query).get("domain")


# ─── A 类：ASCII 短词子串事故 ─────────────────────────────────────────────────
# 每条 (查询, 不得命中的域)。修复前实测均命中右侧域。
SUBSTRING_ACCIDENTS = [
    ("technique for reducing latency", "hackernews_search"),   # hn ⊂ technique
    ("monetary policy tightening 2026", "art_museum"),         # monet ⊂ monetary
    ("settlement 结算 周期", "astro_space"),                     # TLE ⊂ settlement
    ("newspaper 行业 衰退", "academic"),                         # paper ⊂ newspaper
    ("Kevin 的 项目", "vehicle_data"),                          # VIN ⊂ kevin
    ("notch 设计 模式", "medical"),                              # otc ⊂ notch
    ("drugstore 连锁", "chem_search"),                          # drug ⊂ drugstore
    ("price of iPhone 15", "stock_query"),                     # price 过泛
    ("who wrote the linux kernel", "org_entity"),              # who ⊂ 疑问句开头
]


# ─── B 类：整词多义（定向收窄 / 行首负向排除处理）──────────────────────────
# 这些词本身是合法触发词，只是还有另一个义项。修法是**保留真查询、只排除
# 实际撞车的那种说法**，而不是把词删掉或走让位表——让位表的判据是「长主题句
# + 无本域意图词」，意图词永远列不全，2026-09-19 实测它会把「上海天气 未来
# 一周」和「東京 おすすめ ラーメン 屋 はどこ」一起误让位（三条既有回归门红）。
POLYSEMY_MISROUTES = [
    ("气候变化 极端天气 2026", "weather_query"),      # 行首负向排除气候议题
    ("在哪里 查看 报错 日志", "geo_places"),           # 行首负向排除软件语境
    ("Rust 编程指南 入门", "medical"),                 # 指南 → 临床/诊疗指南
    ("novel method for graph embedding", "book_search"),
    ("image crop 参数", "soil_agri"),
    ("fate of the project", "anime_encyclopedia"),
    ("single threaded 性能", "media_search"),
    ("type cast 性能", "film_search"),
    ("project manager 职责", "sports_search"),
    ("React Native metro 报错", "transport_rt"),
    ("income inequality 研究", "modal_card"),
    ("report 生成 工具", "financial_news"),
    ("指数 函数 求导", "stock_query"),                 # 指数 → 指数行情/大盘指数
    ("Adam momentum 优化器", "ths_hot_search"),
    ("国家自然科学基金 申请 2026", "fund_query"),       # 基金 → 基金净值/公募基金
    ("sentiment analysis 教程", "social"),
    ("动画 效果 CSS", "anime_encyclopedia"),
    ("Telegram 电报 机器人 开发", "cls_telegraph_search"),
    ("美国 人口 结构 变化", "macro_data"),             # 人口 → 人口数据/人口统计
    ("世界 贸易 组织 改革", "macro_data"),             # 贸易 → 贸易额/进出口
]


# ─── 对照面：真查询必须仍然命中自己的域 ───────────────────────────────────────
# 只锁「误判消失」等于允许把域改废；这些用例保证能力没被误伤。
LEGITIMATE_ROUTES = [
    # A 类修复点的真查询
    ("hn search 讨论", "hackernews_search"),
    ("CISA KEV 漏洞", "security_search"),
    ("卫星 TLE 轨道根数", "astro_space"),
    ("莫奈 monet 画作", "art_museum"),
    ("深度学习 paper 论文", "academic"),
    ("车辆 VIN 查询", "vehicle_data"),
    ("药品 说明书", "medical"),
    ("阿司匹林 分子式", "chem_search"),
    ("苹果股价 行情", "stock_query"),
    # B 类修复点的真查询（意图词必须仍然豁免让位）
    ("北京 今天 天气", "weather_query"),
    ("上海 天气 怎么样", "weather_query"),
    ("北京 天气", "weather_query"),
    ("埃菲尔铁塔 在哪里", "geo_places"),
    ("埃菲尔铁塔 经纬度", "geo_places"),
    ("火影忍者 番剧 声优", "anime_encyclopedia"),
    ("三体 小说 书评", "book_search"),
    ("土壤 重金属 检测", "soil_agri"),
    ("糖尿病 症状 用药", "medical"),
    ("沪深300 基金净值", "fund_query"),
    ("财联社 电报 快讯", "cls_telegraph_search"),
    ("NBA 球队 排名", "sports_search"),
    ("周杰伦 新歌 专辑", "media_search"),
    ("电影 主演 是谁", "film_search"),
    ("北京 地铁 时刻表", "transport_rt"),
    # 曾经被「让位表」方案误伤的用例（2026-09-19 三条既有回归门同时红）——
    # 这两条是定向收窄相对让位表的核心优势，必须常驻。
    ("上海天气 未来一周", "weather_query"),
    ("東京 おすすめ ラーメン 屋 はどこ", "geo_places"),
    # 收窄后的真查询
    ("上证指数 行情", "stock_query"),
    ("临床指南 糖尿病", "medical"),
    ("沪深300 基金定投", "fund_query"),
    ("财联社 快讯", "cls_telegraph_search"),
]


def test_substring_accidents_do_not_hijack():
    """短 ASCII 备选不得撞进不相干的英文单词。"""
    wrong = [(q, _domain(q)) for q, bad in SUBSTRING_ACCIDENTS
             if _domain(q) == bad]
    assert not wrong, f"子串事故复发：{wrong}"


def test_polysemous_domains_yield_on_topical_queries():
    """多义域在长主题句上让位给通用组合。"""
    wrong = [(q, _domain(q)) for q, bad in POLYSEMY_MISROUTES
             if _domain(q) == bad]
    assert not wrong, f"多义域仍然劫持：{wrong}"


def test_legitimate_queries_still_route_to_their_domain():
    """对照面：修复不得把域改废。"""
    wrong = [(q, _domain(q), want) for q, want in LEGITIMATE_ROUTES
             if _domain(q) != want]
    assert not wrong, f"真查询被误让位（能力回退）：{wrong}"


def test_pointed_intent_re_stays_minimal():
    """让位表必须**保持最小**：只收「域主源是唯一入口、误命中无通用保底」的
    点查域。

    2026-09-19 实测：把 weather_query / geo_places 等 9 个多义域加进来治误判，
    三条既有回归门同时红——「上海天气 未来一周」被让位到 chinese_general、
    「東京 おすすめ ラーメン 屋 はどこ」丢掉 geo 主源。让位判据是「长主题句
    + 无本域意图词」，而意图词表永远列不全（未来一周、はどこ 都漏了），失败
    模式是**静默的能力回退**。多义域改用定向负向排除（见 config.yaml 注释）。
    """
    from route import _POINTED_INTENT_RE
    extra = sorted(set(_POINTED_INTENT_RE) - {"package_search", "ai_model"})
    assert not extra, f"让位表被扩充（会静默让位真查询）：{extra}"


def test_domain_patterns_compile():
    """config.yaml 里写坏的正则会被静默丢弃（route.py 的 `except re.error`），
    那个域从此永不命中且无任何报错。这里把「写坏」变成可见失败。"""
    import re
    from config import load_config
    broken = []
    for d in load_config().get("domains", []):
        for p in d.get("patterns") or []:
            try:
                re.compile(p)
            except re.error as e:
                broken.append((d.get("name"), p, str(e)))
    assert not broken, f"域正则编译失败（该域已静默失能）：{broken}"
