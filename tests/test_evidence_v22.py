#!/usr/bin/env python3
"""Argo evidence v2.2 单元测试：Selection×Absorption、SERP 降权、时效、证据密度。"""
from __future__ import annotations

import os
import sys
import unittest

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPT_DIR))

from content_signals import score_evidence_density, compute_content_quality  # noqa: E402
from evidence import (  # noqa: E402
    compute_credibility,
    is_serp_or_jump_url,
    score_authority,
    score_freshness,
    score_traceability,
)


class TestSerpDemotion(unittest.TestCase):
    def test_baidu_search_is_serp(self):
        self.assertTrue(
            is_serp_or_jump_url(
                "https://www.baidu.com/s?wd=%E5%85%AC%E5%8B%9F%E5%9F%BA%E9%87%91"
            )
        )

    def test_baidu_link_is_serp(self):
        self.assertTrue(
            is_serp_or_jump_url(
                "http://www.baidu.com/link?url=abc123xyz"
            )
        )

    def test_sogou_link_is_serp(self):
        self.assertTrue(
            is_serp_or_jump_url("https://www.sogou.com/link?url=xxx")
        )

    def test_real_article_not_serp(self):
        self.assertFalse(
            is_serp_or_jump_url(
                "https://finance.eastmoney.com/a/202607213815797914.html"
            )
        )

    def test_serp_authority_very_low(self):
        auth = score_authority("https://www.baidu.com/s?wd=test")
        self.assertTrue(auth["is_serp"])
        self.assertLessEqual(auth["score"], 0.15)

    def test_bing_google_serp(self):
        self.assertTrue(is_serp_or_jump_url("https://www.bing.com/search?q=pytest"))
        self.assertTrue(is_serp_or_jump_url("https://www.google.com/search?q=pytest"))
        self.assertTrue(is_serp_or_jump_url("https://www.google.com/url?q=http://example.com"))
        self.assertTrue(is_serp_or_jump_url("https://weixin.sogou.com/weixin?type=2&query=foo"))

    def test_source_type_floor_eastmoney(self):
        # 通用域名 + eastmoney 引擎 → 类型保底抬升
        auth = score_authority("https://example-finance-blog.test/a/1", source="eastmoney")
        self.assertGreaterEqual(auth["score"], 0.85)


class TestEvidenceDensity(unittest.TestCase):
    def test_numbers_and_compare_boost(self):
        text = "截至2026年二季度末，电子行业持仓占比升至约43%，通信约17%，环比大增。"
        d = score_evidence_density(text, "公募持仓")
        self.assertTrue(d["has_numbers"])
        self.assertTrue(d["has_comparison"])
        self.assertGreaterEqual(d["absorption_score"], 0.45)

    def test_qa_title_penalty(self):
        weak = score_evidence_density("随便聊聊", "基金怎么样？")
        strong = score_evidence_density(
            "根据披露，股票市值8.84万亿，较一季度增加9.6%。",
            "二季报股票持仓",
        )
        self.assertTrue(weak["is_qa_format"])
        self.assertGreater(strong["absorption_score"], weak["absorption_score"])

    def test_quality_includes_evidence_fields(self):
        q = compute_content_quality(
            "这是一篇关于定义的长文。" * 20
            + "所谓GEO是指生成式引擎优化。步骤如下：第一、第二。对比A与B，增长15%。",
            "GEO 定义与对比",
        )
        self.assertIn("has_numbers", q)
        self.assertIn("absorption_score", q)
        self.assertTrue(q["content_ok"] or q["word_count"] > 0)


