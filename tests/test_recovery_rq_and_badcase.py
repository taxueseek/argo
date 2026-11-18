"""回归测试：recovery_runner 传递改写后的 rq，以及 empty_result 坏例归因。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import badcase_causes
import badcase_ledger
import recovery_runner


class _DummyQuota:
    def add(self, eng: str, ok: bool) -> None:
        pass


def test_recovery_runner_passes_rewritten_query_rq() -> None:
    seen_queries: list[tuple[str, str]] = []

    def fake_search(q: str, eng: str, **_kw):
        seen_queries.append((q, eng))
        # 原查询包含引号和尾修饰，只有放宽后的查询才返回结果
        if "「" not in q and "请问" not in q:
            return [{"title": "GB/T 15834-2011 标点符号用法", "url": "https://example.com/1", "snippet": "GB/T 15834-2011 国家标准"}]
        return []

    merged, info = recovery_runner.run_pipeline_recovery(
        query='请问 "GB/T 15834-2011" 最新',
        merged=[],
        raw_results={"bocha": []},
        decision={"domain": "general", "engines_fallback": ["bing"]},
        engines=["bocha"],
        available_engines_fn=lambda routable_only=True: ["bocha", "bing"],
        max_results=5,
        max_rec_level="L3",
        echo_triggered=False,
        primary_grace_s=0.0,
        timeout=2.0,
        depth="balanced",
        mode="auto",
        since_iso=None,
        until_iso=None,
        skip_cache=False,
        quota_batch=_DummyQuota(),
        engine_search_fn=fake_search,
    )

    assert len(merged) == 1
    assert info is not None and info["recovered"] is True
    assert any("请问" not in q for q, _ in seen_queries), f"expected relaxed query, got {seen_queries}"


def test_empty_result_classified_as_parse_empty() -> None:
    cause = badcase_ledger.classify_root_cause("empty_result", "count=0, mode=auto")
    assert cause == badcase_causes.ROOT_CAUSE_PARSE_EMPTY
