#!/usr/bin/env python3
"""mcp_setup.py — Argo 多客户端 MCP 一键注入/诊断/还原（自研，零第三方依赖）

设计对齐 Argo 单一真源哲学（config.yaml / engines/specs/*.yaml）：
  - 客户端描述真源 = mcp/clients.yaml（改客户端不改代码；加一行 YAML 即可）
  - PHP/Python 均为 stdlib，不引入 tomlkit/toml 等第三方依赖

安全与可逆（借鉴分布式系统写入范式，而非复制任何外部代码）：
  - atomic_write：同目录临时文件 + os.replace，并发读者只见旧或新，绝无半截
  - 备份：写入前 COPY 原文件到 ~/.argo/mcp-backup/<ts>-<client>.<ext>，可回滚
  - 权限：含密钥的配置文件以 0600 写入
  - --dry-run：只预览不写；--undo：精确移除 entry 或从备份回滚
  - JSON patch 保留未改动子树原文；TOML 走"行级 append section"不动已有内容

用法：
  python3 scripts/mcp_setup.py status                     # 诊断各客户端
  python3 scripts/mcp_setup.py inject --all               # 注入所有已安装
  python3 scripts/mcp_setup.py inject --claude-code       # 注入指定
  python3 scripts/mcp_setup.py inject --all --dry-run     # 预览
  python3 scripts/mcp_setup.py undo --all                 # 还原
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml  # pyyaml（Argo 已有依赖）
except ImportError:  # pragma: no cover
    yaml = None

ENTRY_NAME = "argo"


def _backup_dir() -> Path:
    # 备份目录跟随 home（ARGO_HOME_OVERRIDE 隔离时也隔离），保证 e2e/测试不污染真实 ~/.argo
    return _home() / ".argo" / "mcp-backup"


# ── 真源加载 ──────────────────────────────────────────────────────────────

def _clients_file() -> Path:
    override = os.environ.get("ARGO_CLIENTS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "mcp" / "clients.yaml"


def load_clients() -> list[dict[str, Any]]:
    """读取 mcp/clients.yaml（声明式真源）。读取失败抛异常（fail-loud）。"""
    path = _clients_file()
    if not path.exists():
        raise FileNotFoundError(f"客户端真源不存在: {path}")
    if yaml is None:
        raise RuntimeError("需要 pyyaml（Argo 已有依赖）")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    clients = data.get("clients") or []
    if not isinstance(clients, list) or not clients:
        raise ValueError(f"clients.yaml 缺少 clients 列表: {path}")
    return clients


def _home() -> Path:
    # 对齐 install.sh 的 HOME 解析；KEENABLE 式 HOME 覆盖便于 e2e 隔离，但用 Argo 自有名
    override = os.environ.get("ARGO_HOME_OVERRIDE", "").strip()
    home = Path(override).expanduser() if override else Path.home()
    return home


def _resolve(path_str: str) -> Path:
    """解析相对 home 或绝对路径; 支持 {home} 占位。"""
    s = path_str.strip()
    if s.startswith("{home}"):
        s = s.replace("{home}", str(_home()), 1)
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = _home() / p
    return p


def detect(client: dict[str, Any]) -> bool:
    """客户端是否已安装（detect 路径任一存在）。无 detect 字段则看 config_path 父目录。"""
    detects = client.get("detect") or []
    if detects:
        return any(_resolve(d).exists() for d in detects)
    return _resolve(client["config_path"]).parent.exists()


# ── MCP 命令模板 ────────────────────────────────────────────────────────

def _mcp_command(client: dict[str, Any]) -> dict[str, Any]:
    """生成该客户端应写入的 argo MCP entry（stdio 命令 + 可选 env）。

    v2.8.5 起 command 指向 mcp_launch.sh 启动器而非 python 直启：
    启动器负责注入引擎密钥（~/.config/argo/env 唯一真源 + launchctl 兜底），
    任何客户端/启动上下文行为一致——密钥配置一次，处处生效。
    """
    install_dir = Path(__file__).resolve().parent.parent
    launcher = install_dir / "scripts" / "mcp_launch.sh"
    return {
        "command": str(launcher),
        "args": [],
        "env": {"PYTHONIOENCODING": "utf-8"},
    }


# ── 备份 ────────────────────────────────────────────────────────────────

def backup_file(path: Path) -> Path | None:
    """写入前备份原文件（带时间戳）。成功返回备份路径，原文件不存在返回 None。"""
    if not path.exists():
        return None
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = backup_dir / f"{ts}_{path.name}"
    shutil.copy2(path, backup)
    return backup


def list_backups(file_name: str) -> list[Path]:
    """列出某配置文件的所有历史备份（按时间倒序）。"""
    backup_dir = _backup_dir()
    if not backup_dir.exists():
        return []
    return sorted(
        (p for p in backup_dir.glob(f"*_{file_name}") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )


# ── 原子写 ──────────────────────────────────────────────────────────────

def atomic_write(path: Path, content: str, secret: bool = False) -> None:
    """同目录临时文件 + os.replace（原子）。含密钥时 0600 权限。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if secret:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── JSON patch（保留未改动子树原文）──────────────────────────────────────