class TestFreshness(unittest.TestCase):
    def test_ignore_historical_since_year(self):
        """「2015年以来」不应把时效打成2015旧闻。"""
        r = {
            "title": "公募极致抱团",
            "snippet": "电子+通信合计近60%，创2015年以来新高。2026年二季度末主动偏股基金加仓。",
            "url": "https://wallstreetcn.com/articles/3777647",
        }
        f = score_freshness(r)
        # 应识别到 2026 而非 2015
        self.assertIn("2026", f["reason"])
        self.assertGreaterEqual(f["score"], 0.7)

    def test_full_date_preferred(self):
        r = {
            "title": "基金持仓",
            "snippet": "发布于2026年07月21日，电子持仓占比上升。",
            "url": "https://example.com/a",
        }
        f = score_freshness(r)
        self.assertIn("2026", f["reason"])
        self.assertGreaterEqual(f["score"], 0.8)


class TestTwoStageCredibility(unittest.TestCase):
    def test_evidence_page_ranks_above_serp(self):
        results = [
            {
                "title": "相关搜索公募基金持仓",
                "url": "https://www.baidu.com/s?wd=公募",
                "snippet": "",
                "score": 0.9,
                "source": "local_baidu",
            },
            {
                "title": "中际旭创取代宁德时代登顶",
                "url": "https://finance.eastmoney.com/a/202607213815797914.html",
                "snippet": "截至2026年二季度末，公募股票持仓8.84万亿，较一季度增加9.60%。",
                "score": 0.5,
                "source": "anysearch",
            },
        ]
        report = compute_credibility(results, "2026公募基金二季报持仓")
        self.assertEqual(report["framework"], "selection_x_absorption_v2.3_traceability")
        best = report["results"][0]
        self.assertIn("eastmoney", best["url"])
        self.assertGreaterEqual(best["credibility"]["final"], 0.5)
        # SERP 应在后面或显著更低
        serp = next(r for r in report["results"] if "baidu.com/s" in r["url"])
        self.assertLess(serp["credibility"]["final"], best["credibility"]["final"])
        self.assertTrue(serp["credibility"]["authority"]["is_serp"])

    def test_commercial_ranking_demoted(self):
        auth = score_authority("https://www.maigoo.com/brand/list.html")
        self.assertLessEqual(auth["score"], 0.35)

    def test_cs_com_cn_high(self):
        auth = score_authority(
            "https://www.cs.com.cn/tzjj/01/2026/07/22/detail_2026072210026163.html"
        )
        self.assertGreaterEqual(auth["score"], 0.85)

    def test_wenshu_demoted_on_policy_query(self):
        """#34：裁判文书站对政策/市场查询答非所问，条件降权要压过引擎保底分。"""
        url = ("https://wenshu.court.gov.cn/website/wenshu/181107ANFZ0BXSK4/"
               "index.html?docId=x")
        auth = score_authority(url, "anysearch", "第十一批国家集采 中选结果 执行")
        self.assertLessEqual(auth["score"], 0.2)
        self.assertEqual(auth["tier"], "very_low")

    def test_wenshu_keeps_authority_on_legal_query(self):
        """对照面：法律语境下裁判文书是一手源，不得连坐降权。"""
        url = ("https://wenshu.court.gov.cn/website/wenshu/181107ANFZ0BXSK4/"
               "index.html?docId=x")
        auth = score_authority(url, "anysearch", "裁判文书 判决 检索 方法")
        self.assertGreaterEqual(auth["score"], 0.9)

    def test_wenshu_without_query_keeps_old_table(self):
        """无 query 的评分路径（状态展示/研究摘要）保持原表行为。"""
        url = ("https://wenshu.court.gov.cn/website/wenshu/181107ANFZ0BXSK4/"
               "index.html?docId=x")
        self.assertGreaterEqual(score_authority(url, "anysearch")["score"], 0.9)


