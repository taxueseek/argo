#!/usr/bin/env python3
"""DSH 插件目录构建器 —— DeepSeek Harness 插件生态检索。

三个 DSH 插件数据源的形态各不相同，接入方式也分三档：

  · deepseek1024（13855 条）/ dsh_hub（18933 条）—— 标准搜索 API，
    走 L1 声明式（`engines/specs/deepseek1024.yaml`、`engines/specs/dsh_hub.yaml`），
    本文件不含它们的实现。
  · dshopen（894 条精选）—— **无搜索 API**：`/index.json` 是静态全量目录，
    任何 query 参数都返回同一份 1.3MB 文件（实测 `/index.json?query=x` 与不带参数
    逐字节相同）。这类「全量目录 + 本地匹配」没有声明式表达方式，故落到 L3。

为什么 dshopen 值得单独一个 builder：它是四个候选源里**精选质量最高**的一个
（894 条零空标题/零空描述、53 条 verified 标签、labels 体系规范、601 条带
`engines.dsh` 版本约束），且与 deepseek1024 只重叠 562 条——两个源是互补而非
替代。它缺的是 API，不是数据。

缓存策略（为什么不用 ETag）：上游支持 ETag，但 `http_open` 会把 304 归因为
「HTTP 失败」（非 2xx 一律走 `_note_http_failure`），一次正常的缓存命中会在
失败寄存器里留一条假记录。改用 TTL 缓存绕开：1.3MB 全量、每天重建，12h TTL
意味着最多半天延迟，比污染归因划算。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from engines_http import UA
from engines_base import safe_search, http_open, rank_score

# 上游：静态全量目录。Cloudflare 前置，支持 ETag（本 builder 刻意不用，见文件头）。
_DSHOPEN_INDEX_URL = "https://dshopen.org/index.json"

# 12h。dshopen 每天全量重建（sitemap 的 lastmod 全为当天），半天延迟可接受；
# 再短就会让「同一天多次搜索」反复拉 1.3MB。
_CACHE_TTL = 12 * 3600

_CACHE_FILE = "dshopen_index.json"
_META_FILE = "dshopen_index.meta.json"

# 字段权重：名字最重（用户找的往往是包名），中文描述次之，分类与英文再次。
# 与 find_dsh_plugin.py 的 FIELD_WEIGHT 同一口径，便于两处结果互相对照。
_FIELD_WEIGHTS: dict[str, float] = {
    "name": 3.0,
    "title_zh": 2.0,
    "title_en": 1.5,
    "desc_zh": 1.5,
    "desc_en": 1.0,
    "keywords": 1.2,
    "category": 0.8,
}

# 引擎档位分（rank_score 的 base）：dshopen 是精选源、覆盖最小，档位低于
# 通用源但高于长尾垂直源——它「条条可信」的优势要能压过「只有 894 条」的劣势。
_BASE_SCORE = 0.75

# 上游把星数写进了描述开头（881/894 条形如「★ 15 │ 分层蒸馏记忆：…」），
# 方便它自己的卡片渲染。搜素结果里星数已有独立字段，这个前缀只剩噪音，剥掉。
# 星号可能是 ★ 或 *，数字含千分位与小数（如 ★ 89,165 │）。
_STAR_PREFIX_RE = re.compile(r"^[★*]\s*\d[\d,\.]*\s*│\s*")

# dshopen 的 11 个分类 → 中文展示名（取自上游 metadata.categories 的 zh 标题）。
# 用于摘要前缀，让用户一眼看出「这是哪类插件」。
_CATEGORY_ZH: dict[str, str] = {
    "interface": "界面",
    "tools": "工具",
    "skills": "技能",
    "memory": "记忆",
    "providers": "模型供应商",
    "usage": "用量与费用",
    "integrations": "集成",
    "appearance": "外观",
    "workflow": "工作流与协作",
    "development": "开发与运维",
    "knowledge": "知识",
}


def _cache_dir() -> Path:
    """缓存目录。优先走 argo_paths（与 cache.db 同根），失败回落 ~/.cache。"""
    try:
        from argo_paths import ensure_state_dir

        return ensure_state_dir("engines")
    except Exception:
        return Path.home() / ".cache" / "unified-search" / "engines"


def _load_plugins(timeout: float) -> list[dict[str, Any]]:
    """读 dshopen 全量目录：缓存新鲜直接用，否则拉取并落盘。

    任何一步失败都返回已缓存的那份（哪怕过期）——目录搜索是「有旧数据胜过
    没数据」的场景，上游抖动不该让整个引擎空转。
    """
    cdir = _cache_dir()
    cache_file = cdir / _CACHE_FILE
    meta_file = cdir / _META_FILE

    stale: list[dict[str, Any]] | None = None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        plugins = data.get("plugins")
        if isinstance(plugins, list):
            stale = plugins
            if time.time() - float(meta.get("ts") or 0) < _CACHE_TTL:
                return plugins
    except Exception:
        stale = None

    try:
        req = urllib.request.Request(
            _DSHOPEN_INDEX_URL,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        with http_open(req, timeout=timeout, engine="dshopen") as resp:
            payload = json.loads(resp.read())
        plugins = payload.get("plugins") if isinstance(payload, dict) else None
        if not isinstance(plugins, list):
            return stale or []
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            meta_file.write_text(
                json.dumps({"ts": time.time()}), encoding="utf-8"
            )
        except OSError:
            pass  # 只读挂载 / 权限受限：内存里这份仍然可用
        return plugins
    except Exception:
        return stale or []


def _entry_fields(entry: dict[str, Any]) -> dict[str, str]:
    """摊平成待匹配文本。title/description 是 {en, zh} 双语对象，缺失侧留空。"""
    meta = entry.get("metadata") or {}
    title = entry.get("title") or {}
    desc = entry.get("description") or {}
    return {
        "name": entry.get("name") or "",
        "title_zh": title.get("zh") or "",
        "title_en": title.get("en") or "",
        "desc_zh": desc.get("zh") or "",
        "desc_en": desc.get("en") or "",
        "keywords": " ".join(meta.get("keywords") or []),
        "category": meta.get("category") or "",
    }


def _tokens(query: str) -> list[str]:
    """查询切分：英文按词（≥2 字符）、中文按 2 字以上片段，去重保序。

    英文单词不切单字符——`a`/`x` 这类在包名里无处不在，切了只会把不相关的
    条目全捞进来。中文同理：单字噪音大，2 字片段是「记忆」「插件」这类词的
    自然粒度。整段中文（如「后台任务」）作为一个 token 保留，比切成二字
    滑窗更准。
    """
    q = (query or "").lower().strip()
    if not q:
        return []
    toks: list[str] = []
    for w in re.findall(r"[a-z0-9]+", q):
        if len(w) >= 2:
            toks.append(w)
    for seg in re.findall(r"[一-鿿]{2,}", q):
        toks.append(seg)
    if not toks:
        toks = re.findall(r"[一-鿿]", q)
    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _build_idf(plugins: list[dict[str, Any]], tokens: list[str]) -> dict[str, float]:
    """IDF：罕见 token 权重大，满语料 token 权重小。

    为什么必须有：dshopen 的 894 条里几乎每条都含「dsh」（包名前缀），
    纯布尔命中下「dsh」对**所有**条目同加 3.0 分——区分度为零，top-10
    会退化成「按星数排」。实测查询「dsh 插件 记忆」有 887/894 条命中，
    真正相关的「记忆」类条目排不上来。IDF 把这类满语料词压到接近 0。
    """
    n = len(plugins) or 1
    idf: dict[str, float] = {}
    for tok in tokens:
        hits = 0
        for entry in plugins:
            fields = _entry_fields(entry)
            if any(tok in (v or "").lower() for v in fields.values()):
                hits += 1
                if hits >= n:  # 满语料：不必再数
                    break
        # +1 平滑：避免 log(0)；满语料词得 ≈1.0（不是 0，仍保留微弱信号）
        idf[tok] = math.log((n + 1) / (hits + 1)) + 1.0
    return idf


def _score(
    fields: dict[str, str], tokens: list[str], idf: dict[str, float]
) -> float:
    """命中字段权重 × token IDF 求和。未命中任何字段的条目直接淘汰。"""
    lowered = {k: v.lower() for k, v in fields.items()}
    total = 0.0
    for tok in tokens:
        w_tok = idf.get(tok, 1.0)
        for field, weight in _FIELD_WEIGHTS.items():
            if tok in lowered.get(field, ""):
                total += weight * w_tok
    return total


def _star_boost(entry: dict[str, Any]) -> float:
    """星数加权：log 尺度，避免大仓碾压。

    注意 dshopen 的星数有**父仓污染**：19 个多包仓库（58 个条目，占 6.5%）取的是
    父仓星数（如 @linxin666/* 显示 9511 星，repo 实为 zhu1090093659/dsh-web）。
    这个偏差无法在索引侧修正，只能在解读时留意——加权幅度因此压得很低
    （log1p/10，0 星与 1 万星只差约 0.92 倍）。
    """
    meta = entry.get("metadata") or {}
    stars = (meta.get("sortKeys") or {}).get("github-stars") or 0
    try:
        stars = float(stars)
    except (TypeError, ValueError):
        return 1.0
    if stars <= 0:
        return 1.0
    return 1.0 + math.log1p(stars) / 10.0


def _iso(epoch: Any) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError, OSError):
        return ""


def _build_dshopen_engine(spec: dict[str, Any]) -> Any:
    """dshopen 插件目录（静态全量索引 + 本地匹配）。"""
    timeout = float(spec.get("timeout", 15))

    @safe_search
    def _engine(
        query: str, n: int = 5, _timeout: float | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        to = _timeout or timeout
        tokens = _tokens(query)
        if not tokens:
            return []
        plugins = _load_plugins(to)

        idf = _build_idf(plugins, tokens)
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in plugins:
            if not isinstance(entry, dict):
                continue
            s = _score(_entry_fields(entry), tokens, idf)
            if s <= 0:
                continue
            scored.append((s * _star_boost(entry), entry))

        # 稳定排序：同分按包名字典序，避免同批查询的结果顺序抖动
        scored.sort(key=lambda x: (-x[0], (x[1].get("name") or "")))

        results: list[dict[str, Any]] = []
        for rank, (_, entry) in enumerate(scored[: max(1, n)]):
            repo = (entry.get("repository") or {}).get("url") or ""
            url = repo or entry.get("homepage") or ""
            if not url:
                continue
            meta = entry.get("metadata") or {}
            desc = (entry.get("description") or {}).get("zh") or ""
            desc = _STAR_PREFIX_RE.sub("", desc).strip()
            # 摘要前缀带分类。两个理由：一是给用户看「这是哪类插件」；二是
            # 五维精排的「相关性」维度比对摘要与查询——上游摘要只写功能
            # （「为 DSH 接入记忆服务」），不含「插件」二字，中文查询
            # 「dsh 插件 记忆」的 CJK 二元组匹配不上，实测同批条目会输给
            # 通用源的文章标题（那边标题恰好含「DSH 记忆插件」全词）。
            # 前缀是真实分类信息，不是凑词。
            cat = _CATEGORY_ZH.get(meta.get("category") or "", "")
            if cat:
                desc = f"DSH 插件（{cat}）：{desc}" if desc else f"DSH 插件（{cat}）"
            versions = entry.get("versions") or []
            published = versions[0].get("publishTimestamp") if versions else None
            # 标题取中文展示名（title.zh），不是 npm 包名。原因：五维精排的
            # 「相关性」维度比对标题与查询，中文查询（「dsh 记忆插件」）对上
            # 中文标题（「OpenViking 记忆」）得分高，对上英文包名
            # （@openviking/dsh-memory-plugin）几乎不得分——实测同一批条目
            # 因标题形态不同，最终 top-6 全被通用源占据。包名仍保留在
            # upstream（install 的权威答案）与 metadata 里，不丢。
            title_obj = entry.get("title") or {}
            title = (
                title_obj.get("zh")
                or title_obj.get("en")
                or entry.get("name")
                or ""
            )
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": desc[:300],
                    "source": "dshopen",
                    "score": rank_score(_BASE_SCORE, rank),
                    "published_at": _iso(published),
                    # 可验证出处：npm 包名是「装什么」的权威答案
                    "upstream": entry.get("name") or "",
                    "metadata": {
                        "category": meta.get("category") or "",
                        "labels": meta.get("labels") or [],
                        "stars": (meta.get("sortKeys") or {}).get("github-stars") or 0,
                        "engines_dsh": (
                            (versions[0].get("engines") or {}).get("dsh") if versions else ""
                        ) or "",
                    },
                }
            )
        return results

    return _engine
