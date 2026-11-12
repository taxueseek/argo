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
        bak_file = f_path.with_suffix(f_path.suffix + ".bak")
        if bak_file.exists():
            bak_file.unlink()


def test_single_field_high_confidence_with_shadow_verification():
    """方案 B 验证：单字段更名若锚点健康且影子试跑成功，置信度应为 0.95。"""
    raw = {
        "items": [
            {
                "id": 1,
                "headline": "Quantum Breakthrough",
                "url": "https://example.com/quantum/1",
            },
            {
                "id": 2,
                "headline": "Fusion Milestone",
                "url": "https://example.com/fusion/2",
            },
        ]
    }
    # 仅 title 更名为 headline，url 完好
    spec = {
        "output_map": {
            "items": "items",
            "item_title": "title",  # 失效
            "item_url": "url",      # 依然有效，构成锚点
        }
    }
    diag = diagnose_schema_drift(raw, spec)
    assert not diag["ok"]
    assert diag["drift_detected"]
    assert diag["suggested_patch"] == {"item_title": "headline"}
    assert diag["shadow_verified"] is True
    assert diag["confidence"] >= 0.95


def test_shadow_verification_failure_drops_confidence():
    """方案 B 验证：若候选字段在样本中产生空值，影子试跑失败且置信度降权。"""
    raw = {
        "items": [
            {
                "headline": "Valid First Item",
                "url": "https://example.com/1",
            },
            {
                "headline": "",  # 第二条标题为空
                "url": "https://example.com/2",
            },
        ]
    }
    spec = {
        "output_map": {
            "items": "items",
            "item_title": "title",
            "item_url": "url",
        }
    }
    diag = diagnose_schema_drift(raw, spec)
    assert diag["drift_detected"]
    assert diag["shadow_verified"] is False
    assert diag["confidence"] <= 0.60
    assert any("影子试跑未通过" in iss for iss in diag["issues"])


def test_apply_schema_patch_creates_backup():
    """方案 B 验证：应用补丁必须在同目录下生成 .bak 备份。"""
    yaml_content = "engine_id: test\noutput_map:\n  item_title: old\n"
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        f_path = Path(f.name)
    bak_path = f_path.with_suffix(f_path.suffix + ".bak")

    try:
        ok, msg = apply_schema_patch(f_path, {"item_title": "new"})
        assert ok
        assert bak_path.is_file()
        assert "item_title: old" in bak_path.read_text(encoding="utf-8")
        assert "item_title: new" in f_path.read_text(encoding="utf-8")
        assert "备份至" in msg
    finally:
        if f_path.exists():
            f_path.unlink()
        if bak_path.exists():
            bak_path.unlink()


def test_apply_schema_patch_preserves_inline_comments_and_indent():
    """保真结构化写入：保留行内注释与缩进，插入新键不破坏语法。"""
    yaml_content = """# 模块 Spec
output_map:
  items: data.list # 列表
  item_title: old_name # 标题字段
"""
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        f_path = Path(f.name)

    try:
        ok, msg = apply_schema_patch(f_path, {"item_title": "headline", "item_url": "link"})
        assert ok
        content = f_path.read_text(encoding="utf-8")
        assert "item_title: headline  # 标题字段" in content
        assert "item_url: link" in content
        assert "items: data.list # 列表" in content
    finally:
        if f_path.exists():
            f_path.unlink()
        bak = f_path.with_suffix(f_path.suffix + ".bak")
        if bak.exists():
            bak.unlink()


def test_apply_schema_patch_rejects_broken_yaml():
    """保真结构化写入门禁：若目标文件不是合法字典结构，拒绝破坏并拦截。"""
    yaml_content = """- 这是一个列表并非字典
- 第二行
"""
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        f_path = Path(f.name)

    try:
        ok, msg = apply_schema_patch(f_path, {"item_title": "new"})
        assert not ok
        assert "output_map" in msg
    finally:
        if f_path.exists():
            f_path.unlink()


def test_apply_schema_patch_keeps_history_backups(tmp_path):
    """连续修复不得覆盖上一份备份：每次写盘留一个时间戳 .bak。

    回退验证：把备份改回固定 `spec.yaml.bak` 后，第一次的原始内容会消失，
    本用例的 `item_title: old` 断言失败。
    """
    spec = tmp_path / "spec.yaml"
    spec.write_text("engine_id: t\noutput_map:\n  item_title: old\n", encoding="utf-8")

    assert apply_schema_patch(spec, {"item_title": "middle"})[0]
    assert apply_schema_patch(spec, {"item_title": "new"})[0]

    backups = sorted(tmp_path.glob("spec.yaml.*.bak"))
    assert len(backups) == 2, [p.name for p in backups]
    contents = [p.read_text(encoding="utf-8") for p in backups]
    assert any("item_title: old" in c for c in contents), "第一份备份必须是原始内容"
    assert any("item_title: middle" in c for c in contents)


def test_backup_failure_aborts_before_writing(tmp_path, monkeypatch):
    """备份写不进去时必须中止，不能在没有回滚凭据的情况下改 spec。"""
    spec = tmp_path / "spec.yaml"
    original = "engine_id: t\noutput_map:\n  item_title: old\n"
    spec.write_text(original, encoding="utf-8")

    real_write = Path.write_text

    def flaky_write(self, *args, **kwargs):
        if ".bak" in self.name:
            raise OSError("disk full")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write)
    ok, msg = apply_schema_patch(spec, {"item_title": "new"})

    assert not ok
    assert "备份失败" in msg
    assert spec.read_text(encoding="utf-8") == original


def test_patch_keeps_non_output_map_keys(tmp_path):
    """差分门禁：补丁只能改 output_map 内的点名键，兄弟节点原样保留。"""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "engine_id: t\noutput_map:\n  item_title: old\nlimits:\n  qps: 1\n",
        encoding="utf-8",
    )
    assert apply_schema_patch(spec, {"item_title": "new"})[0]
    content = spec.read_text(encoding="utf-8")
    assert "limits:\n  qps: 1" in content
    assert "item_title: new" in content


def test_both_modules_importable_standalone():
    """拆模块后必须能独立导入：先 import drift 不能因循环依赖失败。

    回退验证：让 engines_schema_drift 反过来 `from engines_parsers import ...`
    时，先导 drift 的子进程会以 ImportError 退出。
    """
    import subprocess
    import sys

    for module in ("engines_schema_drift", "engines_parsers"):
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import {module}"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert proc.returncode == 0, f"{module} 独立导入失败：{proc.stderr}"


