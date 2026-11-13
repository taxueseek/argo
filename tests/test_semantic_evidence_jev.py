#!/usr/bin/env python3
"""test_semantic_evidence_jev — JEV/TypeSafe 通道的门禁（2026-10-22）。

## 背景：接入时实测踩中的三个坑

1. **两个通道的端点必须各管各的**。config.yaml 的 `endpoint` 长期写的是
   classifier.dev；JEV 通道若读同一个字段，会把 System One 的请求体
   （state+questions）发到 classifier.dev，拿回
   `401 Invalid API key / Create a workspace key at /app/keys`——报错指向
   「你的 key 不对」，真因是「打错站了」。
2. **HttpClient.post 不设 Content-Type**。它会把 dict 做 json.dumps，但请求
   头只来自浏览器 UA 模板 + extra_headers。缺这个头时 FastAPI 把 body 当
   字符串，回 `422 Input should be a valid dictionary…`——同样指向「请求体
   不是对象」，真因是少一个头。
3. **长度上限是 token 不是字符**。不带
   `TypeSafe-Danger-Zone-Long-Context` 时 32350 tokens 通过、40K 中文字
   （≈40K tokens）回 `max_tokens_exceeded`；带上则 80K 字通过。

## 锁什么

- 通道选择：显式声明优先，未声明按键就绪自动选
- JEV 请求形状：端点、Content-Type、body 的 state/questions/criteria
- 长上下文头的触发阈值
- **每条文本一个请求**（不是多段合一）——官方 jaggedness 明写「state 里塞
  无关细节会降准确率」，官方 cookbook 也是 one request per passage
- 失败路径：无 key / 非 200 / 缺 choice → None；部分失败留位不整体丢弃

全部离线（patch 掉 HTTP 层，不发网络）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import semantic_evidence as se  # noqa: E402


def _ok(label: str = "supports the query", conf: float = 0.9) -> dict:
    return {"answers": {"cls": {"type": "choice", "choice": label,
                                "confidence": conf,
                                "probabilities": {label: 0.9}}}}


class TestTransport(unittest.TestCase):
    """通道选择必须**纯配置驱动**——不能读本机状态。

    初版按 jev_api_key() 是否就绪自动选，结果通道取决于本机
    ~/.config/argo/env 里有没有那把 key：同一份代码在开发机与 CI 上走不同
    分支，既有测试在开发机上静默改道。机器状态不该改变代码路径。本类把
    「不依赖 key」钉死。
    """

    def test_显式声明优先(self):
        with patch.object(se, "_config", return_value={"transport": "classify"}):
            assert se._transport() == "classify"
        with patch.object(se, "_config", return_value={"transport": "jev"}):
            assert se._transport() == "jev"
        with patch.object(se, "_config", return_value={"transport": "gemini"}):
            assert se._transport() == "gemini"

    def test_未声明时用默认且与_key_无关(self):
        for key in ("", "k"):
            with patch.object(se, "_config", return_value={}), \
                    patch.object(se, "jev_api_key", return_value=key):
                assert se._transport() == se.DEFAULT_TRANSPORT, (
                    f"key={key!r} 时通道变了——又依赖本机状态了")

    def test_非法声明回落默认(self):
        with patch.object(se, "_config", return_value={"transport": "typo"}):
            assert se._transport() == se.DEFAULT_TRANSPORT


class TestEndpointsAreSeparate(unittest.TestCase):
    """两个通道各管各的端点——共用会把 System One 的体发到错误的站。"""

    def test_jev_端点独立于_classify_端点(self):
        cfg = {"endpoint": "https://classifier.dev"}
        with patch.object(se, "_config", return_value=cfg):
            assert se._jev_endpoint() == se.DEFAULT_JEV_ENDPOINT
            assert "typesafe.ai" in se._jev_endpoint()
            assert se._endpoint() == "https://classifier.dev"

    def test_jev_端点可被_jev_endpoint_覆盖(self):
        with patch.object(se, "_config",
                          return_value={"jev_endpoint": "https://x.test/v1/"}):
            assert se._jev_endpoint() == "https://x.test/v1"

    def test_默认模型(self):
        with patch.object(se, "_config", return_value={}):
            assert se._jev_model() == "jev-latest"
        with patch.object(se, "_config", return_value={"jev_model": "jev-1.13"}):
            assert se._jev_model() == "jev-1.13"


class TestRequestShape(unittest.TestCase):
    """patch 掉 HttpClient，看真正发出去的 URL / 头 / body。"""

    def _capture(self, state, questions, cfg=None, key="k",
                 state_chars=0):
        sent: dict = {}

        class _FakeClient:
            def __init__(self, **kw):
                pass

            def post(self, url, body=None, extra_headers=None, **kw):
                sent["url"] = url
                sent["body"] = body
                sent["headers"] = extra_headers or {}
                return {"status": 200, "text": __import__("json").dumps(_ok())}

        with patch.object(se, "_config", return_value=cfg or {}), \
                patch.object(se, "jev_api_key", return_value=key), \
                patch("http_client.HttpClient", _FakeClient):
            out = se._jev_post(state, questions, timeout=5.0)
        return out, sent

    def test_url_与鉴权头(self):
        _, sent = self._capture("文本", {"q": {"type": "choice", "criteria": {}}})
        assert sent["url"] == se.DEFAULT_JEV_ENDPOINT
        assert sent["headers"]["Authorization"] == "Bearer k"

    def test_必须带_content_type(self):
        """缺它 → 服务端当字符串 → 422；这条钉死那个实测踩中的坑。"""
        _, sent = self._capture("文本", {"q": {"type": "choice", "criteria": {}}})
        assert sent["headers"].get("Content-Type") == "application/json"

    def test_body_形状(self):
        _, sent = self._capture("正文", {"q": {"type": "choice", "criteria": {}}},
                                cfg={"jev_model": "jev-1.13"})
        assert sent["body"]["model"] == "jev-1.13"
        assert sent["body"]["state"] == "正文"
        assert "q" in sent["body"]["questions"]
        assert "labels" not in sent["body"], "JEV 体里不该混入 classify 通道的字段"

    def test_长上下文头按长度触发(self):
        _, sent = self._capture("x" * (se.JEV_LONG_CONTEXT_CHARS + 10),
                                {"q": {"type": "choice", "criteria": {}}})
        assert sent["headers"].get("TypeSafe-Danger-Zone-Long-Context") == "true"

    def test_短文本不带长上下文头(self):
        _, sent = self._capture("短文本", {"q": {"type": "choice", "criteria": {}}})
        assert "TypeSafe-Danger-Zone-Long-Context" not in sent["headers"]

    def test_无_key_不发请求(self):
        sent: dict = {}

        class _FakeClient:
            def __init__(self, **kw):
                pass

            def post(self, *a, **kw):
                sent["called"] = True
                return {"status": 200, "text": "{}"}

        with patch.object(se, "_config", return_value={}), \
                patch.object(se, "jev_api_key", return_value=""), \
                patch("http_client.HttpClient", _FakeClient):
            out = se._jev_post("x", {"q": {}}, timeout=5.0)
        assert out is None
        assert "called" not in sent


class TestChoiceShape(unittest.TestCase):
    def test_criteria_用标签本身当描述(self):
        """instructions 与 criteria 说同一件事（jaggedness：两者不一致会困惑）。"""
        captured: dict = {}

        def _fake_post(state, questions, *, timeout):
            captured.update(questions)
            return _ok()

        with patch.object(se, "_jev_post", _fake_post):
            r = se._jev_choice("正文", se.SUPPORT_LABELS, "判定立场。", 5.0)
        q = captured["cls"]
        assert q["type"] == "choice"
        assert set(q["criteria"]) == set(se.SUPPORT_LABELS)
        assert r["label"] == "supports the query"
        assert r["confidence"] == 0.9

    def test_缺_choice_返回_None(self):
        with patch.object(se, "_jev_post",
                          lambda *a, **k: {"answers": {"cls": {"type": "choice"}}}):
            assert se._jev_choice("x", se.SUPPORT_LABELS, None, 5.0) is None

    def test_非_200_返回_None(self):
        with patch.object(se, "_jev_post", lambda *a, **k: None):
            assert se._jev_choice("x", se.SUPPORT_LABELS, None, 5.0) is None


class TestPerTextRequests(unittest.TestCase):
    """每条文本一个请求——不是多段合一。"""

    def test_每条一次请求(self):
        calls: list = []

        def _fake_post(state, questions, *, timeout):
            calls.append(state)
            return _ok()

        with patch.object(se, "_jev_post", _fake_post):
            out = se._classify_jev(["a", "b", "c"], se.SUPPORT_LABELS,
                                   instructions=None,
                                   keep=[(0, "a"), (1, "b"), (2, "c")], timeout=5.0)
        assert len(calls) == 3, f"应每条一请求，实际 {len(calls)} 次"
        assert sorted(calls) == ["a", "b", "c"]
        assert len(out) == 3

    def test_部分失败留位不整体丢弃(self):
        def _fake_post(state, questions, *, timeout):
            return _ok() if state == "good" else None

        with patch.object(se, "_jev_post", _fake_post):
            out = se._classify_jev(["good", "bad", "good"], se.SUPPORT_LABELS,
                                   instructions=None,
                                   keep=[(0, "good"), (1, "bad"), (2, "good")],
                                   timeout=5.0)
        assert out[0] is not None and out[1] is None and out[2] is not None, (
            "一条坏不该让整批判定作废")

    def test_全部失败返回_None(self):
        with patch.object(se, "_jev_post", lambda *a, **k: None):
            assert se._classify_jev(["a"], se.SUPPORT_LABELS, instructions=None,
                                    keep=[(0, "a")], timeout=5.0) is None

    def test_空文本位保持_None_且不发请求(self):
        calls: list = []

        def _fake_post(state, questions, *, timeout):
            calls.append(state)
            return _ok()

        with patch.object(se, "_jev_post", _fake_post):
            out = se._classify_jev(["a", "", "c"], se.SUPPORT_LABELS,
                                   instructions=None,
                                   keep=[(0, "a"), (2, "c")], timeout=5.0)
        assert calls == ["a", "c"]
        assert out[1] is None


class TestGateTeeth(unittest.TestCase):
    def test_去掉_content_type_会被抓到(self):
        """把 Content-Type 从请求头里拿掉，TestRequestShape 那条必须失败。

        只断言「变异版本确实缺头」——因为真门禁断的是「有头」，两者取反就
        证明它能被抓住。不把真门禁的逻辑抄一遍（抄一遍的实现回退时照样绿）。
        """
        import json as _json

        sent: dict = {}

        class _Spy:
            def __init__(self, **kw):
                pass

            def post(self, url, body=None, extra_headers=None, **kw):
                sent.clear()
                sent.update(extra_headers or {})
                return {"status": 200, "text": _json.dumps(_ok())}

        def _stripped(state, questions, *, timeout):
            """变异版 _jev_post：其余完全相同，只少一个头。"""
            from http_client import HttpClient
            return HttpClient(timeout=timeout, max_retries=1, jitter=False).post(
                se._jev_endpoint(),
                {"model": se._jev_model(), "state": state, "questions": questions},
                extra_headers={"Authorization": "Bearer k"})

        with patch.object(se, "_config", return_value={}), \
                patch("http_client.HttpClient", _Spy):
            _stripped("x", {"q": {}}, timeout=5.0)

        assert sent.get("Authorization") == "Bearer k", "变异未生效：鉴权头也没了"
        assert "Content-Type" not in sent, (
            "变异版仍带头——说明真门禁断的不是这个差异，失去意义")


class TestPhaseOneEnhancements(unittest.TestCase):
    """第一阶段增强用例：隐私物理屏障、Hash 缓存与一手源分类。"""

    def test_local_privacy_barrier_blocks_local_paths(self):
        """本地文件（file:// 或绝对路径）物理级阻断，绝不进入 JEV 请求队列。"""
        items = [
            {"url": "file:///Users/taxue/project/secret.py", "title": "secret", "text": "def password(): pass"},
            {"url": "/Users/taxue/notes/todo.md", "title": "notes", "text": "my private notes"},
            {"url": "~/workspace/code.rs", "title": "rust", "text": "fn main() {}"},
            {"url": "https://gov.example.com/announcement", "title": "public", "text": "official public announcement"},
        ]
        calls: list = []

        def _fake_post(state, questions, *, timeout):
            calls.append(state)
            return _ok()

        with patch.object(se, "_jev_post", _fake_post):
            res = se.assess_support("test query", items)
            # 本地项被物理拦截，只有唯一公网 url 被发送
            assert len(calls) == 1
            assert "official public announcement" in calls[0]
            assert res is not None
            assert "https://gov.example.com/announcement" in res
            assert "file:///Users/taxue/project/secret.py" not in res

    def test_jev_hash_cache_hit(self):
        """Hash 缓存命中时 0 网络开销，跳过 _jev_post。"""
        import os
        calls: list = []

        def _fake_post(state, questions, *, timeout):
            calls.append(state)
            return _ok(label="supports the query", conf=0.95)

        with patch.dict(os.environ, {"_TEST_ENABLE_SEMANTIC_CACHE": "1"}):
            with patch.object(se, "_jev_post", _fake_post):
                r1 = se._jev_choice("cached text passage", se.SUPPORT_LABELS, "instruction", 5.0)
                assert len(calls) == 1
                assert r1["label"] == "supports the query"
                # 第二次带相同文本调用，应直接命中缓存，calls 次数不变
                r2 = se._jev_choice("cached text passage", se.SUPPORT_LABELS, "instruction", 5.0)
                assert len(calls) == 1
                assert r2["label"] == "supports the query"

    def test_source_types_identifies_primary_sources(self):
        """assess_source_types 对一手官方公告赋予 is_primary 标志。"""
        items = [{"url": "https://gov.cn/policy", "title": "国务院公报", "text": "国务院最新决定..."}]

        def _fake_post(state, questions, *, timeout):
            return _ok(label="primary data or official announcement", conf=0.98)

        with patch.object(se, "_jev_post", _fake_post):
            st = se.assess_source_types(items)
            assert st is not None
            assert st["https://gov.cn/policy"]["is_primary"] is True
            assert st["https://gov.cn/policy"]["confidence"] == 0.98


