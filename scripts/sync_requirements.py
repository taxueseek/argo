#!/usr/bin/env python3
"""sync_requirements.py — 依赖派生清单生成与校验（唯一真源：pyproject.toml）

设计目标：依赖变更只改一处真源（pyproject.toml），派生清单自动生成，消灭手工同步。

派生关系：
  pyproject.toml（唯一真源）
    ├── requirements-dev.txt       由 [dependency-groups].dev 派生（开发/测试依赖）
    └── requirements-optional.txt  由 [project.optional-dependencies].full 派生（全量可选依赖）

用法：
  python3 scripts/sync_requirements.py          # 重新生成两份派生清单
  python3 scripts/sync_requirements.py --check  # 校验派生清单是否与真源一致（不写盘）

退出码：0=一致/生成成功，1=校验不一致（--check 模式）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
DEV_TXT = ROOT / "requirements-dev.txt"
OPT_TXT = ROOT / "requirements-optional.txt"

DEV_HEADER = """# 开发与 CI 依赖（运行期不强制，这些只在测试/门禁/CI 里用）
#
# ⚠️ 本文件是**派生清单**，不是依赖真源。真源在 pyproject.toml 的
#    [dependency-groups].dev；内容与它的对应关系由
#    tests/test_dependency_source_gate.py 机械对账，漂移即红。
#    改依赖请改 pyproject.toml，再跑：
#        python3 scripts/sync_requirements.py
#
# 为什么锁死 pytest 版本：门禁的判定标准必须可复现。pytest 大版本会改收集语义
# （9.x 起 subtests 内建），pytest-timeout 的默认行为也会随版本变。
#
# 版本与 pyproject.toml 一致（含 3.9 档回落）：pytest 9.x 要求 Python >=3.10，
# 3.9 档由 lock 解析到 pytest 8.3.5。这里保持「只写 >=3.10 档」的钉版写法，
# 是因为 pip 用户装这份清单就是在装开发档；3.9 档请直接用 uv sync。
"""

OPT_HEADER = """# 可选依赖（缺失时主流程必须仍然可用；这九件在代码里全是函数内惰性导入）
#
# ⚠️ 本文件是**派生清单**，不是依赖真源。真源在 pyproject.toml 的
#    [project.optional-dependencies].full；内容与它的对应关系由
#    tests/test_dependency_source_gate.py 机械对账，漂移即红。
#    改依赖请改 pyproject.toml，再跑：
#        python3 scripts/sync_requirements.py
#
# CI 的 extras 作业装这一份，用来验证「装全之后仍然通过」；
# 「一个都不装」由 unit 作业天然覆盖，不需要另开一档。
#
# 全部带 `python_version >= '3.10'` 标记：python-jobspy 与 pymupdf 都不支持 3.9，
# 写进 pyproject 时按 marker 分档，所以 3.9 机器上装这份清单会少几件——这是
# 预期行为，缺失时主流程仍可用。
"""


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _clean_entry(spec: str) -> str | None:
    """提取适合 requirements.txt 纯净书写的 spec（过滤掉 3.9 回落的旧版钉版）。"""
    s = spec.strip()
    if not s or s.startswith("#"):
        return None
    # 过滤显式给 python_version < '3.10' 的回落行（如 pytest==8.3.5）
    if "< '3.10'" in s or "<= '3.9'" in s:
        return None
    # 去除环境标记（requirements.txt 给 pip 普通用户装最新档）
    s = re.sub(r";\s*python_version.*$", "", s).strip()
    return s


def generate_dev_txt(data: dict) -> str:
    dev_deps = (data.get("dependency-groups") or {}).get("dev") or []
    lines = [DEV_HEADER.rstrip()]
    pinned = []
    test_deps = []
    for raw in dev_deps:
        cleaned = _clean_entry(raw)
        if not cleaned:
            continue
        if "==" in cleaned:
            pinned.append(cleaned)
        else:
            test_deps.append(cleaned)

    lines.append("")
    lines.extend(pinned)
    lines.append("")
    lines.append("# 下面三件是「测试本身要用的可选依赖」，不是运行期依赖：")
    lines.append("#   curl_cffi  —— 一批用例用 mock 替换 curl_cffi.requests 验 TLS 指纹与 429 停止")
    lines.append("#                 轮换，补丁目标必须真实存在；缺件时这些用例会 skip（tests/_deps.py）")
    lines.append("#   bs4 / lxml —— HTML 引擎解析链，缺件时 keep 数归零，用例会红成「解析坏了」")
    lines.append("# 装在这里的原因：CI 的 unit 作业要跑的是**真实行为**，不是跳过版。")
    lines.append("# 九件可选依赖全装的那档见 requirements-optional.txt 与 extras 作业。")
    lines.extend(test_deps)
    lines.append("")
    return "\n".join(lines)


def generate_opt_txt(data: dict) -> str:
    opt_deps = (data.get("project", {}).get("optional-dependencies") or {}).get("full") or []
    lines = [OPT_HEADER.rstrip(), ""]
    for raw in opt_deps:
        cleaned = _clean_entry(raw)
        if cleaned:
            lines.append(cleaned)
    lines.append("")
    return "\n".join(lines)


def sync(check_only: bool = False) -> bool:
    data = _load_pyproject()
    dev_content = generate_dev_txt(data)
    opt_content = generate_opt_txt(data)

    current_dev = DEV_TXT.read_text(encoding="utf-8") if DEV_TXT.is_file() else ""
    current_opt = OPT_TXT.read_text(encoding="utf-8") if OPT_TXT.is_file() else ""

    dev_match = (current_dev.strip() == dev_content.strip())
    opt_match = (current_opt.strip() == opt_content.strip())

    if check_only:
        if not dev_match:
            print("requirements-dev.txt 与 pyproject.toml 存在漂移，请运行: python3 scripts/sync_requirements.py", file=sys.stderr)
        if not opt_match:
            print("requirements-optional.txt 与 pyproject.toml 存在漂移，请运行: python3 scripts/sync_requirements.py", file=sys.stderr)
        return dev_match and opt_match

    DEV_TXT.write_text(dev_content, encoding="utf-8")
    OPT_TXT.write_text(opt_content, encoding="utf-8")
    print(f"✔ 已由 pyproject.toml 自动派生更新: {DEV_TXT.name}, {OPT_TXT.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="依赖派生清单同步与校验工具")
    parser.add_argument("--check", action="store_true", help="校验派生清单是否与真源一致")
    args = parser.parse_args()

    ok = sync(check_only=args.check)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
