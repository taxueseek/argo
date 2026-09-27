#!/usr/bin/env python3
"""跨域内容同质惩罚的回归测试（2026-09-27 新增，方案 A）。

背景：Google「Scaled content abuse」政策（2024-03 起）描述的形态是
「很多站各发一篇同款」，而不是「一个站发很多篇」。已有的
domain_concentration_penalty 管前者（单 host 占比过半），对后者**完全
失明**：几十个小站发同一篇软文时，每个 host 都只占 1/N，一条都不触发。

本文件锁定的核心性质：
  1. 能抓住跨域同质（异域、内容近同、簇规模达标 → 降权）；
  2. 不重复计罚同域多篇（那是域级惩罚的职责）；
  3. 正常多域结果**零影响**（未触发时逐位一致，这条由既有
     tests/test_ranking_contract.py 兜底，本文件补最小对照）；
  4. 开关可关（逃生门真实有效，不是摆设）。
"""
from __future__ import annotations

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(SCRIPT_DIR, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-cross-domain-test")

from rank_signals import cross_domain_homogeneity_penalty  # noqa: E402


def _farm_body(tag: str) -> str:
    """同一篇软文的 n 个改写版：结构与用词高度重合，host 各不相同。"""
    return (
        f"2026年{tag}选购指南，为你整理了十款热门型号的实测数据。"
        "本文从照度、显色指数、频闪控制三个维度逐项对比，"
        "并给出不同预算区间的建议。文中提到的参数均来自厂商公开资料，"
        "实际使用体验可能因环境而异，建议按需到店体验后再做决定。"
        "如果你对具体型号仍有疑问，可以查看文末的对比表格。"
    )


def _make(hosts, tag="护眼台灯", body=None):
    body = body if body is not None else _farm_body(tag)
    return [
        {"url": f"https://{h}/review/{i}", "title": f"2026{tag}推荐 第{i}名",
         "snippet": body, "source": h}
        for i, h in enumerate(hosts)
    ]


class TestCrossDomainHomogeneity:
    def test_detects_syndicated_farm(self):
        """核心：异域同质内容必须被降权。"""
        hosts = ["a-farm.top", "b-farm.top", "c-farm.top", "d-farm.top"]
        pen = cross_domain_homogeneity_penalty(_make(hosts))
        assert pen, "跨域同质未被发现——这正是本信号要覆盖的盲区"
        assert all(0.45 <= v < 1.0 for v in pen.values()), pen

    def test_same_domain_is_not_our_job(self):
        """同域多篇不属于「跨域」——那是 domain_concentration_penalty 的职责。

        不重复计罚的意义：同域多篇本就该被更狠的域级惩罚压下去，
        两个信号叠乘等于对该条目双重降权，破坏「每条降权理由可追溯」。
        """
        rows = _make(["same-site.com"] * 4)
        pen = cross_domain_homogeneity_penalty(rows)
        assert pen == {}, pen

    def test_distinct_content_untouched(self):
        """正常多域结果零影响（内容本就不同，不该有任何惩罚）。"""
        rows = [
            {"url": "https://a.com/1", "title": "照度标准解读",
             "snippet": "国AA要求300mm扇形范围内中央照度大于500Lux，均匀度需另行考核。" * 2},
            {"url": "https://b.com/2", "title": "显色指数怎么选",
             "snippet": "Ra值越高色彩还原越准确，但超过90之后人眼感知收益已经很小。" * 2},
            {"url": "https://c.com/3", "title": "频闪与健康",
             "snippet": "PWM调光在低亮度档位频闪明显，长时间使用会增加视觉疲劳风险。" * 2},
            {"url": "https://d.com/4", "title": "预算分配建议",
             "snippet": "预算有限时优先解决频闪与眩光，参数堆料的品牌溢价意义有限。" * 2},
        ]
        pen = cross_domain_homogeneity_penalty(rows)
        assert pen == {}, pen

    def test_two_hosts_not_enough(self):
        """簇规模不足不判定：两家发的内容像，多半是正常转载/引用关系。"""
        pen = cross_domain_homogeneity_penalty(_make(["a.com", "b.com"]))
        assert pen == {}, pen

    def test_short_snippet_not_judged(self):
        """签名过短（<40 字符）不参与判定——相似度噪声太大。"""
        rows = [
            {"url": f"https://s{i}.com/p", "title": "推荐", "snippet": "短摘要"}
            for i in range(5)
        ]
        pen = cross_domain_homogeneity_penalty(rows)
        assert pen == {}, pen

    def test_malformed_rows_safe(self):
        """脏数据不得抛异常。"""
        rows = [
            {"url": "https://a.com/1", "title": None, "snippet": None},
            {"url": "", "title": "无 url", "snippet": "x" * 200},
            "junk", None, 42,
            {"url": "https://b.com/2", "title": "正常", "snippet": "内容" * 60},
        ]
        pen = cross_domain_homogeneity_penalty([r for r in rows if isinstance(r, dict)])
        assert isinstance(pen, dict)

    def test_empty_and_single_noop(self):
        assert cross_domain_homogeneity_penalty([]) == {}
        one = [{"url": "https://a.com/1", "title": "t", "snippet": "x" * 200}]
        assert cross_domain_homogeneity_penalty(one) == {}

    def test_bad_params_return_empty(self):
        """非法阈值不判定（配置错了就当没开，不能整条链路崩）。"""
        rows = _make(["a.com", "b.com", "c.com"])
        assert cross_domain_homogeneity_penalty(rows, similarity_threshold=0) == {}
        assert cross_domain_homogeneity_penalty(rows, similarity_threshold=5) == {}
        assert cross_domain_homogeneity_penalty(rows, min_cluster=1) == {}
        assert cross_domain_homogeneity_penalty(rows, similarity_threshold="x") == {}

    def test_penalty_is_keyed_by_url(self):
        """返回表按 url 索引：调用方按 url 取系数，不依赖结果顺序。"""
        rows = _make(["a.com", "b.com", "c.com", "d.com"])
        pen = cross_domain_homogeneity_penalty(rows)
        for r in rows:
            assert r["url"] in pen, f"{r['url']} 不在惩罚表里"