def _json_read(path: Path) -> dict[str, Any]:
    """读取 JSON 配置。缺失=空 dict；存在但非法=抛异常（fail-loud，不覆盖用户文件）。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"配置非法 JSON（未修改）: {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"配置根非对象（未修改）: {path}")
    return data


def _inject_json(client: dict[str, Any], entry: dict[str, Any]) -> str:
    """JSON 客户端注入：合并 entry，保留未改动子树。"""
    path = _resolve(client["config_path"])
    config = _json_read(path)
    servers_key = client["servers_key"]
    if not isinstance(config.get(servers_key), dict):
        config[servers_key] = {}
    config[servers_key][ENTRY_NAME] = entry
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _remove_json(client: dict[str, Any]) -> str | None:
    """JSON 客户端移除 argo entry。返回新内容；无 entry 返回 None。"""
    path = _resolve(client["config_path"])
    if not path.exists():
        return None
    config = _json_read(path)
    servers = config.get(client["servers_key"])
    if isinstance(servers, dict) and ENTRY_NAME in servers:
        del servers[ENTRY_NAME]
        return json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    return None


# ── TOML 行级 append（注释零破坏，不触碰已有内容）─────────────────────────

def _toml_escape(s: str) -> str:
    """TOML 基本字符串转义（仅用于值；本实现值均为简单 ASCII/URL）。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_block(client: dict[str, Any], entry: dict[str, Any]) -> str:
    """生成 [mcp_servers.argo] block 文本。"""
    servers_key = client["servers_key"]
    lines = [f"[{servers_key}.{ENTRY_NAME}]", f'command = "{_toml_escape(entry["command"])}"']
    args = entry.get("args") or []
    args_repr = ", ".join(f'"{_toml_escape(a)}"' for a in args)
    lines.append(f"args = [{args_repr}]")
    env = entry.get("env") or {}
    if env:
        for k, v in env.items():
            lines.append(f'env = {{ {k} = "{_toml_escape(v)}" }}')
    return "\n".join(lines)


def _inject_toml(client: dict[str, Any], entry: dict[str, Any]) -> str:
    """TOML 客户端注入：行级 append section。已存在则报错（不覆盖，避免破坏）。"""
    path = _resolve(client["config_path"])
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    servers_key = client["servers_key"]
    marker = f"[{servers_key}.{ENTRY_NAME}]"
    if marker in existing:
        raise ValueError(f"TOML 已含 argo 配置（若需更新请先 undo）: {path}")
    block = _toml_block(client, entry)
    if existing and not existing.endswith("\n"):
        existing += "\n\n"
    elif existing:
        existing += "\n"
    return existing + block + "\n"


def _remove_toml(client: dict[str, Any]) -> str | None:
    """TOML 客户端移除：删除 [mcp_servers.argo] section。"""
    path = _resolve(client["config_path"])
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    servers_key = client["servers_key"]
    marker = f"[{servers_key}.{ENTRY_NAME}]"
    if marker not in text:
        return None
    # 仅删该 section 到下一个 [section] 或文件尾
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    in_target = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = stripped == marker
        if not in_target:
            out.append(line)
        i += 1
    result = "".join(out).rstrip("\n") + "\n"
    return result


# ── 诊断 ────────────────────────────────────────────────────────────────

def _entry_present(client: dict[str, Any]) -> bool:
    path = _resolve(client["config_path"])
    if not path.exists():
        return False
    fmt = client.get("format", "json")
    if fmt == "toml":
        return f"[{client['servers_key']}.{ENTRY_NAME}]" in path.read_text(encoding="utf-8")
    try:
        config = _json_read(path)
    except ValueError:
        return False
    servers = config.get(client["servers_key"])
    return isinstance(servers, dict) and ENTRY_NAME in servers


def _status_line(client: dict[str, Any]) -> str:
    id_ = client["id"]
    name = client["name"]
    installed = detect(client)
    configured = _entry_present(client)
    state = "已配置" if configured else ("已安装" if installed else "未安装")
    return f"  {id_:<12} {name:<14} {state}"


