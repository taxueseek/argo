#!/usr/bin/env python3
"""engine_admission.py — 引擎生产准入状态

状态文件目录（由 argo_paths 派生，ARGO_ADMISSION_DIR 可覆盖）：
  <状态目录>/admission/<engine_id>.json

字段：
  engine_id, admitted_at, stages_passed, quality_score,
  avg_latency_ms, blocked, reason, updated_at

路由规则：
  - blocked=true → 自动路由跳过
  - 用户 --engine 强制指定仍可调用（调试）
  - 无 admission 文件 → 默认放行（向后兼容存量引擎）
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cli_io import dumps_pretty

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths

# ARGO_ADMISSION_DIR 优先（测试隔离）；未设置时由唯一来源派生目录
DEFAULT_ADMISSION_DIR = Path(
    os.path.expanduser(os.environ.get(
        "ARGO_ADMISSION_DIR",
        str(_paths.state_path("admission")),
    ))
)


def admission_dir() -> Path:
    # 保持每次重复执行结果一致确保目录存在：状态目录会被测试用 ARGO_STATE_DIR 隔离，进程内也
    # 可能切换，缓存目录反而容易写到已被清理的旧目录。exist_ok=True 在目录已存在
    # 时只是一次很便宜的路径查询，真正的重复大头（读记录前先 exists 再 read）已在
    # load_admission 里去掉。
    d = DEFAULT_ADMISSION_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _admission_stem(engine_id: str) -> str:
    """engine_id → 记录文件名（去 .json）。读写共用一处转义，不得各写各的。"""
    return engine_id.replace("/", "_").replace("..", "_")


def admission_path(engine_id: str) -> Path:
    return admission_dir() / f"{_admission_stem(engine_id)}.json"


def _read_path(engine_id: str) -> Path:
    """读记录用的路径，不触发 mkdir。

    读一个还不存在的记录根本不需要先建目录；此前 load_admission 经
    admission_path 每次都 mkdir(parents=True)，一次搜索因此多出约两百次
    路径查询。写记录（save_admission 等）仍走 admission_path 确保目录存在。
    """
    return DEFAULT_ADMISSION_DIR / f"{_admission_stem(engine_id)}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# 读缓存：一次 routable 扫描会对每个引擎问一次 is_blocked()，此前每次都裸读盘
# （实测一次扫描 668 次 read_text / 41 ms；同一份记录被读 3 次——is_blocked、
# is_admitted、引擎详情各来一遍）。按文件路径记忆，**写入路径立即失效**：同
# 进程读己所写永远新鲜（record_validation → load_admission 的测试序列就靠这条
# 保证）；别的进程改了记录则由 TTL 保底（默认 1 s，ARGO_ADMISSION_TTL_S 可调，
# 设 0 关闭记忆、回到逐次读盘）。
#
# 键用路径字符串而非 engine_id：状态目录可被 ARGO_STATE_DIR / ARGO_ADMISSION_DIR
# 在进程内切换（测试隔离的常规手段），按 engine_id 记忆会把「A 目录下没有记录」
# 错认成「B 目录下也没有」。
_admission_read_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

# env 文件回退路径的记忆化：(单调钟读数, 解析值)。
#
# 只记忆「env 文件」这一条，不碰 os.environ 直读——后者是字典查找、本就免费，
# 而且必须逐次生效（本旋钮的测试与 CI 都靠 setenv 控制，记忆化会把 setenv 吞掉，
# 例如 test_ttl_zero_disables_cache）。贵的是回退路径：engine_env.get_env →
# _envfile_load → 对候选密钥文件逐个 stat。
_TTL_MEMO_S = 1.0
_ttl_envfile_memo: tuple[float, float] | None = None


def _parse_ttl(raw: str) -> float:
    """TTL 字符串 → 秒。空值或非法值统一回落 1.0（唯一判据，两处共用）。"""
    try:
        return float(raw or 1.0)
    except ValueError:
        return 1.0


def _envfile_ttl() -> float:
    """从 env 文件解析 TTL，按 _TTL_MEMO_S 秒记忆化。

    本函数在**每个引擎**的准入判定里都会被调用（实测一次路由决策 215 次），
    而回退路径每次都要穿透到 engine_env.get_env → _envfile_load → 对候选密钥
    文件逐个 stat。同一个进程级常量被解 215 遍，实测占准入扫描的三分之一
    （2.79ms → 1.49ms，1.87×）。窗口取 1 秒与「本模块的读缓存默认 TTL」同量级：
    改 env 文件最多 1 秒后生效，热更语义仍成立。
    """
    global _ttl_envfile_memo
    now = time.monotonic()
    hit = _ttl_envfile_memo
    if hit is not None and now - hit[0] < _TTL_MEMO_S:
        return hit[1]
    try:
        from engine_env import get_env
        raw = get_env("ARGO_ADMISSION_TTL_S")
    except Exception:
        raw = ""
    value = _parse_ttl(raw)
    _ttl_envfile_memo = (now, value)
    return value


def _admission_ttl() -> float:
    """读缓存的 TTL（秒）；未设置或非法值回落 1.0。

    走 engine_env.get_env 而不是 os.environ 直读：开关写进 ~/.config/argo/env
    也要生效——与 config._stamp_ttl 同一计算方式（同一个旋钮不该有两套可读位置）。

    os.environ 命中时逐次直读（零成本、setenv 立即生效）；只有回退到 env 文件的
    那条路径走记忆化，见 _envfile_ttl。
    """
    raw = os.environ.get("ARGO_ADMISSION_TTL_S", "")
    if raw.strip():
        return _parse_ttl(raw)
    return _envfile_ttl()


def _read_admission_file(path: Path) -> dict[str, Any] | None:
    """裸读一份准入记录（无缓存）。"""
    # 直接读、靠异常判断文件是否存在：既不先 exists()，也不先建目录。
    # 文件/目录不存在时 read_text 抛 FileNotFoundError（OSError 子类），一并保底。
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# 存在集合：一次 scandir 回答「哪些引擎有记录」，替代逐引擎 open 探测。
# 动机（2026-10-01 实测）：261 引擎只有 99 份记录，全量清单/route 扫描的
# 261 次准入 open 里 162 次注定 ENOENT（62%）——本机 SSD 全扫描 2.8ms 无感，
# 但慢 IO 环境（沙箱/CI/网络盘）单次 syscall 可放大到毫秒级，同一清单 4s+。
# 集合只回答「有没有」，记录内容仍走读缓存逐条读：集合内引擎的读取行为与
# 旧实现逐位一致，减少的只有注定扑空的那部分 open。
#
# 语义与读缓存同一条 TTL（_admission_ttl，ARGO_ADMISSION_TTL_S=0 一并关闭、
# 回到逐次精确读）：窗口内别的进程新建/删除记录最多晚 TTL 秒可见，与读缓存
# 的跨进程新鲜度同一口径。键按目录字符串——状态目录可被测试在进程内切换，
# A 目录扫出的「无记录」不得带进 B 目录。写路径 save_admission 写完即把本
# 引擎并入集合：同进程读己所写不等 TTL。load_admission_fresh 不走集合——
# 读-改-写必须看到磁盘真相，绕过一切记忆。
_admission_dir_set_cache: dict[str, tuple[float, frozenset[str]]] = {}


def _admission_exists_set() -> frozenset[str]:
    """有记录的引擎文件名（去 .json）集合，按 TTL 重扫目录。"""
    key = str(DEFAULT_ADMISSION_DIR)
    now = time.monotonic()
    hit = _admission_dir_set_cache.get(key)
    if hit is not None and now - hit[0] < _admission_ttl():
        return hit[1]
    try:
        stems = frozenset(
            entry.name[: -len(".json")]
            for entry in os.scandir(DEFAULT_ADMISSION_DIR)
            if entry.name.endswith(".json")
        )
    except OSError:
        stems = frozenset()
    _admission_dir_set_cache[key] = (now, stems)
    return stems


def _admission_dir_set_add(dir_key: str, stem: str) -> None:
    """把刚写入的记录并入已缓存的集合；无缓存则不动（下次扫描自然包含）。"""
    hit = _admission_dir_set_cache.get(dir_key)
    if hit is not None:
        _admission_dir_set_cache[dir_key] = (hit[0], hit[1] | {stem})


def load_admission(engine_id: str) -> dict[str, Any] | None:
    """读一份准入记录（带进程内 TTL 缓存，见 _admission_read_cache）。

    **只用于读路径**（路由判定、状态展示）。读-改-写的调用方要用
    load_admission_fresh：TTL 窗口里的旧值会被原样写回，吞掉别的进程刚写入的
    字段（审计实测：子进程写入 stages_passed 的 quality，父进程在 TTL 内读-改-写
    后只剩 health，quality 丢失）。
    """
    path = _read_path(engine_id)
    ttl = _admission_ttl()
    now = time.monotonic()
    if ttl > 0:
        hit = _admission_read_cache.get(str(path))
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        # 存在集合短路：目录里没有这个引擎的记录，连 open 都不做（62% 注定
        # 扑空的那部分）。扑空结果与旧实现一样进读缓存，同 TTL 新鲜度。
        if _admission_stem(engine_id) not in _admission_exists_set():
            _admission_read_cache[str(path)] = (now, None)
            return None
    data = _read_admission_file(path)
    if ttl > 0:
        _admission_read_cache[str(path)] = (now, data)
    return data


def load_admission_fresh(engine_id: str) -> dict[str, Any] | None:
    """读一份准入记录并刷新缓存——读-改-写序列的读端专用。

    写路径必须看到磁盘上的最新状态：缓存的旧值会让「读旧 → 合并 → 写回」丢掉
    别的进程刚写进去的字段，而准入记录是路由的输入，丢字段会直接改变路由结果。
    """
    path = _read_path(engine_id)
    data = _read_admission_file(path)
    if _admission_ttl() > 0:
        _admission_read_cache[str(path)] = (time.monotonic(), data)
    return data


def save_admission(engine_id: str, record: dict[str, Any]) -> dict[str, Any]:
    path = admission_path(engine_id)
    out = {
        "engine_id": engine_id,
        "admitted_at": record.get("admitted_at"),
        "stages_passed": list(record.get("stages_passed") or []),
        "quality_score": record.get("quality_score"),
        "avg_latency_ms": record.get("avg_latency_ms"),
        "blocked": bool(record.get("blocked", False)),
        "reason": record.get("reason") or "",
        "updated_at": _now_iso(),
        "health": record.get("health"),
        "quality": record.get("quality"),
    }
    path.write_text(dumps_pretty(out), encoding="utf-8")
    # 写完即失效：缓存与文件是一对状态，只更新一半会让本进程读到自己没写的旧值
    _admission_read_cache.pop(str(path), None)
    # 存在集合同步并入：新记录不等 TTL 就可读（读己所写）
    _admission_dir_set_add(str(path.parent), path.stem)
    return out


def set_blocked(engine_id: str, blocked: bool, reason: str = "") -> dict[str, Any]:
    # 读端用 fresh：这是读-改-写序列，缓存里的旧值会被原样写回，吞掉别的进程
    # 刚写进去的字段（TTL 窗口内的丢更新）
    current = load_admission_fresh(engine_id) or {"engine_id": engine_id, "stages_passed": []}
    current["blocked"] = blocked
    current["reason"] = reason
    if not blocked and not current.get("admitted_at"):
        current["admitted_at"] = _now_iso()
    return save_admission(engine_id, current)


def record_validation(
    engine_id: str,
    *,
    stages_passed: list[str],
    quality_score: float | None = None,
    avg_latency_ms: float | None = None,
    blocked: bool | None = None,
    reason: str = "",
    health: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    admit: bool = False,
) -> dict[str, Any]:
    """写入验证结果；admit=True 且未 blocked 时标记准入时间。"""
    # 读-改-写：读端必须 fresh，否则 TTL 内的旧值会把并发写入的字段吞掉
    current = load_admission_fresh(engine_id) or {}
    merged_stages = list(dict.fromkeys(
        list(current.get("stages_passed") or []) + list(stages_passed or [])
    ))
    if blocked is None:
        # 默认：health 失败则 block；通过则 unblock
        blocked = "health" not in stages_passed and bool(stages_passed)
        if "health" in (stages_passed or []):
            blocked = False
        if health is not None and not health.get("ok", False) and health.get("status") != "skipped":
            blocked = True

    admitted_at = current.get("admitted_at")
    if admit and not blocked:
        admitted_at = _now_iso()
    elif blocked:
        # 保持历史 admitted_at，但 blocked 生效
        pass

    # reason 必须反映**本次**判定，不得粘滞历史原因。
    # 此前写 `reason or current.get("reason")`：先跑 `--stage health` 失败会留下
    # `health_failed`；之后补跑 `--stage quality`（health 已并入 merged_stages、
    # 本次 health.ok=true 且 blocked=False）时本次 reason 为空 → 回退到历史
    # `health_failed`，与 blocked=False 自相矛盾。引擎详情页据此显示
    # 「blocked=true / reason=health_failed」而 health.status=pass，导致
    # routable=False——实测 24 个引擎（批次九全部 + realtime_index）因此被
    # 错误拉黑、装了没通电。
    if reason:
        eff_reason = reason
    elif blocked:
        eff_reason = "validation_failed"
    else:
        # 本次未 block：清掉历史失败原因，避免与 blocked=False 矛盾
        eff_reason = ""

    record = {
        "admitted_at": admitted_at,
        "stages_passed": merged_stages,
        "quality_score": quality_score if quality_score is not None else current.get("quality_score"),
        "avg_latency_ms": avg_latency_ms if avg_latency_ms is not None else current.get("avg_latency_ms"),
        "blocked": blocked,
        "reason": eff_reason,
        "health": health if health is not None else current.get("health"),
        "quality": quality if quality is not None else current.get("quality"),
    }
    return save_admission(engine_id, record)


def is_blocked(engine_id: str, default: bool = False) -> bool:
    """是否被准入系统拉黑。无记录时 default=False（兼容存量）。"""
    rec = load_admission(engine_id)
    if rec is None:
        return default
    return bool(rec.get("blocked", False))


def is_admitted(engine_id: str) -> bool:
    rec = load_admission(engine_id)
    if rec is None:
        return False
    return bool(rec.get("admitted_at")) and not bool(rec.get("blocked"))


def list_admissions() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    d = admission_dir()
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("engine_id"):
                result[data["engine_id"]] = data
            else:
                result[path.stem] = data if isinstance(data, dict) else {}
        except Exception:
            continue
    return result


def filter_routable(engine_ids: list[str] | set[str]) -> list[str]:
    """过滤掉 blocked 引擎，保持原顺序。"""
    out = []
    for e in engine_ids:
        if is_blocked(e):
            continue
        out.append(e)
    return out
