#!/usr/bin/env python3
"""test_mcp_schema_maas_compliance.py — MaaS / OpenAI Function Calling 协议兼容性门禁。

回归（issue #46）：
  在火山引擎方舟 ModelArk、腾讯云 MaaS 等严格云厂商网关下，若 Function Calling
  的 JSON Schema 中含有 default、minimum、maximum、minItems 等非标注解，
  网关会直接返回 400 The request is invalid: the request was rejected by an internal
  MaaS component。

依据第一性原理、奥卡姆剃刀与 MECE 原则：
  1. 所有 MCP 工具与 DSH 原生工具的 parameters 严格遵守白名单
     （type, description, enum, properties, required, items）；
  2. 默认值与数值范围由 description 明确承载，不靠非标 schema 注解；
  3. 执行层（mcp_handlers）自带默认值与 _clamp_int 兜底，绝不裸奔；
  4. sanitize_schema 提供可复用的纯函数脱糖清洗。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_tools
from mcp_tools import FORBIDDEN_SCHEMA_KEYS, TOOLS, listed_tools, sanitize_schema


def test_tools_contain_no_forbidden_maas_keys():
    """TOOLS 声明必须 100% 杜绝非标注解（default/minimum/maximum 等）。"""
    for tool in TOOLS:
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})
        for prop_name, prop_spec in props.items():
            forbidden = set(prop_spec.keys()) & FORBIDDEN_SCHEMA_KEYS
            assert not forbidden, (
                f"工具 {tool['name']} 的参数 {prop_name} 包含 MaaS 违规注解: {forbidden}"
            )


def test_all_object_schemas_have_properties_and_required():
    """每个 object 类型的 inputSchema 必须显式声明 properties 与 required。"""
    for tool in TOOLS:
        schema = tool.get("inputSchema", {})
        assert schema.get("type") == "object", f"{tool['name']} inputSchema 必须是 object"
        assert isinstance(schema.get("properties"), dict), f"{tool['name']} 必须有 properties 字典"
        assert isinstance(schema.get("required"), list), f"{tool['name']} 必须有 required 列表"


def test_sanitize_schema_recursive():
    """测试 sanitize_schema 纯函数能递归剥离各类嵌套脏注解并补齐结构。"""
    dirty_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索词",
                "default": "test",
                "minLength": 1,
            },
            "count": {
                "type": "integer",
                "description": "数量",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
            "tags": {
                "type": "array",
                "items": {
                    "type": "string",
                    "default": "all",
                },
                "minItems": 1,
                "maxItems": 10,
            },
            "sub_config": {
                "type": "object",
                "properties": {
                    "flag": {"type": "boolean", "default": True},
                },
            },
        },
        "required": ["query"],
    }
    clean = sanitize_schema(dirty_schema)
    assert clean["type"] == "object"
    assert clean["required"] == ["query"]
    assert "default" not in clean["properties"]["query"]
    assert "minLength" not in clean["properties"]["query"]
    assert "minimum" not in clean["properties"]["count"]
    assert "maximum" not in clean["properties"]["count"]
    assert "default" not in clean["properties"]["count"]
    assert "minItems" not in clean["properties"]["tags"]
    assert "maxItems" not in clean["properties"]["tags"]
    assert "default" not in clean["properties"]["tags"]["items"]
    sub = clean["properties"]["sub_config"]
    assert sub["type"] == "object"
    assert sub["required"] == []
    assert "default" not in sub["properties"]["flag"]


def test_native_tools_mjs_is_clean():
    """生成的 packages/dsh-plugin/dsh/native-tools.mjs 必须零违规键。"""
    mjs_path = ROOT / "packages" / "dsh-plugin" / "dsh" / "native-tools.mjs"
    assert mjs_path.exists(), "native-tools.mjs 必须存在"
    text = mjs_path.read_text(encoding="utf-8")
    m = re.search(r"export const NATIVE_TOOLS = (\{.*?\n\});", text, re.S)
    assert m, "必须能解析 NATIVE_TOOLS"
    native_tools = json.loads(m.group(1))

    for name, spec in native_tools.items():
        params = spec.get("parameters", {})
        props = params.get("properties", {})
        for prop_name, prop_spec in props.items():
            forbidden = set(prop_spec.keys()) & FORBIDDEN_SCHEMA_KEYS
            assert not forbidden, (
                f"native-tools.mjs 中 {name} 的参数 {prop_name} 包含 MaaS 违规注解: {forbidden}"
            )


def test_core_tools_contain_defaults_in_description():
    """验证核心工具（argo_search, argo_fetch 等）将默认值内嵌于 description。"""
    search_tool = next(t for t in TOOLS if t["name"] == "argo_search")
    props = search_tool["inputSchema"]["properties"]
    assert "默认 5" in props["max_results"]["description"]
    assert "默认 fast" in props["depth"]["description"]
    assert "默认 auto" in props["mode"]["description"]

    fetch_tool = next(t for t in TOOLS if t["name"] == "argo_fetch")
    f_props = fetch_tool["inputSchema"]["properties"]
    assert "默认 8000" in f_props["max_chars"]["description"]
    assert "默认 text" in f_props["mode"]["description"]
