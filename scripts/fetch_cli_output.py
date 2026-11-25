"""抓取命令的 JSON 摘要与终端正文输出。"""

from cli_io import dumps


# 人类可读输出的默认显示上限（字符）。--limit 管的是交付窗口，这里是显示上限；
# 显式 --full / --limit 时按调用方要求放开，截断时打印可翻页的提示。
_DISPLAY_CHARS = 2000


def print_result(r: dict, *, json_only: bool, include_links: bool, focus: str,
                 pre_focus_len: int, full: bool, limit: int) -> None:
    focus_requested = bool(focus)
    summary = {k: r[k] for k in ("success", "fetch_method", "dispatch_handler",
                                  "content_ok", "quality_score", "page_type",
                                  "source_type", "is_official", "length",
                                  "url", "title", "video_meta") if k in r}
    if json_only and "content" in r:
        summary["content"] = r["content"]
    # 截断必须可见：只给 8,000 字却不说明「还有多少没给」，调用方会当全文用
    for k in ("truncated", "full_length", "full_text_path", "total_length",
              "offset", "full_source", "final_url",
              "login_state_used", "cache_eligible"):
        if k in r:
            summary[k] = r[k]
    if "content_type" in r and "next_offset" in r:
        summary.update({k: r[k] for k in ("status", "content_type", "next_offset")})
        if json_only:
            summary["content"] = r["content"]
    if focus_requested:
        # 显式回报聚焦是否真的生效：正文过短时 focus_applied=False，
        # 不谎报「已省 token」
        summary["focus_applied"] = bool(r.get("focus_applied"))
    if r.get("error"):
        summary["error"] = r["error"]
    if r.get("links"):
        summary["links_count"] = len(r["links"])
        if include_links:
            summary["links"] = r["links"]
    print(dumps(summary))
    if not json_only:
        if r.get("title"):
            print(f"\nTitle: {r['title']}")
        if focus_requested:
            print(f"\n[focus] query={focus!r} applied={bool(r.get('focus_applied'))} "
                  f"chars={pre_focus_len} → {r.get('length', 0)}")
        print(f"\n--- CONTENT ({r['length']} chars) ---")
        # 默认档保持 2000 字显示上限（护住 agent 上下文），但显式 --full /
        # --limit 时必须按调用方要求显示完整交付窗口：此前这行硬编码
        # [:2000]，与 --limit 的语义（本次输出字符数上限，0 表示不限）冲突，
        # 且表头写的是交付长度，于是「说 8000 字、只给 2000 字、不说明还剩
        # 多少」——调用方会把截断正文当全文用（证据核验场景尤其危险）。
        delivered = r.get("content", "") or ""
        show_all = bool(full) or bool(limit and limit > 0)
        shown = delivered if show_all else delivered[:_DISPLAY_CHARS]
        print(shown)
        if len(shown) < len(delivered):
            # 两个字段是不同代码路径写的同一事实：fetch_v3 路径给 full_length，
            # _full_view 路径给 total_length，故这里按可用者兜底。
            full_len = r.get("full_length") or r.get("total_length") or len(delivered)
            if focus_requested:
                # --focus 之后 content 是重排后的段落抽取，不再是正文的连续窗口，
                # 按「本页起点 + 本页长度」推 --offset 会指向无关区间。宁可不给
                # 翻页建议，也不能给错的。
                print(f"\n[truncated] 已显示 {len(shown)}/{len(delivered)} 字"
                      f"（页面全文 {full_len} 字）。当前为 --focus 抽取结果，"
                      f"非正文连续窗口；要完整正文请用 --full")
            else:
                # --offset 是相对正文全文的绝对偏移（见 _full_view），所以下一页
                # 要从「本页起点 + 本页长度」继续，否则已翻过页的调用方会原地打转。
                next_offset = int(r.get("offset") or 0) + len(shown)
                print(f"\n[truncated] 已显示 {len(shown)}/{len(delivered)} 字"
                      f"（页面全文 {full_len} 字）。"
                      f"继续读：--offset {next_offset} --limit {_DISPLAY_CHARS}；"
                      f"全文：--full")
