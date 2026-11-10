#!/usr/bin/env python3
"""批次十六构建器：独立搜索 / 数据集 / 学术论文（2026-11-10）。

  mwmbl       Mwmbl 开源非盈利独立搜索引擎（JSON API，免 key）
  kaggle      Kaggle 数据集搜索（免认证，含许可证/大小/版本）
  hf_papers   Hugging Face Papers 论文检索（免认证，含 upvotes/作者）

为什么放 batch 而不是 engines_builders_data：data 是登记上限 2923 的
祖父文件，新引擎的下一站按既有惯例是 batch 模块（同 batch13/14/15）。
三源均经实测验证（health + quality 准入通过）后入库。

接口事实（2026-11-10 本机实测，非文档抄录）：

  Mwmbl: GET https://mwmbl.org/api/v1/search/?s={q}&limit={n}
    返回 [{"url","title":[{"value","is_bold"}],"extract":[{"value","is_bold"}],"source"}]
    title/extract 是 {value,is_bold} 数组，需拍平为字符串。
    与 Marginalia/Wiby/Lieu/Mojeek 同族但索引互补（更偏技术社区与个人站）。

  Kaggle: GET https://www.kaggle.com/api/v1/datasets/list?search={q}&page=1
    返回 [{"titleNullable","subtitleNullable","urlNullable","licenseNameNullable",
           "totalBytesNullable","usabilityRatingNullable","ref",...}]
    字段名带 Nullable 后缀（Kaggle API 的 Java 序列化遗留）。

  HF Papers: GET https://huggingface.co/api/papers/search?q={q}&limit={n}
    返回 [{"paper":{"id","title","authors","publishedAt","summary","upvotes"},
           "title","publishedAt","summary","upvotes",...}]
    注意：/api/papers?search= 不过滤（只返回最新），必须用 /api/papers/search?q=。
    与 arXiv/OpenAlex 互补：HF Papers 有社交信号（upvotes）与 AI 论文摘要。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

from engines_base import http_open, rank_score, safe_search

logger = logging.getLogger("unified_search.engines")

_UA = "argo-search/2.6 (unified-search@local)"


def _flatten_segments(segments: Any) -> str:
    """把 Mwmbl 的 [{value, is_bold}] 数组拍平成纯文本。"""
    if not isinstance(segments, list):
        return ""
    return "".join(
        str(seg.get("value", "")) for seg in segments if isinstance(seg, dict)
    ).strip()


def _build_mwmbl_engine(spec: dict[str, Any]) -> Any:
    """Mwmbl 开源非盈利独立搜索引擎（JSON API，免认证）。

    与 Marginalia/Wiby/Lieu/Mojeek 同族但索引互补：Mwmbl 更偏技术社区与个人站。
    上游返回的 title/extract 是 {value, is_bold} 数组，需拍平。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://mwmbl.org/api/v1/search/?s=" + urllib.parse.quote(query) + "&limit=" + str(max(1, min(int(n or 5), 50)))
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": _UA}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Mwmbl 失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        results = []
        for _rk, r in enumerate(data[:n]):
            if not isinstance(r, dict):
                continue
            title = _flatten_segments(r.get("title"))
            extract = _flatten_segments(r.get("extract"))
            url_ = r.get("url") or ""
            if not title and not url_:
                continue
            results.append({
                "title": title[:200] or url_[:200],
                "url": url_,
                "snippet": extract[:300],
                "source": "mwmbl",
                "score": rank_score(0.7, _rk),
            })
        return results
    return _engine


def _build_kaggle_engine(spec: dict[str, Any]) -> Any:
    """Kaggle 数据集搜索（免认证，结构化元数据）。

    与 DataCite/Zenodo 互补：Kaggle 是社区数据集，含许可证/大小/版本/URL。
    上游字段名带 Nullable 后缀（Kaggle API 的 Java 序列化遗留）。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://www.kaggle.com/api/v1/datasets/list?search=" + urllib.parse.quote(query) + "&page=1"
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": _UA}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Kaggle 失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        results = []
        for _rk, r in enumerate(data[:n]):
            if not isinstance(r, dict):
                continue
            title = r.get("titleNullable") or r.get("title") or ""
            url_ = r.get("urlNullable") or r.get("url") or ""
            if not title and not url_:
                continue
            subtitle = r.get("subtitleNullable") or ""
            license_ = r.get("licenseNameNullable") or ""
            size = r.get("totalBytesNullable")
            parts = [p for p in (license_, f"{size} bytes" if size else "") if p]
            snippet = (subtitle[:200] + " · " + " · ".join(parts)) if parts else subtitle[:300]
            results.append({
                "title": title[:200],
                "url": url_,
                "snippet": snippet,
                "source": "kaggle",
                "score": rank_score(0.7, _rk),
            })
        return results
    return _engine


def _build_hf_papers_engine(spec: dict[str, Any]) -> Any:
    """Hugging Face Papers 论文检索（免认证，含 upvotes/作者/发布日期）。

    注意：/api/papers?search= 不过滤（只返回最新），必须用 /api/papers/search?q=。
    与 arXiv/OpenAlex 互补：HF Papers 有社交信号（upvotes）与 AI 论文摘要。
    """
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://huggingface.co/api/papers/search?q=" + urllib.parse.quote(query) + "&limit=" + str(max(1, min(int(n or 5), 20)))
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": _UA}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"HF Papers 失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        results = []
        for _rk, r in enumerate(data[:n]):
            if not isinstance(r, dict):
                continue
            paper = r.get("paper") or {}
            pid = paper.get("id") or r.get("id") or ""
            title = paper.get("title") or r.get("title") or ""
            url_ = f"https://huggingface.co/papers/{pid}" if pid else ""
            if not title and not url_:
                continue
            authors = [a.get("name", "") for a in (paper.get("authors") or []) if isinstance(a, dict)][:3]
            published = paper.get("publishedAt") or r.get("publishedAt") or ""
            upvotes = paper.get("upvotes") or r.get("upvotes") or 0
            summary = paper.get("summary") or r.get("summary") or ""
            parts = [p for p in (published[:10], f"upvotes: {upvotes}" if upvotes else "", ", ".join(authors) if authors else "") if p]
            snippet = (summary[:200] + " · " + " · ".join(parts)) if parts else summary[:300]
            results.append({
                "title": title[:200],
                "url": url_,
                "snippet": snippet,
                "source": "hf_papers",
                "score": rank_score(0.7, _rk),
            })
        return results
    return _engine
