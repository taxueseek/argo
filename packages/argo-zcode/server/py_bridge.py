#!/usr/bin/env python3
"""argo-zcode 面板配置桥：被 panel_server.mjs 以子进程调用。

复用 argo 本体的 scripts/config.py（路径常量与加载器），不自行猜配置位置；
依赖 pyyaml——argo 唯一必需第三方依赖，装了 argo 就必有。
子命令：
  panel-data              面板全量数据（引擎状态 + 密钥归属 + 运行时元信息）
  diagnostics             诊断数据（版本/路径自检/daemon/预算/权限）
  list-engines            引擎清单（旧接口，保留兼容）
  set-engine NAME true|false   写覆盖层 config.local.yaml 的 engines.<name>.enabled
  env-path                输出 argo env 文件路径（密钥唯一真源）
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _argo_home() -> str:
    """定位 argo 本体（仓库根）。

    顺序：ARGO_HOME → PATH 里的 argo 反推 → 安装器约定目录。GUI 宿主
    （ZCode 桌面）的 PATH 常常没有 ~/.local/bin，不能只信 which。
    """
    candidates = []
    explicit = os.environ.get("ARGO_HOME", "").strip()
    if explicit:
        candidates.append(explicit)
    which = shutil.which("argo")
    if which:
        candidates.append(os.path.dirname(os.path.dirname(os.path.realpath(which))))
    for base in (
        "~/.local/share/skills-assets/argo",
        "~/.agents/skills/argo",
        "~/argo",
        "~/.argo",
    ):
        candidates.append(os.path.expanduser(base))
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "scripts", "config.py")):
            return candidate
    searched = "、".join(dict.fromkeys(os.path.expanduser(c) for c in candidates if c)) or "（无）"
    print(json.dumps({
        "error": "argo_not_found",
        "hint": f"未找到 argo 本体；设 ARGO_HOME 指向 argo 仓库根后重试。已查找：{searched}",
    }))
    sys.exit(1)


def _argo_bin(home: str) -> str:
    # Windows 上 bin/argo 是无扩展名的 Python 脚本（靠 shebang 才能在 POSIX 跑），
    # 必须优先 .exe / .cmd。
    for name in ("argo.exe", "argo.cmd", "argo"):
        candidate = os.path.join(home, "bin", name)
        if os.path.isfile(candidate):
            return candidate
    for name in ("argo.exe", "argo.cmd", "argo"):
        found = shutil.which(name)
        if found:
            return found
    return "argo"


def _run_argo(home: str, args, timeout: int = 20):
    entry = _argo_bin(home)
    # .cmd/.bat 不是可执行文件，Windows 上必须经 cmd 解释器。
    use_shell = os.name == "nt" and entry.lower().endswith((".cmd", ".bat"))
    cmd = [entry, *args] if use_shell or entry.lower().endswith(".exe") else [sys.executable, entry, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout, shell=use_shell,
            encoding="utf-8", errors="replace",
        )
        return proc.stdout, proc.stderr, proc.returncode
    except Exception as exc:  # noqa: BLE001 - 桥层只报错不抛出
        return "", str(exc), 1


def _daemon_status_fast(home: str):
    """进程内直读 daemon 状态（<1ms），异常时回退 CLI 子进程。

    返回 (人读文案, 机器可读 state)。state 走 argo 本体的词表
    （engine_states.DAEMON_*）：此前面板只能拿中文文案再正则反推 pid、
    用布尔冒充三态，文案一改就静默失配（2026-11-09 收口）。
    """
    # 词表在函数入口 import：CLI 回退分支同样要用，不能只在进程内直读分支
    # 局部 import（此前该分支写死 "running"/"stopped"，改词表不会让它变红）。
    import engine_states as states
    try:
        import daemon as argo_daemon
        alive = argo_daemon._alive()
        sock = argo_daemon._sock_path()
        if alive:
            state = str(alive.get("state") or states.DAEMON_RUNNING)
            return (f"argo daemon 运行中 pid={alive.get('pid')} socket={sock}",
                    state)
        return f"argo daemon 未运行（socket={sock}）", states.DAEMON_STOPPED
    except Exception:  # noqa: BLE001
        # 回退到 CLI 时也拿 --json：state 从字段读，不靠正则反推中文文案。
        # 不校验取值是否在词表内（fail-open）：将来 daemon 加状态值时桥层原样
        # 透传，旧面板最多少显示一档，不会把活着的进程报成 stopped。
        out, _, _ = _run_argo(home, ["daemon", "status", "--json"], 10)
        try:
            payload = json.loads(out)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            state = str(payload.get("state") or "")
            if not state:
                # 旧版 argo 的 --json 只有 running 布尔
                state = (states.DAEMON_RUNNING if payload.get("running")
                         else states.DAEMON_STOPPED)
            text = ("argo daemon 运行中 pid=%s socket=%s"
                    % (payload.get("pid"), payload.get("socket"))
                    if state != states.DAEMON_STOPPED
                    else "argo daemon 未运行（socket=%s）" % payload.get("socket"))
            return text, state
        # 连 --json 都拿不到：退回人读文案 + pid 探活（旧版兼容路径）
        out, _, _ = _run_argo(home, ["daemon", "status"], 10)
        text = out.strip()
        return text, (states.DAEMON_RUNNING if _daemon_running(text)
                      else states.DAEMON_STOPPED)


def _budget_list_fast(home: str) -> str:
    """进程内直读预算表（<2ms），异常时回退 CLI 子进程。"""
    try:
        import contextlib
        import io
        import budget_cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            budget_cli.main(["list"])
        return buf.getvalue().strip()
    except Exception:  # noqa: BLE001
        out, _, _ = _run_argo(home, ["budget", "list"], 10)
        return out.strip()


def _paths_check_fast(home: str, argo_paths) -> tuple[str, int]:
    """进程内执行 paths --check（缩短锁轮回 sleep 至 0.15s，仍满足 >=100ms 互斥阈值），异常时回退 CLI 子进程。"""
    try:
        checks = argo_paths.run_checks(lock_hold_s=0.15)
        marks = {"pass": "通过", "fail": "失败", "warn": "注意", "info": "信息"}
        lines = [f"argo 路径自检（{sys.platform}）"]
        for c in checks:
            lines.append(f"  [{marks.get(c['status'], c['status'])}] {c['check']}：{c['detail']}")
        code = 0 if all(c["status"] != "fail" for c in checks) else 1
        return "\n".join(lines), code
    except Exception:  # noqa: BLE001
        out, err, code = _run_argo(home, ["paths", "--check"], 30)
        return (out.strip() or err.strip()), code


def _load_argo(home: str):
    scripts = os.path.join(home, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import config as argo_config  # argo 本体 scripts/config.py
    import argo_paths
    return argo_config, argo_paths


def _env_file(argo_paths) -> str:
    paths = argo_paths.resolved_paths()
    value = paths.get("env_file_in_use") or paths.get("env_files") or ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value)


def _env_keys(engine_env):
    """argo 认知的 env 键集合：直接复用本体解析（export 前缀、引号、多候选路径）。"""
    try:
        return set(engine_env._envfile_load().keys())
    except Exception:  # noqa: BLE001 - 读取失败按无密钥处理
        return set()


def _detail(home: str):
    """优先进程内调用 engine_status + search_cli._summarize_engine_rows，免去子进程开销并修复缺 --json 返回空的缺陷。"""
    try:
        from engine_status import list_engines_detail, compact_engine_row
        from search_cli import _summarize_engine_rows
        rows = [compact_engine_row(r) for r in list_engines_detail()]
        return _summarize_engine_rows(rows)
    except Exception:  # noqa: BLE001
        out, _, code = _run_argo(home, ["search", "--list-engines", "--detail", "--json"], 25)
        if code != 0 or not out.strip():
            return {}
        try:
            return json.loads(out)
        except ValueError:
            return {}


def _version(home: str) -> str:
    """插件展示用版本：pyproject 优先，其次 package.json，最后包元数据。"""
    try:
        for line in Path(os.path.join(home, "pyproject.toml")).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    try:
        data = json.loads(Path(os.path.join(home, "package.json")).read_text(encoding="utf-8"))
        if data.get("version"):
            return str(data["version"])
    except (OSError, ValueError):
        pass
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version("argo-search")
    except Exception:  # noqa: BLE001 - 未安装为包时属正常
        return ""


def _daemon_running(text: str) -> bool:
    """按 pid 探活，不依赖 CLI 的中文文案。"""
    match = re.search(r"pid=(\d+)", text or "")
    if not match:
        return False
    pid = int(match.group(1))
    if os.name == "nt":
        return True  # Windows 上 os.kill(pid, 0) 语义不同，有 pid 即视为在跑
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _argo_interpreter(home: str) -> str:
    """argo CLI 实际使用的解释器：读入口脚本的 shebang，不经 py_bridge 自己的 Python。"""
    entry = _argo_bin(home)
    try:
        first = Path(entry).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return ""
    if not first.startswith("#!"):
        return ""
    parts = first[2:].strip().split()
    if not parts:
        return ""
    if os.path.basename(parts[0]) == "env" and len(parts) > 1:
        return shutil.which(parts[1]) or parts[1]
    return parts[0]


def _engine_row(spec: dict, name: str, bad: dict, engine_env, states):
    # 密钥判定走 argo 本体 engine_env：required_env_for 覆盖显式声明、别名表、
    # 占位符推断与专用 type；missing_env_for 的 get_env 会热读 ~/.config/argo/env。
    required = [str(k) for k in engine_env.required_env_for(name, spec)]
    missing = [str(k) for k in engine_env.missing_env_for(name, spec)]
    entry = bad.get(name) or {}
    reason = entry.get("reason")
    # 状态口径与 `argo search --list-engines --detail` 对齐：以官方 not_routable
    # 分类为准，再叠加 explicit_only。显式专用源缺 key 仍算「显式专用」（不进路由），
    # 密钥缺口只作为该行提示，不改变状态。
    #
    # 词表来自 argo 本体 scripts/engine_states.py（由调用方注入，见 panel_data）：
    # 此前这里把 quota_exhausted 写成 quota、把 not_routable 写成 explicit，
    # 与 show 层的 ENGINE_BLOCK_REASONS 及 engine_detail.status 各写一份，
    # 改一处漏两处即静默失配（2026-11-09 审计）。
    if not spec.get("enabled", True):
        status = states.ENGINE_BLOCK_DISABLED
    elif reason == states.ENGINE_BLOCK_MISSING_ENV:
        status = states.ENGINE_BLOCK_MISSING_ENV
    elif reason == states.ENGINE_BLOCK_MISSING_DEPS:
        status = states.ENGINE_BLOCK_MISSING_DEPS
    elif reason == states.ENGINE_BLOCK_QUOTA_EXHAUSTED:
        status = states.ENGINE_BLOCK_QUOTA_EXHAUSTED
    elif spec.get("explicit_only"):
        status = states.ENGINE_BLOCK_NOT_ROUTABLE
    else:
        status = states.ENGINE_STATUS_READY
    note = ""
    if status == states.ENGINE_BLOCK_DISABLED:
        note = (spec.get("disabled_reason") or entry.get("disabled_reason") or "")[:220]
    elif status == states.ENGINE_BLOCK_MISSING_ENV:
        note = "配 " + " / ".join(entry.get("missing_env") or missing)
    elif status == states.ENGINE_BLOCK_MISSING_DEPS:
        deps = entry.get("missing_deps") or []
        fixes = [str(d.get("fix") or "") for d in deps if isinstance(d, dict)]
        note = "；".join([f for f in fixes if f]) or "装缺失的后端工具"
    elif status == states.ENGINE_BLOCK_QUOTA_EXHAUSTED:
        note = "等配额窗口重置"
    elif status == states.ENGINE_BLOCK_NOT_ROUTABLE:
        note = "不进自动路由，需 --engine 点名"
    return {
        "name": name,
        "label": (spec.get("label") or name)[:40],
        "family": spec.get("family") or "",
        "coverage": [str(x) for x in (spec.get("coverage") or [])][:4],
        "cost_tier": spec.get("cost_tier") or "",
        "enabled": bool(spec.get("enabled", True)),
        "explicit_only": bool(spec.get("explicit_only")),
        "recommended": bool(spec.get("recommended")),
        "status": status,
        "note": note,
        "required_env": required,
        "missing_env": missing,
        "desc": (spec.get("desc") or "")[:150],
    }


def panel_data(home: str):
    argo_config, argo_paths = _load_argo(home)
    import engine_env
    import engine_states as states
    cfg = argo_config.load_config()
    engines_cfg = cfg.get("engines") or {}
    env_path = _env_file(argo_paths)
    file_keys = _env_keys(engine_env)
    detail = _detail(home)
    bad = {e.get("engine_id"): e for e in (detail.get("not_routable_engines") or [])}

    engines = []
    keys = {}
    for name, spec in engines_cfg.items():
        if not isinstance(spec, dict):
            continue
        row = _engine_row(spec, name, bad, engine_env, states)
        engines.append(row)
        for key in row["required_env"]:
            item = keys.setdefault(key, {
                "key": key,
                "engines": [],
                "configured": False,
                "source": "none",
            })
            if name not in item["engines"]:
                item["engines"].append(name)
            if key not in row["missing_env"]:
                item["configured"] = True
                try:
                    aliases = [a for a in engine_env._name_variants(key) if a != key]
                except Exception:  # noqa: BLE001 - 别名推断失败不影响配置判定
                    aliases = []
                if key in file_keys or any(a in file_keys for a in aliases):
                    item["source"] = "file"
                elif item["source"] == "none":
                    item["source"] = "env"

    # 展示顺序与计数共用同一份词表，改状态名不再改 4 处。
    # ready 是「可自动路由」，not_routable 是「显式专用/其它不可路由」，
    # 与 search_cli._ENGINE_BLOCK_REASONS 的口径一致。
    status_order = (
        states.ENGINE_BLOCK_MISSING_ENV,
        states.ENGINE_BLOCK_MISSING_DEPS,
        states.ENGINE_BLOCK_QUOTA_EXHAUSTED,
        states.ENGINE_BLOCK_DISABLED,
        states.ENGINE_BLOCK_NOT_ROUTABLE,
        states.ENGINE_STATUS_READY,
    )
    order_index = {name: i for i, name in enumerate(status_order)}
    engines.sort(key=lambda r: (order_index.get(r["status"], len(order_index)), r["name"]))
    statuses = (states.ENGINE_STATUS_READY, states.ENGINE_BLOCK_NOT_ROUTABLE,
                states.ENGINE_BLOCK_DISABLED, states.ENGINE_BLOCK_MISSING_ENV,
                states.ENGINE_BLOCK_MISSING_DEPS,
                states.ENGINE_BLOCK_QUOTA_EXHAUSTED)
    counts = {"total": len(engines)}
    counts.update({s: sum(1 for r in engines if r["status"] == s) for s in statuses})
    counts["keys_configured"] = sum(1 for k in keys.values() if k["configured"])
    counts["keys_total"] = len(keys)
    daemon, daemon_state = _daemon_status_fast(home)
    return {
        "version": _version(home),
        "argo_home": home,
        "env_file": env_path,
        "config_base": str(getattr(argo_config, "CONFIG_PATH", "") or ""),
        "config_local": str(getattr(argo_config, "LOCAL_CONFIG_PATH", "") or ""),
        "daemon": daemon,
        "daemon_state": daemon_state,
        # busy 也是活着（只是正在跑别的），只有 stopped 才算没在跑
        "daemon_running": daemon_state != states.DAEMON_STOPPED,
        "engines": engines,
        "keys": sorted(keys.values(), key=lambda k: (-len(k["engines"]), k["key"])),
        "counts": counts,
        "upstream": {key: detail.get(key) for key in
                     ("total", "routable", "explicit_only", "not_routable", "by_reason")},
    }


def diagnostics(home: str):
    argo_config, argo_paths = _load_argo(home)
    import engine_env
    env_path = _env_file(argo_paths)
    paths_check, paths_code = _paths_check_fast(home, argo_paths)
    budget = _budget_list_fast(home)
    daemon, daemon_state = _daemon_status_fast(home)
    detail = _detail(home)
    mode = None
    if os.name != "nt":
        try:
            mode = oct(os.stat(env_path).st_mode & 0o777)
        except OSError:
            pass
    local_path = str(getattr(argo_config, "LOCAL_CONFIG_PATH", "") or "")
    return {
        "version": _version(home),
        "argo_home": home,
        "argo_bin": _argo_bin(home),
        "python": sys.version.split()[0],
        "argo_python": _argo_interpreter(home),
        "platform": sys.platform,
        "env_file": env_path,
        "env_mode": mode,
        "env_keys": len(_env_keys(engine_env)),
        "config_base": str(getattr(argo_config, "CONFIG_PATH", "") or ""),
        "config_local": local_path,
        "config_local_exists": bool(local_path) and os.path.exists(local_path),
        "daemon": daemon,
        "daemon_state": daemon_state,
        "budget": budget,
        "paths_check": paths_check,
        "paths_code": paths_code,
        "engines": {
            "total": detail.get("total"),
            "routable": detail.get("routable"),
            "explicit_only": detail.get("explicit_only"),
            "not_routable": detail.get("not_routable"),
            "by_reason": detail.get("by_reason"),
        },
    }


def main() -> int:
    home = _argo_home()
    argo_config, argo_paths = _load_argo(home)
    import yaml

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "panel-data":
        print(json.dumps(panel_data(home), ensure_ascii=False))
        return 0

    if cmd == "diagnostics":
        print(json.dumps(diagnostics(home), ensure_ascii=False))
        return 0

    if cmd == "list-engines":
        import engine_env
        import engine_states as states
        cfg = argo_config.load_config()
        detail = _detail(home)
        bad = {e.get("engine_id"): e for e in (detail.get("not_routable_engines") or [])}
        engines = []
        for name, spec in (cfg.get("engines") or {}).items():
            if not isinstance(spec, dict):
                continue
            engines.append(_engine_row(spec, name, bad, engine_env, states))
        engines.sort(key=lambda e: (not e["enabled"], e["cost_tier"] != "free", e["name"]))
        print(json.dumps({"engines": engines}, ensure_ascii=False))
        return 0

    if cmd == "set-engine":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "usage: set-engine NAME true|false"}))
            return 1
        name, enabled = sys.argv[2], sys.argv[3] == "true"
        local = {}
        if argo_config.LOCAL_CONFIG_PATH.exists():
            local = yaml.safe_load(argo_config.LOCAL_CONFIG_PATH.read_text()) or {}
        if not isinstance(local, dict):
            local = {}
        local.setdefault("engines", {}).setdefault(name, {})["enabled"] = enabled
        argo_config.LOCAL_CONFIG_PATH.write_text(
            yaml.safe_dump(local, allow_unicode=True, sort_keys=True))
        print(json.dumps({"ok": True, "name": name, "enabled": enabled,
                          "path": str(argo_config.LOCAL_CONFIG_PATH)}))
        return 0

    if cmd == "env-path":
        paths = argo_paths.resolved_paths()
        print(json.dumps({"env_file": paths.get("env_files") or paths.get("env_file_in_use")}))
        return 0

    print(json.dumps({"error": f"unknown command: {cmd}"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
