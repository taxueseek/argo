#!/usr/bin/env python3
"""test_engine_rank_score.py — 引擎内排序分（rank decay）回归测试。

背景（2026-09-13 修复）：多数 builder 原先给同一引擎返回的每条结果写死同一个
常量分（如全部 0.7）。后果是上游 API 自己的相关性排序在进 RRF 前被抹平——融合层
只剩「引擎内序位」这点信息，且 local_five_dim_rerank 的 relevance 维度对同引擎
结果完全没有区分度。

修复：新增 engines_base.rank_score(base, rank)，builder 用 enumerate 的序位把
「上游相关性顺序」编码进 score，base 保持引擎档位不变（跨引擎相对权重不动）。

本测试锁定三件事：
  1. rank_score 本身的数学性质（单调递减、rank0 恒等、异常输入不抛）；
  2. builder 源码里不再有「循环体内写死常量 score」的形态（AST 检查，防回归）；
  3. 真实 builder 产出（mock 网络）分数严格递减，且首条仍等于原档位常量。
"""

import ast
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

BUILDER_FILES = [
    "engines_builders_intl.py",
    "engines_builders_data.py",
    "engines_builders_media.py",
    "engines_builders_science.py",
    "engines_builders_finance.py",
    "engines_builders_archive.py",
    "engines_builders_cn.py",
    "engines_builders_tech.py",
    "engines_builders_data_macro.py",
]


class TestRankScoreMath:
    def test_rank0_equals_base(self):
        from engines_base import rank_score
        assert rank_score(0.7, 0) == 0.7
        assert rank_score(0.95, 0) == 0.95

    def test_monotonic_decreasing(self):
        from engines_base import rank_score
        vals = [rank_score(0.7, i) for i in range(10)]
        assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)), vals

    def test_decay_is_gentle(self):
        """衰减必须温和：位置差不该压过引擎档位差（否则等价于换了套权重）。"""
        from engines_base import rank_score
        # rank4 相对 rank0 的跌幅 < 15%
        assert rank_score(0.7, 4) > 0.7 * 0.85
        # 高档位引擎的第 10 条仍应高于低档位引擎的第 1 条
        assert rank_score(0.95, 10) > rank_score(0.7, 0)

    def test_base_tier_preserved(self):
        """rank0 处的跨引擎档位关系不变（这是修复不改变引擎权重的保证）。"""
        from engines_base import rank_score
        assert rank_score(0.9, 0) > rank_score(0.7, 0)

    @pytest.mark.parametrize("bad", ["x", None, object()])
    def test_bad_rank_does_not_raise(self, bad):
        from engines_base import rank_score
        # 不抛异常即为通过；回落到 base（或安全默认值）
        out = rank_score(0.7, bad)
        assert isinstance(out, float)

    def test_negative_rank_is_base(self):
        from engines_base import rank_score
        assert rank_score(0.7, -1) == 0.7


class TestNoConstantScoresInLoops:
    """AST 检查：循环体内不得再出现常量 score（唯一例外是单答案型 return [{...}]）。"""

    def _loops_with_constant_score(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for k, v in zip(sub.keys, sub.values):
                        if (isinstance(k, ast.Constant) and k.value == "score"
                                and isinstance(v, ast.Constant)
                                and isinstance(v.value, (int, float))
                                and not isinstance(v.value, bool)):
                            hits.append((node.lineno, v.lineno, v.value))
        return hits

    @pytest.mark.parametrize("fname", BUILDER_FILES)
    def test_no_constant_score_inside_for_loop(self, fname):
        path = SCRIPTS_DIR / fname
        hits = self._loops_with_constant_score(path)
        # 允许「聚合/单答案」行：它们是循环内 append 的汇总项（如北向资金合计、
        # 反向汇率），语义上就是常量，不参与引擎内排序。
        allowed = {(623, 624, 0.85)} if fname == "engines_builders_cn.py" else set()
        unexpected = [h for h in hits if h not in allowed]
        assert not unexpected, (
            f"{fname} 仍有循环内常量 score（上游排序会被抹平）: {unexpected}"
        )


class TestBuildersEmitDescendingScores:
    """真实 builder 产出分数应严格递减（mock 网络层，不发真实请求）。"""

    def _items(self, payload):
        return payload

    def test_cnii_descending(self, monkeypatch):
        """cnii（原先全部写死 0.7）现在应给出口内递减分。"""
        import engines_builders_intl as bi
        records = [{"title": f"论文 {i}", "link": {"@id": f"https://doi.org/10.{i}"},
                    "prism:publicationDate": "2026", "description": "摘要"}
                   for i in range(5)]
        payload = {"items": records}

        def fake_json(url, timeout, engine=""):
            return payload

        monkeypatch.setattr(bi, "_http_json", fake_json, raising=False)
        spec = {"_name": "cnii", "timeout": 5}
        engine = bi._build_cnii_engine(spec)
        out = engine("metasearch", n=5)
        scores = [r["score"] for r in out]
        assert scores, "cnii 应返回结果"
        assert all(scores[i] > scores[i + 1] for i in range(len(scores) - 1)), scores
        assert scores[0] == 0.7, "首条应保持原档位常量"

    def test_ndl_descending(self, monkeypatch):
        """NDL（原先全部写死 0.7）同样递减。"""
        import engines_builders_intl as bi
        xml = "".join(
            f"<item><title>book {i}</title><link>https://ndl.go.jp/{i}</link>"
            f"<description>d</description></item>" for i in range(4)
        )
        raw = f"<rss><channel>{xml}</channel></rss>".encode()

        class FakeResp:
            def __init__(self, data): self._d = data
            def read(self): return self._d
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(bi, "http_open", lambda req, timeout=None, engine="": FakeResp(raw),
                            raising=False)
        spec = {"_name": "ndl", "timeout": 5}
        engine = bi._build_ndl_engine(spec)
        out = engine("tesla", n=4)
        scores = [r["score"] for r in out]
        assert scores
        assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)), scores
        assert scores[0] == 0.7
