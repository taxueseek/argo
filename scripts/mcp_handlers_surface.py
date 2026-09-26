"""mcp_handlers_surface — 2026-09-26 补齐 CLI 面的五工具 handler。

extract/preflight/answer/watch/cite 的执行体独立成模块：mcp_handlers.py
涨破 1000 行硬上限（门禁），按 route_* 先例拆分。handle_surface_tool
返回 None 表示非本模块工具，调用方继续走未知工具分支。
"""

from __future__ import annotations

from typing import Any

from mcp_handlers import _clamp_int, _dumps, _err_is, _lazy_cached, _ok


def handle_surface_tool(name: str, arguments: dict[str, Any],
                        pretty: bool = False) -> dict[str, Any] | None:
    """五工具（extract/preflight/answer/watch/cite）分发；None=非本模块工具。"""
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
        return _ok(out, pretty=pretty)

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
