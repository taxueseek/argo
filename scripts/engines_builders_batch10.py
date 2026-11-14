#!/usr/bin/env python3
"""批次十构建器：知乎开放平台引擎簇（zhihu_global / zhihu / zhihu_questions / zhihu_kb）。

为什么不在 engines_builders_cn.py 里加：该文件被 test_module_size_gate.py
祖父清单冻结在 2319 行（只能减不能增），故统一收敛在本模块维护（远低于
1000 行上限）。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from engine_env import get_env
from engines_base import (
    http_open,
    rank_score,
    safe_search,
)
from engines_builders_cn import _parse_site_filter
from social_engines import zhihu_engine as _ze


class _LazyLogger:
    """延迟创建 logger，避免 import logging 的重量级 import 链。"""
    _logger = None

    def __getattr__(self, name: str):
        if _LazyLogger._logger is None:
            import logging
            _LazyLogger._logger = logging.getLogger("unified_search.engines")
            if not _LazyLogger._logger.handlers:
                _LazyLogger._logger.setLevel(logging.WARNING)
                _LazyLogger._logger.addHandler(logging.StreamHandler(sys.stderr))
        return getattr(_LazyLogger._logger, name)


logger = _LazyLogger()

_ZHIHU_COUNT_MAX = 20  # global_search / question_recommendations 官方上限
_ZHIHU_KB_LIMIT_MAX = 10  # knowledge/search 官方上限
_ZHIHU_QUESTION_URL_RE = re.compile(r"(https?://(?:www\.)?zhihu\.com/question/\d+)")
_ZHIHU_QUESTION_ID_RE = re.compile(r"(?:^|\s)(?:q|question):(\d+)(?=\s|$)", re.I)
_KB_LIST_QUERY_RE = re.compile(r"^(?:bases?|list|知识库列表|我的知识库)(?::\s*(.*))?$", re.I)
_KB_SCOPE_OP_RE = re.compile(r"(?:^|\s)scope:(personal|subscription|public|all|created|subscribed)(?=\s|$)", re.I)
_KB_ID_OP_RE = re.compile(r"(?:^|\s)kb:(\d+)(?=\s|$)", re.I)
_DB_OPERATOR_RE = re.compile(r"(?:^|\s)db:(all|realtime|static)(?=\s|$)", re.I)


def _zhihu_epoch(value: Any) -> int | None:
    """ISO 时间窗 → 秒级时间戳；空/不可解析返回 None（不阻断检索）。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _parse_db_operator(query: str) -> tuple[str, str]:
    """解析 `db:realtime|static|all` 查询操作符，返回 (db, 剩余查询)。"""
    match = _DB_OPERATOR_RE.search(query or "")
    if not match:
        return "all", query
    db = match.group(1).lower()
    rest = _DB_OPERATOR_RE.sub(" ", query, count=1).strip()
    return db, rest or (query or "")


def _build_zhihu_publish_filter(since: Any, until: Any) -> str:
    """把 --since/--until 下推成官方 Filter（publish_time 支持 >=/<=）。"""
    since_ts = _zhihu_epoch(since)
    until_ts = _zhihu_epoch(until)
    parts = []
    if since_ts is not None:
        parts.append(f"publish_time>={since_ts}")
    if until_ts is not None:
        parts.append(f"publish_time<={until_ts}")
    return " AND ".join(parts)


