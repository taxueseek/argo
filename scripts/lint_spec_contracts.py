#!/usr/bin/env python3
"""lint_spec_contracts.py — 引擎规格契约 lint（声明层复发护栏）。

背景：声明式引擎规格与消费层之间隔了四层契约（spec.search_args → CLI 实参
→ 解析格式 → output_map/envelope 键），历史上反复出的事故都卡在「层与层
之间没有机器校验」：
  - 2026-10-27 bilibili.search_args 少传 --json，social_meta 在 JSON 解析前
    就不存在，三层各错一点（video P0）
  - 2026-11-03 xhs CLI 未下发 --json，_parse_xhs_output 的 json.loads 失败
    静默返回空列表（issue #36）
本 lint 不做运行时校验（那是引擎与用例的职责），只在声明层钉不变式，让同
类规格在提交前被拦下。只读 YAML、零运行时导入，供门禁与
tests/test_spec_contracts.py 调用。

不变式：
  C1  type: cli 且声明了 output_map ⇒ search_args 必须显式请求机器可读输
      出（含 "json" 的旗标）或声明 output_format。output_map 是结构化输出
      的字段映射，人读文本流上它整体是死代码——spec 必须表态。
  C2  结构完整性：type: cli 必须带 cmd；engine_id 必填且不得为空。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
SPECS_DIR = SKILL_DIR / "engines" / "specs"
CONFIG_PATH = SKILL_DIR / "config.yaml"

# C1 的显式豁免名单（engine_id）：规格声明的 CLI 默认输出就是机器可读、
# 且无 --json 类旗标可传时，在此登记并注明依据。当前为空——出现新豁免
# 必须在行内写清实测依据，否则优先改 spec。
JSON_FLAG_EXEMPT: set[str] = set()


def load_specs() -> list[tuple[str, str, dict[str, Any]]]:
    """收集声明式规格：(来源标签, engine_id, spec)。含 engines/specs/*.yaml
    与 config.yaml engines 段里的 dict 条目。读不了的文件计为一条结构违例。"""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for p in sorted(SPECS_DIR.glob("*.yaml")):
        try:
            import yaml

            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception as e:  # 坏 YAML 本身就是结构违例
            out.append((f"specs/{p.name}", "", {"_load_error": str(e)}))
            continue
        if isinstance(data, dict):
            out.append((f"specs/{p.name}", str(data.get("engine_id", "")), data))
    try:
        import yaml

        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    for name, spec in (cfg.get("engines") or {}).items():
        if isinstance(spec, dict) and spec.get("type") == "cli":
            out.append(("config.yaml", str(name), spec))
    return out


def lint_specs(specs: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    """返回违例列表（空 = 通过）。每条带不变式编号与文件出处。"""
    violations: list[str] = []
    for origin, engine_id, spec in specs:
        where = f"{origin}:{engine_id or '?'}"
        if spec.get("_load_error"):
            violations.append(f"C2 {where}: YAML 解析失败 {spec['_load_error']}")
            continue
        etype = spec.get("type")
        # C2 结构完整性
        if etype == "cli":
            cmd = spec.get("cmd")
            if not cmd or (isinstance(cmd, list) and not cmd):
                violations.append(f"C2 {where}: type: cli 缺 cmd，规格永远发不出请求")
        if not str(engine_id or "").strip() and origin.startswith("specs/"):
            violations.append(f"C2 {origin}: 缺 engine_id")
        # C1 机器可读输出契约
        if etype == "cli":
            omap = spec.get("output_map")
            if isinstance(omap, dict) and omap and engine_id not in JSON_FLAG_EXEMPT:
                args = [str(a) for a in (spec.get("search_args") or [])]
                fmt = str(spec.get("output_format", "")).strip().lower()
                asks_machine_readable = fmt in ("json", "yaml") or any(
                    "json" in a.lower() for a in args
                )
                if not asks_machine_readable:
                    violations.append(
                        f"C1 {where}: 声明了 output_map 但 search_args 未请求 "
                        f"JSON 且未声明 output_format——人读文本流上 output_map "
                        f"整体是死代码（bilibili/xhs 同类根因）"
                    )
    return violations


def main(argv: list[str] | None = None) -> int:
    violations = lint_specs(load_specs())
    if violations:
        for v in violations:
            print(f"❌ {v}", file=sys.stderr)
        print(f"spec 契约 lint：{len(violations)} 条违例", file=sys.stderr)
        return 1
    print(f"✅ spec 契约一致（{len(load_specs())} 份规格）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
