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


def test_sanitize_schema_is_wired_into_tools_list():
    """净化函数必须真的挂在 tools/list 出口，不能只是被自己的测试调用。

    回归（2026-11-11 审计）：aedee08 提交信息称「彻底修复 MaaS 网关 400 拦截」，
    实际交付的是 sanitize_schema 纯函数 + 它自己的测试——服务端三条路径
    （mcp_transport / mcp_server / mcp_local）一处都没调用它。源头写干净是
    「现在没问题」，挂上净化才是「以后加回 default 也不会打到严格网关」。

    判据用行为而非源码文本：往真源里塞一个违规键，tools/list 必须把它剥掉。
    源码文本判据的弱点是「把 sanitize_tool 的名字改掉就绕过了」，而这条判据
    只要净化没发生就红。
    """
    from mcp_transport import handle_rpc

    target = next(t for t in TOOLS if t["name"] == "argo_search")
    original = target["inputSchema"]["properties"]["max_results"]
    try:
        # 直接污染真源（模拟「以后有人加回 default」），不碰生产代码
        target["inputSchema"]["properties"]["max_results"] = {
            **original, "default": 5, "minimum": 1, "maximum": 20,
        }
        served = handle_rpc("tools/list", {})["tools"]
        served_search = next(t for t in served if t["name"] == "argo_search")
        prop = served_search["inputSchema"]["properties"]["max_results"]
        assert "default" not in prop, (
            "tools/list 出口没有净化：schema 里的 default 直达客户端，"
            "严格 MaaS 网关会因此 400（issue #46）")
        assert "minimum" not in prop and "maximum" not in prop
        # 净化不得剥掉模型要读的语义
        assert "description" in prop
        assert "type" in prop
    finally:
        target["inputSchema"]["properties"]["max_results"] = original


def test_gate_has_teeth_wiring():
    """自检：确认净化函数本身会剥掉 default（否则上一条判据恒真、等于没检查）。"""
    target = next(t for t in TOOLS if t["name"] == "argo_search")
    original = target["inputSchema"]["properties"]["query"]
    try:
        target["inputSchema"]["properties"]["query"] = {**original, "default": "x"}
        assert "default" in target["inputSchema"]["properties"]["query"], \
            "造错样本没生效——等于没检查"
        assert "default" not in sanitize_schema(
            target["inputSchema"]["properties"]["query"]), \
            "sanitize_schema 不剥 default，上一条判据失去意义"
    finally:
        target["inputSchema"]["properties"]["query"] = original
