#!/usr/bin/env python3
"""test_route_precision_20261023.py — 真实 usage log 回放出来的误路由回归。

2026-10-23 对一组脱敏真实查询回放当前路由，按「弱触发词抢先、后面有更具体
的域」和「垂直域零结果/靠恢复链兜底」两类筛出下列样本。**所有样本都改写
成同结构的脱敏句**：只保留触发词与语义关系，不含原始查询、个人品牌或
真实公司名，保证用例可公开、可迁移，不是对某一次日志的过拟合。

覆盖的串匹配类别：
  - CJK 构词成分：雪 ⊂ 雪原、型月 ⊂ 大模型月报、馆藏 ⊂ 馆藏电子全文、
    动车 ⊂ 电动车、火山 ⊂ 火山引擎
  - ASCII 子串：F1 ⊂ off11、Go ⊂ argo
  - 跨域同形：代码 ⊂ 统一社会信用代码、汇率 ⊂ 示例金融库接口
  - 缺省兜底：裸「漏洞/vulnerability」没有域接
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from route import route_query  # noqa: E402
from macro_countries import is_foreign_macro_query  # noqa: E402


def _domain(query: str) -> str:
    return route_query(query).get("domain") or ""


# (查询, 不得命中的域, 说明)
MISROUTES = [
    ("雪原 开源 投研 搜索工具", "weather_query", "雪 ⊂ 品牌名"),
    ("某云产品 官方 指南 火山引擎", "geo_places", "火山 ⊂ 火山引擎"),
    ("macOS 菜单栏 第三方 图标 不显示 消失 问题", "image_search", "图标是 UI 控件"),
    ("菜单栏图标不显示", "image_search", "图标是 UI 控件"),
    ("AI大模型月报 2026年8月 汇总", "anime_encyclopedia", "型月 ⊂ 大模型月报"),
    ("固态电池 量产 良率 off11", "sports_search", "F1 ⊂ off11"),
    ("钙钛矿 太阳能 稳定性 off10", "sports_search", "F1 ⊂ off10"),
    ("统一社会信用代码 查询", "code_search", "代码 ⊂ 工商字段"),
    ("统一社会信用代码 查询", "chinese_tech_deep", "代码 ⊂ 工商字段"),
    ("基金代码 查询", "code_search", "基金代码是金融字段"),
    ("基金代码 查询", "chinese_tech_deep", "基金代码是金融字段"),
    ("海关 商品代码 查询", "code_search", "商品编码是贸易字段"),
    ("商品编码 查询", "trade_stats", "裸商品编码可能是零售 SKU"),
    ("示例金融库 开源 接口 汇率 中间价 python github", "fx_query", "汇率 ⊂ 库接口"),
    ("图书馆 馆藏 电子全文 查询", "art_museum", "馆藏 ⊂ 图书馆"),
    ("电动车 充电 案例 创新", "modal_card", "动车 ⊂ 电动车"),
    ("央行 降准 最新", "org_entity", "降准是政策新闻不是机构实体"),
    ("AI agent framework 2026 latest", "news_realtime", "latest 在技术查询里"),
    ("PDF extract text image", "image_search", "image 是待处理对象"),
    ("重启后 django 自检", "chinese_tech_deep", "Go ⊂ django"),
    ("示例公司 官方博客 如何维护代码库 死代码", "code_search", "文章语境不是找仓库"),
    ("GPT 工程实践 清理死代码", "academic", "工程实践不是论文"),
    ("kubernetes ingress vs gateway api", "code_search", "对比句不是找仓库"),
    ("清理AI生成屎山代码", "code_search", "中文文章语境"),
    ("雪原 开源 投研 搜索工具", "code_search", "裸开源不是代码意图"),
]


def test_historical_misroutes_do_not_recur():
    wrong = [(q, _domain(q), bad) for q, bad, _why in MISROUTES
             if _domain(q) == bad]
    assert not wrong, f"误路由复发：{wrong}"


# (查询, 期望域) —— 收窄规则后真查询必须仍然命中
LEGITIMATE = [
    ("上海明天下雨吗", "weather_query"),
    ("明天会下雪吗", "weather_query"),
    ("火山 地貌", "geo_places"),
    ("富士山 海拔", "geo_places"),
    ("壁纸 下载", "image_search"),
    ("动漫 型月", "anime_encyclopedia"),
    ("型月 模型 手办", "anime_encyclopedia"),
    ("F1 赛车", "sports_search"),
    ("用 Go 写服务", "chinese_tech_deep"),
    ("代码 生成器", "code_search"),
    ("基金代码 查询", "fund_query"),
    ("海关 商品代码 查询", "trade_stats"),
    ("开源项目 推荐", "code_search"),
    ("示例 API 接入教程", "code_search"),
    ("GPT 论文 综述", "academic"),
    ("人民币 汇率 今日", "fx_query"),
    ("博物馆 馆藏", "art_museum"),
    ("财报 公告", "financial_news"),
    ("某科技有限公司 股东", "company_search"),
    ("央行 行长", "org_entity"),
    ("latest news on AI", "news_realtime"),
]


def test_legitimate_queries_still_hit_their_domain():
    wrong = [(q, _domain(q), want) for q, want in LEGITIMATE
             if _domain(q) != want]
    assert not wrong, f"真查询被误让位：{wrong}"


def test_registry_queries_go_to_company_search():
    for q in ("统一社会信用代码 查询",
              "示例科技有限公司 统一社会信用代码 法定代表人"):
        assert _domain(q) == "company_search", q


def test_vulnerability_queries_go_to_security_search():
    for q in ("示例包 漏洞", "示例 python package vulnerability"):
        assert _domain(q) == "security_search", q


def test_china_macro_queries_front_worldbank_not_fred():
    """FRED 是美国序列；中国统计口径查询不能让 FRED 领队返回美国 GDP。"""
    for q in ("国家统计局 一季度 GDP", "中国 一季度 GDP"):
        decision = route_query(q)
        assert decision.get("domain") == "macro_data", q
        combo = decision.get("engines_combo") or []
        assert combo and combo[0] == "worldbank", (q, combo)
    assert is_foreign_macro_query("国家统计局 一季度 GDP") is True
    assert is_foreign_macro_query("国内 CPI 数据") is True
    # 同一句话里同时有美国词时不能误判成中国口径
    assert is_foreign_macro_query("美国 统计局 CPI") is False
    assert route_query("美国 统计局 CPI").get("engines_combo", [None])[0] == "fred"
