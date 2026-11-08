#!/usr/bin/env python3
"""test_local_search_registry — 子技能登记表 / 文档 / 主清单三方一致检查。

## 守的是什么

`sub-skills/local-search/SKILL.md` 长期写着「33 个本地搜索引擎」，而同目录
`config.yaml` 实际是 **32 个**（2026-09-13 实查）。表里的 32 行是对的，只有
散文计数错——这类漂移没有任何机械检查，只能靠人读出来。

更要紧的是**两个登记表的关系**：同一批 `local_*` 名字在主 `config.yaml` 与
子技能 `config.yaml` 里各声明一次，写法不同（主清单 `type: http` + `format`，
子技能 `type: json|xml|rss|html|cli`）。两处必须指向同一上游、同一响应格式，
否则「同一个名字」在两条调用路径上会拿到不同结果。

本文件锁三件事：
  1. 文档计数 == 子技能 config 实际条数（散文与表格都算）；
  2. 文档表格的引擎名集合 == 子技能 config 的引擎名集合（增删引擎必须同步改文档）；
  3. 跨登记表同名项必须同上游、同响应格式（防两处各改各的）。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LS_DIR = ROOT / "sub-skills" / "local-search"
sys.path.insert(0, str(ROOT / "scripts"))

# 两处 type 写法的等价映射：子技能的「格式型」type ↔ 主清单的 format 值
_FORMAT_TYPES = {"json": "json", "xml": "xml", "rss": "xml", "html": "html"}


def _ls_config() -> dict:
    return yaml.safe_load((LS_DIR / "config.yaml").read_text(encoding="utf-8")) or {}


def _ls_engines() -> dict:
    return _ls_config().get("engines") or {}


def _skill_md() -> str:
    return (LS_DIR / "SKILL.md").read_text(encoding="utf-8")


def _table_rows(md: str) -> list[str]:
    """取「本地引擎列表」表的首列引擎名。"""
    names: list[str] = []
    in_table = False
    for line in md.splitlines():
        if line.startswith("### 本地引擎列表"):
            in_table = True
            continue
        if in_table:
            if line.startswith("###") or line.startswith("## "):
                break
            m = re.match(r"\|\s*(local_[a-z0-9_]+)\s*\|", line)
            if m:
                names.append(m.group(1))
    return names


class TestDocMatchesRegistry(unittest.TestCase):
    def test_registry_not_empty(self):
        """扫描面不能为空——读法失效时全部检查会静默通过。"""
        self.assertGreaterEqual(len(_ls_engines()), 20,
                                "子技能 config.yaml 读不到引擎或数量异常")

    def test_stated_count_matches_registry(self):
        md = _skill_md()
        n = len(_ls_engines())
        for pat in (rf"的\s*(\d+)\s*个本地搜索引擎", r"本地引擎列表（(\d+)\s*个"):
            m = re.search(pat, md)
            self.assertIsNotNone(m, f"SKILL.md 里找不到计数表述：{pat}")
            self.assertEqual(int(m.group(1)), n,
                             f"SKILL.md 写 {m.group(1)} 个，config.yaml 实际 {n} 个")

    def test_default_enabled_count_matches(self):
        md = _skill_md()
        m = re.search(r"本地引擎列表（\d+\s*个，(\d+)\s*个默认启用）", md)
        self.assertIsNotNone(m, "SKILL.md 未声明默认启用数量")
        enabled = sum(1 for v in _ls_engines().values()
                      if isinstance(v, dict) and v.get("enabled", True))
        self.assertEqual(int(m.group(1)), enabled,
                         f"SKILL.md 写 {m.group(1)} 个默认启用，实际 {enabled} 个")

    def test_table_rows_match_registry(self):
        rows = set(_table_rows(_skill_md()))
        reg = set(_ls_engines())
        self.assertTrue(rows, "SKILL.md 引擎表解析为空——表格格式变了？")
        self.assertFalse(reg - rows,
                         f"子技能 config 有但文档表里没有：{sorted(reg - rows)}")
        self.assertFalse(rows - reg,
                         f"文档表里有但子技能 config 没有：{sorted(rows - reg)}")


class TestCrossRegistryConsistency(unittest.TestCase):
    """主清单与子技能清单的同名项必须同上游、同格式。"""

    def setUp(self):
        from config import load_config
        self.main = load_config().get("engines") or {}

    def _main_format(self, spec: dict) -> str:
        return str(spec.get("format") or spec.get("output_format") or "").lower()

    def test_same_name_same_upstream(self):
        mismatched = []
        for name, v in _ls_engines().items():
            if not isinstance(v, dict):
                continue
            main = self.main.get(name)
            if not isinstance(main, dict):
                continue  # 只在两处都声明时才比对
            ls_url = str(v.get("url") or "")
            main_url = str(main.get("url") or main.get("cmd") or "")
            if ls_url and main_url and ls_url != main_url:
                mismatched.append(f"{name}: 子技能 {ls_url} ≠ 主清单 {main_url}")
        self.assertFalse(mismatched, (
            "同名引擎在两份登记表里指向不同上游——同一名字会按调用路径给出"
            "不同结果：\n  " + "\n  ".join(mismatched)))

    def test_same_name_same_response_format(self):
        mismatched = []
        for name, v in _ls_engines().items():
            if not isinstance(v, dict):
                continue
            main = self.main.get(name)
            if not isinstance(main, dict):
                continue
            ls_type = str(v.get("type") or "").lower()
            if ls_type not in _FORMAT_TYPES:
                continue  # cli 等非格式型 type 不参与比对
            want = _FORMAT_TYPES[ls_type]
            got = self._main_format(main)
            if got and got != want:
                mismatched.append(f"{name}: 子技能 type={ls_type}（→{want}）≠ 主清单 format={got}")
        self.assertFalse(mismatched, (
            "同名引擎的响应格式声明不一致——解析器会按不同格式解析同一份响应：\n  "
            + "\n  ".join(mismatched)))

    def test_disabled_in_main_is_not_enabled_in_subskill(self):
        mismatched = []
        for name, v in _ls_engines().items():
            if not isinstance(v, dict):
                continue
            main = self.main.get(name)
            if not isinstance(main, dict):
                continue
            ls_on = bool(v.get("enabled", True))
            main_on = bool(main.get("enabled", True))
            if not main_on and ls_on:
                mismatched.append(f"{name}: 主清单已停用 (enabled=False)，子技能仍开启 (enabled=True)")
        self.assertFalse(mismatched, (
            "同名引擎在主清单已停用，子技能不得继续开启派发死源：\n  "
            + "\n  ".join(mismatched)))


if __name__ == "__main__":
    unittest.main()
