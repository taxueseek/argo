#!/usr/bin/env python3
"""env_store — env 密钥文件写回（settings set/apply 的唯一写路径）。

定位（2026-11-07，方案 A/B 共用地基）：engine_env 只读不写，全仓此前没有
一条安全的 envfile 写路径；#41 类「装了引擎全 skipped」的修复动作因此只能
让用户手改文件。本模块把写回收口到一处：

  - 行级补丁，不整文件重写：注释、空行、未知行原样保留（与 mcp_setup 的
    「保留未改动子树原文」同一哲学）；
  - 候选链合并：`ARGO_X` 与历史裸名 `X` 视为同一逻辑键（复用
    engine_env._name_variants）——set 时首个命中行就地替换、其余命中行删除，
    不留「一半读得到一半读不到」的分裂键（#41 同根因）；
  - 值经 shlex 引用：空格/井号等特殊字符不会把一行拆成两行；
  - 原子写 + 0600 + 单份 .bak 备份（argo_paths.atomic_write_text 唯一来源）；
  - 写后 reset_envfile_cache()，本进程与常驻 daemon 的下一次读取立即见新值。

安全红线：任何回显/日志/报告只准出现键名与掩码尾 4 位，明文值出了调用方的
stdin / JSON 输入就不该再出现在任何输出里。
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argo_paths  # noqa: E402
import engine_env  # noqa: E402

# env 变量名：字母/下划线开头，仅字母数字下划线（含小写——跨平台 env 不强求大写）
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 值里出现这些字符才需要引用；纯安全集不加引号，保持文件可读
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./^-]+$")


class EnvStoreError(ValueError):
    """键名/值/请求形状非法。消息面向 CLI 直接展示。"""


def mask(value: str) -> str:
    """掩码：只承认「已配置」与尾 4 位，任何长度都不回显明文。"""
    v = (value or "").strip()
    if not v:
        return "缺失"
    if len(v) >= 8:
        return f"已配置（…{v[-4:]}）"
    return "已配置"


def _quote_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise EnvStoreError("值不能包含换行（一行一个变量）")
    if not value:
        raise EnvStoreError("值不能为空（要清除请用 unset）")
    if _SAFE_VALUE_RE.fullmatch(value):
        return value
    return shlex.quote(value)


def _validate_names(names: list[str]) -> None:
    for n in names:
        if not _KEY_RE.fullmatch(n or ""):
            raise EnvStoreError(f"非法变量名：{n!r}（仅字母/数字/下划线，"
                                "字母或下划线开头）")


def _parse_line(line: str) -> tuple[str, str] | None:
    """解析出 entry 的 (key, value)；注释/空行/无法解析行返回 None。

    解析口径与 engine_env._parse_envfile 一致（export 前缀、shlex、引号），
    不复用该私有函数：本模块要的是「逐行」而非「整文件字典」。
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("export "):
        s = s[len("export "):]
    if "=" not in s:
        return None
    k, _, v = s.partition("=")
    k = k.strip()
    if not k:
        return None
    return k, v.strip()




def patch_lines(lines: list[str], sets: dict[str, str],
                unsets: list[str]) -> tuple[list[str], dict[str, Any]]:
    """行级补丁。返回 (新行列表, 报告)；报告只含键名与动作，不含任何值。"""
    sets = dict(sets or {})
    unsets = list(unsets or [])
    _validate_names(list(sets) + list(unsets))
    conflict = sorted(set(sets) & set(unsets))
    if conflict:
        raise EnvStoreError(f"同一键同时出现在 set 与 unset：{', '.join(conflict)}")

    report: dict[str, Any] = {"replaced": [], "appended": [],
                              "merged_legacy": [], "removed": [],
                              "absent": []}

    # 逐逻辑键处理：命中行就地替换为规范名，多余命中行删除
    for key, value in sets.items():
        variants = set(engine_env._name_variants(key))
        replaced = False
        kept: list[str] = []
        for line in lines:
            parsed = _parse_line(line)
            if parsed and parsed[0] in variants:
                if not replaced:
                    nl = "\n" if line.endswith("\n") else ""
                    kept.append(f"{key}={_quote_value(value)}{nl}")
                    report["replaced"].append(key)
                    if parsed[0] != key:
                        report["merged_legacy"].append(parsed[0])
                    replaced = True
                else:
                    if parsed[0] != key:
                        report["merged_legacy"].append(parsed[0])
                    else:
                        report["merged_legacy"].append(key)
            else:
                kept.append(line)
        if not replaced:
            # 末行无换行时先补上，否则新行会粘在旧值尾部（"B=2C=3"）
            if kept and not kept[-1].endswith("\n"):
                kept[-1] += "\n"
            kept.append(f"{key}={_quote_value(value)}\n")
            report["appended"].append(key)
        lines = kept

    for key in unsets:
        variants = set(engine_env._name_variants(key))
        kept: list[str] = []
        removed = False
        for line in lines:
            parsed = _parse_line(line)
            if parsed and parsed[0] in variants:
                removed = True
                report["removed"].append(key)
                continue
            kept.append(line)
        lines = kept
        if not removed:
            report["absent"].append(key)  # 文件里本就没有，如实入账

    # 保证文件以换行结尾
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    return lines, report


def envfile_path() -> Path:
    """写回目标：与读取同一候选链的首选路径（显式 ARGO_ENV_FILE 优先）。"""
    return engine_env._envfile_path()


def apply_changes(sets: dict[str, str] | None = None,
                  unsets: list[str] | None = None) -> dict[str, Any]:
    """校验 → 行级补丁 → 备份 → 原子写 → 失效缓存。报告不含任何明文值。"""
    sets = dict(sets or {})
    unsets = list(unsets or [])
    if not sets and not unsets:
        raise EnvStoreError("空变更：没有要写入或清除的键")

    path = envfile_path()
    old_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = old_text.splitlines(keepends=True)

    new_lines, report = patch_lines(lines, sets, unsets)
    new_text = "".join(new_lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    backup: str | None = None
    if path.is_file():
        backup_path = path.with_suffix(path.suffix + ".bak")
        # 备份同样走原子写 + 0600：先写后 chmod 会留一个按 umask（常为 0644）
        # 可读的窗口，而备份里是旧密钥明文
        argo_paths.atomic_write_text(backup_path, old_text, mode=0o600)
        backup = str(backup_path)

    path.parent.mkdir(parents=True, exist_ok=True)
    argo_paths.atomic_write_text(path, new_text, mode=0o600)

    engine_env.reset_envfile_cache()

    report["path"] = str(path)
    report["backup"] = backup
    return report


def format_report(report: dict[str, Any]) -> str:
    """把报告渲染成人话（键名 + 动作 + 路径；无任何值）。"""
    out: list[str] = []
    if report.get("replaced"):
        out.append("已覆盖：" + "、".join(report["replaced"]))
    if report.get("appended"):
        out.append("已新增：" + "、".join(report["appended"]))
    if report.get("merged_legacy"):
        out.append("合并历史裸名行：" + "、".join(sorted(set(
            report["merged_legacy"]))))
    if report.get("removed"):
        out.append("已清除：" + "、".join(sorted(set(report["removed"]))))
    if report.get("absent"):
        out.append("未找到（无需清除）：" + "、".join(sorted(set(
            report["absent"]))))
    out.append(f"写入 {report.get('path', '?')}"
               + (f"（旧文件备份在 {report['backup']}）"
                  if report.get("backup") else "（新建文件）"))
    return "\n".join(out)
