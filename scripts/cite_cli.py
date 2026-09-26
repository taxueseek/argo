#!/usr/bin/env python3
"""cite_cli.py — argo cite 子命令：DOI → 引用条目直出。

用法：argo cite DOI [DOI...] [--style gbt7714|gbt7714n|apa|bibtex] [--json]

多条 DOI 逐条独立处理：单条失败不中断其余（失败走 stderr / JSON error 项，
退出码 1）。引用格式化与元数据获取在 citations.py，本文件只做参数面与输出面。
"""

from __future__ import annotations

import argparse
import sys

from citations import CiteError, fetch_metadata, format_citation
from cli_io import dumps

STYLES = ("gbt7714", "gbt7714n", "apa", "bibtex")
DEFAULT_STYLE = "gbt7714"


def _run(dois: list[str], style: str, as_json: bool) -> int:
    """逐条取元数据并格式化。返回退出码：全部成功 0，任一失败 1。"""
    failed = False
    used_keys: set[str] = set()  # bibtex 批内键去重：同一批输出不撞键
    results: list[dict] = []
    for doi in dois:
        try:
            meta = fetch_metadata(doi)
            citation = format_citation(meta, style, used_keys=used_keys)
        except CiteError as e:
            failed = True
            if as_json:
                results.append({"doi": doi, "error": str(e)})
            else:
                print(f"错误: {doi}: {e}", file=sys.stderr)
            continue
        if as_json:
            results.append({"doi": doi, "style": style, "citation": citation})
        else:
            print(citation)
    if as_json:
        print(dumps(results))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="argo cite", description="DOI → 引用条目（GB/T 7714 / 顺序编码 / APA / BibTeX）")
    ap.add_argument("dois", nargs="+", help="DOI，可多个（空格分隔）")
    ap.add_argument("--style", choices=STYLES, default=DEFAULT_STYLE,
                    help="引用格式（默认 gbt7714）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)
    return _run(args.dois, args.style, args.json)


if __name__ == "__main__":
    sys.exit(main())
