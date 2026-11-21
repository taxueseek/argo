#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ego-search 浏览器通道并发槽位、同站节奏锁与站点级终态冷却期。

无守护进程设计（基于 OS 文件锁实现跨进程防风控调度）：
  - 并发槽位：基于 OS 文件锁（fcntl.flock / msvcrt.locking），单站最多 SITE_SLOTS=2，
    全局最多 BROWSER_SLOTS=4，防止多 Agent 并发挤爆浏览器或触发平台风控；
    排队耗时单独统计（wait_ms），不扣减单次执行超时预算。
  - 同站节奏锁：同站搜索/API 请求至少间隔 min_interval（默认 8.0s），
    通过站点独占锁串行读写时间戳，避免双进程同时跳过冷却。
  - 站点级终态冷却期：站点返回登录失效/限流/风控拦截时写入冷却记录，
    冷却期内直接返回结构化 handoff_required，阻止 Agent 重试把账号刷封。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

SITE_SLOTS = 2
BROWSER_SLOTS = 4
DEFAULT_QUEUE_WAIT = 120.0
DEFAULT_SITE_INTERVAL = 8.0
DEFAULT_COOLDOWN_SECONDS = 3600
_POLL_SECONDS = 0.15


def state_root() -> Path:
    """调用期解析 ego-search 状态根目录（不放导入期，避免测试 env 切换失配）。"""
    override = os.environ.get("EGO_SEARCH_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".local" / "state")
    return base / "ego-search"


def default_site_interval() -> float:
    raw = os.environ.get("EGO_SEARCH_SITE_INTERVAL", "").strip()
    if raw:
        try:
            v = float(raw)
            return max(0.0, v)
        except ValueError:
            return DEFAULT_SITE_INTERVAL
    return DEFAULT_SITE_INTERVAL


def normalize_site(site: str | None) -> str:
    """把站点/URL/task_space 归一为稳定主机键（如 zhihu.com）。"""
    if not site:
        return "default"
    s = site.strip().lower()
    if s.startswith("site:"):
        s = s[5:]
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    host = s.split("/", 1)[0].split(":", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or "default"


class SlotBusyError(RuntimeError):
    """槽位排队超时。"""


@contextmanager
def _try_file_lock(name: str, *, root: Path | None = None) -> Iterator[None]:
    """非阻塞 OS 文件锁；已被占用时抛 SlotBusyError。"""
    lock_dir = (root or state_root()) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    path = lock_dir / f"{digest}.lock"
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SlotBusyError(f"slot busy: {name}") from exc
        try:
            yield
        finally:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _acquire_any(
    names: list[str],
    *,
    wait: float,
    root: Path | None = None,
    poll: float = _POLL_SECONDS,
) -> ExitStack:
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        for name in names:
            stack = ExitStack()
            try:
                stack.enter_context(_try_file_lock(name, root=root))
                return stack
            except SlotBusyError:
                stack.close()
        if time.monotonic() >= deadline:
            raise SlotBusyError(f"waited >{wait:.1f}s for slots {names[0]}..")
        time.sleep(min(poll, max(0.01, deadline - time.monotonic())))


@contextmanager
def browser_slot(
    site: str,
    *,
    wait: float = DEFAULT_QUEUE_WAIT,
    site_slots: int = SITE_SLOTS,
    browser_slots: int = BROWSER_SLOTS,
    root: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """占领单站 + 全局浏览器槽位，yield 排队统计（wait_ms）。"""
    norm = normalize_site(site)
    started = time.monotonic()
    with ExitStack() as stack:
        stack.enter_context(
            _acquire_any(
                [f"site:{norm}:{i}" for i in range(max(1, site_slots))],
                wait=wait,
                root=root,
            )
        )
        remain = max(0.0, wait - (time.monotonic() - started))
        stack.enter_context(
            _acquire_any(
                [f"browser:slot:{i}" for i in range(max(1, browser_slots))],
                wait=remain,
                root=root,
            )
        )
        yield {"site": norm, "wait_ms": int((time.monotonic() - started) * 1000)}


def _pacing_path(root: Path | None = None) -> Path:
    return (root or state_root()) / "pacing_state.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名带线程号：同进程两线程并发写同一文件时旧名（仅 pid）会互踩
    # os.replace 抛 FileNotFoundError（2026-11-21 审计）
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def pace_site(
    site: str,
    *,
    min_interval: float | None = None,
    root: Path | None = None,
) -> float:
    """保证同站两次请求至少间隔 min_interval 秒，返回实际等待秒数。"""
    interval = default_site_interval() if min_interval is None else max(0.0, float(min_interval))
    if interval <= 0:
        return 0.0
    norm = normalize_site(site)
    path = _pacing_path(root)
    # 写侧锁名统一为 pacing:state：同一状态文件只允许一把写锁——旧 pace:{site}
    # 与 record_cooldown 的 pacing:state 是两把名字不同的锁，跨进程并发时后写者
    # 会整体覆盖前写者（丢冷却记录或节奏时间戳，2026-11-21 审计）。本锁内
    # sleep 至多一个 interval（默认 8s），短于 record_cooldown 的 15s 等待预算。
    with _acquire_any(["pacing:state"], wait=DEFAULT_QUEUE_WAIT, root=root):
        data = _load_json(path)
        starts = data.setdefault("request_starts", {})
        last = starts.get(norm)
        waited = 0.0
        if isinstance(last, (int, float)):
            gap = interval - max(0.0, time.time() - float(last))
            if gap > 0:
                time.sleep(gap)
                waited = gap
        starts[norm] = time.time()
        _atomic_write_json(path, data)
        return waited


def check_cooldown(site: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """检查站点是否处于终态冷却期；未冷却返回 None。"""
    norm = normalize_site(site)
    data = _load_json(_pacing_path(root))
    entry = (data.get("cooldowns") or {}).get(norm)
    if not isinstance(entry, dict):
        return None
    until = float(entry.get("until") or 0)
    remain = int(until - time.time())
    if remain <= 0:
        return None
    return {
        "site": norm,
        "reason": str(entry.get("reason") or "cooldown"),
        "retry_after_seconds": max(1, remain),
        "login_url": entry.get("login_url") or "",
    }


def record_cooldown(
    site: str,
    reason: str,
    *,
    seconds: int = DEFAULT_COOLDOWN_SECONDS,
    login_url: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """写入站点级终态冷却期（登录失效/限流/风控拦截），防 Agent 重试刷封。"""
    norm = normalize_site(site)
    path = _pacing_path(root)
    now = time.time()
    entry = {
        "reason": reason,
        "at": now,
        "until": now + max(1, int(seconds)),
        "login_url": login_url,
    }
    with _acquire_any(["pacing:state"], wait=15.0, root=root):
        data = _load_json(path)
        data.setdefault("cooldowns", {})[norm] = entry
        _atomic_write_json(path, data)
    return {"site": norm, **entry}


def clear_cooldown(site: str | None = None, *, root: Path | None = None) -> list[str]:
    """清除指定站点（或全部站点）冷却记录，返回被清除的站点列表。"""
    path = _pacing_path(root)
    with _acquire_any(["pacing:state"], wait=15.0, root=root):
        data = _load_json(path)
        cds = data.get("cooldowns") or {}
        if site is None:
            cleared = sorted(cds.keys())
            data["cooldowns"] = {}
        else:
            norm = normalize_site(site)
            cleared = [norm] if norm in cds else []
            cds.pop(norm, None)
            data["cooldowns"] = cds
        _atomic_write_json(path, data)
    return cleared


def active_cooldowns(*, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """列出当前所有仍在生效的站点冷却记录（只读，供 status 展示）。"""
    now = time.time()
    data = _load_json(_pacing_path(root))
    out: dict[str, dict[str, Any]] = {}
    for site, entry in (data.get("cooldowns") or {}).items():
        if not isinstance(entry, dict):
            continue
        remain = int(float(entry.get("until") or 0) - now)
        if remain > 0:
            out[site] = {
                "reason": entry.get("reason") or "cooldown",
                "retry_after_seconds": remain,
                "login_url": entry.get("login_url") or "",
            }
    return out
