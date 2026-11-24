"""知乎回答定位：只读接口契约、分页和共同截止时间。"""

import io
import json
import sys
import threading
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import zhihu_answer_lookup as z


Q, A = "123", "987"
URL = f"https://www.zhihu.com/question/{Q}/answer/{A}"
TARGET = dict(Url=URL, ContentType="answer", ContentToken=A, Summary="<em>摘要</em>")


def page(items=(), cursor=None):
    paging = {"IsEnd": cursor is None, "NextOffset": cursor}
    return {"Code": 0, "Data": {"Items": list(items), "Paging": paging}}


@pytest.fixture
def server(monkeypatch):
    replies, calls = [], []
    monkeypatch.setattr(z, "get_env", lambda names: "fixture")

    def serve(req, timeout):
        url = urlsplit(req.full_url)
        params = parse_qs(url.query)
        assert req.get_method() == "GET"
        assert (url.scheme, url.netloc, url.path) == (
            "https",
            "developer.zhihu.com",
            "/api/v1/content/question_answers",
        )
        assert params["QuestionUrl"] == [URL.split("/answer/")[0]]
        assert params["Limit"] == ["50"]
        assert req.unredirected_hdrs["Authorization"] == "Bearer fixture"
        assert 0 < timeout <= 10
        calls.append(int(params["Offset"][0]))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(
            reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        )

    monkeypatch.setattr(z, "open_url", serve)
    return replies, calls


def run(offset=0):
    return z.lookup(Q, A, offset, monotonic() + 10)


def test_exact_pair_and_early_stop(server):
    replies, calls = server
    misses = [
        dict(TARGET, Url=URL.replace("/123/", "/321/")),
        dict(TARGET, Url=URL + "0"),
        dict(TARGET, ContentToken="9870"),
        dict(TARGET, ContentType="article"),
    ]
    replies.extend([page(misses, 14), page(), page()])
    replies[1] = page([TARGET], 81)
    assert run(7) == dict(status="found", text="摘要", url=URL, content_type="summary")
    assert calls == [7, 14]


def test_four_pages_then_resume_to_end(server):
    replies, calls = server
    replies.extend([page(cursor=n) for n in [9, 36, 40, 98]] + [page()])
    result = run()
    assert result["status"] == "continue" and result["content_type"] == "status"
    assert result["next_offset"] == 98 and calls == [0, 9, 36, 40]
    result = run(result["next_offset"])
    assert result["status"] == "not_found" and "next_offset" not in result
    assert calls == [0, 9, 36, 40, 98]


@pytest.mark.parametrize(
    "payload",
    [
        {"Code": 401},
        {},
        {"Code": 0, "Data": {}},
        b"not json",
        *[
            {"Code": 0, "Data": {"Items": [], "Paging": fields}}
            for fields in (
                {},
                {"IsEnd": False},
                {"IsEnd": 0, "NextOffset": 50},
                *[
                    {"IsEnd": False, "NextOffset": value}
                    for value in [0, -1, True, "50"]
                ],
            )
        ],
        OSError("network"),
        HTTPError(URL, 429, "rate limit", {}, None),
        page([dict(TARGET, Summary="")], 40),
    ],
)
def test_errors_are_not_exhaustion(server, payload):
    replies, calls = server
    replies.append(payload)
    result = run()
    assert result["status"] == "error" and result["text"] and result["url"] == URL
    assert "next_offset" not in result and calls == [0]


def test_deadline_between_pages_uses_upstream_cursor(server, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(z, "monotonic", lambda: now[0])
    seen = []

    def first(question_url, offset, secret, deadline):
        seen.append((offset, deadline))
        now[0] = deadline
        return page(cursor=13)

    monkeypatch.setattr(z, "_page", first)
    result = z.lookup(Q, A, 5, 11.0)
    assert result["next_offset"] == 13 and seen == [(5, 11.0)]
    assert z.lookup(Q, A, 13, 11.0)["next_offset"] == 13 and len(seen) == 1


def test_blocking_transport_cannot_consume_extra_budget(server, monkeypatch):
    release = threading.Event()

    def stalled(*args):
        release.wait(2)
        return page(cursor=50)

    monkeypatch.setattr(z, "_request", stalled)
    start = monotonic()
    try:
        result = z.lookup(Q, A, 7, start + 0.05)
        assert monotonic() - start < 1
        assert result["status"] == "continue" and result["next_offset"] == 7
    finally:
        release.set()


def test_invalid_inputs_and_missing_key_make_no_requests(server, monkeypatch):
    assert z.lookup("bad", A, 0, monotonic() + 1)["status"] == "error"
    assert run(-1)["status"] == "error"
    monkeypatch.setattr(z, "get_env", lambda names: "")
    assert run()["status"] == "error"
    assert server[1] == []
