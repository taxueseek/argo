#!/usr/bin/env python3
"""seek.py → argo CLI 桥接适配器（纯结构翻译，零逻辑）。

seek.py 原生 JSON 是 path/line/snippet/mtime（本机文件检索契约）；CLI 桥接
的行映射吃 title/url/snippet/published_at（argo 结果契约，固定别名表不含
path）。直连会产出 title/url 全空的空壳条目被下游清掉，故经此适配。
seek.py 的参数原样透传，末尾强制 --json；url 产出 file:// URI（本地文件
条目的真实打开方式），mtime 进 published_at 供时效维度消费。

实测背景（2026-11-10）：seek.py --json 96ms（rg 引擎，code scope），
30 条结构化结果；L3 插件机制（ENGINE_TYPE/build_engine）文档先行未落地，
故走「适配器脚本 + CLI 桥接」而非插件。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SEEK = (
    Path(__file__).resolve().parent.parent
    / "sub-skills" / "local-seek" / "scripts" / "seek.py"
)


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--json"]
    try:
        proc = subprocess.run(
            [sys.executable, str(SEEK), *argv, "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError, OSError):
        data = {}

    base = data.get("path") or "."
    out: list[dict] = []
    for r in data.get("results", []):
        if not isinstance(r, dict):
            continue
        p = str(r.get("path") or "")
        line = r.get("line")
        row = {
            "title": f"{p}:{line}" if line else p,
            "snippet": str(r.get("snippet") or "")[:300],
        }
        if p:
            try:
                row["url"] = Path(base).joinpath(p).resolve().as_uri()
            except (ValueError, OSError):
                pass
        if r.get("mtime"):
            row["published_at"] = str(r["mtime"])
        out.append(row)
    print(json.dumps({"results": out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
