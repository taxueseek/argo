#!/usr/bin/env python3
"""local_search_adapter.py — local-search 兼容入口

保留原 adapter 接口，内部委托 search_v3.search_engines 执行。
unified-search 可通过 --sub-skill local-search 或 --local-first 调用本入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

from search_v3 import search_engines


def _parse_engine_list(value: str) -> list[str]:
    return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="local-search 兼容入口（委托 search_v3）")
    parser.add_argument("query", nargs="?", help="搜索关键词")
    parser.add_argument("--engine", "-e", default="", help="引擎名，多个用逗号分隔")
    parser.add_argument("--n", type=int, default=5, help="每引擎结果数")
    parser.add_argument("--timeout", "-t", type=float, default=None, help="超时秒数")
    parser.add_argument("--max-parallel", type=int, default=5)
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存")
    parser.add_argument("--mode", default="fast", choices=["fast", "auto", "deep", "budget"],
                        help="unified-search 模式透传")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if not args.query:
        parser.error("必须提供搜索关键词")

    engines = _parse_engine_list(args.engine) if args.engine else None
    result = search_engines(
        args.query,
        engines=engines,
        n=args.n,
        timeout=args.timeout,
        max_parallel=args.max_parallel,
        skip_cache=args.no_cache,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
