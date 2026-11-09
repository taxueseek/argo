#!/usr/bin/env python3
"""usage_log.py — argo 本地使用日志（append-only JSONL，仅本机，不外发）

纪律（开发做减法）：
  - 不做平台：只有「追加一条」「读最近 N 条」两个操作
  - 失败静默：任何异常都吞掉返回 False，绝不拖累搜索主路径
  - 目录可注入：ARGO_USAGE_LOG_DIR 覆盖（测试隔离），默认 <状态目录>/usage_log/
  - 总开关：ARGO_USAGE_LOG=0 / false 完全关闭
  - 夹具守卫：query 流 engines_used 出现注册表外引擎名即判测试夹具，整条拒写
  - 脱敏：query 等敏感字段由调用方截断，本模块不放大

Schema（每条一行 JSON，UTF-8）：
  {"ts": "...", "stream": "recovery", "version": 1, ...业务字段}
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths
from engine_env import env_flag  # 布尔开关统一判断（见 env_flag 的说明）

_STREAM_VERSION = 1

# 单流体积上限与回缩目标。
#
# why（2026-09-17 实测）：append-only 而无任何轮转，三个流已涨到 771 KB
# 且只增不减。本模块唯一的读取方是 tail（只看最近若干条），旧记录没有读者，
# 所以超限时直接回缩，不另存归档——归档是「留给会来取的人」，这里没有。
#
# why 改成按字节回缩（2026-11-09）：原实现是「取 lines[-_KEEP_LINES:]」，
# 但单行实测已达 525 B，2000 行就是 1.05 MiB，早已超过 1 MiB 上限；于是
# 每次 emit 都判定超限、全读 1 MiB、再用同一个 2000 行原样写回——回缩成了
# 每次调用都做一次全量读写的空转。按字节从尾部保留才能真正把文件压下来。
_MAX_BYTES = 1024 * 1024
_TRIM_TO_BYTES = _MAX_BYTES // 2


def _trim_if_oversized(path: Path) -> None:
    """超过上限就把文件回缩到约一半，保留尾部整行；任何失败静默。

    先 stat 再决定要不要读：绝大多数调用只付一次 stat 的代价。从尾部按字节
    累计到 _TRIM_TO_BYTES 即可，不必解析 JSON；丢弃半行前缀（第一行可能不完整）
    保证每行仍是合法 JSONL。回缩到一半而不是贴着上限，是为了不会每写入几条
    就又触发一次全量读写。
    """
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        data = path.read_bytes()
        cut = max(0, len(data) - _TRIM_TO_BYTES)
        nl = data.find(b"\n", cut)
        if nl >= 0:
            start = nl + 1                      # 丢掉不完整的前缀行
        else:
            # 切点之后到 EOF 没有换行：最后一行异常巨大或未以换行结束。
            # 保留这完整一行，别把整个日志清空，也别写出半行非法 JSON。
            prev = data.rfind(b"\n", 0, cut)
            start = prev + 1
        kept = data[start:]
        if not kept or len(kept) >= len(data):
            return                              # 压不动就不动，避免无意义全量重写
        # 走 argo_paths 的原子写唯一来源，避免回缩中途崩溃留下半截文件
        _paths.atomic_write_text(path, kept.decode("utf-8", errors="replace"))
    except Exception:
        return


def usage_log_dir() -> Path:
    # ARGO_USAGE_LOG_DIR 优先（测试隔离）；未设置时由唯一来源派生
    override = os.environ.get("ARGO_USAGE_LOG_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _paths.state_path("usage_log")


def stream_dir() -> Path:
    """使用日志目录（公开读出口：stats 等读者需要展示数据在哪）。"""
    return usage_log_dir()


def _enabled() -> bool:
    return env_flag("ARGO_USAGE_LOG")


# 引擎改名前的历史别名（表外但真实）：与 usage_log_clean._ALIAS_KEEP 同一账。
_RENAMED_ALIAS_KEEP = frozenset({"openlibrary", "bing"})


def _canonical_engines() -> frozenset[str]:
    """注册表引擎名全集（含历史别名豁免）。注册表加载失败返回空集——
    守卫随之失效放行（fail-open）：守卫是防脏数据的，不能反过来丢真日志。

    不做外层缓存：available_engines 走 get_registry 的 mtime 指纹自愈，每次
    现取才能看见「daemon 常驻期间新增的本地引擎」；外层冻结只会把新引擎的
    真日志误杀成夹具。每次构建 frozenset 是微秒级，query 流每搜索只写一条。
    """
    try:
        from engines import available_engines
        return frozenset(available_engines()) | _RENAMED_ALIAS_KEEP
    except Exception:
        return frozenset()


def _fixture_engines(record: dict[str, Any]) -> list[str]:
    """query 记录里的注册表外引擎名（别名豁免后）。

    engines_used 缺失/非列表/为空一律视为无夹具信号：真实搜索的该字段
    永远是引擎 id 列表，守卫只对「有列表且含表外名」的形态做判定。
    """
    used = record.get("engines_used")
    if not isinstance(used, list) or not used:
        return []
    canon = _canonical_engines()
    if not canon:
        return []
    return [e for e in used if not isinstance(e, str) or e not in canon]


def emit(stream: str, record: dict[str, Any]) -> bool:
    """追加一条日志记录到 <usage_log_dir>/<stream>.jsonl。

    失败静默返回 False，绝不抛异常；记录内会补 ts / stream / version。
    """
    if not _enabled():
        return False
    if stream == "query" and _fixture_engines(record):
        # 夹具守卫（2026-10-25）：测试夹具引擎名（fast / slow / quick_primary
        # 这类注册表外名）屡次写进生产 query.jsonl（conftest 的
        # ARGO_USAGE_LOG=0 与 is_synthetic_run 的黑名单各漏过一批，最近一次
        # 2026-10-24），stats 的均值与最慢/高频引擎榜随之失真。黑名单永远
        # 追不全新夹具名，这里在唯一落盘口用注册表白名单反向判定：表外名
        # 即夹具，整条拒写。存量清理走 usage_log_clean.py（隔离不删除）。
        # 只拦 query 流：route/recovery 的字段形态与测试惯例不同（route 的
        # 格式测试本身用 e0..e14 假名），且实测污染只发生在 query 流。
        return False
    try:
        line = json.dumps(
            {
                "ts": datetime.now().astimezone().isoformat(timespec="microseconds"),
                "stream": stream,
                "version": _STREAM_VERSION,
                **record,
            },
            ensure_ascii=False,
        )
        d = usage_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{stream}.jsonl"
        # 「回缩 + 追加」必须在同一个文件锁内，这是数据不丢的前提。
        #
        # why：回缩走 argo_paths.atomic_write_text，它是 os.replace —— **换
        # inode 并 unlink 旧 inode**。若两段之间没有锁：进程 A 回缩替换了文件，
        # 进程 B 此前已按旧路径打开的追加 fd（或紧接着 open 到的旧 inode）
        # 就写进了已被 unlink 的孤儿 inode —— 记录「写成功」但永远读不到。
        # 长驻 MCP server（多线程并发 search）与 CLI 同写一个流文件时，文件
        # 恰好跨过 1 MiB 的那一刻就会触发。这与 quota.py 当初补文件锁要修的
        # 是同一类 bug（load-modify-write 缺跨进程锁则计数丢失）。
        with _paths.file_lock(path):
            _trim_if_oversized(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception:
        return False


def tail(stream: str, n: int = 10) -> list[dict[str, Any]]:
    """读取最近 n 条记录（供分析与测试）。读失败返回空列表。"""
    try:
        d = usage_log_dir()
        lines = (d / f"{stream}.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[-n:]]
    except Exception:
        return []
