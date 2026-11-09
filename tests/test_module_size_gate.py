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

# 祖父清单：路径 → (上限行数, 为什么现在不拆)
#
# 只记数字和一句当前理由。每次升降的经过写在提交信息里，用
# `git log -p -- tests/test_module_size_gate.py` 查，不再堆在这里
#（2026-11-08 前说明文字两周从 10.6k 字涨到 14.3k 字，清单本身成了流水账）。
GRANDFATHERED: dict[str, tuple[int, str]] = {
    "scripts/engines_builders_cn.py": (2283, "中文源构建器，逐源一段；下一步按主题拆（同 engines_builders_data 2026-11-08 的做法）"),
    "scripts/engines_builders_batch9.py": (1476, "批次九源构建器，逐源一段；下一步按主题拆"),
    "scripts/engines_builders_intl.py": (1242, "国际源构建器，含各源 XML/RSS 解析差异；下一步按地区拆"),
    "scripts/engines_builders_tech.py": (1281, "技术社区源构建器，逐源一段；下一步按主题拆"),
    "scripts/fetch_v3.py": (1905, "抓取降级链，每级要保留顺序与超时语义；拆分候选：article_meta 接线段"),
    "scripts/engines_base.py": (1564, "引擎基类 + HTTP 出口 + 输出映射，与全部源声明的字段契约绑在一起"),
    "scripts/cache.py": (1373, "存储实现与门面；键、命中判据、准入策略已分别拆到 cache_key_vdom / cache_similarity / cache_guard"),
    "scripts/http_client.py": (1035, "UA 轮换 + Cookie + 重试 + 主机节流共用一个会话对象"),
    "scripts/search_rank.py": (983, "RRF 融合 + 去重 + 五维重排；拆分候选：降权分支"),
    "scripts/job.py": (1150, "招聘多平台聚合，各平台解析各成一段（数据表性质）"),
    "scripts/matrix_search_eval.py": (1313, "离线路由矩阵，用例表占多数"),
    "scripts/route.py": (1010, "路由决策主干（route_query + 三个判定器）"),
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
