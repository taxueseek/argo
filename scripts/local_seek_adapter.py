#!/usr/bin/env python3
"""seek.py → argo CLI 桥接适配器（跨平台兼容与来源隔离）。

1. 来源隔离：严格标注 [本机] 前缀、source_type='local'、channel='local_filesystem'、
   is_local=True，生成规范 file:// URI，杜绝与公网/联网搜索结果混淆。
2. 跨平台开箱即用：显式 utf-8 管道编码，防 Windows GBK 控制台乱码；Path.resolve().as_uri()
   同时支持 Windows 驱动器盘符与 POSIX 路径；提供进程内 run_query 容错降级。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SEEK_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "local-seek" / "scripts"
SEEK = SEEK_DIR / "seek.py"


def _run_seek(argv: list[str]) -> dict:
    """运行 seek.py，优先子进程隔离，受限环境降级为进程内调用。"""
    try:
        proc = subprocess.run(
            [sys.executable, str(SEEK), *argv, "--json"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0 and proc.stdout:
            return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass

    # 进程内容错降级
    try:
        if str(SEEK_DIR) not in sys.path:
            sys.path.insert(0, str(SEEK_DIR))
        import seek as seek_mod
        text, rc = seek_mod.run_query([*argv, "--json"], time_budget=30.0)
        if rc == 0 and text:
            return json.loads(text)
    except Exception:
        pass
    return {}


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--json"]
    data = _run_seek(argv)

    base = data.get("path") or "."
    out: list[dict] = []
    base_path = Path(base).resolve()

    for r in data.get("results", []):
        if not isinstance(r, dict):
            continue
        p = str(r.get("path") or "")
        line = r.get("line")
        snippet = str(r.get("snippet") or "")[:300]
        # 来源显式隔离标识：明确标记为 [本机]
        title_loc = f"{p}:{line}" if line else p
        row: dict = {
            "title": f"[本机] {title_loc}",
            "snippet": f"[本机文件] {snippet}" if not snippet.startswith("[本机") else snippet,
            "source_type": "local",
            "channel": "local_filesystem",
            "is_local": True,
            "engine": "local_seek",
        }
        if p:
            try:
                abs_p = (base_path / p).resolve()
                row["url"] = abs_p.as_uri()
            except (ValueError, OSError):
                norm_p = p.replace("\\", "/")
                row["url"] = f"file:///{norm_p.lstrip('/')}"
        if r.get("mtime"):
            row["published_at"] = str(r["mtime"])
        out.append(row)

    print(json.dumps({"results": out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
