#!/usr/bin/env python3
"""route_primary.py — primary 可执行性顺位与域否决查询。

## 为什么单独成模块

`route.py` 已压着 1000 行硬限，而这两件事都是**独立的判定**，与路由主干的
装配流程无关：

  1. `promote_executable_primary`：combo 定稿后把 primary 顺位到第一个
     「路由与执行都判可用」的引擎；
  2. `domain_vetoed_engines` / `vetoes_apply`：把域规则的 `unless` 否决
     事实交给 TF-IDF 注入层。

放在这里，route.py 只留两个调用点。
"""

from __future__ import annotations

from typing import Any

from config import get_engines
from route_combo import is_engine_routable


def promote_executable_primary(combo: list[str], specs: dict | None = None,
                               enabled: set[str] | None = None
                               ) -> tuple[list[str], str | None]:
    """把 primary 顺位到第一个「路由与执行都判可用」的引擎。

    路由准入与执行准入此前不是同一套判据：`is_engine_routable` 查
    enabled/依赖/熔断/配额/死源/语言，而执行层的 `engine_status` 另算
    dep_ready——两者在 2026-11-02 才对齐。即便对齐后仍有一个缺口：
    `is_engine_routable` 默认 check_deps=True，但域装配早期
    （_get_engines_combo 与 policy 截断）不看依赖，于是 xiaohongshu 这类缺
    CLI 的引擎能一路占住首位，实际交付由 fallback 接管，reason 却写着
    「→ 小红书」。

    顺位不改写 combo 内容，只把「执行不了」的成员换到后面；全组都不可执行
    时保持原序（fail-open，由执行层报错而不是路由层静默换源）。
    """
    if not combo:
        return combo, None
    # 必须自己取 specs：route 的 _specs_snapshot() 只读已加载的 engines 模块，
    # 路由路径下它恒为空表，依赖判据会因此对全部引擎失效。
    if specs is None:
        try:
            specs = get_engines()
        except Exception:
            specs = {}
    if is_engine_routable(combo[0], enabled=enabled, specs=specs):
        return combo, None
    for cand in combo[1:]:
        if is_engine_routable(cand, enabled=enabled, specs=specs):
            rest = [e for e in combo if e != cand]
            return [cand] + rest, combo[0]
    return combo, None


def domain_vetoed_engines(query: str,
                          domains: list[dict[str, Any]] | None = None) -> set[str]:
    """被域规则显式否决的引擎集合（供 TF-IDF 注入层不再复活）。

    域规则与 TF-IDF 选源此前是两条互不知情的路径：域规则用 `unless` 否决了
    某个意图（例如 academic 的「查重/降重」写作语境），TF-IDF 却可以按词面
    高分把该域的主源重新注入首位——实测「怎么写论文 查重 降重」域判到
    chinese_general、reason 仍写「[TF-IDF→cnii]」，primary 变成日文学术源。
    把「否决」这个事实交给注入层，让词面分不能覆盖域规则的语义判断。

    判据：某域**本可命中**（match 成立）但因 unless 被否决时，该域声明的
    引擎即视为被否决。与 match_domains 共用 `_ensure_domain_compiled`。
    """
    if domains is None:
        from route_domains import get_domains as _gd, _get_compiled_domains, \
            _ensure_domain_compiled
        domains = _gd()
    else:
        from route_domains import _get_compiled_domains, _ensure_domain_compiled
    compiled = _get_compiled_domains(domains)
    vetoed: set[str] = set()
    for domain in compiled:
        for match_re, unless_re in _ensure_domain_compiled(domain):
            if unless_re is not None and unless_re.search(query):
                if match_re.search(query):
                    vetoed.update(domain.get("engines_combo") or [])
                    vetoed.update([domain["primary"]] if domain.get("primary") else [])
                break
    return vetoed


def vetoes_apply(query: str, domains: list[dict[str, Any]] | None = None,
                 hard_domain: bool = False) -> bool:
    """本次路由是否处在「域规则已否决」状态。

    只有**没有实域命中**时才让否决生效：零实域命中说明所有域要么未匹配、
    要么被 unless 否决，此时 TF-IDF 注入是唯一能改变主源的力量，必须受
    域规则约束。一旦已有实域命中，该域自身的引擎选择是声明式的，注入只作
    辅源，不否决。
    """
    if hard_domain:
        return False
    return bool(domain_vetoed_engines(query, domains))
