#!/usr/bin/env python3
"""test_engine_catalog.py — 搜索源使用文档的防漂移检查。

文档是生成的（scripts/gen_engine_catalog.py），本测试确保它**与当前引擎声明
一致**。没有这道门，文档会退化成「某天写过一次的说明」——本仓真实发生过：
README 写「12 个 MCP 工具」而实际 14 个、写「150+ 引擎」而分不清收录与可用。

检查从真实入口取事实（运行时函数 + CLI），不读文档里的自述数字。
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
BIN_ARGO = SKILL_DIR / "bin" / "argo"
DOC = SKILL_DIR / "docs" / "ENGINE_CATALOG.md"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _run_argo(*args, timeout=180):
    return subprocess.run([sys.executable, str(BIN_ARGO), "search", *args],
                          capture_output=True, text=True, timeout=timeout)


def test_doc_exists():
    assert DOC.exists(), "搜索源使用文档缺失：docs/ENGINE_CATALOG.md"


def test_doc_not_stale():
    """文档必须与当前声明一致（改引擎不重新生成 → 这里失败）。"""
    r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "gen_engine_catalog.py"), "--check"],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (
        "文档已过期，重新生成：python3 scripts/gen_engine_catalog.py\n"
        f"{r.stdout}{r.stderr}")


def test_declared_totals_match_runtime():
    """文档里的「收录 / 开箱可用」必须等于照声明算出来的数。

    只比声明计算方式：文档要能跨机器生成复核，不能把「本机此刻配没配密钥、
    有没有被熔断」写进去——那样换个环境生成就会与磁盘不符，检查随机变红。
    """
    from config import load_config
    cfg = load_config()
    specs = {n: s for n, s in (cfg.get("engines") or {}).items()
             if isinstance(s, dict)}
    enabled = {n: s for n, s in specs.items() if s.get("enabled", True)}
    from engine_status import list_engines_detail
    detail = {r["engine_id"]: r for r in list_engines_detail()}
    usable = [n for n in enabled
              if not detail[n]["required_env"] and not detail[n]["requires"]]
    text = DOC.read_text(encoding="utf-8")
    assert f"收录 {len(specs)} 个源" in text, f"文档收录数 ≠ 声明 {len(specs)}"
    assert f"开箱可用 {len(usable)} 个" in text, \
        f"文档可用数 ≠ 声明口径可用 {len(usable)}（{len(enabled)} 启用 - 需密钥/需工具）"


def test_routable_only_flag_actually_filters():
    """--routable-only 必须真筛（曾因 available_engines 不收参数而静默失效）。"""
    plain = _run_argo("--list-engines")
    routable = _run_argo("--list-engines", "--routable-only")
    assert plain.returncode == 0 and routable.returncode == 0
    all_ids = json.loads(plain.stdout[plain.stdout.index("["):])
    ro_ids = json.loads(routable.stdout[routable.stdout.index("["):])
    assert len(ro_ids) < len(all_ids), (
        f"--routable-only 没有筛掉任何东西（{len(ro_ids)}/{len(all_ids)}）")
    from engine_status import list_routable_engine_ids
    assert set(ro_ids) == set(list_routable_engine_ids()), \
        "--routable-only 的结果与 routable 判定不一致"


def test_routable_only_signature_contract():
    """两个入口必须一直收 routable_only。

    调用侧此前包着 `except TypeError: 回退全量`，等函数补上参数后回退就变成
    死代码：它只会在签名被改坏时静默把「筛过了」变成「没筛」。死回退已删，
    这条契约改用显式断言守着（参数在、且真的被接受）。
    """
    from config import get_engines, load_config
    from engines import available_engines

    specs = get_engines(load_config(), routable_only=True)  # 不收就 TypeError
    assert isinstance(specs, dict) and specs, "routable_only 过滤后不应该为空"
    names = available_engines(routable_only=True)          # 不收就 TypeError
    assert isinstance(names, list)
    assert set(names) <= set(available_engines())


def test_routable_degrade_logs_instead_of_silent(caplog):
    """可选依赖缺失时按历史语义返回未过滤集，但必须留痕。

    静默返回未过滤集的代价是 blocked / env 未就绪的引擎重新可路由——
    本仓反复踩的「静默失效」，所以降级可以，无声不行。
    """
    from config import get_engines, load_config

    cfg = load_config()
    unfiltered = set(get_engines(cfg))
    with patch.dict(sys.modules, {"engine_env": None}), \
         caplog.at_level("WARNING", logger="unified_search.config"):
        result = get_engines(cfg, routable_only=True)
    assert set(result) == unfiltered, "预期降级为未过滤集"
    messages = [r.getMessage() for r in caplog.records]
    assert any("routable_only" in m for m in messages), \
        f"降级必须打 WARNING，不能静默（实际：{messages}）"


def test_dead_sources_are_zero_or_declared_explicit_only():
    """可达性门：没有未声明的死源。

    真死源（既不可达、又没声明 explicit_only）会让这条测试失败——这是
    「新增引擎忘了接线」的保底网。
    """
    from config import load_config, get_engines, get_domains
    from engine_policy import GENERAL_FREE_FALLBACK
    from topic_research_profiles import list_profiles
    import research as _research

    specs = get_engines(load_config(), routable_only=False)
    reachable = set(GENERAL_FREE_FALLBACK) | {"local_search"}
    for d in get_domains(load_config()):
        reachable |= set(d.get("engines_combo") or [])
    for p in list_profiles():
        reachable |= set(p.get("engines") or [])
    reachable |= (set(_research._RESEARCH_EN_BOOSTS)
                  | set(_research._RESEARCH_ACADEMIC_BOOSTS)
                  | set(_research._RESEARCH_JA_KO_BOOSTS))
    dp = json.loads((SKILL_DIR / "backends" / "domain_profiles.json")
                    .read_text(encoding="utf-8"))
    reachable |= {k for k, v in dp.items()
                  if isinstance(v, dict) and (v.get("documents") or [])}

    dead = sorted(
        n for n, s in specs.items()
        if isinstance(s, dict) and s.get("enabled", True)
        and n not in reachable and not n.startswith("local_")
        and not s.get("explicit_only"))
    assert not dead, f"未声明的不达源（接线或声明 explicit_only）: {dead}"


def test_explicit_only_not_also_auto_routed():
    """声明与实际必须一致：声明 explicit_only 又进了自动路径就是有一边说谎。"""
    from config import load_config, get_engines, get_domains
    from engine_policy import GENERAL_FREE_FALLBACK

    specs = get_engines(load_config(), routable_only=False)
    declared = {n for n, s in specs.items()
                if isinstance(s, dict) and s.get("enabled", True) and s.get("explicit_only")}
    auto = set(GENERAL_FREE_FALLBACK)
    for d in get_domains(load_config()):
        auto |= set(d.get("engines_combo") or [])
    conflict = sorted(declared & auto)
    assert not conflict, f"既声明 explicit_only 又在自动路径上: {conflict}"


def test_disabled_engines_declare_reason():
    """停用的引擎必须写 disabled_reason（2026-09-29）。

    实锤：72fd95a 源治理轮一次性关停 6 个引擎，config 里只有 enabled:false
    没有原因——下一个人看到「这个源为什么关着」无从查起，重开与否变成
    拍脑袋。数据层的「静默失败」：停用不写理由，与代码里吞异常同类。
    本测试让「停用不写原因」变成可见失败。
    """
    from config import load_config

    engines = load_config().get("engines") or {}
    silent = [name for name, spec in engines.items()
              if isinstance(spec, dict) and spec.get("enabled") is False
              and not str(spec.get("disabled_reason") or "").strip()]
    assert not silent, (
        f"以下引擎 enabled:false 但未写 disabled_reason：{silent}\n"
        "修法：在 config.yaml 对应引擎下补 disabled_reason（停用原因 + 重开条件）")
