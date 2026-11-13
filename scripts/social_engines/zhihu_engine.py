#!/usr/bin/env python3
"""知乎搜索引擎（知乎开放平台 zhihu_search API）

密钥：环境变量 ZHIHU_ACCESS_SECRET（或 ARGO_ZHIHU_ACCESS_SECRET），
未配置时返回空列表（不抛错、不静默假成功——error 由调用侧显示）。
保持一致 social_engines 统一 schema，sentiment 聚合按 social_meta 直接可用。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
from typing import Any

# 出口调度唯一入口（issue #13 同类修复）：urlopen 不认 config.yaml 的
# network.proxy，裸用会在「必须经代理才能出网」的环境里整源连不上。
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from net_proxy import http_get  # noqa: E402

ZHIHU_API = "https://developer.zhihu.com/api/v1/content/zhihu_search"
ZHIHU_QUESTION_ANSWERS_API = "https://developer.zhihu.com/api/v1/content/question_answers"


def fetch_question_answers(question_url: str, limit: int = 3, timeout: float = 2.5) -> list[dict[str, Any]]:
    """获取知乎问题下的精选回答摘要（官方 question_answers 端点）。

    返回项含 token、url、summary。网络异常或配额限制时静默降级返回空列表，不阻断主流程。
    """
    if not question_url or not question_url.strip():
        return []
    secret = _secret()
    if not secret:
        return []

    # 规范化问题 URL：剥离多余路径和查询参数
    m = re.search(r"(https?://(?:www\.)?zhihu\.com/question/\d+)", question_url.strip())
    clean_url = m.group(1) if m else question_url.strip()

    params = urllib.parse.urlencode({
        "QuestionUrl": clean_url,
        "Limit": str(max(1, min(int(limit), 20))),
        "Offset": "0",
    })
    url = f"{ZHIHU_QUESTION_ANSWERS_API}?{params}"
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "X-Request-Timestamp": str(int(time.time())),
        "User-Agent": "argo-search/2.6 (unified-search@local)",
    }
    try:
        body, _ = http_get(url, headers, timeout=timeout)
        data = json.loads(body.decode("utf-8", "replace"))
        if data.get("Code") in (0, None):
            items = (data.get("Data") or {}).get("Items") or []
            answers = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                summary = (item.get("Summary") or "").strip()
                ans_url = item.get("Url") or ""
                token = str(item.get("ContentToken") or "")
                if summary or ans_url:
                    answers.append({
                        "token": token,
                        "url": ans_url,
                        "summary": summary,
                    })
            return answers
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return []


def _secret() -> str:
    # 走 engine_env 规范访问器：os.environ 优先，~/.config/argo/env 热读保底
    # （密钥轮换改文件即生效，无需重启）
    try:
        from engine_env import get_env
        return get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
    except ImportError:
        return os.environ.get("ZHIHU_ACCESS_SECRET") or os.environ.get("ARGO_ZHIHU_ACCESS_SECRET", "")


def search(query: str, n: int = 5, expand_questions: bool = True) -> list[dict[str, Any]]:
    """通过知乎开放平台 zhihu_search 搜索站内内容。"""
    if not query or not query.strip():
        return []
    secret = _secret()
    if not secret:
        return []

    # 向上游知乎开放平台请求略多于 n 的候选池（至多 20 条），便于质量重排与去噪
    fetch_count = max(1, min(max(int(n) * 2, int(n)), 20))
    params = urllib.parse.urlencode({
        "Query": query.strip(),
        "Count": str(fetch_count),
        "SearchDB": "zhihu",
    })
    url = f"{ZHIHU_API}?{params}"
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "X-Request-Timestamp": str(int(time.time())),
        "User-Agent": "argo-search/2.6 (unified-search@local)",
    }
    try:
        body, _ = http_get(url, headers, timeout=10)
        data = json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 密钥无效/过期（401/403）等必须暴露为 auth-failed，而不是静默
        # no-results——调用侧把「没配置」「没结果」区分开才可行动
        return [{"error": f"zhihu API HTTP {e.code}", "source": "zhihu"}]
    except Exception as e:
        return [{"error": f"zhihu API {type(e).__name__}: {e}", "source": "zhihu"}]

    if data.get("Code") not in (0, None):
        return [{"error": f"zhihu API Code={data.get('Code')} "
                          f"{str(data.get('Message') or '')[:100]}",
                 "source": "zhihu"}]

    candidates: list[dict[str, Any]] = []
    items = (data.get("Data") or {}).get("Items") or []
    expanded_count = 0
    max_expand = 2  # 一次搜索最多下钻展开前 2 个纯问题，平衡配额与延迟

    for rank, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = (item.get("Title") or "").strip()
        # API 标题统一带「 - 知乎」尾巴：截断前剥掉（先剥再切，尾巴不占正文）
        if title.endswith(" - 知乎"):
            title = title[: -len(" - 知乎")].rstrip()
        url_ = item.get("Url") or ""
        raw_text = (item.get("ContentText") or title or "").strip()
        snippet = re.sub(r"\s+", " ", raw_text)[:500]
        if not title and not url_:
            continue
        content_id = str(item.get("ContentID") or "")
        author = item.get("AuthorName") or ""
        content_type = (item.get("ContentType") or "answer").lower()
        try:
            likes = int(item.get("VoteUpCount") or 0)
        except (ValueError, TypeError):
            likes = 0
        try:
            comments = int(item.get("CommentCount") or 0)
        except (ValueError, TypeError):
            comments = 0
        edit_ts = item.get("EditTime")
        published = None
        if edit_ts is not None:
            try:
                published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(edit_ts)))
            except (TypeError, ValueError, OSError):
                published = None

        # 识别纯问题：支持自动下钻抓取 Top 精选回答摘要（方案 B：Question Answers Expansion）
        is_pure_question = (content_type == "question") or ("/question/" in url_ and "/answer/" not in url_)
        expanded_answers: list[dict[str, Any]] = []
        if is_pure_question and expand_questions and expanded_count < max_expand:
            expanded_count += 1
            expanded_answers = fetch_question_answers(url_, limit=3)
            if expanded_answers:
                answer_summaries = [f"【回答 {i+1}】{a['summary']}" for i, a in enumerate(expanded_answers[:2]) if a.get("summary")]
                if answer_summaries:
                    addon = " ".join(answer_summaries)
                    snippet = f"{snippet} | {addon}"[:500] if snippet else addon[:500]

        # 结合上游相关性序位与社区点赞/评论做质量综合评分（第一性原理：高赞真实实测加权）
        base_relevance = max(1.0 - rank * 0.05, 0.2)
        like_boost = min(0.3, math.log10(likes + 1) * 0.08)
        comment_boost = min(0.1, math.log10(comments + 1) * 0.03)
        expand_boost = 0.08 if expanded_answers else 0.0
        quality_score = round(min(1.0, base_relevance * 0.7 + like_boost + comment_boost + expand_boost), 4)

        social_meta = {
            "platform": "zhihu",
            "content_type": content_type,
            "id": content_id,
            "author": author,
            "likes": likes,
            "upvotes": likes,
            "comments": comments,
            "provider": "zhihu_openapi",
            "is_question": is_pure_question,
            "has_expanded_answers": bool(expanded_answers),
        }
        if expanded_answers:
            social_meta["answers"] = [
                {"url": a["url"], "summary": a["summary"]} for a in expanded_answers
            ]

        candidates.append({
            "title": title[:100] + ("..." if len(title) > 100 else ""),
            "url": url_,
            "snippet": snippet,
            "source": "zhihu",
            "score": quality_score,
            "published_at": published,
            "social_meta": social_meta,
        })

    # 按综合得分降序微调，确保高赞高质量答案排在前面，并保留前 n 条
    candidates.sort(key=lambda r: r["score"], reverse=True)
    return candidates[:n]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Zhihu search engine (zhihu_openapi)")
    parser.add_argument("action", nargs="?", default="search", help="search | answers")
    parser.add_argument("query", nargs="?", help="Search query or question URL")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--no-expand", action="store_true", help="Disable question answers expansion")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        print("[]")
        return

    if args.action in ("answers", "question_answers"):
        answers = fetch_question_answers(args.query, limit=args.n)
        if args.json:
            print(json.dumps(answers, ensure_ascii=False))
        else:
            for i, a in enumerate(answers, 1):
                print(f"### Answer {i}")
                print(f"- URL: {a['url']}")
                print(f"- {a['summary']}")
                print()
        return

    results = search(args.query, args.n, expand_questions=not args.no_expand)
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            meta = r.get("social_meta") or {}
            print(f"### {i}. {r['title']}")
            print(f"- **URL**: {r['url']}")
            print(f"- {r['snippet'][:250]}")
            print(f"- likes={meta.get('likes')} comments={meta.get('comments')} @{meta.get('author', '')} "
                  f"[question={meta.get('is_question')} expanded={meta.get('has_expanded_answers')}]")
            print()


if __name__ == "__main__":
    main()
