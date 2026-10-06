#!/usr/bin/env python3
"""cli_io.py — CLI 标准输入/输出判据的唯一来源。

## 为什么需要它

判断「stdin 有没有数据」最直觉的写法是 `not sys.stdin.isatty()`，但它是**错的**：
`/dev/null`、已关闭的 fd、以及**任何非交互环境下的空 stdin** 都不是 tty，
于是这个判据会把「没有数据」判成「有数据」。

后果在 argo 的主战场（脚本 / CI / cron / agent 调用）上最严重：
`argo evidence "query"` 会走进「读管道」分支、拿到空串，然后崩在
`json.load(sys.stdin)` 上（2026-09-15 实测复现，退出码 1 + Traceback）。

真正的判据是 fd 的**类型**：
  - S_ISFIFO → `... | argo ...`（管道）
  - S_ISREG  → `argo ... < data.json`（文件重定向）
  - S_ISCHR  → 终端或 /dev/null（无数据）

但「fd 类型对」只解决「要不要读」，不解决「读到什么时候」。它们合起来才是
完整的判据——**只判类型会让空管道永久阻塞**：

  FIFO 的 `read()` 要等到写端关闭才返回。而 agent / CI / cron 起的子进程，
  stdin 正是一个「空、且在整个子进程生命周期内不关闭」的管道；终端里
  `argo evidence "q"` 也是同一个形态。实测（2026-09-27）：
  `bin/argo evidence "query" --json` 在管道 stdin 下 90 秒不返回，
  `< /dev/null` 才 1 秒退出。后果是所有调用方（尤其 agent）只能等自己的超时。

所以管道分支**必须带截止时间**：先等「第一个字节到达」，再带总上限读到 EOF。
两个上限都写在这里，是因为「判据」和「期限」是同一件事的两半，分散到调用方
就会各自漂移（`bin/argo evidence` 与 `batch_probe` 都会各写一遍）。

## stdout 序列化同理

`--json` 输出是给 Agent / 脚本读的，缩进只增加传输体积与 token，不增加任何
信息。MCP 侧早已如此（`mcp_handlers._dumps` 默认 `separators=(",", ":")`），
但 CLI 侧此前在 45 处各写一遍 `json.dumps(..., indent=2)`——同一份载荷两套
计算方式，实测多占 22% 体积。这里给 stdout 一个唯一入口。

## stdout/stderr 的编码也归这里

给子进程注入 `PYTHONUTF8=1` 是本仓既有做法（mcp_server / search.py 的
local-seek / recompute 都这么做），但**自家进程**从没做过同一件事。实测
`PYTHONIOENCODING=ascii python3 scripts/search.py "贵州茅台"` 直接
UnicodeEncodeError 崩掉；Windows 老终端（cp1252/cp936）走的是同一条路。
输出契约既然定的是 UTF-8 JSON，进程自己的 stdout 就必须跟上，见
`ensure_utf8_stdio()`。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from typing import Any

__all__ = ["stdin_is_piped", "read_stdin_if_piped", "dumps", "dumps_pretty",
           "ensure_utf8_stdio"]

# stdout 的紧凑分隔符（无冗余空格）：与 mcp_handlers._dumps 的默认计算方式一致。
_COMPACT = (",", ":")

# 管道 stdin 的两个截止时间（秒）。见模块头「必须带截止时间」。
#
# 第一个字节：实测 `echo '{...}' | argo ...` 的首字节在毫秒级到达，0.25s 已是
# 两个数量级的余量；再长只是让「空管道」这一类多等，没有收益。
_FIRST_BYTE_WAIT_S = 0.25
# 总上限：正常载荷（evidence 的搜索结果 JSON、preflight 的 URL 清单）在毫秒级
# 读完，3s 只用来兜「写端写了半截又不关闭」的半开管道——超时返回**已读到的
# 部分**而不是空串，让 json.load 在调用方那里响亮地失败，而不是被静默当成
# 「没有 stdin 数据」而改走另一条分支（那会变成悄悄换答案）。
_TOTAL_WAIT_S = 3.0
# 单次系统调用的读取块大小。
_CHUNK = 65536
# 无数据时的轮询间隔：期限只有 0.25s/3s 两级，10ms 足够精确而几乎不烧 CPU。
_POLL_INTERVAL_S = 0.01


def dumps(obj: Any) -> str:
    """CLI stdout 的 JSON 序列化唯一来源（默认紧凑）。

    用途边界：**stdout**。写进磁盘的归档文件（`archive_run` 的 public.json /
    coverage.json 等）是给人翻的，仍用 `dumps_pretty`——「机器读 stdout、
    人读文件」这条线让两种格式各归其位，而不是按文件拍脑袋。
    """
    return json.dumps(obj, ensure_ascii=False, separators=_COMPACT)


def dumps_pretty(obj: Any) -> str:
    """人读场景（归档文件、诊断输出）的缩进序列化。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def ensure_utf8_stdio() -> None:
    """把本进程的 stdout/stderr 固定成「机器可读、且不会炸」的编码。

    why：子进程侧早就注入了 `PYTHONUTF8=1`，自家进程却从来没人管。实测
    `PYTHONIOENCODING=ascii python3 scripts/search.py "贵州茅台"` 直接
    UnicodeEncodeError + Traceback（退出码 1）；Windows 老控制台
    （cp1252/cp936）是同一个故障形态。而 stdout 的契约是 UTF-8 JSON
    （见 `dumps`），进程自己的流必须跟上，否则「能力都在、结果出不来」。

    **分两档，因为管道与终端的正确行为不同**：
      - 非终端（管道 / 文件 / agent 消费）：强制 UTF-8。机器读的字节必须是
        UTF-8，否则下游 decode 出的就是乱码，而这类调用正是 argo 的主战场。
      - 终端：保留平台编码，只把 errors 放宽成 `backslashreplace`。中文
        Windows 的 cp936 终端本来就能正确显示中文，硬换成 UTF-8 反而变乱码；
        而写不出的字符以转义形式出现，既不崩、也不像 `replace` 那样把字符
        静默换成 `?`（那等于悄悄损坏数据，比崩更难查）。

    只碰 encoding / errors，不动缓冲与换行；任何不支持 reconfigure 的流
    （测试替身、已被包过的流）静默跳过——这是纯加固，失败不该影响主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="backslashreplace")
            else:
                stream.reconfigure(encoding="utf-8",
                                   errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            continue


def stdout_is_tty() -> bool:
    """stdout 是否是终端（人类消费）。

    Agent 消费场景（管道/重定向/capture_output）下 stdout 不是 tty，
    此时可自动启用 --fields agent 瘦身。判据是 os.isatty(fileno())——
    直接问内核「是不是终端」；与 stdin_is_piped 同构的是 fail-safe 结构：
    fd 取不到时这里当终端（不瘦身），那边当没数据。
    """
    try:
        return os.isatty(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return True  # fail-safe：取不到就当终端，不瘦身


def stdin_is_piped() -> bool:
    """stdin 是否接了管道或文件重定向（而非终端 / /dev/null / 未打开）。"""
    mode = _stdin_mode()
    return mode is not None and (stat.S_ISFIFO(mode) or stat.S_ISREG(mode))


def _stdin_mode() -> int | None:
    """stdin 的 st_mode；取不到（已关闭 / 非法 fd）返回 None。

    「取得到吗」与「是什么类型」拆开，是因为前者要 fail-safe 成「没有数据」，
    后者才谈得上判据；两件事混在一个函数里时，新增调用方总有一半会忘。
    """
    try:
        return os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return None


def read_stdin_if_piped() -> str:
    """读走 stdin 的全部内容；未接管道/重定向时返回空串。

    一次性读完而不是判空后再读——fd 只能顺序读一遍，窥探会吃掉内容。
    （带截止时间的管道分支同理：**边读边攒**，而不是先窥探再读第二次。）
    """
    mode = _stdin_mode()
    if mode is None:
        return ""
    if stat.S_ISREG(mode):
        # 文件重定向：read() 到 EOF 立即返回，本来就不会挂，无需期限
        return _read_all_text()
    if not stat.S_ISFIFO(mode):
        return ""  # 终端 / /dev/null / 其它字符设备：没有数据
    return _read_pipe_with_deadline()


def _read_all_text() -> str:
    try:
        return sys.stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return ""


def _read_pipe_with_deadline() -> str:
    """读管道 stdin，带「首字节」与「总时长」两个截止时间（见模块头）。

    做法是把 fd 临时切成非阻塞、用 `os.read` 轮询，而不是「另起线程去阻塞读」：
    **daemon 线程解不开这个锁**——它阻塞在 BufferedReader 的 C 读上时，解释器
    退出会撞上 `_enter_buffered_busy: could not acquire lock for
    <_io.BufferedReader name='<stdin>'> at interpreter shutdown`（本机实测直接
    SIGABRT，rc=-6，比原来的挂起更难查）。非阻塞轮询不留后台线程，就没有这个
    收尾问题。

    前提：本函数是 stdin 的第一个读者。走 `os.read(fd)` 会绕过文本层的缓冲，
    若此前已有代码读过 stdin，缓冲区里那一截就取不到了。现有调用方
    （`bin/argo` 的 evidence 分支、`batch_probe` 的 stdin URL 清单）都在进程
    最开头调用它。
    """
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        return ""
    try:
        was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
    except (OSError, ValueError, AttributeError):
        # 设不了非阻塞（少数平台 / 管道类型）：退回改动前的语义（可能阻塞），
        # 但绝不猜数据、也不造假数据
        return _read_all_text()

    deadline_first = time.monotonic() + _FIRST_BYTE_WAIT_S
    deadline_total = deadline_first + _TOTAL_WAIT_S
    chunks: list[bytes] = []
    try:
        while True:
            try:
                block = os.read(fd, _CHUNK)
            except BlockingIOError:
                block = None
            except (OSError, ValueError):
                break  # fd 坏了/关了：手上有多少算多少
            if block is not None:
                if not block:
                    break  # EOF：写端关闭，正常收尾
                chunks.append(block)
                continue
            # 这一刻没有数据可读
            if not chunks:
                if time.monotonic() >= deadline_first:
                    return ""  # 首字节没来：按「没有 stdin 数据」处理
            elif time.monotonic() >= deadline_total:
                break  # 半开管道：带已读到的部分返回，让调用方响亮地失败
            time.sleep(_POLL_INTERVAL_S)
    finally:
        try:
            if was_blocking:
                os.set_blocking(fd, True)
        except (OSError, ValueError, AttributeError):
            pass
    payload = b"".join(chunks)
    encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
    return payload.decode(encoding, errors="replace")
