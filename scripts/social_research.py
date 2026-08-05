#!/usr/bin/env python3
"""
social_research.py — 社交舆情研究模块（从 research.py 拆分）

承载跨平台 UGC 情绪与讨论分析：
  - social_sentiment_research：并行抓取多平台 → 互动聚合 → 话题提取
  - aggregate_social_sentiment：MCP argo_social_search mode=sentiment 用
  - _extract_topics：中英文兼容话题提取
  - _print_social_report：终端报告输出

从 research.py 拆分以控制文件规模（code-review：单文件 <1k 行）。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def social_sentiment_research(query: str, platforms: list[str] | None = None,
                              max_results: int = 5) -> dict[str, Any]:
    """社交舆情研究：跨平台 UGC 情绪与讨论分析"""
    if platforms is None:
        platforms = ["twitter", "reddit", "xiaohongshu"]

    from search import super_search

    platform_results: dict[str, list] = {}
    all_results: list[dict] = []
    engines_used: set[str] = set()
    t0 = time.time()

    def _one(platform: str) -> tuple[str, list[dict], set[str]]:
        try:
            result = super_search(query, engine=platform, n=max_results, mode="fast")
            return platform, result.get("results", []), set(result.get("engines_used", []))
        except Exception:
            return platform, [], set()

    # 并行抓取各平台（串行时 3 平台 × 秒级延迟累积；并行取最慢平台耗时）
    with ThreadPoolExecutor(max_workers=min(len(platforms), 4)) as ex:
        futures = {ex.submit(_one, p): p for p in platforms}
        for fut in as_completed(futures, timeout=90):
            platform, results, used = fut.result()
            platform_results[platform] = results
            all_results.extend(results)
            engines_used.update(used)

    # 互动数据聚合
    engagement_totals = {"likes": 0, "comments": 0, "shares": 0, "views": 0}
    titles: list[str] = []

    for r in all_results:
        meta = r.get("social_meta", {})
        if isinstance(meta, dict):
            engagement_totals["likes"] += meta.get("likes", meta.get("upvotes", meta.get("attitudes_count", 0)))
            engagement_totals["comments"] += meta.get("comments", meta.get("num_comments", 0))
            engagement_totals["shares"] += meta.get("shares", meta.get("retweets", meta.get("reposts_count", 0)))
            engagement_totals["views"] += meta.get("views", meta.get("play_count", 0))
        # 收集标题用于话题提取
        title = r.get("title", "")
        if title:
            titles.append(title)

    # 中英文兼容的话题提取
    top_topics_list = _extract_topics(titles, top_k=10)
    elapsed = int((time.time() - t0) * 1000)

    return {
        "query": query,
        "mode": "social-sentiment",
        "platforms": platforms,
        "total_posts": len(all_results),
        "platform_breakdown": {p: len(r) for p, r in platform_results.items()},
        "engagement_totals": engagement_totals,
        "top_topics": top_topics_list,
        "cross_platform_posts": [
            {
                "platform": r.get("source", ""),
                "title": r.get("title", "")[:100],
                "url": r.get("url", ""),
                "snippet": r.get("snippet", "")[:200],
                "social_meta": r.get("social_meta", {}),
            }
            for r in all_results[:15]
        ],
        "engines_used": sorted(engines_used),
        "elapsed_ms": elapsed,
    }


def aggregate_social_sentiment(query: str, platforms: list[str],
                               platform_results: dict[str, list]) -> dict[str, Any]:
    """社交舆情聚合（MCP argo_social_search mode=sentiment 用）。

    输入为按平台分组的已抓取帖子（social_engines 输出，含 social_meta），
    聚合互动数据汇总与平台分布，限流返回代表性帖子。原为 MCP 分发层的
    内联逻辑，下沉到本模块避免逻辑放错层。
    """
    all_posts: list = []
    for p in platforms:
        all_posts.extend(platform_results.get(p) or [])

    engagement_totals = {"likes": 0, "comments": 0, "reposts": 0, "shares": 0}
    for post in all_posts:
        meta = post.get("social_meta", {}) if isinstance(post, dict) else {}
        engagement_totals["likes"] += meta.get("likes", 0) or meta.get("like_count", 0) or 0
        engagement_totals["comments"] += meta.get("comments", 0) or 0
        engagement_totals["reposts"] += meta.get("reposts", 0) or 0
        engagement_totals["shares"] += meta.get("shares", 0) or 0

    return {
        "query": query,
        "platforms": platforms,
        "platform_breakdown": {p: len(platform_results.get(p) or []) for p in platforms},
        "total_posts": len(all_posts),
        "engagement_totals": engagement_totals,
        "posts": all_posts[:30],  # 限流
    }


def _extract_topics(titles: list[str], top_k: int = 10) -> list[dict]:
    """从标题列表提取话题（中英文兼容）。

    英文用 split() 分词，中文用字符级 bigram。
    过滤单字停用词，返回 top_k 个话题。
    """
    import re
    from collections import Counter

    chinese_stopwords = set(
        '的了是在和我你她他它这就也不很但而或如果因为所以而且或者虽然但是可以应该需要已经正在'
        '之前之后时候地方问题工作生活东西事情时间今天昨天明天今年去年明年个些吗呢啊吧哦嗯哈'
        '呀嘛哪谁什么怎么多少几样种类下上里中后前时来回过开给让把被对从向比跟和与及当比'
        '如例包括相关关于根据通过进行使用作为成为具有属于位于来自获得达到实现完成产生形成'
        '存在发生发展提供包含涉及适用于'
    )

    topic_counter = Counter()

    for title in titles:
        # 英文分词（仅保留长度 >= 3 的词）
        en_words = re.findall(r'[a-zA-Z]{2,}', title.lower())
        for w in en_words:
            if len(w) >= 3:
                topic_counter[w] += 1

        # 中文 bigram（连续 2 个中文字符）
        chinese_chars = re.findall(r'[一-鿿]', title)
        for i in range(len(chinese_chars) - 1):
            bigram = chinese_chars[i] + chinese_chars[i + 1]
            if bigram[0] not in chinese_stopwords and bigram[1] not in chinese_stopwords:
                topic_counter[bigram] += 1

    return [{"topic": t, "mentions": c} for t, c in topic_counter.most_common(top_k)]


def _print_social_report(report: dict):
    """打印社交舆情报告"""
    print(f"\n{'='*60}")
    print(f"社交舆情分析：{report['query']}")
    print(f"{'='*60}")
    print(f"平台：{', '.join(report['platforms'])} | 引擎：{', '.join(report['engines_used'])}")
    print(f"抓取帖子：{report['total_posts']} | 耗时：{report['elapsed_ms']}ms")
    print()
    print("── 平台分布 ──")
    for platform, count in report["platform_breakdown"].items():
        print(f"  {platform}: {count} 条")
    print()
    print("── 互动数据汇总 ──")
    eng = report["engagement_totals"]
    print(f"  点赞/投票：{eng['likes']:,} | 评论：{eng['comments']:,} | 转发：{eng['shares']:,} | 观看：{eng['views']:,}")
    print()
    print("── 高频讨论话题 ──")
    for topic in report["top_topics"]:
        print(f"  「{topic['topic']}」 ({topic['mentions']} 次)")
    print()
    print("── 代表性内容 ──")
    for post in report["cross_platform_posts"][:5]:
        meta = post.get("social_meta", {})
        engagement = ""
        if isinstance(meta, dict):
            likes = meta.get("likes", meta.get("upvotes", meta.get("attitudes_count", 0)))
            comments = meta.get("comments", meta.get("num_comments", 0))
            engagement = f" | 👍{likes} 💬{comments}"
        print(f"  [{post['platform']}] {post['title'][:60]}{engagement}")
        print(f"    {post['url']}")
        print()