class TestCnPortalAuthorityParity(unittest.TestCase):
    """主流门户不得被压进「内容农场」档（09-26 审查第 7 项，最后一项 P0）。

    evidence_tier.py 的 B 级「知名第三方」名单含 163.com / sohu.com /
    sina.com.cn；AUTHORITY_TIERS 却把这三者压在 Tier 4（0.4-0.55）——
    两份名单自相矛盾，中文主流媒体被系统性降权。现在归入 Tier 3；
    k.sina.com.cn（聚合/博客通道）留在 Tier 4。
    """

    def test_cn_portals_not_in_farm_tier(self):
        for url in (
            "https://www.163.com/news/abc.html",
            "https://news.sina.com.cn/c/2026-10-01/doc-abc.shtml",
            "https://www.sohu.com/a/123456_789",
        ):
            with self.subTest(url=url):
                auth = score_authority(url)
                self.assertGreaterEqual(
                    auth["score"], 0.6,
                    f"{url} authority={auth['score']}，仍被当低质源压分",
                )

    def test_sina_farm_channel_stays_low(self):
        auth = score_authority("https://k.sina.com.cn/article_123.html")
        self.assertLessEqual(auth["score"], 0.6)

    def test_longest_suffix_wins_for_sina(self):
        """k.sina.com.cn 必须匹配自己的表项而不是父域 sina.com.cn。"""
        k_auth = score_authority("https://k.sina.com.cn/article_123.html")
        parent_auth = score_authority(
            "https://news.sina.com.cn/c/2026-10-01/doc-abc.shtml")
        self.assertLess(k_auth["score"], parent_auth["score"])


