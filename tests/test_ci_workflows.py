#!/usr/bin/env python3
"""CI 工作流的形状门禁：锁住「为什么这样分作业」，而不是锁住每一行。

## 为什么需要

工作流是唯一一处「改错了没人报错」的配置：YAML 语法对、键名写错、作业被
误删、`paths-ignore` 把测试目录也忽略掉——都不影响本地任何测试，只在你推
上去之后才发现（或者更糟：一直静默不跑，你以为它在跑）。2026-10-22 第一次
上 CI 时，自己就在 smoke 步骤里写过一条根本不存在的子命令；这类错误本地跑
不出来，只能靠这道门。

## 只锁四件会被无声破坏的事

1. 文件能解析（YAML 语法错误 = 整条流水线消失，GitHub 不会红给你看）；
2. 作业还在（gates/unit/extras/smoke；nightly 三件 + notify）；
3. PR 不跑 live、不跑 nightly、`pull_request_target` 不出现（fork 提权的经典口子）；
4. 分片参数与耗时表还在（`--splits 4 --group`、`--durations-path tests/.test_durations`），
   否则「分片」退化成「四份全量」，分钟数悄悄乘四。
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.exists(), f"工作流不见了：{path}"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{name} 不是映射"
    return doc


def _triggers(doc: dict) -> dict:
    # PyYAML 按 YAML 1.1 把裸 on 解析成布尔 True；GitHub 按 1.2 当字符串。
    trig = doc.get("on", doc.get(True))
    assert trig, "缺 on: 触发条件"
    return trig


def _steps_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", ""))
                     for step in job.get("steps") or [])


def test_both_workflows_parse():
    for name in ("ci.yml", "nightly.yml"):
        assert _load(name)["jobs"], f"{name} 没有作业"


def test_ci_has_the_four_jobs():
    jobs = _load("ci.yml")["jobs"]
    assert set(jobs) == {"gates", "unit", "extras", "smoke"}, sorted(jobs)
    # gates 必须复用本地那套，不能在 CI 里另写一套判断
    assert "scripts/pre-commit" in _steps_text(jobs["gates"])


def test_unit_is_sharded_with_durations():
    unit = _load("ci.yml")["jobs"]["unit"]
    assert (unit.get("strategy") or {}).get("matrix", {}).get("group") == [1, 2, 3, 4]
    text = _steps_text(unit)
    assert "--splits 4" in text and "--group" in text
    assert "--durations-path tests/.test_durations" in text
    # 耗时表必须真的在仓库里，否则切片会退化成按名字糊切
    assert (ROOT / "tests" / ".test_durations").exists()


def test_pr_does_not_run_live_or_nightly():
    ci = _load("ci.yml")
    trig = _triggers(ci)
    assert "pull_request" in trig
    assert "pull_request_target" not in trig, "pull_request_target 会拿到写权限的 token"
    text = "\n".join(_steps_text(j) for j in ci["jobs"].values())
    assert "replay_eval.py --record" not in text, "录制会联网，不能进 PR"
    assert "-m live" not in text


def test_nightly_is_scheduled_and_reports():
    doc = _load("nightly.yml")
    trig = _triggers(doc)
    assert "schedule" in trig and "workflow_dispatch" in trig
    jobs = doc["jobs"]
    assert {"replay", "drift", "ledger", "notify"} <= set(jobs), sorted(jobs)
    # 漂移要看基线，基线丢了就变成「只记录不判断」
    drift = _steps_text(jobs["drift"])
    assert "tests/golden/drift_baseline_report.json" in drift
    assert "scripts/ci_drift_gate.py" in drift
    assert (ROOT / "tests" / "golden" / "drift_baseline_report.json").exists()
    assert jobs["notify"].get("if") == "failure()"


def test_extras_stays_off_the_pr_path():
    """装全九件可选依赖实测 9 分钟，进 PR 就把 2 分钟反馈变成 9 分钟。

    可选依赖的漂移是周级别的，PR 上用 gates/unit/smoke 就够；extras 只在
    push main 与手动触发时跑。改回 PR 前先想清楚这条关键路径。
    """
    extras = _load("ci.yml")["jobs"]["extras"]
    assert "pull_request" in (extras.get("if") or ""), extras.get("if")


def test_requirements_files_exist_and_are_wired():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest-split" in dev, "分片插件不能只写在 workflow 里"
    for dep in ("curl_cffi", "beautifulsoup4", "lxml"):
        assert dep in dev, f"测试真正要用的 {dep} 不在 requirements-dev"
    opt = (ROOT / "requirements-optional.txt").read_text(encoding="utf-8")
    assert "curl_cffi" in opt and "rapidocr" in opt


def test_unit_installs_rg_for_local_seek():
    """local-seek 的 5 条用例依赖 rg；CI 镜像默认没有，实测一缺就红成
    「grep 执行失败」。装它只要 1 秒，比在用例里跳过更值。"""
    unit = _load("ci.yml")["jobs"]["unit"]
    assert "ripgrep" in _steps_text(unit)
    extras = _load("ci.yml")["jobs"]["extras"]
    assert "ripgrep" in _steps_text(extras)


def test_extras_is_sharded_like_unit():
    """装全依赖后的全量在 2 核 runner 上约 8 分钟：不切片就还是「8 分钟任务」。
    四片共用同一个 venv 缓存（key 相同），所以并行不增加安装成本。"""
    extras = _load("ci.yml")["jobs"]["extras"]
    assert (extras.get("strategy") or {}).get("matrix", {}).get("group") == [1, 2, 3, 4]
    text = _steps_text(extras)
    assert "--splits 4" in text and "--group" in text
    assert "--durations-path tests/.test_durations" in text


def test_repo_entrypoint_reaches_path():
    """一批用例 subprocess 跑 `argo …`：conftest 必须把 repo/bin 放上 PATH，
    否则在没装过 argo 的机器（CI）上红成 FileNotFoundError: 'argo'。"""
    import shutil
    found = shutil.which("argo")
    assert found, "repo/bin 没上 PATH"
    assert Path(found).resolve().as_posix().endswith("/bin/argo")


def test_extras_reuses_cached_venv_but_self_heals():
    """extras 的 9 件依赖约 5 分钟安装；缓存整个 venv 前提是 key 覆盖两份
    requirements，且不能只信 cache-hit（镜像换 Python 补丁版后旧 venv 会失效，
    而 cache-hit 仍为 true）。这里锁这两个约束。"""
    raw = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "actions/cache@v4" in raw
    assert "hashFiles('requirements-dev.txt', 'requirements-optional.txt')" in raw
    extras = _load("ci.yml")["jobs"]["extras"]
    text = _steps_text(extras)
    assert ".venv/bin/python -c" in text, "缺依赖探针：缓存坏了会整轮红"
    assert "steps.deps.outputs.ready != 'true'" in raw, "探针结论没接到安装步骤的 if"
    assert "rm -rf .venv" in text, "旧 venv 失效时必须重建"


def test_release_uses_versioned_notes_when_present():
    """发布正文优先用 docs/RELEASE_NOTES_<tag>.md。

    为什么锁：`generate_release_notes: true` 只按 PR/commit 作者生成正文，
    issue 报告者的致谢不会出现；版本化发布说明里写了署名，必须让它进
    Release 正文，而不是只躺在仓库里。
    """
    doc = _load("release.yml")
    assert doc["jobs"]["publish-release"], "release.yml 解析后没有作业"
    raw = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "docs/RELEASE_NOTES_${tag}.md" in raw
    assert "body_path: ${{ steps.notes.outputs.path }}" in raw
    assert "generate_release_notes: false" in raw
