#!/usr/bin/env python3
"""gen_native_tools.py — 从 Python 真源生成 DSH 侧两个 ESM 常量表。

两张表都走同一份生成物 `packages/dsh-plugin/dsh/native-tools.mjs`（纯 ESM，
Node 18 可直接 import），避免 JS 侧手写第二份声明：

1. `NATIVE_TOOLS`：来源 `scripts/mcp_tools.py` 的 inputSchema。此前 index.js
   手写 argo_search/argo_fetch 两个规格，与 Python 侧已出现措辞/参数漂移空间。
2. `STATES`：来源 `scripts/engine_states.py` 的状态词表。此前 DSH 面板的
   ready/loading/unavailable、wide-research 的 conclusion_cap 三态、ZCode 桥
   的 daemon 四态全靠字面量，改词表不会让任何东西变红（2026-11-10 审计）。

生成的规格覆盖除 argo_research 外的全部工具：research 是分钟级调度，
CLI 单发默认 60s 超时会拦腰杀研究，只应经 MCP 或 wide_research 入口使用。

漂移检查：tests/test_native_tools_sync.py 重算本脚本输出并与仓库内文件
逐字节比对——改 mcp_tools.py 或 engine_states.py 后必须重新生成，否则红。

用法：
  python3 scripts/gen_native_tools.py          # 写文件
  python3 scripts/gen_native_tools.py --check  # 只校验不写（CI/测试用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from cli_io import dumps_pretty

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import engine_states  # noqa: E402
from mcp_tools import TOOLS, sanitize_tool  # noqa: E402

OUT_PATH = SCRIPT_DIR.parent / "packages" / "dsh-plugin" / "dsh" / "native-tools.mjs"

# CLI 单发形态排除项（见模块 docstring）
_EXCLUDED = {"argo_research"}

HEADER = """\
// 本文件由 scripts/gen_native_tools.py 从 Python 真源生成 —— 勿手改；
// 改 mcp_tools.py 或 engine_states.py 后重新生成，
// tests/test_native_tools_sync.py 负责漂移门禁。
//
// 说明：
//   - NATIVE_TOOLS：description / parameters 与 MCP 模型可见 schema 完全一致
//   - allowed = parameters.properties 键序（原生侧 pickArgs 白名单）
//   - 不含 argo_research：分钟级编排不适合 CLI 单发（默认超时会中断），
//     走 MCP 形态或 wide_research 入口
//   - STATES：engine_states.py 状态词表的只读投影，按状态机分组；
//     JS 侧一律从这里取值，不再手写 ready/low/running 等字面量
//
"""


def build_native_tools() -> dict:
    out: dict[str, dict] = {}
    for tool in TOOLS:
        tool = sanitize_tool(tool)
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


def build_states() -> dict:
    """engine_states.py 跨侧需要的词表投影（按状态机分组）。

    每组输出 `{语义名: 状态值}` 而非数组：JS 侧按名字取值
    （`STATES.conclusionCap.low`），不依赖数组顺序——此前按字母序解构
    会把 low/medium/high 全对错位。排序只为生成物稳定。
    """
    def as_enum(values) -> dict:
        return {v: v for v in sorted(values)}

    return {
        "plugin": as_enum(engine_states.PLUGIN_STATES),
        "daemon": as_enum(engine_states.DAEMON_STATES),
        "conclusionCap": as_enum(engine_states.CONCLUSION_CAP_STATUSES),
    }


def render(native_tools: dict, states: dict) -> str:
    tools_body = dumps_pretty(native_tools)
    states_body = dumps_pretty(states)
    return (HEADER
            + "export const NATIVE_TOOLS = " + tools_body + ";\n\n"
            + "export const STATES = " + states_body + ";\n")


def main() -> int:
    native_tools = build_native_tools()
    states = build_states()
    rendered = render(native_tools, states)
    if "--check" in sys.argv:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != rendered:
            print(f"FAIL: {OUT_PATH.name} 与 Python 真源漂移——请运行 "
                  f"python3 scripts/gen_native_tools.py 重新生成", file=sys.stderr)
            return 1
        print(f"OK: {len(native_tools)} 个原生工具规格 + {len(states)} 组状态词表与真源一致")
        return 0
    OUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"已生成 {OUT_PATH}（{len(native_tools)} 个工具 + {len(states)} 组状态词表）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
