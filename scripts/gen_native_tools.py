#!/usr/bin/env python3
"""gen_native_tools.py — 从 schema 唯一真源生成 DSH 原生工具规格表。

schema 真源是 scripts/mcp_tools.py（模型可见 inputSchema 只在该文件维护）。
本脚本把它翻译为 packages/dsh-plugin/dsh/native-tools.mjs（纯 ESM 常量，
Node 18 可直接 import），使 DSH 原生一等工具（nativeTools 配置）无需手写
第二份 schema——此前 index.js 手写 argo_search/argo_fetch 两个规格，与
Python 侧已经出现措辞/参数漂移空间。

生成的规格覆盖除 argo_research 外的全部工具：research 是分钟级编排，
CLI 单发默认 60s 超时会拦腰杀研究，只应经 MCP 或 wide_research 入口使用。

漂移门禁：tests/test_native_tools_sync.py 重算本脚本输出并与仓库内文件
逐字节比对——改 mcp_tools.py 后必须重新生成，否则测试红。

用法：
  python3 scripts/gen_native_tools.py          # 写文件
  python3 scripts/gen_native_tools.py --check  # 只校验不写（CI/测试用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from mcp_tools import TOOLS  # noqa: E402

OUT_PATH = SCRIPT_DIR.parent / "packages" / "dsh-plugin" / "dsh" / "native-tools.mjs"

# CLI 单发形态排除项（见模块 docstring）
_EXCLUDED = {"argo_research"}

HEADER = """\
// 本文件由 scripts/gen_native_tools.py 从 schema 唯一真源
// scripts/mcp_tools.py 生成 —— 勿手改；改 schema 后重新生成，
// tests/test_native_tools_sync.py 负责漂移门禁。
//
// 说明：
//   - description / parameters 与 MCP 模型可见 schema 完全一致
//   - allowed = parameters.properties 键序（原生侧 pickArgs 白名单）
//   - 不含 argo_research：分钟级编排不适合 CLI 单发（默认超时会中断），
//     走 MCP 形态或 wide_research 入口
//
"""


def build_native_tools() -> dict:
    out: dict[str, dict] = {}
    for tool in TOOLS:
        name = tool["name"]
        if name in _EXCLUDED:
            continue
        schema = tool["inputSchema"]
        out[name] = {
            "description": tool["description"],
            "parameters": {
                **schema,
                "additionalProperties": False,
            },
            "allowed": list(schema.get("properties", {}).keys()),
        }
    return out


def render(native_tools: dict) -> str:
    body = json.dumps(native_tools, ensure_ascii=False, indent=2)
    return HEADER + "export const NATIVE_TOOLS = " + body + ";\n"


def main() -> int:
    native_tools = build_native_tools()
    rendered = render(native_tools)
    if "--check" in sys.argv:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != rendered:
            print(f"FAIL: {OUT_PATH.name} 与 mcp_tools.py 漂移——请运行 "
                  f"python3 scripts/gen_native_tools.py 重新生成", file=sys.stderr)
            return 1
        print(f"OK: {len(native_tools)} 个原生工具规格与 mcp_tools.py 一致")
        return 0
    OUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"已生成 {OUT_PATH}（{len(native_tools)} 个工具）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
