#!/usr/bin/env python3
"""test_dsh_plugin_env_gate.py — DSH 插件 argoEnv 白名单与 argo 源码的对拍门禁。

packages/dsh-plugin/dsh/specs.js 的 ARGO_ENV_SPECS 是「面板可露出的 argo
环境变量开关」白名单：apply 期对白名单外的名字 loud fail。本测试保证白名单
里每个名字都在 argo 源码（scripts/ 与 bin/）有真实读取点——拼错或抄了个
不存在的名字，此处必须红，而不是等用户在面板上配了不生效。

反向（argo 新增了环境变量但面板没露出）不设失败： curated 是渐进过程，
新增开关要不要进面板是产品决策；这里只把增量打印出来，提醒同步。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SPECS = ROOT / "packages" / "dsh-plugin" / "dsh" / "specs.js"


def _plugin_allowlist() -> set[str]:
    src = PLUGIN_SPECS.read_text(encoding="utf-8")
    m = re.search(r"export const ARGO_ENV_SPECS = Object\.freeze\(\[(.*?)\n\]\)", src, re.S)
    assert m, "specs.js 缺少 ARGO_ENV_SPECS 导出"
    return set(re.findall(r"name: '(ARGO_[A-Z0-9_]+)'", m.group(1)))


def _argo_source_env_names() -> set[str]:
    names: set[str] = set()
    for base in (ROOT / "scripts", ROOT / "bin"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix not in (".py", ".js") or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            names.update(re.findall(r"ARGO_[A-Z][A-Z0-9_]*", text))
    return names


def test_allowlist_names_exist_in_argo_source():
    allowlist = _plugin_allowlist()
    assert allowlist, "白名单解析为空：正则或导出形状变了？"
    source_names = _argo_source_env_names()
    phantom = sorted(allowlist - source_names)
    assert not phantom, (
        f"白名单里的名字在 argo 源码没有读取点（拼错或已删除）: {phantom}"
    )


def test_allowlist_delta_is_reported(capsys):
    """argo 源码里有、白名单没有的增量只打印不失败（curated 是渐进过程）。

    这条只报增量、不设断言，是给人看的观察值。此前用 ``capsys.disabled()``
    绕过捕获，每次全量 pytest 都往 stdout 插 2547 字符 / 110 个名字，把进度条
    劈成两截（实测 argo-fulltest-2313.log）；其中 11 个还是源码正则扫出的前缀
    碎片（ARGO_FETCH_ / ARGO_QUOTA_ 之类），诊断本身也不准。交给 pytest 正常
    捕获：需要观察时 ``-s`` 即可看到，日常不再污染进度输出。
    """
    allowlist = _plugin_allowlist()
    delta = sorted(_argo_source_env_names() - allowlist)
    print(f"[argoEnv] allowlist={len(allowlist)} names, "
          f"argo-source-only={len(delta)}: {delta}")


def test_readme_stated_allowlist_count_matches_code():
    """README 里写死的「白名单 N 个」必须等于 ARGO_ENV_SPECS 的实际条数。

    why（2026-10-21）：`paid` 分组的两个开关（ARGO_PAID_SEARCH、
    ARGO_BUDGET_DEEPSEEK_SEARCH）随付费预算功能加进白名单后，README 两处
    「28 个」没跟着改，而文档数字此前**没有任何门禁**——上面两条测试只核对
    「名单里的名字在 argo 源码有读取点」，核对不到「文档说的条数对不对」。
    于是 28 一路飘着，直到有人按文档找「预算」找不到入口。

    只做数字对拍，不做文案审校：数字是可判定的，措辞不是。分组名、说明
    文案仍然靠 review。
    """
    count = len(_plugin_allowlist())
    readme = (ROOT / "packages" / "dsh-plugin" / "README.md").read_text(encoding="utf-8")
    stated = set(re.findall(r"白名单\s*(\d+)\s*个", readme))
    assert stated, "README 里没有「白名单 N 个」的表述：门禁失去锚点，请恢复该措辞"
    wrong = sorted(n for n in stated if int(n) != count)
    assert not wrong, (
        f"README 写「白名单 {','.join(wrong)} 个」，实际 ARGO_ENV_SPECS 有 {count} 个。"
        f" 增删开关时请同步 README（两处：设置界面的「引擎」分区、argoEnv 说明）"
    )
