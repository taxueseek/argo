#!/usr/bin/env python3
"""serp_guard — HTML SERP 垃圾结果守卫（判定规则单点）。

事故背景（dsh-free-search issue #38/#35 同款事故，argo 的 local_* 引擎同样
暴露）：html 引擎解析的是无 API 的网页 SERP，上游偶发把缓存 SERP / 与查询
完全无关的结果页当正常页返回，解析层照常抽出标题摘要，垃圾条目静默混进
聚合，调用方把「答非所问」当成「网上没有」——失败伪装成功是红线，这类
混入是它的隐形变体。

裁决口径（全部量化，无模糊地带）：

  1. query token：CJK 连续段取相邻二元组、拉丁/数字连续段取小写词。
     token 少于 2 个时恒 False（无裁决权）——单 token 的巧合重叠率太高，
     拦了误杀大于漏放。单字 CJK 查询（如「猫」）只有 0 个二元组，双字
     CJK 查询（如「猫粮」）只有 1 个，都天然落在无裁决区。
  2. results 只取前 5 条；每条取 title+snippet。URL 不参与：缓存/跳转
     链接的 URL 常回显 query 参数（uddg=、q=），会正好把垃圾页放行，
     标题+摘要才是「这页在答什么」的信号。
  3. 前 5 条全部与 query token 零交集 → 整页判垃圾（True）。

为什么调用方对判垃圾应整页丢弃而不是过滤后返回幸存者：前 5 条全无关说明
这一页整体答非所问（缓存页/错路由），幸存的一两条是同一页的噪声，过滤
返回等于给垃圾留门，宁缺勿假。调用方（engines_base）判垃圾后返回 [] 且
不记 note_failure——结果无关不是引擎故障，不该进熔断归因。

ARGO_SERP_GUARD=0 整体关闭守卫（逃生门：规则误杀时第一反应是关闸复现，
而不是删代码）。

不 import search_rank：本模块处在 engines_base 的冷启动热路径上，search_rank
较重且有环风险；分词口径按其 _tokens/_bigrams 的语义在本模块最小复刻
（CJK 二元组 + 小写拉丁词），不共享代码。
"""

from __future__ import annotations

import os
import re

# CJK 连续段 / 小写拉丁数字词（findall 前文本已 lower()）
_RUN_RE = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9]+")
_GUARD_WINDOW = 5      # 只看前 5 条：SERP 的「答非所问」看头部即可下结论
_MIN_QUERY_TOKENS = 2  # query token 少于此数 → 无裁决权，恒不拦
_SWITCH_ENV = "ARGO_SERP_GUARD"


def guard_enabled() -> bool:
    """逃生门：仅 ARGO_SERP_GUARD=0 关闭，其余取值（含未设置）均开启。"""
    return os.environ.get(_SWITCH_ENV, "") != "0"


def _extract_tokens(text: str) -> set[str]:
    """提取 token：CJK 相邻二元组 + 小写拉丁/数字词（与 search_rank 同口径）。"""
    tokens: set[str] = set()
    for run in _RUN_RE.findall((text or "").lower()):
        if "\u4e00" <= run[0] <= "\u9fff":
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
        else:
            tokens.add(run)
    return tokens


def _result_text(item: object) -> str:
    """单条结果的相关性文本 = title + snippet；非 dict 或字段缺失按空处理。"""
    if not isinstance(item, dict):
        return ""
    return f"{item.get('title') or ''} {item.get('snippet') or ''}"


def is_junk_serp(query: str, results: list[dict]) -> bool:
    """判一页 SERP 结果是否整体答非所问（口径见模块 docstring）。

    返回 True 表示整页判垃圾，调用方应丢弃全部条目；返回 False 表示不拦
    （含 query 无裁决权、results 为空、守卫被关闭——三种都不拦）。
    """
    if not guard_enabled():
        return False
    q_tokens = _extract_tokens(query if isinstance(query, str) else "")
    if len(q_tokens) < _MIN_QUERY_TOKENS:
        return False
    if not isinstance(results, list):
        return False
    head = [r for r in results[:_GUARD_WINDOW] if r is not None]
    if not head:
        return False
    for item in head:
        if _extract_tokens(_result_text(item)) & q_tokens:
            return False
    return True
