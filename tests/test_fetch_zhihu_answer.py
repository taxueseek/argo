"""Zhihu fetch orchestration contract, independent of browser and API access."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import zhihu_answer_fetch as fetch
import zhihu_answer_body
import zhihu_answer_lookup

URL = "https://www.zhihu.com/question/123/answer/456"


def call(**kwargs):
    return fetch.fetch_answer(
        URL, **dict(dict(offset=None, max_chars=3, timeout=10), **kwargs)
    )


@pytest.mark.parametrize(
    "url",
    [
        URL + "/x",
        URL + "?q=x",
        URL + "#x",
        URL.replace(".com", ".com.fake"),
        URL.replace("123", "%31"),
        URL.replace("https:", "http:"),
    ],
)
def test_only_exact_url(url):
    assert not fetch.match_answer(url)


def test_body_first_budget_and_truncation(monkeypatch):
    body = Mock(return_value="完整回答")
    monkeypatch.setattr(zhihu_answer_body, "read_body", body)
    monkeypatch.setattr(zhihu_answer_lookup, "lookup", Mock(side_effect=AssertionError))
    monkeypatch.setattr(fetch.time, "monotonic", lambda: 100)
    out = call(deadline_s=4)
    body.assert_called_once_with("123", "456", 2)
    assert (
        out["content_type"],
        out["content"],
        out["full_length"],
        out["truncated"],
    ) == ("body", "完整回", 4, True)


@pytest.mark.parametrize("offset,attempt_body", [(None, True), (0, False), (50, False)])
def test_manual_cursor_and_shared_deadline(monkeypatch, offset, attempt_body):
    body = Mock(return_value="")
    lookup = Mock(
        return_value=dict(
            status="continue",
            text="待续",
            url=URL,
            content_type="status",
            next_offset=200,
        )
    )
    monkeypatch.setattr(zhihu_answer_body, "read_body", body)
    monkeypatch.setattr(zhihu_answer_lookup, "lookup", lookup)
    monkeypatch.setattr(fetch.time, "monotonic", lambda: 100)
    out = call(offset=offset)
    assert body.called == attempt_body
    lookup.assert_called_once_with("123", "456", offset or 0, 110)
    assert (out["status"], out["next_offset"], out["success"]) == (
        "continue",
        200,
        False,
    )


def test_summary_requires_matching_source(monkeypatch):
    item = dict(status="found", text="摘录", url=URL, content_type="summary")
    monkeypatch.setattr(zhihu_answer_lookup, "lookup", Mock(return_value=item))
    assert call(offset=0)["content_type"] == "summary"
    item["url"] += "7"
    assert call(offset=0)["status"] == "error"
