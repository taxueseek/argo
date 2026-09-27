#!/usr/bin/env python3
"""v2ex_nodes.py — V2EX 节点路由：把「全站热帖过滤」升级为「相关节点内检索」。

## 问题（第一批的遗留）

第一批把 V2EX 引擎从「抓 /search 页产出幻觉」改成「hot+latest 候选池 + 本地
相关性过滤」。但候选池只有 10+10=20 条**全站热帖**，与查询无关，导致长尾
查询必然落空（实测 MacBook/招聘 返回 0 条）。

当时判定「官方 API 无搜索端点，只能这样」——**这个退让是错的**。

## 重新定义

官方 API 确实没有搜索，但有 `show.json?node_name=X&page=N`：**按节点取帖**。
实测 V2EX 有 1376 个节点，其中 1246 个有内容，节点名 99% 是英文实体词
（python/docker/iphone/paper…）。于是真实问题不是「如何搜索」，而是：

    「如何把用户查询映射到节点空间」

## 为什么是倒排而不是模糊匹配

实测：节点名 99% 是英文实体词，且节点自带 title（中文标题）与 header
（语义描述）。所以 `查询词元 ∩ 节点名` 就是**精确命中**——不需要算相似度、
不需要 embedding。这消除了「准确 vs 通用」的假两难。

## 三层递进（每层失败即降级，不阻断）

  L1 精确倒排       查询词元 ∩ 节点名            conf=1.0  零 API
  L2 同义/中英扩展   复用 argo query_synonyms_cn  conf=0.8  零 API
  L3 header 语义保底 L1/L2 全空时在描述里找词     conf=0.5  零 API

三层全空 = 该查询在 V2EX 确无对应资源，**诚实返回空**，由调用方降级到
hot/latest 保底（而不是伪造结果）。

## 成本

节点表 1376 条 ≈ 200KB，变化极慢 → 本地缓存 24h。匹配阶段**零 API 调用**。
取帖按 Top-K 节点并发，K=3 时占用配额 3/600 = 0.5%。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from cjk_tokens import cjk_term_grams

# 节点表缓存
_CACHE_TTL = 24 * 3600          # 24h：节点表变化极慢
_CACHE_FILE = "v2ex_nodes_cache.json"

# 单字/双字节点名不参与匹配：实测 "c"/"io"/"u"/"in" 等单字节点会造成噪声命中。
# 这是**格式约束**（英文单字几乎必然误命中任意查询词元），不是质量判断。
MIN_NODE_NAME_LEN = 3

# 注意：这里曾尝试加「节点热度门槛」（topics >= N）过滤冷门节点，已废弃。
# 实测数据否定了这类「节点固有属性」判据：
#   · 节点主题数中位数仅 15，一半节点 <15 —— 热度不存在自然分界
#   · u(大学)/in(分享邀请码)/pro(PRO) 都「名实相符」—— 一致性也不能区分
# 根本原因：节点好坏不是固有属性，而是「节点 × 查询」的联合属性。
# 同一个 paper 节点，查「论文」是好节点、查「数据库」是坏节点。
# 正确做法是把判据后移到「结果」：路由只给候选，好坏由既有相关性过滤
# （_term_hits）决定——命中冷门节点则池小、过滤后为空、诚实返回；
# 命中热门节点则池大、过滤后命中。分工清晰，无需在路由层设阈值。

# 默认取几个节点的帖子（并发）。3 个 = 30 条候选，配额占用 0.5%
DEFAULT_TOP_K = 3

# 节点表完整性下限：低于此数视为「上游半截响应/解析异常」，不写缓存。
# V2EX 实际 1300+ 节点，测试 fixture 通常只有几条，用它区分真假数据。
MIN_NODE_TABLE_SIZE = 100

_lock = threading.Lock()
_mem_cache: dict[str, Any] = {}


def _cache_path() -> Path:
    try:
        import argo_paths
        return argo_paths.state_path(_CACHE_FILE)
    except Exception:
        return Path.home() / ".cache" / "unified-search" / _CACHE_FILE


def _load_cached_nodes() -> list[dict] | None:
    """读本地节点表缓存；过期或损坏返回 None。"""
    p = _cache_path()
    try:
        st = p.stat()
    except OSError:
        return None
    if time.time() - st.st_mtime > _CACHE_TTL:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if isinstance(nodes, list) and nodes:
            return nodes
    except Exception:
        pass
    return None


def _save_nodes(nodes: list[dict]) -> None:
    """写节点表缓存。

    防御：节点数低于完整性下限时不写。V2EX 实际有 1300+ 节点，若某次
    只解析出极少数（上游半截响应 / 测试注入的 fixture / 解析逻辑变更），
    写进缓存会把不完整数据固化 24h —— 实测曾因测试写入 1 个假节点，
    导致后续所有真实查询路由失败（缓存里只有 python 一个节点）。
    """
    if len(nodes) < MIN_NODE_TABLE_SIZE:
        return
    p = _cache_path()
    try:
        # 原子写走唯一来源（唯一 tmp 名）——旧实现固定 `.tmp` 名，
        # 并发进程互相搬走临时文件导致写失败。
        import argo_paths as _paths
        _paths.atomic_write_json(p, {"nodes": nodes}, indent=None)
    except Exception:
        pass  # 缓存是加速层，写失败不影响功能


def fetch_all_nodes(fetcher=None, engine: str = "v2ex") -> list[dict]:
    """取全节点表（带 24h 本地缓存 + 进程内缓存）。

    fetcher(url) -> raw text，默认走 argo 统一 GET 出口。
    只保留有内容的节点（topics>0）：空节点取不到帖子，参与匹配无意义。
    engine：默认 fetcher 的归因归属（缺失会把失败记到伪引擎 "?" 名下）。
    """
    global _mem_cache
    with _lock:
        cached = _load_cached_nodes()
        if cached:
            return cached
        if _mem_cache.get("nodes"):
            return _mem_cache["nodes"]

    if fetcher is None:
        def fetcher(url):  # type: ignore
            from engines_base import _http_get_raw
            return _http_get_raw(url, {"Accept": "application/json"}, 10,
                                 engine=engine)

    try:
        raw = fetcher("https://www.v2ex.com/api/nodes/all.json")
        if not raw:
            return []
        data = json.loads(raw)
    except Exception:
        return []

    if not isinstance(data, list):
        return []
    nodes = [
        n for n in data
        if isinstance(n, dict) and n.get("name")
        and int(n.get("topics") or 0) > 0
    ]
    if nodes:
        _save_nodes(nodes)
    with _lock:
        _mem_cache["nodes"] = nodes
    return nodes


def _eligible(n: dict) -> bool:
    """节点是否可参与路由：仅要求名字长度达标。

    这是**格式约束**而非质量判断：英文单字节点（c/io/u/in）几乎必然
    误命中任意查询词元，属结构性噪声，故排除。
    刻意不做热度/名实一致等「固有属性」筛选——见文件头部说明，
    节点好坏是「节点 × 查询」的联合属性，路由层无法预判。
    """
    return len((n.get("name") or "")) >= MIN_NODE_NAME_LEN


def _tokenize(query: str) -> set[str]:
    """查询 → 拉丁词元集合（长度门槛见 MIN_NODE_NAME_LEN）。"""
    toks = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    return {t for t in toks if len(t) >= MIN_NODE_NAME_LEN}


def _expand_synonyms(query: str) -> set[str]:
    """复用 argo 已有的中英同义表做扩展（不新建维护面）。

    表在 backends/query_synonyms_cn.json，含 synonyms(中→多语) 与
    en_to_cn(英→中)。这里只取「中文意图 → 英文词面」方向，
    用于把「论文」扩成 paper、「教程」扩成 guide。
    """
    out: set[str] = set()
    q = (query or "").lower()
    if not q:
        return out
    try:
        p = Path(__file__).resolve().parent.parent / "backends" / "query_synonyms_cn.json"
    except Exception:
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return out
    syn = data.get("synonyms") or {}
    for cn, eqs in syn.items():
        if cn and cn in q and isinstance(eqs, list):
            for e in eqs:
                out.update(re.findall(r"[a-z0-9]+", str(e).lower()))
    return {t for t in out if len(t) >= MIN_NODE_NAME_LEN}


def pick_nodes(query: str, *, top_k: int = DEFAULT_TOP_K,
               nodes: list[dict] | None = None,
               fetcher=None) -> dict[str, Any]:
    """查询 → 节点列表（三层递进）。

    返回 {"nodes": [name...], "confidence": float, "layer": str, "scores": {...}}
    layer: exact / synonym / header / none
    无命中时返回 nodes=[] + layer="none"，调用方据此降级到 hot/latest。
    """
    nodes = nodes if nodes is not None else fetch_all_nodes(fetcher=fetcher)
    if not nodes:
        return {"nodes": [], "confidence": 0.0, "layer": "none", "scores": {}}

    q = (query or "").lower()
    terms = _tokenize(q)

    # ── L1 精确倒排：查询词元 ∩ 节点名 ────────────────────────────────
    hits: dict[str, float] = {}
    for n in nodes:
        if not _eligible(n):
            continue
        name = (n.get("name") or "").lower()
        if name in terms:
            # 节点名被查询直接引用 = 最强信号；热度作次级权重避免大节点垄断
            hits[name] = 10.0 + min(int(n.get("topics") or 0) / 100000, 1.0)
    if hits:
        ranked = sorted(hits.items(), key=lambda x: -x[1])
        return {"nodes": [n for n, _ in ranked[:top_k]], "confidence": 1.0,
                "layer": "exact", "scores": dict(ranked[:top_k])}

    # ── L2 同义/中英扩展后倒排 ────────────────────────────────────────
    expanded = terms | _expand_synonyms(q)
    if expanded != terms:
        for n in nodes:
            if not _eligible(n):
                continue
            name = (n.get("name") or "").lower()
            if name in expanded:
                hits[name] = 8.0 + min(int(n.get("topics") or 0) / 100000, 1.0)
        if hits:
            ranked = sorted(hits.items(), key=lambda x: -x[1])
            return {"nodes": [n for n, _ in ranked[:top_k]], "confidence": 0.8,
                    "layer": "synonym", "scores": dict(ranked[:top_k])}

    # ── L2b 中文标题反查 ──────────────────────────────────────────────
    # 节点的 title/header 是中文（jobs=「酷工作」、rent=「租房」、
    # shanghai=「上海」），可把中文查询意图反查到英文节点名。
    # 实测：「工作」→jobs/career/remote 有效；「招聘」→ 无（词面差异，
    # 此时诚实回落 L3/none，不硬凑）。
    # 要求 CJK 词长 ≥2，避免单字（「上」「大」）噪声；长串按 2-gram 展开
    # （cjk_tokens），否则「怎么找工作」整句当一个词，与「酷工作」永不交集。
    cjk_terms = set(cjk_term_grams(q))
    if cjk_terms:
        for n in nodes:
            if not _eligible(n):
                continue
            name = (n.get("name") or "").lower()
            blob = f"{n.get('title') or ''} {n.get('header') or ''}"
            hit = sum(1 for t in cjk_terms if t in blob)
            if hit:
                hits[name] = float(hit) * 2.0 + min(
                    int(n.get("topics") or 0) / 100000, 1.0)
        if hits:
            ranked = sorted(hits.items(), key=lambda x: -x[1])
            return {"nodes": [n for n, _ in ranked[:top_k]], "confidence": 0.7,
                    "layer": "cn_title", "scores": dict(ranked[:top_k])}

    # ── L3 header 语义保底（低置信）────────────────────────────────────
    # 只在描述文本里找词；要求词长 ≥4 以免通用短词误命中
    probe = {t for t in (terms | _expand_synonyms(q)) if len(t) >= 4}
    if probe:
        for n in nodes:
            if not _eligible(n):
                continue
            header = (n.get("header") or "").lower()
            title = (n.get("title") or "").lower()
            blob = f"{header} {title}"
            hit = sum(1 for t in probe if t in blob)
            if hit:
                name = (n.get("name") or "").lower()
                hits[name] = float(hit) + min(int(n.get("topics") or 0) / 100000, 1.0)
        if hits:
            ranked = sorted(hits.items(), key=lambda x: -x[1])
            return {"nodes": [n for n, _ in ranked[:top_k]], "confidence": 0.5,
                    "layer": "header", "scores": dict(ranked[:top_k])}

    return {"nodes": [], "confidence": 0.0, "layer": "none", "scores": {}}


def clear_cache() -> None:
    """清空内存与磁盘节点缓存（测试与强制刷新用）。"""
    with _lock:
        _mem_cache.clear()
    try:
        _cache_path().unlink(missing_ok=True)
    except Exception:
        pass
