"""抓取命令行的参数定义。"""

import argparse


def build_parser() -> argparse.ArgumentParser:
    """构建抓取命令的参数解析器。"""
    p = argparse.ArgumentParser(description="Argo fetch v3 — 四级抓取（零依赖）")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--browser", "--use-browser", dest="browser",
                   action="store_true",
                   help="强制使用浏览器（--use-browser 同义）")
    p.add_argument("--no-fallback", action="store_true", help="禁用浏览器降级")
    p.add_argument("--actions", type=str,
                   help="页面交互 JSON（如 '[{\"click\":\"#btn\"}]'）")
    p.add_argument("--focus", type=str, default="",
                   help="BM25 聚焦关键词：只返回相关段落，省 token")
    p.add_argument("--focus-top", type=int, default=5,
                   help="--focus 无段落超阈值时的回退保留段落数（默认 5）")
    p.add_argument("--full", action="store_true",
                   help="取全文：优先读本地全文存档，没有则按抓取全量取回，"
                        "不受 --max-chars 限制（用于复核被截断的部分）")
    p.add_argument("--answer-offset", type=int, help="仅在用户明确要求续查知乎回答摘要时传入 next_offset")
    p.add_argument("--offset", type=int, default=0,
                   help="从正文第 N 个字符开始输出（配合 --limit 翻页读长文）")
    p.add_argument("--limit", type=int, default=0,
                   help="本次输出字符数上限；0 表示不额外限制")
    p.add_argument("--include-links", action="store_true",
                   help="JSON 摘要附带正文链接（默认只给 links_count，省 token）")
    p.add_argument("--json", action="store_true",
                   help="只输出 JSON 摘要（缺省还会附人类可读的正文段；"
                        "Usage 一直把 --json 列为 Common flags，此前却报"
                        " unrecognized arguments）")
    return p