class TestTraceability(unittest.TestCase):
    """Test traceability scoring for different source types.

    借鉴 doubao-sentiment-tracker 的溯源四场景方法论：
    - 一手信源（用户直接发布的原始帖子）：高分
    - 原始媒体报道（新闻机构原创报道）：高分
    - 综合报道/评论（引用了原始来源）：中分
    - 二手转述/洗稿：低分
    - 无法判断：中性分
    """

    def test_social_post_is_primary(self):
        """社交媒体原帖应得高分（一手信源）。"""
        r = {
            "title": "用户使用体验分享",
            "snippet": "这个产品真的很好用，推荐给大家。",
            "url": "https://weibo.com/1234567890/ABC123",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertTrue(t["is_primary"])
        self.assertGreaterEqual(t["score"], 0.85)
        self.assertEqual(t["platform_type"], "primary")

    def test_zhihu_answer_is_primary(self):
        """知乎回答应得高分（一手信源）。"""
        r = {
            "title": "如何评价某产品？",
            "snippet": "作为一名从业者，我认为...",
            "url": "https://www.zhihu.com/question/123/answer/456",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertTrue(t["is_primary"])
        self.assertGreaterEqual(t["score"], 0.85)

    def test_github_issue_is_primary(self):
        """GitHub issue 应得高分（一手信源）。"""
        r = {
            "title": "Bug: something doesn't work",
            "snippet": "Steps to reproduce: ...",
            "url": "https://github.com/user/repo/issues/123",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertTrue(t["is_primary"])
        self.assertGreaterEqual(t["score"], 0.85)

    def test_original_news_report(self):
        """原始媒体报道应得高分。"""
        r = {
            "title": "某公司发布新产品",
            "snippet": "本报讯（记者XXX）某公司今日发布...",
            "url": "https://www.xinhuanet.com/tech/20260101/abc.html",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertTrue(t["is_primary"])
        self.assertGreaterEqual(t["score"], 0.90)
        self.assertEqual(t["platform_type"], "official_media")

    def test_reuters_report(self):
        """路透社报道应得高分。"""
        r = {
            "title": "Company X launches new product",
            "snippet": "By Jane Doe, Reuters",
            "url": "https://www.reuters.com/technology/2026/01/01/abc",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertTrue(t["is_primary"])
        self.assertGreaterEqual(t["score"], 0.90)

    def test_aggregator_is_secondary(self):
        """聚合/二手平台应得低分。"""
        r = {
            "title": "某产品使用体验",
            "snippet": "最近在用某产品，感觉还不错...",
            "url": "https://baijiahao.baidu.com/s?id=123456",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertFalse(t["is_primary"])
        self.assertLessEqual(t["score"], 0.45)
        self.assertEqual(t["platform_type"], "aggregator")

    def test_toutiao_is_secondary(self):
        """头条号应得低分。"""
        r = {
            "title": "某事件分析",
            "snippet": "近日，某事件引发关注...",
            "url": "https://www.toutiao.com/article/123456/",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertFalse(t["is_primary"])
        self.assertLessEqual(t["score"], 0.45)

    def test_repost_marker_lowers_score(self):
        """含转载标记的内容应得低分。"""
        r = {
            "title": "某新闻转载",
            "snippet": "转载：某公司今日发布新产品...",
            "url": "https://weibo.com/1234567890/ABC123",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertFalse(t["is_primary"])
        self.assertLessEqual(t["score"], 0.50)

    def test_serp_url_is_untraceable(self):
        """SERP URL 应得最低分。"""
        r = {
            "title": "搜索结果",
            "snippet": "",
            "url": "https://www.baidu.com/s?wd=test",
            "source": "local_baidu",
        }
        t = score_traceability(r)
        self.assertFalse(t["is_primary"])
        self.assertLessEqual(t["score"], 0.15)
        self.assertEqual(t["platform_type"], "serp")

    def test_unknown_source_is_neutral(self):
        """未知来源应得中性分。"""
        r = {
            "title": "某网页内容",
            "snippet": "这是一些内容...",
            "url": "https://www.example.com/page123",
            "source": "anysearch",
        }
        t = score_traceability(r)
        self.assertFalse(t["is_primary"])
        self.assertAlmostEqual(t["score"], 0.50, places=2)
        self.assertEqual(t["platform_type"], "unknown")

    def test_social_meta_boosts_score(self):
        """有 social_meta 的内容应得高分。"""
        r = {
            "title": "用户分享",
            "snippet": "这个产品真的很好用",
            "url": "https://www.example.com/share/123",
            "source": "anysearch",
            "content_type": "social_post",
            "social_meta": {"author": "user123", "likes": 100},
        }
        t = score_traceability(r)
        self.assertTrue(t["is_primary"])
        self.assertGreaterEqual(t["score"], 0.70)

    def test_traceability_in_credibility(self):
        """Traceability 应被包含在最终 credibility 计算中。"""
        results = [
            {
                "title": "微博原帖",
                "url": "https://weibo.com/1234567890/ABC123",
                "snippet": "这个产品真的很好用，推荐给大家。",
                "score": 0.5,
                "source": "anysearch",
            },
            {
                "title": "百家号转载",
                "url": "https://baijiahao.baidu.com/s?id=123456",
                "snippet": "这个产品真的很好用，推荐给大家。",
                "score": 0.5,
                "source": "anysearch",
            },
        ]
        report = compute_credibility(results, "产品评价")
        self.assertEqual(report["framework"], "selection_x_absorption_v2.3_traceability")
        # 微博原帖应排在百家号转载之前
        best = report["results"][0]
        self.assertIn("weibo.com", best["url"])
        # 验证 traceability 字段存在
        self.assertIn("traceability", best["credibility"])
        self.assertIn("is_primary", best["credibility"]["traceability"])

    def test_traceability_improves_ranking(self):
        """一手信源应排在二手源之前（即使其他维度相同）。"""
        results = [
            {
                "title": "聚合平台内容",
                "url": "https://baijiahao.baidu.com/s?id=123456",
                "snippet": "截至2026年，某产品市场占有率达30%，同比增长15%。",
                "score": 0.5,
                "source": "anysearch",
            },
            {
                "title": "微博原帖",
                "url": "https://weibo.com/1234567890/ABC123",
                "snippet": "截至2026年，某产品市场占有率达30%，同比增长15%。",
                "score": 0.5,
                "source": "anysearch",
            },
        ]
        report = compute_credibility(results, "产品市场占有率")
        # 微博原帖（一手信源）应排在百家号（二手源）之前
        best = report["results"][0]
        self.assertIn("weibo.com", best["url"])


if __name__ == "__main__":
    unittest.main()
