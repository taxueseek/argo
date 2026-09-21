#!/usr/bin/env python3
"""test_domain_patterns — 域级「静默失效」检查（离线、确定性）。

## 守的是什么

路由的域匹配依赖 `config.yaml` 的 `domains[].patterns` 正则。`route.py` 的
`_compile_domain_patterns` 对编译失败是 **静默跳过**：

```python
try:
    regexes.append(re.compile(p))
except re.error:
    continue          # ← 一个正则笔误 = 该 pattern 永久失效，且无任何提示
```

若某域只有一条 pattern，写错它等于**整个域死掉**——查询全部落回 catch-all，
症状是「这个垂类怎么突然不准了」，而没有任何一环会报错。这类「配置静默失效」
是本仓反复踩的坑（死引擎、准入粘滞、加槽被前置裁剪吃掉都是同一形态），故这里
把域侧的可机械判定项一次性锁住。

## 判据（全部为运行时不变量，当前实测均成立）

1. 每个域至少一条 pattern，且**每条都能编译**（编译失败逐条报出域名与原文）；
2. 域的 `engines_combo` 非空；
3. `primary` / `fallback` 均已收录，且 `primary` 在 combo 内；
4. combo 里至少有一个引擎处于启用态（否则该域命中后必然零结果）。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _load():
    from config import load_config
    return load_config()


class TestDomainPatternsCompile(unittest.TestCase):
    def setUp(self):
        self.cfg = _load()
        self.domains = [d for d in (self.cfg.get("domains") or []) if isinstance(d, dict)]

    def test_domains_exist(self):
        """扫描面不能为空——读法失效时其余检查会静默全过。"""
        self.assertGreaterEqual(len(self.domains), 70,
                                f"只读到 {len(self.domains)} 个域，配置加载可能失效")

    # 「每条正则都能编译」的检查在 tests/test_domain_rule_schema.py：那里同时
    # 覆盖条目形态、内联否决禁令、unless 反例与字段白名单，此处不再留第二份
    # 实现（重复门禁会各自漂移，旧版只认字符串条目）。

    def test_no_domain_without_patterns(self):
        empty = [d.get("name") for d in self.domains if not (d.get("patterns") or [])]
        self.assertFalse(empty, (
            "这些域没有 patterns，永远不会被正则命中（只能靠 TF-IDF 兜）：\n  "
            + "\n  ".join(empty)))

    def test_compiled_count_matches_declared(self):
        """直接对冲 route 的静默跳过：编译结果条数必须等于声明条数。"""
        from route_domains import _compile_domain_patterns, _split_pattern_entry
        mismatched = []
        for src, comp in zip(self.domains, _compile_domain_patterns(self.domains)):
            want = len(src.get("patterns") or [])
            want_unless = sum(
                1 for p in (src.get("patterns") or [])
                if _split_pattern_entry(p)[1])
            rules = comp.get("_rules") or []
            got_unless = sum(1 for _m, u in rules if u is not None)
            if want != len(rules) or want_unless != got_unless:
                mismatched.append(
                    f"{src.get('name')}: 声明 {want} 条触发词/{want_unless} 条 unless，"
                    f"编译成功 {len(rules)}/{got_unless}")
        self.assertFalse(mismatched, (
            "有规则被 _compile_domain_patterns 静默丢弃（编译失败的都进不了 _rules）：\n  "
            + "\n  ".join(mismatched)))


class TestDomainWiringInvariants(unittest.TestCase):
    """域命中之后必须真的能出结果——这些不变量一旦破坏，症状只是「变不准」。"""

    def setUp(self):
        cfg = _load()
        self.cfg = cfg
        self.engines = cfg.get("engines") or {}
        self.domains = [d for d in (cfg.get("domains") or []) if isinstance(d, dict)]

    def _enabled(self, name: str) -> bool:
        return bool((self.engines.get(name) or {}).get("enabled", True))

    def test_combo_non_empty(self):
        bad = [d.get("name") for d in self.domains
               if not (d.get("engines_combo") or []) and not d.get("primary")]
        self.assertFalse(bad, f"这些域既无 engines_combo 也无 primary：{bad}")

    def test_primary_declared_and_inside_combo(self):
        bad = []
        for d in self.domains:
            name, combo, primary = d.get("name"), list(d.get("engines_combo") or []), d.get("primary")
            if primary and primary not in self.engines:
                bad.append(f"{name}: primary={primary} 未收录")
            if primary and combo and primary not in combo:
                bad.append(f"{name}: primary={primary} 不在 engines_combo 中（域主源永远排不进候选）")
        self.assertFalse(bad, "\n  ".join(["域主源接线不一致："] + bad))

    def test_fallback_declared(self):
        bad = [f"{d.get('name')}: fallback={d.get('fallback')} 未收录"
               for d in self.domains
               if d.get("fallback") and d["fallback"] not in self.engines]
        self.assertFalse(bad, "\n  ".join(["fallback 指向未收录引擎："] + bad))

    def test_combo_has_at_least_one_enabled_engine(self):
        bad = []
        for d in self.domains:
            combo = [e for e in (d.get("engines_combo") or []) if e in self.engines]
            if combo and not any(self._enabled(e) for e in combo):
                bad.append(f"{d.get('name')}: combo={combo[:4]} 内无启用引擎 → 命中即零结果")
        self.assertFalse(bad, "\n  ".join(["这些域命中后必然零结果："] + bad))


if __name__ == "__main__":
    unittest.main()
