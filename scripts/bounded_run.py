#!/usr/bin/env python3
"""bounded_run.py — 有界并发执行：总墙钟时间一到就返回，不被慢任务拖住。

背景
----
仓库里多处用 ``with ThreadPoolExecutor(...) as ex:`` 配合
``as_completed(futures, timeout=T)`` 想实现「最多等 T 秒」。但 ``with`` 退出时
会执行 ``shutdown(wait=True)``，把所有还在跑的任务 join 完才真正返回——即使
``as_completed`` 已经超时，调用方仍会被一个卡住的网络读拖住，T 秒预算形同虚设
（实测单查询被拖到 76s）；而且超时抛出的 TimeoutError 若没人接，会直接中断整次
调用，已经拿到的结果也丢了。

做法
----
改用 daemon 线程加轮询：

* 任务跑在 daemon 线程上，到点就返回已完成的结果，**不去 join 还没完成的任务**
  （daemon 线程不会阻止进程退出，各任务内部仍有自己的超时保底）；
* 单个任务抛异常只记成该任务失败，不影响其它任务；
* 总墙钟时间被 ``wait_s`` 硬性约束，不会超出。

search.py 的引擎并发已经是这套做法，这里抽成通用实现，供 research / social /
crawl / fetch 等复用，避免同一类问题在各处重复出现。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Generic, TypeVar

I = TypeVar("I")
O = TypeVar("O")


class TaskError(Generic[I]):
    """单个任务执行时抛了异常。用对象而非魔法字符串承载，避免和任务正常返回值混淆。"""

    __slots__ = ("exc", "item")

    def __init__(self, item: I, exc: BaseException):
        self.item = item
        self.exc = exc

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"TaskError(item={self.item!r}, exc={self.exc!r})"


def run_bounded(
    items: list[I],
    worker: Callable[[I], O],
    wait_s: float,
    max_workers: int = 3,
    *,
    enough: Callable[[list[tuple[I, Any]]], bool] | None = None,
    stagger_s: float = 0.0,
    poll: float = 0.01,
) -> tuple[list[tuple[I, Any]], list[I]]:
    """并发执行 ``worker(item)``，总等待不超过 ``wait_s`` 秒。

    返回 ``(finished, unfinished)``：

    * ``finished``：已完成任务的 ``(item, value)`` 列表，按完成先后排列；
      任务抛异常时 ``value`` 是 :class:`TaskError`，调用方按需转成失败占位。
    * ``unfinished``：到点仍没完成（或被 ``enough`` 提前收尾时还在跑）的 item 列表，
      调用方可据此补超时提示。这些任务不会被 join，本函数因此能准时返回。

    ``enough(finished)`` 返回 True 时提前收尾：不再启动排队中的剩余任务，已在跑的
    任务归入 unfinished。``max_workers`` 限制同时在跑的任务数。

    ``stagger_s`` 起步间隔（秒），单调：0 = 不节流（立即填满并发位）；值越大越接近
    严格串行（大于单任务超时上限即完全退回串行）。仅在**有任务在跑**时节制——
    对冲语义（首任务先跑，宽限窗内交付就只付一次调用），而非无条件串行化；在跑
    任务数为 0 时立即补发（前一个已返回，没有可对冲的对象，等满窗纯属浪费）。
    engine_dispatch 的 hedged race 与 search_pipeline 的恢复链是同一哲学的两个
    配置：改这里的起步语义，两边同时生效。
    """
    queue = list(items)
    running: list[tuple[threading.Thread, I, dict]] = []
    finished: list[tuple[I, Any]] = []
    deadline = time.monotonic() + max(0.0, float(wait_s))
    last_start: float | None = None

    def _start(item: I) -> None:
        holder: dict = {}

        def _work() -> None:
            try:
                holder["v"] = worker(item)
            except BaseException as exc:  # noqa: BLE001 - 任意任务异常都要隔离，不能让一个任务炸掉整组
                holder["v"] = TaskError(item, exc)

        th = threading.Thread(target=_work, daemon=True)
        th.start()
        running.append((th, item, holder))

    while queue or running:
        if time.monotonic() >= deadline:
            break
        # 按并发上限补启动排队任务
        while queue and len(running) < max(1, int(max_workers)):
            if (stagger_s > 0 and running and last_start is not None
                    and (time.monotonic() - last_start) < stagger_s):
                break  # 起步节流：等满 stagger 再补发（对冲）
            _start(queue.pop(0))
            last_start = time.monotonic()
        progressed = False
        for entry in list(running):
            th, item, holder = entry
            if th.is_alive():
                continue
            running.remove(entry)
            value = holder.get("v", TaskError(item, RuntimeError("任务结束但没有返回值")))
            finished.append((item, value))
            progressed = True
            if enough is not None and enough(finished):
                queue.clear()
                break
        if enough is not None and enough(finished):
            break
        if not progressed:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            time.sleep(min(poll, remain))

    # 收尾：还活着的不等待（关键：不 join），计入 unfinished；恰在末次轮询后完成的
    # 照常收进来，避免结果悄悄丢失。因并发上限到点仍没排上起跑的任务同样算未完成，
    # 不能让它既不在 finished、也不在 unfinished 里被悄悄丢掉。
    unfinished: list[I] = list(queue)
    for th, item, holder in running:
        if th.is_alive():
            unfinished.append(item)
        else:
            value = holder.get("v", TaskError(item, RuntimeError("任务结束但没有返回值")))
            finished.append((item, value))
    return finished, unfinished