# ── 主命令 ──────────────────────────────────────────────────────────────

def cmd_status(clients: list[dict[str, Any]]) -> None:
    print("Argo MCP 客户端状态\n")
    for c in clients:
        print(_status_line(c))
    print("\n可用: inject [--all|--client ...] | undo [--all|--client ...] | status")


def _select(clients: list[dict[str, Any]], flags: list[str]) -> list[dict[str, Any]]:
    """从 flags（--all 或 --client id 列表，支持逗号分隔）选目标客户端。

    返回已安装的目标；--all 选全部已安装。未知 id 忽略并提示。
    """
    if not flags:
        return []
    expanded: list[str] = []
    for f in flags:
        expanded.extend(x.strip() for x in f.split(",") if x.strip())
    if any(x == "all" for x in expanded):
        return [c for c in clients if detect(c)]
    wanted = set(expanded)
    unknown = wanted - {c["id"] for c in clients}
    if unknown:
        print(f"  （忽略未知客户端: {', '.join(sorted(unknown))}）")
    return [c for c in clients if c["id"] in wanted]


def cmd_inject(clients: list[dict[str, Any]], flags: list[str], dry_run: bool) -> None:
    targets = _select(clients, flags)
    if not targets:
        print("未找到匹配的已安装客户端。可执行 status 查看，或用 --all。")
        return
    print(f"将注入 {', '.join(c['name'] for c in targets)} ...\n")
    for c in targets:
        path = _resolve(c["config_path"])
        fmt = c.get("format", "json")
        entry = _mcp_command(c)
        try:
            if dry_run:
                new_content = (
                    _inject_toml(c, entry) if fmt == "toml" else _inject_json(c, entry)
                )
                print(f"  [dry-run] {c['name']} → {path}")
                continue
            backup = backup_file(path)
            new_content = _inject_toml(c, entry) if fmt == "toml" else _inject_json(c, entry)
            atomic_write(path, new_content, secret=True)
            note = f"（备份 {backup.name}）" if backup else ""
            print(f"  ✔ 已注入 {c['name']} → {path} {note}")
        except (OSError, ValueError) as e:
            print(f"  ✘ {c['name']}: {e}")


def cmd_undo(clients: list[dict[str, Any]], flags: list[str], dry_run: bool) -> None:
    targets = _select(clients, flags)
    if not targets:
        print("未找到匹配的客户端。可用 status 查看，或用 --all。")
        return
    print(f"将还原 {', '.join(c['name'] for c in targets)} ...\n")
    for c in targets:
        path = _resolve(c["config_path"])
        fmt = c.get("format", "json")
        try:
            if dry_run:
                print(f"  [dry-run] {c['name']} → 移除 argo entry (或从备份回滚)")
                continue
            new_content = _remove_toml(c) if fmt == "toml" else _remove_json(c)
            if new_content is None:
                # entry 不存在 → 尝试从最近备份恢复
                backups = list_backups(path.name)
                if backups:
                    shutil.copy2(backups[0], path)
                    print(f"  ↺ 从备份恢复 {c['name']} → {backups[0].name}")
                else:
                    print(f"  - {c['name']} 无 argo entry，无备份可恢复")
                continue
            atomic_write(path, new_content, secret=True)
            print(f"  ✔ 已还原 {c['name']} → {path}")
        except (OSError, ValueError) as e:
            print(f"  ✘ {c['name']}: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Argo 多客户端 MCP 注入/诊断/还原")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="诊断各客户端")

    p_inject = sub.add_parser("inject", help="注入 MCP 配置")
    p_inject.add_argument("--all", action="store_true", help="所有已安装客户端")
    p_inject.add_argument("--client", action="append", default=[], help="指定客户端 id（可多次）")
    p_inject.add_argument("--dry-run", action="store_true", help="只预览不写")

    p_undo = sub.add_parser("undo", help="还原 MCP 配置")
    p_undo.add_argument("--all", action="store_true", help="所有客户端")
    p_undo.add_argument("--client", action="append", default=[], help="指定客户端 id（可多次）")
    p_undo.add_argument("--dry-run", action="store_true", help="只预览不写")

    args = parser.parse_args(argv)

    try:
        clients = load_clients()
    except Exception as e:
        print(f"加载客户端真源失败: {e}", file=sys.stderr)
        return 1

    if args.command == "status":
        cmd_status(clients)
    elif args.command == "inject":
        cmd_inject(clients, (["all"] if args.all else args.client), args.dry_run)
    elif args.command == "undo":
        cmd_undo(clients, (["all"] if args.all else args.client), args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
