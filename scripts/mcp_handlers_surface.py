"""mcp_handlers_surface — 2026-09-26 补齐 CLI 面的五工具 handler。

extract/preflight/answer/watch/cite 的执行体独立成模块：mcp_handlers.py
涨破 1000 行硬上限（门禁），按 route_* 先例拆分。handle_surface_tool
返回 None 表示非本模块工具，调用方继续走未知工具分支。
"""

from __future__ import annotations

from typing import Any

from mcp_handlers import (
    _cap_extract_output,
    _clamp_int,
    _dumps,
    _err_is,
    _lazy_cached,
    _ok,
    _required_params,
)

# 必填参数校验已收口到 mcp_handlers._required_params（从 mcp_tools 的
# schema 真读，单一真源）。历史教训保留：本模块刚拆分时曾自抄一份必填表
# 且五个新工具全没进 mcp_handlers 的手抄表，execute_tool("argo_answer", {})
# 抛 KeyError 变 -32000 而不是契约的 -32602。当时本模块的修法是自行读
# schema；现在主表也改读 schema，两处消费同一函数，不再可能漂移。


def _check_required(name: str, arguments: dict[str, Any]) -> list[str]:
    """返回缺失的必填参数名；空 list = 通过。"""
    return [p for p in _required_params(name) if not arguments.get(p)]


# 本模块承接的工具名。放在这里而不是靠 if 链自然落空：新增一个工具时
# 「忘了加进这个集合」会立刻表现为返回 None（被调用方当成未知工具），
# 比静默走进某个分支更容易发现。
_SURFACE_TOOLS = frozenset({
    "argo_extract", "argo_preflight", "argo_answer", "argo_watch", "argo_cite",
})


def handle_surface_tool(name: str, arguments: dict[str, Any],
                        pretty: bool = False) -> dict[str, Any] | None:
    """五工具（extract/preflight/answer/watch/cite）分发；None=非本模块工具。"""
    if name not in _SURFACE_TOOLS:
        return None
    # 必填校验放在分发之后、任何 arguments[...] 取值之前：缺参数要报
    # -32602（契约规定的「Invalid params」），而不是让 arguments["query"]
    # 抛 KeyError 后被兜底 except 变成 -32000 —— 后者对调用方毫无信息量，
    # 也不知道该补哪个参数。
    _missing = _check_required(name, arguments)
    if _missing:
        return {
            "content": [{"type": "text", "text": _dumps({
                "error": {
                    "code": -32602,
                    "message": "Missing required parameter(s): "
                               + ", ".join(_missing),
                },
            })}],
            "isError": True,
        }

    if name == "argo_extract":
        extract_mod = _lazy_cached("extract")
        max_chars = _clamp_int(arguments.get("max_chars", 50000), 50000, 1000, 200000)
        fetched = extract_mod._extract_fetch(arguments["url"], max_chars, 15)
        if not fetched.get("success"):
            return {"content": [{"type": "text", "text": _dumps(
                {"error": fetched.get("error") or "抓取失败"})}], "isError": True}
        html = fetched.get("html") or fetched.get("content") or ""
        mode = arguments.get("mode", "all")
        out = {}
        if mode in ("tables", "all"):
            out["tables"] = extract_mod.extract_tables(html)
        if mode in ("metadata", "all"):
            out["metadata"] = extract_mod.extract_metadata(html)
        if mode in ("jsonld", "all"):
            out["jsonld"] = extract_mod.extract_jsonld(html)
        # 输出上限不是可选优化：表格数/单元格、metadata 值、jsonld 体积各自
        # 设限并在超限时打 *_truncated 标记。拆分本模块时这行曾被漏掉，
        # 于是 _cap_extract_output 成了孤儿函数、argo_extract 变成唯一一条
        # 没有上限的正文抽取路径——恰好是它最需要的那个（表格多的页面）。
        return _ok(_cap_extract_output(out), pretty=pretty)

    if name == "argo_preflight":
        probe_mod = _lazy_cached("batch_probe")
        urls = [str(u).strip() for u in arguments.get("urls") or [] if str(u).strip()]
        if not urls:
            return {"content": [{"type": "text", "text": _dumps(
                {"error": "urls 为空"})}], "isError": True}
        report = probe_mod.probe_batch(urls, do_probe=bool(arguments.get("probe", False)))
        return _ok(report, pretty=pretty)

    if name == "argo_answer":
        answer_mod = _lazy_cached("answer")
        resp, err = answer_mod.seltz_answer(
            arguments["query"], timeout=40.0,
            scope=arguments.get("scope") or None,
            model=arguments.get("model") or None)
        if err or resp is None:
            return {"content": [{"type": "text", "text": _dumps(
                {"error": err or "直答无响应"})}], "isError": True}
        return _ok(resp, pretty=pretty)

    if name == "argo_watch":
        watch_mod = _lazy_cached("watch")
        action = arguments.get("action", "")
        url = arguments.get("url") or ""
        if action == "add":
            if not url:
                return _err_is("add 需要 url")
            return _ok(watch_mod.cmd_add(url, arguments.get("note", "")), pretty=pretty)
        if action == "check":
            return _ok(watch_mod.cmd_check(url or None), pretty=pretty)
        if action == "list":
            return _ok(watch_mod.cmd_list(), pretty=pretty)
        if action == "remove":
            if not url:
                return _err_is("remove 需要 url")
            removed = watch_mod.cmd_remove(url)
            return _ok({"removed": bool(removed), "url": url}, pretty=pretty)
        return _err_is(f"未知 action: {action}")

    if name == "argo_cite":
        cite_mod = _lazy_cached("citations")
        style = arguments.get("style", "gbt7714")
        dois = [str(d).strip() for d in arguments.get("dois") or [] if str(d).strip()]
        if not dois:
            return {"content": [{"type": "text", "text": _dumps(
                {"error": "dois 为空"})}], "isError": True}
        citations_out = []
        used_keys: set = set()
        for doi in dois:
            try:
                meta = cite_mod.fetch_metadata(doi)
                citations_out.append({
                    "doi": doi,
                    "citation": cite_mod.format_citation(meta, style, used_keys),
                })
            except Exception as e:
                citations_out.append({"doi": doi, "error": f"{type(e).__name__}: {e}"})
        return _ok({"style": style, "citations": citations_out}, pretty=pretty)

    return None
