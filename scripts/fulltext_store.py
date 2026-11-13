#!/usr/bin/env python3
"""fulltext_store.py — 抓取正文的全文存档。

问题：`max_chars` 同时管两件事——「交给 Agent 多少」（上下文预算）和
「取回物留多少」（证据完整性）。前者必须有界，后者不该有损。合成一个机制
的后果是：默认档 8,000 字抓一个 118,789 字的页面，剩下 93% 当场销毁，
既没有存档、也没有信号、更没法回读。而 argo 的定位是「搜索与证据核验」，
证据被截断销毁与定位直接冲突。

原则：**交付视图与存档副本分离**。
  - `content`（交付给 Agent）仍然受 max_chars 约束，token 预算不变
  - 完整正文另存一份到状态目录，结果里给 `full_text_path` 与 `truncated` 标记
  - 回读走本地文件，不重新联网

只在**确实发生截断**时写档：没截断就没有副本被销毁，写档纯属占盘。

存储形态：
  <state_root>/fulltext/<sha1(url|kind)[:16]>.<ext>   正文，纯文本可直接读
  <state_root>/fulltext/index.jsonl                   追加式索引，仅用于列举

读取只按 URL 哈希定位，不依赖索引——索引坏了、被删了都不影响回读，
它只是给人看的一份台账。

开关：ARGO_FULLTEXT=0 关闭存档；目录可用 ARGO_FULLTEXT_DIR 覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from engine_env import env_flag

# 单个归档上限：超过它的页面本身就不适合整份留存（实测最大的正常页面在
# 2 MB 量级；出现 8 MB 以上的多半是误抓到的数据文件而不是文章）。
MAX_FILE_BYTES = 8 * 1024 * 1024
# 归档总量上限，超出按最久未访问（mtime）淘汰
MAX_FILES = 300
MAX_TOTAL_BYTES = 256 * 1024 * 1024

_EXT = {"md": "md", "text": "txt", "html": "html"}


def enabled() -> bool:
    """全文存档开关：ARGO_FULLTEXT=0 关闭，默认开启。"""
    return env_flag("ARGO_FULLTEXT")


def store_dir() -> Path:
    override = (os.environ.get("ARGO_FULLTEXT_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        import argo_paths
        return argo_paths.ensure_state_dir("fulltext")
    except Exception:
        return Path.home() / ".cache" / "unified-search" / "fulltext"


def key_for(url: str, kind: str = "md") -> str:
    """归档文件名主体。kind 参与哈希：同一 URL 的正文与 HTML 是两份东西。"""
    raw = f"{kind}|{url}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def path_for(url: str, kind: str = "md") -> Path:
    return store_dir() / f"{key_for(url, kind)}.{_EXT.get(kind, 'txt')}"


def save(url: str, text: str, kind: str = "md") -> str | None:
    """存档完整正文，返回路径；不满足条件或失败时返回 None。

    **永不上抛**：存档是增值能力，不是抓取的前置条件。写不进去（只读挂载、
    磁盘满、权限不足）只是少一份副本，不该让一次成功的抓取变成失败。
    """
    if not enabled() or not text:
        return None
    size = len(text.encode("utf-8", errors="replace"))
    if size > MAX_FILE_BYTES:
        return None
    try:
        import argo_paths
        path = path_for(url, kind)
        argo_paths.atomic_write_text(path, text)
        _index(url, kind, path, size)
        _evict()
        return str(path)
    except Exception:
        return None


def load(url: str, kind: str = "md") -> str | None:
    """按 URL 回读归档正文；没有则 None。"""
    try:
        path = path_for(url, kind)
        if path.is_file():
            os.utime(path, None)  # 回读即续命，参与 LRU
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return None


def load_path(path: str) -> str | None:
    """按显式路径回读（结果里给的 full_text_path 可能来自旧状态目录）。"""
    try:
        p = Path(path).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return None


def entries() -> list[dict]:
    """列举归档（按 mtime 新→旧）。用于自省与排障。"""
    out: list[dict] = []
    try:
        d = store_dir()
        for p in d.iterdir():
            if p.suffix == ".jsonl" or not p.is_file():
                continue
            st = p.stat()
            out.append({"path": str(p), "bytes": st.st_size,
                        "mtime": st.st_mtime})
    except Exception:
        return []
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


def _index(url: str, kind: str, path: Path, size: int) -> None:
    """追加一行台账。写入失败不影响主流程（回读不依赖它）。"""
    try:
        line = json.dumps({"url": url, "kind": kind, "path": str(path),
                           "bytes": size, "ts": int(time.time())},
                          ensure_ascii=False) + "\n"
        with open(store_dir() / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _evict() -> None:
    """按数量与总量淘汰最旧的归档。

    entries() 是 mtime 新→旧的。两个上限分开处理：先按数量砍到 MAX_FILES，
    再按总量继续砍——写成单个循环配一个 break 很容易在 i=0 处就退出
    （此处连踩两次），分开写反而没有出错空间。
    """
    items = entries()
    total = sum(e["bytes"] for e in items)
    dropped = 0

    def _drop(seq: list) -> int:
        nonlocal dropped
        try:
            os.unlink(seq["path"])
        except Exception:
            return 0
        dropped += 1
        return seq["bytes"]

    while len(items) > MAX_FILES:
        total -= _drop(items.pop())
    while items and total > MAX_TOTAL_BYTES:
        total -= _drop(items.pop())

    # 淘汰发生了才对账：台账与正文同生命周期。追加式台账曾只写不截断，
    # 正文有 MAX_FILES/MAX_TOTAL 淘汰而台账没有——生产库实测 688 行里
    # 386 行（56%）指向已淘汰文件，纯死行。
    if dropped:
        _reconcile_index()


def _reconcile_index() -> None:
    """把 index.jsonl 重写为与现存正文对齐的最小台账；失败不影响主流程。

    回读只按 URL 哈希定位（见模块头），台账只是给人看的列举台账——重写
    坏了最多丢列举能力，不丢回读能力。同一文件多次存档的记录保留最后一条
    （追加序里最后出现的行元数据最新）。
    """
    try:
        d = store_dir()
        idx = d / "index.jsonl"
        if not idx.is_file():
            return
        alive = {e["path"] for e in entries()}
        last: dict[str, str] = {}
        with idx.open(encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue  # 坏行丢弃，不让一行脏数据卡住整个台账
                p = rec.get("path")
                if isinstance(p, str):
                    last[p] = line
        kept = [last[p] for p in last if p in alive]
        import argo_paths
        argo_paths.atomic_write_text(idx, "".join(l + "\n" for l in kept))
    except Exception:
        pass

