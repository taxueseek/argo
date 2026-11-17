#!/usr/bin/env python3
"""test_ua_single_source_gate.py — UA 与礼貌池邮箱单源门禁。

根因（2026-11-18 收口前）：scripts/ 与 engines/specs/ 里 86 处
`argo-search/<版本>` 手抄字面量漂移出 7 个版本号（1.0~2.9，无一等于当前
版），21 处带保留域假邮箱（unified-search@local），拿不到 Crossref/OpenAlex
礼貌池待遇且升版必漏改。真源收敛到 engines_http.UA 与
engines_http.contact_email()（ARGO_CONTACT_EMAIL）；本门禁拦截两条路径的
字面量回潮。

回退验证：往任意 scripts/*.py 塞回 `"argo-search/2.4"` 字面量，本文件
test_no_versioned_ua_literal 应红。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_UA_VERSIONED = re.compile(r"argo-search/[0-9]")
_FAKE_CONTACT = re.compile(r"unified-search@local")
_SCAN_ROOTS = [ROOT / "scripts", ROOT / "engines" / "specs"]
# 真源文件自身允许出现（UA 定义为 f-string，不带版本数字；这里豁免的是
# 注释里对历史形态的描述性引用）
_EXEMPT = {ROOT / "scripts" / "engines_http.py"}


def _scan(pattern: re.Pattern[str]) -> list[str]:
    offenders: list[str] = []
    for root in _SCAN_ROOTS:
        for p in sorted(root.rglob("*.py")) + sorted(root.rglob("*.yaml")):
            if p in _EXEMPT:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    return offenders


def test_no_versioned_ua_literal() -> None:
    """禁止 argo-search/<数字版本> 字面量回潮（真源 engines_http.UA）。"""
    offenders = _scan(_UA_VERSIONED)
    assert not offenders, "UA 版本字面量回潮，请引用 engines_http.UA 单源:\n" + "\n".join(offenders)


def test_no_fake_contact_email() -> None:
    """禁止保留域假邮箱回潮（真源 engines_http.contact_email / ARGO_CONTACT_EMAIL）。"""
    offenders = _scan(_FAKE_CONTACT)
    assert not offenders, "假邮箱字面量回潮，请走 contact_email() 注入:\n" + "\n".join(offenders)


def test_single_source_defines_ua() -> None:
    """真源存在且版本随包走（不被降级为死字面量）。"""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from engines_http import UA, contact_email  # noqa: E402
    assert "argo-search/" in UA and "{_pkg_version" not in UA, f"UA 未展开版本号: {UA}"
    assert callable(contact_email)
