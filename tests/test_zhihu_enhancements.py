#!/usr/bin/env python3
"""tests/test_zhihu_enhancements.py — 知乎开放平台引擎簇元信号与只读原语回归测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class _FakeResp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestZhihuMetaSignals(unittest.TestCase):
    def test_zhihu_global_authority_and_published_at(self):
        """zhihu_global 应透传 upstream_authority/published_at，且空 AuthorityLevel 不写空串阻断回填。"""
        import engines_builders_batch10 as b10
        from search_output import _ensure_authority_levels

        payload = {
            "Code": 0,
            "Data": {
                "Items": [
                    {
                        "Title": "权威报告 - 知乎",
                        "Url": "https://www.gov.cn/demo",
                        "ContentText": "<em>核心</em>摘要",
                        "AuthorName": "官方",
                        "AuthorBadgeText": "官方机构",
                        "CommentInfoList": [{"Content": "一条精选评论"}],
                        "AuthorityLevel": "4",
                        "EditTime": 1748355858,
                        "VoteUpCount": 50,
                        "CommentCount": 10,
                    },
                    {
                        "Title": "无权威等级条目",
                        "Url": "https://example.org/post",
                        "ContentText": "普通摘要",
                        "AuthorityLevel": "",
                        "EditTime": 0,
                    },
                ]
            },
        }
        eng = b10._build_zhihu_global_engine({"_name": "zhihu_global", "timeout": 5})
        with patch.object(b10, "get_env", return_value="sec"), \
                patch.object(b10, "http_open", return_value=_FakeResp(payload)):
            out = eng("测试查询", n=5)

        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "权威报告")
        self.assertEqual(out[0]["snippet"], "核心摘要")
        self.assertEqual(out[0]["upstream_authority"], 4)
        self.assertNotIn("authority_level", out[0])
        self.assertTrue(out[0]["published_at"].startswith("2025-"))
        self.assertEqual(out[0]["social_meta"]["author_badge_text"], "官方机构")
        self.assertEqual(out[0]["social_meta"]["top_comments"], ["一条精选评论"])

        # 第二条 AuthorityLevel="" 不应写空串到 authority_level，以便下游信号/精排回填
        self.assertNotIn("authority_level", out[1])
        self.assertNotIn("upstream_authority", out[1])
        _ensure_authority_levels(out)
        self.assertEqual(out[0]["authority_level"], 4)
        from rank_signals import authority_level
        self.assertEqual(authority_level(0.6, out[1].get("upstream_authority")), 2)

    def test_zhihu_search_contract_and_signals(self):
        """zhihu 站内搜索走 zhihu_engine：Count<=10、无 SearchDB、解析 AuthorityLevel 与 published_at。"""
        import engines
        from social_engines import zhihu_engine as ze

        captured_urls: list[str] = []

        def fake_http_get(url: str, headers: dict, timeout: float = 10):
            captured_urls.append(url)
            body = json.dumps({
                "Code": 0,
                "Data": {
                    "Items": [
                        {
                            "Title": "RAG 评测 - 知乎",
                            "ContentType": "Article",
                            "ContentID": "123",
                            "ContentText": "<em>RAG</em> 实测细节",
                            "Url": "https://zhuanlan.zhihu.com/p/123",
                            "VoteUpCount": 128,
                            "CommentCount": 15,
                            "AuthorName": "答主A",
                            "AuthorBadgeText": "人工智能话题优秀答主",
                            "CommentInfoList": [{"Content": "很扎实"}],
                            "AuthorityLevel": "3",
                            "RankingScore": 0.95,
                            "EditTime": 1748355858,
                        }
                    ]
                },
            }).encode("utf-8")
            return body, {}

        with patch.object(ze, "http_get", side_effect=fake_http_get), \
                patch.object(ze, "_secret", return_value="sec"):
            res = ze.search("RAG", n=15, expand_questions=False)

        self.assertEqual(len(captured_urls), 1)
        self.assertIn("Count=10", captured_urls[0])
        self.assertNotIn("SearchDB", captured_urls[0])
        self.assertEqual(res[0]["upstream_authority"], 3)
        self.assertEqual(res[0]["snippet"], "RAG 实测细节")
        self.assertEqual(res[0]["social_meta"]["author_badge_text"], "人工智能话题优秀答主")
        self.assertEqual(res[0]["social_meta"]["top_comments"], ["很扎实"])
        self.assertAlmostEqual(res[0]["social_meta"]["ranking_score"], 0.95)
        self.assertTrue(res[0]["published_at"].startswith("2025-"))

        # 验证 engines.py 已将 zhihu 注册到专用 builder（非通用 http）
        reg = engines.get_registry()
        self.assertIn("zhihu", reg)


class TestZhihuQuestionsAndKB(unittest.TestCase):
    def test_zhihu_questions_url_and_topic_modes(self):
        """zhihu_questions 区分问题 URL 回答摘要与主题推荐两种形态（画像推荐属创作侧，已移出搜索面）。"""
        import engines_builders_batch10 as b10

        seen_urls: list[str] = []

        def fake_open(req, timeout=10, engine=""):
            seen_urls.append(req.full_url)
            if "question_answers" in req.full_url:
                return _FakeResp({
                    "Code": 0,
                    "Data": {"Items": [{"ContentToken": "99", "Url": "https://www.zhihu.com/answer/99", "Summary": "实测有效"}]},
                })
            return _FakeResp({
                "Code": 0,
                "Data": {"Items": [{"Title": "怎么选空净？", "Url": "https://www.zhihu.com/question/1"}]},
            })

        eng = b10._build_zhihu_questions_engine({"_name": "zhihu_questions", "timeout": 5})
        with patch.object(b10, "get_env", return_value="sec"), \
                patch.object(b10, "http_open", side_effect=fake_open):
            ans_res = eng("https://www.zhihu.com/question/1", n=3)
            qid_res = eng("q:1", n=3)
            rec_res = eng("空气净化器", n=5)

        self.assertEqual(ans_res[0]["social_meta"]["mode"], "question_answers")
        self.assertEqual(ans_res[0]["snippet"], "实测有效")
        self.assertEqual(qid_res[0]["social_meta"]["question_url"], "https://www.zhihu.com/question/1")
        self.assertEqual(rec_res[0]["social_meta"]["mode"], "query")
        # 荐题统一按关键词携带 Query；上游只回 Title/Url，不得造合成 snippet
        self.assertIn("Query=", seen_urls[2])
        self.assertNotIn("snippet", rec_res[0])
        self.assertNotIn("知乎母问题", json.dumps(rec_res, ensure_ascii=False))

    def test_zhihu_kb_list_and_search(self):
        """zhihu_kb 支持列库（bases）与 scope:/kb: 只读 RAG 检索，适配真实 DocName/Content 字段。"""
        import engines_builders_batch10 as b10

        captured_reqs: list[tuple[str, bytes | None]] = []

        def fake_open(req, timeout=10, engine=""):
            captured_reqs.append((req.full_url, req.data))
            if "/knowledge/bases" in req.full_url:
                return _FakeResp({
                    "Code": 0,
                    "Data": {"Items": [{"KnowledgeBaseID": "750", "Name": "私域库", "Relation": "created", "ContentCount": 3, "Visibility": "private", "UpdatedAt": 1753262809}]},
                })
            return _FakeResp({
                "Code": 0,
                "Data": {
                    "Items": [
                        {
                            "KnowledgeBaseID": "750",
                            "DocName": "退款规则 v2",
                            "RecallContentID": "abc",
                            "OriginUrl": "",
                            "Content": ["7 天无理由", "拆封除外"],
                        }
                    ]
                },
            })

        eng = b10._build_zhihu_kb_engine({"_name": "zhihu_kb", "timeout": 5})
        with patch.object(b10, "get_env", return_value="sec"), \
                patch.object(b10, "http_open", side_effect=fake_open):
            bases_out = eng("bases", n=5)
            rag_out = eng("scope:public kb:750 退款规则", n=5)

        self.assertEqual(bases_out[0]["social_meta"]["knowledge_base_id"], "750")
        self.assertEqual(rag_out[0]["title"], "退款规则 v2")
        self.assertIn("7 天无理由", rag_out[0]["snippet"])
        # URL 指向公开可达的 knowledge-base 页；recall 定位符留 social_meta 不进 URL
        self.assertEqual(rag_out[0]["url"], "https://www.zhihu.com/knowledge-base/750")
        self.assertEqual(rag_out[0]["social_meta"]["recall_content_id"], "abc")
        sent_body = json.loads(captured_reqs[1][1].decode("utf-8"))
        self.assertEqual(sent_body["RecallScopes"], ["public"])
        self.assertEqual(sent_body["KnowledgeBaseIDs"], ["750"])
        self.assertEqual(sent_body["Query"], "退款规则")

    def test_env_family_and_explicit_only_wiring(self):
        """验证 engine_env、engine_families 与 explicit_only 声明一致。"""
        from config import load_config, get_engines
        from engine_env import KNOWN_ENV_ALIASES
        from engine_families import family_of

        specs = get_engines(load_config(), routable_only=False)
        for name in ("zhihu_questions", "zhihu_kb"):
            self.assertIn(name, specs)
            self.assertTrue(specs[name].get("explicit_only"))
            self.assertIn(name, KNOWN_ENV_ALIASES)
        self.assertEqual(family_of("zhihu_questions"), "social")
        self.assertEqual(family_of("zhihu_kb"), "personal_data")


if __name__ == "__main__":
    unittest.main()
