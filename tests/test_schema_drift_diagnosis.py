#!/usr/bin/env python3
"""test_schema_drift_diagnosis.py — 声明式上游结构漂移自动化自省对拍测试。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from engines_parsers import (
    diagnose_schema_drift,
    render_drift_report,
    apply_schema_patch,
)


def test_envelope_drift_detection():
    """测试 Envelope 阶段：业务错误封套拦截。"""
    raw = {"error": "quota exceeded", "status": "fail"}
    spec = {
        "output_map": {
            "items": "results",
            "item_title": "title",
            "item_url": "url",
        }
    }
    diag = diagnose_schema_drift(raw, spec)
    assert not diag["ok"]
    assert diag["stage"] == "envelope"
    assert not diag["drift_detected"]
    assert any("业务错误封套" in iss for iss in diag["issues"])


def test_container_drift_detection():
    """测试 Container 阶段：items 容器路径位移对拍与自动修复。"""
    raw = {
        "status": "success",
        "result_payload": {
            "records": [
                {
                    "id": 101,
                    "title": "Quantum Computing Advances",
                    "url": "https://example.com/item/101",
                    "desc": "Summary of quantum advances in 2026.",
                },
                {
                    "id": 102,
                    "title": "Neural Interfaces Overview",
                    "url": "https://example.com/item/102",
                    "desc": "Neural interface trends.",
                },
            ]
        },
    }
    spec = {
        "output_map": {
            "items": "data.items",  # 旧路径失效
            "item_title": "title",
            "item_url": "url",
        }
    }
    diag = diagnose_schema_drift(raw, spec)
    assert not diag["ok"]
    assert diag["drift_detected"]
    assert diag["container_drift"] is not None
    assert diag["container_drift"]["to"] == "result_payload.records"
    assert diag["suggested_patch"]["items"] == "result_payload.records"
    assert diag["confidence"] >= 0.90


def test_field_drift_renamed():
    """测试 Field 阶段：字段更名启发式同义对拍。"""
    raw = {
        "items": [
            {
                "id": 1,
                "headline": "Breakthrough in Fusion Energy",
                "web_url": "https://fusion.org/news/1",
                "abstract": "Researchers achieve net positive energy output.",
            }
        ]
    }
    spec = {
        "output_map": {
            "items": "items",
            "item_title": "title",    # 已更名为 headline
            "item_url": "url",        # 已更名为 web_url
            "item_summary": "snippet",  # 已更名为 abstract
        }
    }
    diag = diagnose_schema_drift(raw, spec)
    assert not diag["ok"]
    assert diag["drift_detected"]
    assert diag["stage"] == "field"
    patch = diag["suggested_patch"]
    assert patch["item_title"] == "headline"
    assert patch["item_url"] == "web_url"
    assert patch["item_summary"] == "abstract"


def test_nested_field_drift():
    """测试 Field 阶段：嵌套下沉层级路径对拍。"""
    raw = {
        "data": [
            {
                "attributes": {
                    "artwork_title": "Water Lilies",
                },
                "links": {
                    "self": "https://gallery.org/artworks/water-lilies",
                },
            }
        ]
    }
    spec = {
        "output_map": {
            "items": "data",
            "item_title": "title",
            "item_url": "url",
        }
    }
    diag = diagnose_schema_drift(raw, spec)
    assert diag["drift_detected"]
    patch = diag["suggested_patch"]
    assert patch["item_title"] == "attributes.artwork_title"
    assert patch["item_url"] == "links.self"


def test_render_drift_report():
    """测试终端诊断报告格式化。"""
    diag = {
        "drift_detected": True,
        "stage": "field",
        "confidence": 0.95,
        "issues": ["标题字段失效: 'title' 为空 -> 候选 'headline'"],
        "suggested_patch": {
            "item_title": "headline",
        },
    }
    report = render_drift_report(diag, engine_id="demo_eng")
    assert "[demo_eng]" in report
    assert "headline" in report
    assert "95%" in report


def test_apply_schema_patch():
    """测试 Spec 原地补丁应用并严格保留注释。"""
    yaml_content = """# 这是一个重要的引擎 Spec
# 维护人：taxueseek

engine_id: demo_spec
type: http
output_map:
  items: old_items
  item_title: old_title
  item_url: old_url
# 底部注释不能丢
timeout: 10
"""
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        f_path = Path(f.name)

    try:
        patch = {
            "items": "new_items",
            "item_title": "new_title",
        }
        ok, msg = apply_schema_patch(f_path, patch)
        assert ok
        content = f_path.read_text(encoding="utf-8")
        assert "# 这是一个重要的引擎 Spec" in content
        assert "# 底部注释不能丢" in content
        assert "items: new_items" in content
        assert "item_title: new_title" in content
        assert "item_url: old_url" in content
    finally:
        if f_path.exists():
            f_path.unlink()