def _zhihu_http_json(
    url: str,
    secret: str,
    source: str,
    timeout: float,
    engine_name: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """统一执行知乎开放平台 JSON 请求并收敛 HTTP/业务错误为 error item。"""
    headers = _ze.zhihu_headers(secret)
    data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with http_open(req, timeout=timeout, engine=engine_name) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, {"error": f"{source} API HTTP {e.code}", "source": source}
    except Exception as e:
        logger.warning(f"{source} 失败: {e}")
        return None, {"error": f"{source} {type(e).__name__}: {e}", "source": source}
    if not isinstance(payload, dict):
        return None, {"error": f"{source} 响应格式异常", "source": source}
    if payload.get("Code") not in (0, None):
        logger.warning(f"{source} 返回码异常: {payload.get('Code')} {payload.get('Message')}")
        return None, _ze.zhihu_error_item(source, payload.get("Code"), payload.get("Message"))
    return payload, None


# ── 1. zhihu_global：知乎开放平台全网搜索 ────────────────────────────────────

def _build_zhihu_global_engine(spec: dict[str, Any]) -> Any:
    """知乎开放平台全网搜索（developer.zhihu.com global_search）。"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []

        filter_expr, search_query = _parse_site_filter(query)
        db, search_query = _parse_db_operator(search_query or query)
        _time_filter = _build_zhihu_publish_filter(kwargs.get("since"), kwargs.get("until"))
        if _time_filter:
            filter_expr = f"{filter_expr} AND {_time_filter}" if filter_expr else _time_filter

        filtered = bool(filter_expr or db != "all")
        params: dict[str, Any] = {
            "Query": search_query or query,
            "Count": str(_ZHIHU_COUNT_MAX if filtered else min(int(n or 5), _ZHIHU_COUNT_MAX)),
            "SearchDB": db,
        }
        if filter_expr:
            params["Filter"] = filter_expr
        url = "https://developer.zhihu.com/api/v1/content/global_search?" + urllib.parse.urlencode(params)
        data, err = _zhihu_http_json(
            url, secret, "zhihu_global", to, spec.get("_name", "zhihu_global")
        )
        if err:
            return [err]
        items = ((data or {}).get("Data") or {}).get("Items") or []
        results: list[dict[str, Any]] = []
        for rk, item in enumerate(items[:n]):
            if not isinstance(item, dict):
                continue
            title = _ze.clean_zhihu_title(item.get("Title"))
            url_ = item.get("Url") or ""
            snippet = _ze.strip_html_tags(item.get("ContentText") or "")
            if not title and not url_:
                continue
            upstream_auth = _ze.parse_authority_level(item.get("AuthorityLevel"))
            badge_text = str(item.get("AuthorBadgeText") or "").strip()
            top_comments = _ze.extract_comment_snippets(item.get("CommentInfoList"))
            social_meta: dict[str, Any] = {
                "author": item.get("AuthorName") or "",
                "content_type": item.get("ContentType") or "",
                "vote_up": item.get("VoteUpCount") or 0,
                "comment_count": item.get("CommentCount") or 0,
                "authority_level": item.get("AuthorityLevel") or "",
                "edit_time": item.get("EditTime") or 0,
            }
            if badge_text:
                social_meta["author_badge_text"] = badge_text
            if top_comments:
                social_meta["top_comments"] = top_comments

            entry: dict[str, Any] = {
                "title": title[:200],
                "url": url_,
                "snippet": snippet[:300],
                "source": "zhihu_global",
                "score": rank_score(0.7, rk),
                "published_at": _ze.epoch_to_iso(item.get("EditTime")),
                "social_meta": social_meta,
            }
            # 只在有效 0-4 时透传 upstream_authority；空值交给 rank_signals/search_output 兜底
            if upstream_auth is not None:
                entry["upstream_authority"] = upstream_auth
            results.append(entry)
        return results

    return _engine


# ── 2. zhihu：知乎站内结构化搜索（含问题下钻 + AuthorityLevel）──────────────

def _build_zhihu_engine(spec: dict[str, Any]) -> Any:
    """知乎站内搜索（复用 social_engines/zhihu_engine.py 结构化解析与问题下钻）。"""
    timeout = float(spec.get("timeout", 10))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = float(_timeout or timeout)
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []
        expand_questions = bool(kwargs.get("expand_questions", True))
        return _ze.search(
            query,
            n=int(n or 5),
            expand_questions=expand_questions,
            _secret_override=secret,
            _timeout=to,
        )

    return _engine


# ── 3. zhihu_questions：母问题发现与同题回答摘要（只读，explicit_only）────────

def _build_zhihu_questions_engine(spec: dict[str, Any]) -> Any:
    """知乎问题发现（question_recommendations）与问题下回答摘要（question_answers）。"""
    timeout = float(spec.get("timeout", 10))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = float(_timeout or timeout)
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []
        q = (query or "").strip()
        if not q:
            return []

        m_url = _ZHIHU_QUESTION_URL_RE.search(q)
        m_qid = _ZHIHU_QUESTION_ID_RE.search(q) if not m_url else None
        if m_url or m_qid:
            clean_qurl = m_url.group(1) if m_url else f"https://www.zhihu.com/question/{m_qid.group(1)}"
            params = urllib.parse.urlencode({
                "QuestionUrl": clean_qurl,
                "Limit": str(max(1, min(int(n or 5), 50))),
                "Offset": "0",
            })
            url = f"https://developer.zhihu.com/api/v1/content/question_answers?{params}"
            data, err = _zhihu_http_json(
                url, secret, "zhihu_questions", to, spec.get("_name", "zhihu_questions")
            )
            if err:
                return [err]
            items = ((data or {}).get("Data") or {}).get("Items") or []
            results: list[dict[str, Any]] = []
            for rk, item in enumerate(items[:n]):
                if not isinstance(item, dict):
                    continue
                summary = _ze.strip_html_tags(item.get("Summary") or "")
                ans_url = item.get("Url") or ""
                token = str(item.get("ContentToken") or "")
                ctype = str(item.get("ContentType") or "answer").lower()
                if not summary and not ans_url:
                    continue
                head = summary[:60] + ("..." if len(summary) > 60 else "")
                results.append({
                    "title": head or f"回答 {token}",
                    "url": ans_url or clean_qurl,
                    "snippet": summary[:500],
                    "source": "zhihu_questions",
                    "score": rank_score(0.85, rk),
                    "social_meta": {
                        "platform": "zhihu",
                        "content_type": ctype,
                        "token": token,
                        "question_url": clean_qurl,
                        "mode": "question_answers",
                    },
                })
            return results

        # 主题推荐模式：按关键词荐题（画像推荐属创作侧选题，2026-11-15 移出搜索面）
        count = max(1, min(int(n or 5), _ZHIHU_COUNT_MAX))
        rec_params: dict[str, str] = {"Count": str(count), "Query": q}
        url = (
            "https://developer.zhihu.com/api/v1/user/question_recommendations?"
            + urllib.parse.urlencode(rec_params)
        )
        data, err = _zhihu_http_json(
            url, secret, "zhihu_questions", to, spec.get("_name", "zhihu_questions")
        )
        if err:
            return [err]
        items = ((data or {}).get("Data") or {}).get("Items") or []
        results = []
        for rk, item in enumerate(items[:n]):
            if not isinstance(item, dict):
                continue
            title = _ze.clean_zhihu_title(item.get("Title"))
            q_url = item.get("Url") or ""
            if not title and not q_url:
                continue
            # 上游荐题实测只回 Title/Url（2026-11-15 抓包），无源摘要就不造 snippet
            results.append({
                "title": title[:200] or q_url,
                "url": q_url,
                "source": "zhihu_questions",
                "score": rank_score(0.8, rk),
                "social_meta": {
                    "platform": "zhihu",
                    "content_type": "question",
                    "mode": "query",
                },
            })
        return results

    return _engine


# ── 4. zhihu_kb：知乎知识库只读 RAG 检索与列库（explicit_only）──────────────

def _parse_kb_query(query: str) -> tuple[list[str], list[str], str]:
    """解析 `scope:` 与 `kb:<id>` 操作符，返回 (scopes, base_ids, 剩余查询)。"""
    scopes: list[str] = []
    base_ids: list[str] = []
    text = (query or "").strip()

    for m in _KB_SCOPE_OP_RE.finditer(text):
        sc = m.group(1).lower()
        if sc == "all":
            for s in ("personal", "subscription", "public"):
                if s not in scopes:
                    scopes.append(s)
        elif sc in ("personal", "subscription", "public") and sc not in scopes:
            scopes.append(sc)
    text = _KB_SCOPE_OP_RE.sub(" ", text).strip()

    for m in _KB_ID_OP_RE.finditer(text):
        bid = m.group(1)
        if bid not in base_ids:
            base_ids.append(bid)
    text = _KB_ID_OP_RE.sub(" ", text).strip()
    return scopes, base_ids, text


def _build_zhihu_kb_engine(spec: dict[str, Any]) -> Any:
    """知乎知识库只读检索（/api/v1/knowledge/bases 与 /api/v1/knowledge/search）。"""
    timeout = float(spec.get("timeout", 10))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = float(_timeout or timeout)
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []
        q = (query or "").strip()
        if not q:
            return []

        m_list = _KB_LIST_QUERY_RE.match(q)
        if m_list:
            scope_arg = (m_list.group(1) or "").strip().lower()
            scope = scope_arg if scope_arg in ("all", "created", "subscribed") else "all"
            url = f"https://developer.zhihu.com/api/v1/knowledge/bases?Scope={scope}"
            data, err = _zhihu_http_json(
                url, secret, "zhihu_kb", to, spec.get("_name", "zhihu_kb")
            )
            if err:
                return [err]
            items = ((data or {}).get("Data") or {}).get("Items") or []
            results: list[dict[str, Any]] = []
            for rk, item in enumerate(items[:n]):
                if not isinstance(item, dict):
                    continue
                kb_id = str(item.get("KnowledgeBaseID") or "")
                name = str(item.get("Name") or kb_id).strip()
                desc = str(item.get("Description") or "").strip()
                rel = str(item.get("Relation") or "")
                cnt = item.get("ContentCount") or 0
                vis = str(item.get("Visibility") or "")
                results.append({
                    "title": f"{name} (ID={kb_id})",
                    "url": f"https://www.zhihu.com/knowledge-base/{kb_id}" if kb_id else "https://developer.zhihu.com/profile",
                    "snippet": f"[{rel}/{vis}] 内容数={cnt}。{desc}"[:400],
                    "source": "zhihu_kb",
                    "score": rank_score(0.8, rk),
                    "published_at": _ze.epoch_to_iso(item.get("UpdatedAt")),
                    "social_meta": {
                        "platform": "zhihu_kb",
                        "knowledge_base_id": kb_id,
                        "relation": rel,
                        "visibility": vis,
                        "content_count": cnt,
                        "is_knowledge_base": True,
                    },
                })
            return results

        scopes, base_ids, clean_q = _parse_kb_query(q)
        if not clean_q:
            return []
        if not scopes and not base_ids:
            scopes = ["personal"]
        body: dict[str, Any] = {
            "Query": clean_q,
            "Limit": max(1, min(int(n or 5), _ZHIHU_KB_LIMIT_MAX)),
        }
        if scopes:
            body["RecallScopes"] = scopes
        if base_ids:
            body["KnowledgeBaseIDs"] = base_ids

        # POST 检索不自动重试，严格单次调用
        data, err = _zhihu_http_json(
            "https://developer.zhihu.com/api/v1/knowledge/search",
            secret,
            "zhihu_kb",
            to,
            spec.get("_name", "zhihu_kb"),
            method="POST",
            body=body,
        )
        if err:
            return [err]
        items = ((data or {}).get("Data") or {}).get("Items") or []
        results = []
        for rk, item in enumerate(items[:n]):
            if not isinstance(item, dict):
                continue
            kb_id = str(item.get("KnowledgeBaseID") or "")
            recall_id = str(item.get("RecallContentID") or "")
            doc_name = str(item.get("DocName") or item.get("Title") or f"知识库片段 {recall_id}").strip()
            origin_url = str(item.get("OriginUrl") or "").strip()
            chunks = item.get("Content")
            if isinstance(chunks, list):
                text = "\n".join(_ze.strip_html_tags(c) for c in chunks if str(c or "").strip())
            else:
                text = _ze.strip_html_tags(chunks or "")
            if not text and not doc_name:
                continue
            if origin_url:
                item_url = origin_url
            elif kb_id:
                # knowledge-base 页公开可达（2026-11-15 实测 200）；recall_id 属
                # 服务端检索定位符，不是页面参数，留 social_meta 不进 URL
                item_url = f"https://www.zhihu.com/knowledge-base/{kb_id}"
            else:
                item_url = "https://developer.zhihu.com/profile"
            results.append({
                "title": doc_name[:200],
                "url": item_url,
                "snippet": text[:800],
                "source": "zhihu_kb",
                "score": rank_score(0.85, rk),
                "social_meta": {
                    "platform": "zhihu_kb",
                    "knowledge_base_id": kb_id,
                    "recall_content_id": recall_id,
                    "has_origin_url": bool(origin_url),
                    "is_knowledge_base": True,
                },
            })
        return results

    return _engine