class TestPhaseTwoSubstantiveFilter(unittest.TestCase):
    """第二阶段增强用例：播客与长文本实质探讨判定、顺带提及校准去噪与本地隔离。"""

    def test_substantive_discussion_classification(self):
        """区分实质深度探讨 vs 顺带提及。"""
        items = [
            {"url": "https://podcast.example.com/deep_ep", "title": "深入拆解架构", "text": "从底核指令集全方位解析架构"},
            {"url": "https://podcast.example.com/passing_ep", "title": "周末闲聊", "text": "喝咖啡时顺口打了个比方提到架构"},
        ]

        def _fake_post(state, questions, *, timeout):
            if "全方位解析" in state:
                return _ok(label="substantive in-depth discussion", conf=0.99)
            return _ok(label="passing mention or brief reference", conf=0.95)

        with patch.object(se, "_jev_post", _fake_post):
            res = se.assess_substantive_discussion("芯片架构", items)
            assert res is not None
            deep_res = res["https://podcast.example.com/deep_ep"]
            assert deep_res["is_substantive"] is True
            assert deep_res["is_passing_mention"] is False

            passing_res = res["https://podcast.example.com/passing_ep"]
            assert passing_res["is_substantive"] is False
            assert passing_res["is_passing_mention"] is True

    def test_substantive_discussion_blocks_local_paths(self):
        """本地文件绝对路径不外发给实质探讨分类。"""
        items = [
            {"url": "file:///Users/taxue/podcast_transcript.txt", "title": "local", "text": "local private transcript"},
            {"url": "https://xiaoyuzhoufm.com/episode/123", "title": "public podcast", "text": "public podcast transcript"},
        ]
        calls: list = []

        def _fake_post(state, questions, *, timeout):
            calls.append(state)
            return _ok(label="substantive in-depth discussion", conf=0.96)

        with patch.object(se, "_jev_post", _fake_post):
            res = se.assess_substantive_discussion("播客主题", items)
            assert len(calls) == 1
            assert "public podcast transcript" in calls[0]
            assert "file:///Users/taxue/podcast_transcript.txt" not in res
            assert "https://xiaoyuzhoufm.com/episode/123" in res

    def test_evidence_loop_substantive_and_passing_adjustments(self):
        """测试 evidence_loop 对实质探讨 (+0.15) 与顺带提及 (-0.10) 的吸收分调整及统计。"""
        from evidence_loop import verify_results

        results = [
            {"url": "https://podcast.example.com/ep1", "title": "Deep Discussion", "score": 0.8},
            {"url": "https://podcast.example.com/ep2", "title": "Passing Chat", "score": 0.75},
        ]

        def fake_fetch(url, max_chars=8000, timeout=10.0):
            if "ep1" in url:
                return {"url": url, "success": True, "content": "深度探讨了主题细节内容，包含大量实质逻辑。"}
            return {"url": url, "success": True, "content": "闲聊顺带提了一句，主要聊别的。"}

        def fake_post(state, questions, *, timeout):
            if "深度探讨" in state:
                return _ok(label="substantive in-depth discussion", conf=0.95)
            return _ok(label="passing mention or brief reference", conf=0.92)

        with patch.dict(os.environ, {"ARGO_SEMANTIC_EVIDENCE": "1"}), \
             patch.object(se, "enabled", return_value=True), \
             patch.object(se, "_jev_post", fake_post):
            out = verify_results(results, query="测试主题", top_k=2, fetch_fn=fake_fetch)
            assert out is not None
            sem = out.get("semantic") or {}
            assert sem.get("substantive_discussions") == 1
            assert sem.get("passing_mentions") == 1

            v_items = {v["url"]: v for v in out.get("verified", [])}
            assert v_items["https://podcast.example.com/ep1"].get("substantive_discussion") is True
            assert v_items["https://podcast.example.com/ep2"].get("passing_mention") is True

    def test_cache_key_normalization_against_dynamic_content(self):
        """测试缓存键在动态时间戳、HTML 残留与多余空白下保持稳态一致。"""
        labels = ("labelA", "labelB")
        instructions = "Test instructions"
        t1 = "<div>Title</div>\nUpdated: 2026-11-11 08:30:15\nThis is core content about AI."
        t2 = "Title\nUpdated: 3 minutes ago\nThis is core content about AI."
        key1 = se._get_cache_key(t1, labels, instructions)
        key2 = se._get_cache_key(t2, labels, instructions)
        self.assertEqual(key1, key2)

    def test_cache_key_distinguishes_content_beyond_1200_chars(self):
        """正文差异出现在 1200 字符之后时必须产生不同的键。

        回归：原实现取 s[:1200] 作键，而送入判定的正文上限是 MAX_TEXT_CHARS=8000，
        于是两段仅在后 6800 字符不同的文本共享同一 label/confidence——语义核验
        的正确性缺陷，且错误结果会被缓存 TTL 7 天。
        """
        labels = ("labelA", "labelB")
        instructions = "Test instructions"
        a = "x" * 1200 + "DIFFERENT_A" + "y" * 500
        b = "x" * 1200 + "DIFFERENT_B" + "y" * 500
        self.assertNotEqual(se._get_cache_key(a, labels, instructions),
                            se._get_cache_key(b, labels, instructions))

        # 边界：差异恰在 1190 处本就应不同（确认修复没引入反向回归）
        a2 = "x" * 1190 + "AAA" + "y" * 500
        b2 = "x" * 1190 + "BBB" + "y" * 500
        self.assertNotEqual(se._get_cache_key(a2, labels, instructions),
                            se._get_cache_key(b2, labels, instructions))

    def test_local_target_covers_relative_paths(self):
        """本地隔离必须覆盖 ./ 相对路径——本机检索最常见的形态。

        回归：原实现只匹配 ../ 与绝对路径，./notes.md 会被当成公网 URL 送进
        JEV 队列，私域内容外发。
        """
        for p in ("file:///Users/x/secret.md", "/Users/x/s.md", "/home/x/s.md",
                  "~/notes.md", "./relative.md", ".\\local.md", "../parent.md",
                  "C:\\Users\\x\\a.doc", "/private/tmp/x"):
            self.assertTrue(se.is_local_target(p), f"本地路径未被拦截: {p!r}")
        for u in ("https://example.com/x", "http://example.com/x",
                  "https://r.jina.ai/https://example.com"):
            self.assertFalse(se.is_local_target(u), f"公网 URL 被误拦: {u!r}")

    def test_jev_deadline_default_is_safe(self):
        """测试 JEV deadline 默认提升至 1.2s，足够覆盖 0.55s 真实冷调用。"""
        self.assertGreaterEqual(se.DEFAULT_JEV_DEADLINE_S, 1.2)

    def test_heuristic_depth_and_source_type_fallback(self):
        """测试本地启发式降级引擎：无需外部 Key 或网络，瞬间提供高精度深度判定与一手源识别。"""
        # 1. 深度探讨判定
        deep_res = se.heuristic_substantive_discussion(
            "量子计算",
            "本章详细推导量子纠错码的构造与容错量子计算门集合。首先通过稳定子形式建立纠错基底，随后给出表面码的容错阈值证明，展示了量子计算核心瓶颈的工程解法。",
            "量子计算纠错架构深度剖析",
        )
        self.assertEqual(deep_res["label"], "substantive in-depth discussion")
        self.assertGreaterEqual(deep_res["confidence"], se.SUPPORT_THRESHOLD)

        # 2. 顺带提及判定
        passing_res = se.heuristic_substantive_discussion(
            "量子计算",
            "今天天气不错，下午顺便去喝了杯咖啡，路上听朋友提了一嘴量子计算很有意思，然后我们去看了电影。",
            "周末日常碎碎念",
        )
        self.assertEqual(passing_res["label"], "passing mention or brief reference")
        self.assertGreaterEqual(passing_res["confidence"], se.SUPPORT_THRESHOLD)

        # 3. 一手源与社交源识别
        pri_res = se.heuristic_source_type("https://www.sec.gov/edgar/searchedgar/companysearch", "SEC Filing 10-K", "")
        self.assertEqual(pri_res["label"], "primary data or official announcement")
        self.assertGreaterEqual(pri_res["confidence"], se.SUPPORT_THRESHOLD)

        soc_res = se.heuristic_source_type("https://x.com/someone/status/123", "Short tweet", "")
        self.assertEqual(soc_res["label"], "social narrative")

        # 4. 离线/无 Key 降级集成验证：classify 返回 None 时平滑走启发式
        items = [
            {"url": "https://sec.gov/data", "title": "量子计算官方白皮书", "text": "量子计算详细架构规范与全量公式推导内容，覆盖了全部实施细节。"},
            {"url": "https://blog.com/diary", "title": "日常博客", "text": "顺带提及了一下量子计算。"},
        ]
        with patch.object(se, "classify", return_value=None):
            sub_map = se.assess_substantive_discussion("量子计算", items, allow_heuristic=True)
            self.assertIsNotNone(sub_map)
            self.assertTrue(sub_map["https://sec.gov/data"]["is_substantive"])
            self.assertTrue(sub_map["https://blog.com/diary"]["is_passing_mention"])

            type_map = se.assess_source_types(items, allow_heuristic=True)
            self.assertIsNotNone(type_map)
            self.assertTrue(type_map["https://sec.gov/data"]["is_primary"])

    def test_jev_depth_pipeline_contract(self):
        """深度判定管道契约（mock 与黄金标注零耦合）。

        为什么 mock 不能抄黄金标注：首版（264b112）mock 按 title 返回
        expected_label，预测恒等于标注、F1 恒满分——「F1>=0.90 门禁」对
        判定质量退化零敏感，属循环论证（评审 2026-11-12 撤销）。本测试只锁
        管道契约：私域剔除、空正文过滤、输入构建与截断、label→字段映射、
        SUPPORT_THRESHOLD 边界。
        """
        long_text = "超" * (se.MAX_TEXT_CHARS + 200)
        items = [
            {"url": "https://example.com/a", "title": "深入讲解", "text": "实质性正文"},
            {"url": "https://example.com/b", "title": "", "text": "无标题正文"},
            {"url": "https://example.com/c", "title": "超长文本", "text": long_text},
            {"url": "file:///tmp/secret-notes.md", "title": "本地文件", "text": "私域内容"},
            {"url": "https://example.com/e", "title": "空正文", "text": "   "},
        ]
        captured = {}

        def contract_mock(texts, labels, instructions=None, criteria=None):
            captured["texts"] = list(texts)
            captured["labels"] = list(labels)
            captured["instructions"] = instructions
            captured["criteria"] = criteria
            # 固定测试向量，与黄金标注零耦合；条数须与 usable 三条对齐
            return [
                {"label": "substantive in-depth discussion", "confidence": 0.95},
                {"label": "passing mention or brief reference", "confidence": 0.71},
                {"label": "substantive in-depth discussion", "confidence": 0.4},
            ]

        with patch.object(se, "classify", side_effect=contract_mock):
            res = se.assess_substantive_discussion("量子计算", items)
        self.assertIsNotNone(res)
        # 本地文件（file://）与空正文被剔除，只有 3 条进模型
        self.assertEqual(sorted(res),
                         ["https://example.com/a", "https://example.com/b", "https://example.com/c"])
        # 输入契约：title 拼在正文前、超长正文按 MAX_TEXT_CHARS 截断、instructions 带 topic
        self.assertTrue(captured["texts"][0].startswith("深入讲解\n"))
        self.assertLessEqual(len(captured["texts"][2]), len("超长文本\n") + se.MAX_TEXT_CHARS)
        self.assertIn("量子计算", captured["instructions"])
        self.assertEqual(captured["labels"], list(se.SUBSTANTIVE_DISCUSSION_LABELS))
        self.assertEqual(captured["criteria"], se.SUBSTANTIVE_DISCUSSION_CRITERIA)
        # 映射契约：label/conf 透传，is_substantive 受 SUPPORT_THRESHOLD 门控
        self.assertEqual(res["https://example.com/a"]["confidence"], 0.95)
        self.assertTrue(res["https://example.com/a"]["is_substantive"])
        self.assertTrue(res["https://example.com/b"]["is_passing_mention"])  # 0.71 刚过 0.7
        self.assertFalse(res["https://example.com/b"]["is_substantive"])
        self.assertFalse(res["https://example.com/c"]["is_substantive"])  # label 对但 0.4 低于阈值


