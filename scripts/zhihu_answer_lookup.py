"""按问题、回答 ID 读取知乎开放平台的回答摘要。"""

from __future__ import annotations

import json
import math
import re
import threading
from concurrent.futures import Future, TimeoutError as WaitTimeout
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

from engine_env import get_env
from net_proxy import open_url
from social_engines.zhihu_engine import (
    ZHIHU_QUESTION_ANSWERS_API,
    strip_html_tags,
    zhihu_headers,
)

_PAGE_LIMIT = 50
_MAX_PAGES = 4
_MAX_BYTES = 2 * 1024 * 1024
_IN_FLIGHT = threading.BoundedSemaphore(8)


def _request(question_url, offset, secret, deadline):
    params = urlencode(
        {"QuestionUrl": question_url, "Limit": _PAGE_LIMIT, "Offset": offset}
    )
    req = Request(f"{ZHIHU_QUESTION_ANSWERS_API}?{params}")
    for key, value in zhihu_headers(secret).items():
        req.add_unredirected_header(key, value)
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    with open_url(req, timeout=remaining) as response:
        body = response.read(_MAX_BYTES + 1)
    if len(body) > _MAX_BYTES:
        raise ValueError("page exceeds size limit")
    return json.loads(body)


def _page(question_url, offset, secret, deadline):
    # urllib 的 socket 超时按单次 I/O 计；调用方只等待总预算内的时间。
    future = Future()
    if not _IN_FLIGHT.acquire(blocking=False):
        raise OSError("lookup capacity reached")

    def run():
        try:
            future.set_result(_request(question_url, offset, secret, deadline))
        except Exception as exc:
            future.set_exception(exc)
        finally:
            _IN_FLIGHT.release()

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError:
        _IN_FLIGHT.release()
        raise
    return future.result(timeout=max(0.0, deadline - monotonic()))


def lookup(question: str, answer: str, offset: int, deadline: float) -> dict:
    """读取最多四页。continue 的 next_offset 指向首个尚未检查的页。"""
    url = ""

    def result(status, text, **extra):
        return dict(
            status=status,
            text=text,
            url=url,
            content_type="summary" if status == "found" else "status",
            **extra,
        )

    if not all(
        isinstance(x, str) and re.fullmatch("[0-9]+", x) for x in (question, answer)
    ):
        return result("error", "问题 ID 和回答 ID 必须是十进制数字。")
    question_url = f"https://www.zhihu.com/question/{question}"
    url = f"{question_url}/answer/{answer}"
    if type(offset) is not int or offset < 0 or not math.isfinite(deadline):
        return result("error", "查询偏移或截止时间无效。")
    secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
    if not secret:
        return result("error", "缺少知乎开放平台凭据。")
    for _ in range(_MAX_PAGES):
        if monotonic() >= deadline:
            break
        try:
            payload = _page(question_url, offset, secret, deadline)
        except (TimeoutError, WaitTimeout):
            if monotonic() >= deadline:
                break
            return result("error", "知乎接口请求超时。")
        except HTTPError as exc:
            exc.close()
            return result("error", f"知乎接口返回 HTTP {exc.code}。")
        except (OSError, HTTPException, ValueError, RuntimeError) as exc:
            return result("error", f"知乎接口读取失败（{type(exc).__name__}）。")
        if not isinstance(payload, dict) or type(payload.get("Code")) is not int:
            return result("error", "知乎接口响应缺少有效状态码。")
        if payload["Code"] != 0:
            return result("error", f"知乎接口错误：Code={payload['Code']}。")
        data = payload.get("Data")
        if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
            return result("error", "知乎接口回答列表格式异常。")
        for item in data["Items"]:
            if not isinstance(item, dict):
                return result("error", "知乎接口回答条目格式异常。")
            if (
                item.get("Url") == url
                and str(item.get("ContentToken")) == answer
                and str(item.get("ContentType")).lower() == "answer"
            ):
                raw = item.get("Summary")
                text = strip_html_tags(raw) if isinstance(raw, str) else ""
                if text:
                    return result("found", text)
                return result("error", "已定位回答，知乎接口未提供摘要。")
        paging = data.get("Paging")
        if not isinstance(paging, dict) or type(paging.get("IsEnd")) is not bool:
            return result("error", "知乎接口缺少有效末页标记。")
        if paging["IsEnd"]:
            return result("not_found", "已到知乎接口末页，本次查询范围内未找到该回答。")
        cursor = paging.get("NextOffset")
        if type(cursor) is not int or cursor <= offset:
            return result("error", "知乎接口未提供有效的下一页偏移。")
        offset = cursor
    return result(
        "continue", f"尚未查完，可从偏移 {offset} 继续查询。", next_offset=offset
    )
