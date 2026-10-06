#!/usr/bin/env python3
"""browser_auth.py — argo 自持浏览器登录态（持久 profile，A2 车道）

落地方案：docs-local/连接器能力接入_方案调研_2026-09-26.md §4 方案 A 的 A2。

模型（与既有凭据纪律对齐）：
  - profile 是 argo 自持的独立 Chromium profile（不是用户主 Chrome），
    落在状态根 browser-profiles/<site>/。
  - cookie 只存在于 Chrome 自己的存储里（macOS Keychain / Windows DPAPI
    加密），argo 全程不读取、不导出 cookie 值——比导出 state 文件再加密
    （agent-browser --session 的做法）少一类落盘凭据。
  - 抓取走 chrome_cdp 的「页面内带凭证 fetch」（fetch_json）：cookie 由
    浏览器附加，argo 只收响应体。
  - 登录态结果标 login_state_used=True / cache_eligible=False，
    cache.assert_cacheable 据此拒绝进公共缓存。

CLI（bin/argo 的 auth 子命令）：
  argo auth login <url>     开可见窗口登录/扫码一次（Ctrl-C 放弃）
  argo auth status          列出已登录站点
  argo auth logout <site>   删除该站点 profile

开关：ARGO_FETCH_AUTH=0 关闭抓取侧登录态车道；auth login/status/logout
不受它管（用户显式管理自己的 profile 永远可用）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ENV_AUTH_FETCH = "ARGO_AUTH_FETCH"

# 备名：与抓取降级链分级开关家族（ARGO_FETCH_*）保持同族命名
ENV_AUTH_FETCH_ALIAS = "ARGO_FETCH_AUTH"

_HOST_RE = re.compile(r"[^a-z0-9.-]")


def auth_fetch_enabled() -> bool:
    """抓取侧登录态车道开关：ARGO_AUTH_FETCH / ARGO_FETCH_AUTH=0 关闭，默认开启。"""
    for name in (ENV_AUTH_FETCH, ENV_AUTH_FETCH_ALIAS):
        try:
            from engine_env import get_env
            val = get_env(name)
        except ImportError:
            val = os.environ.get(name, "")
        if str(val).strip().lower() in ("0", "false", "no", "off"):
            return False
        if str(val).strip():
            return True
    return True


def profile_root() -> Path:
    """持久 profile 根目录：状态根/browser-profiles（ARGO_STATE_DIR 可整体隔离）。"""
    from argo_paths import state_root
    return state_root() / "browser-profiles"


def site_host(url_or_host: str) -> str:
    """URL 或主机名 → 站点身份（去 scheme/port/path，去 www.，小写）。

    返回值直接用作 profile 目录名，所以顺带做文件名安全审查：
    出现 [^a-z0-9.-]（路径穿越、空格、怪字符）或没有点（排除裸词）
    一律拒绝。localhost 显式放行（本地开发站登录是真实场景）。
    """
    s = (url_or_host or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if s.endswith(":80") or s.endswith(":443"):
        s = s.rsplit(":", 1)[0]
    if ":" in s:  # 带端口（或 IPv6 字面量）→ 取端口前段；IPv6 不支持，报错更诚实
        head = s.split(":", 1)[0]
        if not head:
            raise ValueError(f"unsupported site: {url_or_host!r}")
        s = head
    s = s.lower()
    if s.startswith("www."):
        s = s[4:]
    if not s or _HOST_RE.search(s):
        raise ValueError(f"invalid site: {url_or_host!r}")
    if s.startswith("."):  # "." / ".." / ".com"：点号打头做目录名就是穿越
        raise ValueError(f"invalid site: {url_or_host!r}")
    if "." not in s and s != "localhost":
        raise ValueError(f"invalid site (no dot): {url_or_host!r}")
    return s


def _profile_dir(host: str) -> Path:
    return profile_root() / host


def _read_meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(d: Path, host: str, created: str | None = None) -> None:
    """原子写 meta.json：profile 的 argo 登记标记（Chrome 不认识也不在乎它）。"""
    prev = _read_meta(d)
    meta = {
        "site": host,
        "created": created or prev.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_used": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "argo": "browser-profiles/v1",
    }
    tmp = d / f"meta.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def profile_for(url_or_host: str) -> str | None:
    """该站点的持久 profile 路径；没登录过（或站点名不合法）返回 None。

    以 meta.json 存在为准：裸目录可能是上次登录失败的残骸，不能当登录态。
    """
    try:
        host = site_host(url_or_host)
    except ValueError:
        return None
    d = _profile_dir(host)
    if d.is_dir() and (d / "meta.json").is_file():
        return str(d)
    return None


def auth_profile_for_fetch(url_or_host: str,
                           allow_browser_lane: bool = True) -> str | None:
    """抓取侧入口：车道让路（开关关 / 调用方禁浏览器 / 未登录）→ None。

    allow_browser_lane=False 对应 use_browser_fallback=False 的调用方
    （批量爬取、evidence 核验等刻意无浏览器的路径）——登录态车道同样让路，
    未认证不破默认路径的另一半。
    """
    if not allow_browser_lane or not auth_fetch_enabled():
        return None
    return profile_for(url_or_host)


def login(url_or_host: str, chrome_path: str | None = None,
          confirm=None) -> dict:
    """开可见 Chrome 窗口让用户登录一次，profile 持久保存。

    confirm：等待登录完成的钩子（None → 终端 input()）。可注入以便测试。
    """
    host = site_host(url_or_host)
    d = _profile_dir(host)
    fresh = not d.is_dir()
    d.mkdir(parents=True, exist_ok=True)

    target = url_or_host.strip()
    if "://" not in target:
        target = "https://" + target

    from chrome_cdp import _ChromeProcess
    proc = _ChromeProcess(chrome_path=chrome_path, headless=False,
                          user_data_dir=str(d), start_url=target)
    try:
        proc.start()
    except Exception as e:
        # 新建 profile 时启动即败（占位 SingletonLock / 磁盘满等）→ 别留裸目录
        if fresh:
            shutil.rmtree(d, ignore_errors=True)
        msg = str(e)
        if "CDP failed to start" in msg or "Singleton" in msg:
            msg += "（该 profile 可能已有 Chrome 在跑：关掉它或先 argo auth logout）"
        return {"ok": False, "site": host, "error": msg[:200]}

    try:
        if confirm is None:
            print(f"\n[{host}] 已在打开的 Chrome 窗口中登录/扫码。", file=sys.stderr)
            print("完成后回到终端按 Enter 保存登录态（Ctrl-C 放弃）：", file=sys.stderr)
            try:
                input()
            except EOFError:
                return {"ok": False, "site": host,
                        "error": "非交互终端无法确认登录完成（需要 tty）"}
        else:
            confirm()
        _write_meta(d, host)
        return {"ok": True, "site": host, "profile": str(d)}
    finally:
        try:
            proc.stop()
        except Exception:
            pass


def status() -> list[dict]:
    """已登录站点清单（只读 meta，绝不触碰 profile 内的浏览器存储）。"""
    root = profile_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and (d / "meta.json").is_file()):
            continue
        m = _read_meta(d)
        out.append({
            "site": m.get("site") or d.name,
            "created": m.get("created"),
            "last_used": m.get("last_used"),
            "path": str(d),
        })
    return out


def logout(url_or_host: str) -> dict:
    """删除站点 profile。Chrome 还开着该 profile 时拒绝（SingletonLock 探活）。"""
    host = site_host(url_or_host)
    d = _profile_dir(host)
    if not d.is_dir():
        return {"ok": False, "site": host, "error": f"no profile for {host}"}
    lock = d / "SingletonLock"
    if lock.is_symlink():
        # Chrome 的 SingletonLock 是 hostname-PID 软链；PID 活着 → 别删活人脚下的地毯
        pid = os.readlink(lock).rsplit("-", 1)[-1]
        if pid.isdigit():
            try:
                os.kill(int(pid), 0)
            except OSError:
                pass  # 进程已死：锁是残骸，放行删除
            else:
                return {"ok": False, "site": host,
                        "error": f"Chrome(pid {pid}) 正在使用该 profile，先关闭再 logout"}
    shutil.rmtree(d)
    return {"ok": True, "site": host}


def touch(url_or_host: str) -> None:
    """抓取命中登录态车道时更新 last_used（失败静默——只是观测字段）。"""
    try:
        host = site_host(url_or_host)
        d = _profile_dir(host)
        if d.is_dir() and (d / "meta.json").is_file():
            _write_meta(d, host)
    except Exception:
        pass


# ─── CLI（bin/argo 的 auth 子命令入口）──────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    use_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip() or "argo auth — 浏览器登录态")
        sys.exit(0 if argv else 1)

    cmd, rest = argv[0], argv[1:]

    def _emit_json(payload: dict) -> None:
        # stdout 一律紧凑 JSON（context-budget 门禁：禁缩进 JSON / dumps_pretty）
        try:
            from cli_io import dumps
            print(dumps(payload))
        except ImportError:
            print(json.dumps(payload, ensure_ascii=False))

    if cmd == "login":
        if not rest:
            print("用法：argo auth login <url>（如 argo auth login https://example.com）",
                  file=sys.stderr)
            sys.exit(1)
        chrome_path = None
        if "--chrome-path" in rest:
            i = rest.index("--chrome-path")
            if i + 1 < len(rest):
                chrome_path = rest[i + 1]
                del rest[i:i + 2]
        out = login(rest[0], chrome_path=chrome_path)
        if use_json:
            _emit_json(out)
        elif out.get("ok"):
            print(f"已保存 {out['site']} 的登录态（{out['profile']}）\n"
                  f"之后 argo fetch 该站点自动带会话；argo auth status 可查看。")
        else:
            print(f"登录失败：{out.get('error')}", file=sys.stderr)
        sys.exit(0 if out.get("ok") else 1)

    if cmd == "status":
        profiles = status()
        if use_json:
            _emit_json({"profiles": profiles})
        elif not profiles:
            print("（尚无已登录站点——argo auth login <url> 添加）")
        else:
            for p in profiles:
                print(f"{p['site']}  建于 {p.get('created') or '?'}  "
                      f"最近使用 {p.get('last_used') or '?'}")
        sys.exit(0)

    if cmd == "logout":
        if not rest:
            print("用法：argo auth logout <site>（站点见 argo auth status）",
                  file=sys.stderr)
            sys.exit(1)
        out = logout(rest[0])
        if use_json:
            _emit_json(out)
        elif out.get("ok"):
            print(f"已删除 {out['site']} 的登录态 profile。")
        else:
            print(f"删除失败：{out.get('error')}", file=sys.stderr)
        sys.exit(0 if out.get("ok") else 1)

    print(f"Unknown auth subcommand: {cmd}（可用：login / status / logout）",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