class TestGeminiDecisionChannel(unittest.TestCase):
    """Gemini Decision 1 专属决策通道测试。"""

    def test_gemini_defaults(self):
        self.assertEqual(se.DEFAULT_GEMINI_MODEL, "gemini-decision-1")
        self.assertIn("decisions", se.DEFAULT_GEMINI_ENDPOINT)
        self.assertEqual(se.DEFAULT_TRANSPORT, "gemini")

    def test_gemini_choice_request_shape(self):
        captured = {}

        def mock_post(url, body, extra_headers=None):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = extra_headers
            return {
                "status": 200,
                "text": json.dumps({
                    "answers": {
                        "eval": {
                            "choice": "substantive in-depth discussion",
                            "confidence": 0.98,
                            "probabilities": {"substantive in-depth discussion": 0.98},
                            "type": "choice"
                        }
                    }
                })
            }

        with patch.object(se, "gemini_api_key", return_value="fake_gemini_key"), \
             patch("http_client.HttpClient.post", side_effect=mock_post):
            res = se._gemini_choice("测试文本", ["substantive in-depth discussion", "passing mention"], "说明", 5.0)
            self.assertIsNotNone(res)
            self.assertEqual(res["label"], "substantive in-depth discussion")
            self.assertEqual(res["confidence"], 0.98)
            self.assertEqual(captured["headers"]["x-goog-api-key"], "fake_gemini_key")
            self.assertEqual(captured["body"]["model"], "gemini-decision-1")
            self.assertIn("eval", captured["body"]["questions"])

    def test_gemini_fallback_to_jev_when_gemini_unavailable(self):
        jev_called = []

        def mock_jev(texts, labels, **kwargs):
            jev_called.append(True)
            return [{"label": "passing mention or brief reference", "confidence": 0.88}]

        with patch.object(se, "_transport", return_value="gemini"), \
             patch.object(se, "_classify_gemini", return_value=None), \
             patch.object(se, "_classify_jev", side_effect=mock_jev):
            res = se.classify(["短文"], ["substantive", "passing mention"])
            self.assertIsNotNone(res)
            self.assertTrue(jev_called, "Gemini 不可用时必须平滑回退至 JEV")

    def test_gemini_deadline_independent_from_jev(self):
        """Gemini 通道的整批截止独立于 JEV，不复用 1.2s。

        实测依据（2026-11-13 本机，gemini-decision-1）：单条 p95 2251ms，
        沿用 JEV 的 1.2s 会在批量里静默截断（6 组 4 条里 1 组只回 3/4）。
        用 sleep 模拟慢响应，验证 5s 截止不截断、1s 截止会截断。
        """
        self.assertGreater(se.DEFAULT_GEMINI_DEADLINE_S, se.DEFAULT_JEV_DEADLINE_S)

        def slow_choice(text, labels, instructions, timeout, criteria=None):
            time.sleep(2.0)
            return {"label": labels[0], "confidence": 0.9}

        texts = ["文本一", "文本二", "文本三", "文本四"]
        keep = [(i, t) for i, t in enumerate(texts)]
        with patch.object(se, "gemini_api_key", return_value="fake"), \
             patch.object(se, "_gemini_choice", side_effect=slow_choice):
            res = se._classify_gemini(texts, ("supports the query",), keep=keep)
        got = sum(1 for r in (res or []) if r)
        self.assertEqual(got, 4, "5s 截止下 4 条 2s 慢响应不应被截断")

        # 对照：显式传 1.0s 截止必须截断，证明截止参数真的生效
        with patch.object(se, "gemini_api_key", return_value="fake"), \
             patch.object(se, "_gemini_choice", side_effect=slow_choice):
            res_fast = se._run_choice_batch(
                len(texts), keep,
                lambda t: slow_choice(t, ("x",), None, 5.0),
                timeout=5.0, deadline_s=1.0)
        got_fast = sum(1 for r in (res_fast or []) if r)
        self.assertLess(got_fast, 4, "1s 截止下必须截断")


if __name__ == "__main__":
    unittest.main()

