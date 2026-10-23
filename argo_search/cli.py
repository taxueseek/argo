"""Console entrypoint for the packaged Argo CLI."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _data_root() -> Path:
    return Path(__file__).resolve().parent / "_data"


def _run(relative: str) -> None:
    script = _data_root() / relative
    if not script.is_file():
        raise SystemExit(
            f"argo package data missing: {script}。"
            "请用 scripts/build_release_assets.sh 构建 wheel，或改用源码安装。"
        )
    # bin/argo 与 scripts/*.py 都按自身 __file__ 推导 ARGO_HOME/导入路径；
    # 把脚本目录放进 sys.path 后 runpy 的执行环境才与直接运行一致。
    sys.path.insert(0, str(script.parent))
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")


def main() -> None:
    _run("bin/argo")


if __name__ == "__main__":
    main()
