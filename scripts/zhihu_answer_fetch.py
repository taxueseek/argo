"""Fetch a verified Zhihu answer or an explicitly labelled API summary."""

from __future__ import annotations

import math
import re
import time


_ANSWER = re.compile(
    r"https://www\.zhihu\.com/question/([1-9][0-9]{0,19})/answer/([1-9][0-9]{0,19})"
)


def match_answer(url: str):
    return _ANSWER.fullmatch(url)


def finish_focus(result, max_chars):
    if "next_offset" in result and max_chars > 0:
        content = result["content"][:max_chars]
        result.update(content=content, length=len(content),
                      truncated=result["truncated"] or result.get("focus_applied", False)
                      or len(content) < result["full_length"])
    return result


def fetch_answer(url, *, offset, max_chars, timeout, deadline_s=None, browser=True):
    def result(status, kind, text, next_offset=None):
        content = text[:max_chars] if max_chars > 0 else text
        return dict(
            url=url,
            status=status,
            content_type=kind,
            content=content,
            success=status == "found",
            next_offset=next_offset,
            length=len(content),
            full_length=len(text),
            truncated=content != text,
        )

    match = match_answer(url)
    if not match or (offset is not None and (type(offset) is not int or offset < 0)):
        return result(
            "error", "status", "需要精确的知乎回答 URL 和非负整数 answer_offset。"
        )
    if not math.isfinite(timeout) or timeout <= 0 or max_chars < 0:
        return result("error", "status", "抓取预算无效。")
    budget = min(timeout, deadline_s) if deadline_s and deadline_s > 0 else timeout
    deadline = time.monotonic() + budget
    question, answer = match.groups()
    if offset is None and browser:
        from zhihu_answer_body import read_body

        text = read_body(question, answer, max(0, deadline - time.monotonic()) / 2)
        if text and time.monotonic() < deadline:
            return result("found", "body", text)
    from zhihu_answer_lookup import lookup

    item = lookup(question, answer, offset or 0, deadline)
    if item["url"] != url:
        return result("error", "status", "摘要来源与目标回答不符。")
    return result(
        item["status"], item["content_type"], item["text"], item.get("next_offset")
    )
