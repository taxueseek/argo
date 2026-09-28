#!/usr/bin/env python3
"""test_module_size_gate.py — 单文件体积门禁（防「千行文件」复发）。

## 为什么要这道门

2026-09-21 的结构审查里，route.py 长到 2402 行、search.py 3033 行，同一批
职责（域规则 / 语言选源 / 组合装配 / 预算策略 / 排序 / 输出 / 缓存）全挤在
两个文件里。后果不是「不好看」，而是**改动的定位成本**：修一条域正则要在
2400 行里找匹配层，改排序要读完整条网络调度链。

拆完之后需要一道门把趋势钉住，否则半年后又会回到原样。规则三条：

1. **上限 1000 行**：`scripts/` 下任何文件不得超过；
2. **祖父清单**：拆不动的大文件（引擎构建器这类数据表、尚未分解的
   execute_search/super_search）显式登记 `上限 = 登记时的行数`，
   只能减不能增；
3. **新增文件天然合规**：新文件不在清单里，直接吃 1000 上限。

## 为什么要「只能减不能增」而不是留余量

留余量（比如「上限 = 当前 + 10%」）在多次小改动后会被吃光，且没人察觉。
写死成当前行数，任何增长都要**在这个文件里改一个数字**——一次可见、可评审的
动作，而不是无声的漂移。想加功能又超上限时，正解是拆模块（把上限往下调），
不是把数字往上调；真要上调，理由必须写在 reason 里。

## 清单怎么维护

- 拆走一块 → 把该条的上限改成新的行数（或整条删掉，若已 ≤ 1000）；
- 上限**不许**高于文件实际行数（下面有用例锁），所以清单不会虚高。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

HARD_LIMIT = 1000

# 祖父清单：路径 → (上限行数, 为什么不拆/拆到哪一步)
#
# 排序即待办优先级（最上面的最该拆）。新增条目要在 PR 描述里说明为什么
# 现在不拆——「以后再拆」不算理由，要写清楚卡在哪（缺锁、缺测试、边界不清）。
GRANDFATHERED: dict[str, tuple[int, str]] = {
    "scripts/engines_builders_data.py": (
        2924, "源声明构建器（数据表性质，逐源一段），拆开只是把同一张表切成多份；"
              "2026-09-27 −3：百度百科相关度门与化学 token 改用 cjk_tokens 共享切分"),
    "scripts/engines_builders_cn.py": (
        2319, "中文源声明构建器，逐源一段（数据表性质）；按语言/领域切只会把同一张表切碎"),
    "scripts/engines_builders_batch9.py": (
        1468, "批次九的源声明构建器，逐源一段；与既有构建器同形，拆开不减少概念"),
    "scripts/engines_builders_intl.py": (
        1220, "国际（日韩/欧洲）源声明构建器，逐源一段；含各源 XML/RSS 解析差异"),
    "scripts/engines_builders_tech.py": (
        1600, "技术社区源声明构建器（V2EX/StackExchange 等），逐源一段；"
              "2026-09-26 批次十增强净增 +402：sov2ex 一级全文来源（v2ex 就地升级）、"
              "tineye/bing_rss 新引擎、exa 免 key 匿名通道——与原逐源一段同性质，"
              "拆出只会把同一张表切碎（新引擎的下一站是 batch 模块）；"
              "2026-09-28 并行会话进行中的搜狗微信中间链解析 WIP（+96 行，"
              "engines_builders_tech.py 工作区未提交改动）——上限暂按工作区现值"
              "登记以免门禁误伤在途工作，随该工作正式提交后由其转正或回调"),
    "scripts/fetch_v3.py": (
        1793, "抓取降级链（HTTP→md 变体→TLS 指纹→jina→Parallel→浏览器），"
              "每级都要保留顺序与超时语义，尚未找到能一次搬走且可验证的切面；"
              "2026-09-27 +10：identity memory 内存缓存（dirty flag + 30s 写盘节流）"),
    "scripts/engines_base.py": (
        1509, "引擎基类 + HTTP 出口 + 输出映射，与 100+ 源声明的字段契约绑在一起；"
              "2026-09-26 +42：SERP 垃圾守卫接线（守卫本体独立在 serp_guard.py，此处只留"
              "冻结集+调用点）、反爬全文级标记（DDG challenge 实测）、key 脱敏三形态兜底；"
              "同日 −28：移除 DDG Instant Answer 解析器（引擎随本机可达性门下线）；"
              "同日 +41：{UUID} 进程级身份占位符（BAIDUID 反关联）+ tls_impersonate/"
              "resolve_redirects 两契约接线（跳转解析本体独立在 jump_resolver.py）；"
              "同日 −1：语言参数动态化补上 mkt（_build_html_engine 这侧漏了，"
              "lang_detect 的 mkt 表一直备着却无人调用），注释同步精简；"
              "2026-09-27 +14：logging 延迟加载（_get_logger 模式）+ "
              "_redact_secrets fallback 加基本脱敏（Bearer token + key=value）；"
              "2026-09-28 +2：_detect_anti_bot head 区按需 lower（性能优化）"),
    "scripts/cache.py": (
        1375, "结果缓存 + 路由软命中 + 指纹，正在按「键/存储/命中策略」三段考虑"
              "（+1=except_sets 具名异常导入行；+3=2026-09-27 退化写入守卫的两处"
              "调用点与一行 import——判定逻辑本身已拆到 cache_guard.py，本文件"
              "只剩调用，不再是「准入策略混在存储实现里」的状态；"
              "2026-09-27 +9：sqlite3 延迟导入（_get_sqlite3 模式）+ L1 100→500；"
              "−1=2026-09-27 引擎级垂直域维度（--domain/--sub_domain）并入缓存键，"
              "摊键逻辑拆到 cache_key_vdom.py，本文件只留调用点；"
              "2026-09-28 +2：normalize_query 加 @lru_cache 装饰器（性能优化）"),
    "scripts/http_client.py": (
        1005, "HTTP 客户端（UA 轮换 + Cookie 积累 + 重试 + 主机节流）；"
              "2026-09-27 +8：host throttle buckets 加 LRU 淘汰（100 个上限）"),
    "scripts/search_rank.py": (
        1017, "RRF 融合 + minhash 去重 + 五维重排；"
              "2026-09-27 +18：_weight_cache/_rel_factor_cache 加 TTL+大小限制（_evict_cache）"),
    "scripts/job.py": (
        1182, "招聘多平台聚合，各平台解析各成一段（数据表性质）"),
    "scripts/matrix_search_eval.py": (
        1103, "离线路由矩阵（138 条检查项），用例表占多数"),
    "scripts/route.py": (
        1006, "路由决策主干（route_query + 三个 _route_by_* 判定器）；"
              "2026-09-28 拆出 Bangs 解析（route_bangs.py，−26 行）后登记；"
              "下一刀：_route_by_domain（268 行垂直域主判定）独立成模块"),
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _python_files() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.is_file())


def test_no_file_exceeds_hard_limit_unless_grandfathered():
    offenders = []
    for path in _python_files():
        rel = str(path.relative_to(ROOT))
        if rel in GRANDFATHERED:
            continue
        n = _line_count(path)
        if n > HARD_LIMIT:
            offenders.append(f"{rel}: {n} 行（上限 {HARD_LIMIT}）")
    assert not offenders, (
        "文件超过 1000 行且未登记。请先拆模块（见 route_* / search_rank 的做法），"
        "确有必要登记时在 GRANDFATHERED 里写明理由：\n  " + "\n  ".join(offenders))


def test_grandfathered_files_do_not_grow():
    """祖父文件只能减不能增——增长必须显式改这个文件里的数字。"""
    grown = []
    for rel, (ceiling, _reason) in GRANDFATHERED.items():
        path = ROOT / rel
        if not path.is_file():
            grown.append(f"{rel}: 文件不存在（拆走或改名后请删掉本条）")
            continue
        n = _line_count(path)
        if n > ceiling:
            grown.append(f"{rel}: {n} 行 > 登记上限 {ceiling}")
    assert not grown, (
        "祖父文件变大了。正解是拆模块并把上限调低；确要上调请在 GRANDFATHERED "
        "里改数字并说明理由：\n  " + "\n  ".join(grown))


def test_ceilings_are_not_inflated():
    """上限不得高于实际行数：清单一旦虚高，门禁就形同虚设。"""
    inflated = []
    for rel, (ceiling, _reason) in GRANDFATHERED.items():
        path = ROOT / rel
        if not path.is_file():
            continue
        n = _line_count(path)
        if ceiling > n:
            inflated.append(f"{rel}: 上限 {ceiling} > 实际 {n}（请下调到 {n}）")
    assert not inflated, (
        "祖父清单的上限比实际行数高——把上限收到实际行数：\n  " + "\n  ".join(inflated))


@pytest.mark.parametrize("rel", sorted(GRANDFATHERED))
def test_every_grandfathered_entry_has_a_reason(rel):
    """理由不是可选项：没有理由的登记会在下一次审查里被当成噪声删掉。"""
    _ceiling, reason = GRANDFATHERED[rel]
    assert len(reason.strip()) >= 12, f"{rel} 的 reason 太短，写清楚卡在哪"
