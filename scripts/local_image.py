#!/usr/bin/env python3
"""local_image.py — 本地图片语义检索（macOS Vision + 多模态模型复合）。

## 重新定义问题

需求写的是「本地图片能精准高效地搜索整理」。直接照着做会落进一个陷阱：
「精准」看起来要求一个懂语义的模型（CLIP），于是方案变成装 torch + 下 400MB
模型 + 建 FAISS 索引。但把整条链路摊开看，**判断哪张图是用户要的，这件事
本来就在模型手里**——搜出候选之后是模型在看图。CLIP 只是抢在模型之前做了
一次粗糙的相关度排序。

于是问题重定义为：**模型看不了 7 万张图**（那是 7 万次图像调用）。脚本这层
的任务不是「判断得准」，而是**把 7 万张收敛到几十张**，保证召回；精度由模型
在几十张的规模上完成。

这个定义下的技术选择完全不同：
  - 不需要 CLIP 的语义嵌入（那是为了在无模型参与时排序）
  - 需要的是**廉价、确定、可缓存的索引维度**
  - macOS 内置 Vision 一次调用给三样：分类标签、OCR 文本、768 维特征指纹
  - 零依赖、约 0.12 秒/张（4 路并行实测），7.7 万张约 2.5 小时一次性建库

## 三层分工

  廉价层（本模块）：7 万 → 几十张候选。走文件名、分类标签、OCR 文本、
                    特征指纹四个维度，全是确定性计算，可缓存可复算。
  判断层（模型）：  几十张 → 精确结果。把候选拼成联络表（contact sheet）
                    一次性交给多模态模型，它能理解否定、关系、审美——
                    这些是标签和 OCR 表达不了的。
  组织层（模型）：  归类、命名、成集。原来完全空缺的能力。

## 召回的已知边界（诚实记录）

纯靠分类标签 + OCR + 文件名，「暖光下孤独感的照片」这类**纯氛围查询**召回
有洞——标签里不会有「孤独」。这类查询需要真正的图像-文本嵌入（CLIP 路线）。
本模块的处理是：默认走 Vision 路线（零依赖、覆盖日常绝大多数查询），
`SEMANTIC_BACKENDS` 留出可插拔位，需要时再挂 CLIP，接口不变。不为了少数
场景让所有安装背上 GB 级依赖。

## 存储：SQLite 存元数据，指纹单独一个 .npy

`index.db`（SQLite）只放元数据：路径 / mtime / 尺寸 / 标签 / OCR / `fp_slot`。
768 维指纹另存同目录的 `fp.npy`——一个连续的 float32 矩阵，检索时 mmap 只读、
一次矩阵点积出全部相似度。

为什么指纹不留在 SQLite BLOB 里（2026-10-06 实测）：
  旧实现把指纹按行取成 7.7 万个 BLOB，再在 Python 里**逐行** frombuffer +
  两次 `np.linalg.norm` + 点积——7.7 万张约 180ms。换成连续矩阵后一次
  `M @ q` 走 BLAS，7.7 万张约 3.7ms（快约 50 倍）。代价是检索要的是「一整块
  连续内存」，SQLite 给不了，只有文件 mmap 能给。
  指纹在**写入时**就 L2 归一化，检索端只归一化查询向量——点积即余弦，
  不再逐行算范数。

`fp.npy` 是派生件：每次 `index` 结束都按当前 `images` 表重建（新/更新的用本次
探测结果，未变的从旧矩阵按 `fp_slot` 搬），所以它永远与 SQLite 一致，删了也能
从零重建。指纹用 float32 存：7.7 万张约 226MB，占原图总量（~4.3GB）约 5%。

为什么仍不做向量索引（FAISS/hnswlib）：连续矩阵点积已 3.7ms，FAISS 的价值
在千万级，这里是几十万级，引入它是为不存在的规模付费。

索引默认落在 `~/.cache/unified-search/argo-image/`（`ARGO_IMAGE_DB` 可换）。

本能力**默认关闭**：需 `ARGO_LOCAL_IMAGE=1` 才可执行（见 main() 的开关判定）。
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# 项目根（scripts/ 的父目录），Swift 辅助与缩略图都相对它定位
_ROOT = Path(__file__).resolve().parent.parent
_PROBE_BIN = _ROOT / "scripts" / "image_vision" / "vision_probe"
_PROBE_SRC = _ROOT / "scripts" / "image_vision" / "vision_probe.swift"

_EXTS = frozenset(
    (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
     ".heic", ".heif", ".avif", ".jfif")
)

# 跳过目录：与用户既有整理习惯一致（scan_ai_images.py 同口径），
# 加上体积巨大且无检索价值的系统/依赖目录。
_SKIP_DIRS = frozenset((
    ".git", "node_modules", ".Trash", "Library", "$RECYCLE.BIN",
    "System Volume Information", "AppData", ".cache", ".venv", "venv",
    "__pycache__", ".photoslibrary", ".photolibrary", ".photospackage",
    ".library", "site-packages",
))

from argo_paths import platform_cache_default  # noqa: E402

DEFAULT_DB = Path(
    os.environ.get("ARGO_IMAGE_DB")
    or (platform_cache_default() / "argo-image" / "index.db")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    width       INTEGER,
    height      INTEGER,
    inode       INTEGER,
    labels      TEXT,      -- JSON: [{"id":..,"conf":..}]
    ocr         TEXT,      -- 换行连接的 OCR 文本
    fp_slot     INTEGER,   -- 指纹在 fp.npy 里的行号（NULL=无指纹）
    fp_dim      INTEGER,
    indexed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_images_inode ON images(inode);
CREATE INDEX IF NOT EXISTS idx_images_mtime ON images(mtime);
CREATE INDEX IF NOT EXISTS idx_images_fp_slot ON images(fp_slot);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def ensure_probe() -> Path:
    """确保 Swift 辅助已编译；不存在或源码更新则重编。

    二进制不入库（编译产物按架构/系统版本而异），首次使用时编译并缓存。
    编译约 1.7 秒，一次性成本。
    """
    if not _PROBE_SRC.exists():
        raise FileNotFoundError(f"vision_probe.swift 缺失: {_PROBE_SRC}")
    if _PROBE_BIN.exists() and _PROBE_BIN.stat().st_mtime >= _PROBE_SRC.stat().st_mtime:
        return _PROBE_BIN
    _log(f"编译 Vision 辅助（首次，约 2 秒）…")
    r = subprocess.run(
        ["swiftc", "-O", str(_PROBE_SRC), "-o", str(_PROBE_BIN)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    if r.returncode != 0 or not _PROBE_BIN.exists():
        raise RuntimeError(f"编译失败: {r.stderr[:400]}")
    return _PROBE_BIN


def vision_available() -> bool:
    """本机是否具备 Vision 路线（macOS + swiftc）。"""
    if sys.platform != "darwin":
        return False
    import shutil
    return shutil.which("swiftc") is not None


_ENABLE_ENV = "ARGO_LOCAL_IMAGE"


def local_image_enabled() -> bool:
    """本能力默认关闭：需 `ARGO_LOCAL_IMAGE=1` 显式开启（见模块 docstring）。

    为什么默认关：它要 macOS + swiftc 编译辅助、要 numpy，还要先建一份
    ~226MB/7.7 万张 的指纹索引才可用——对绝大多数安装是纯负担。默认关 = 不建库、
    不占资源；要用的人显式打开一次。
    """
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def open_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(db_path).expanduser() if db_path else DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    # 迁移必须前置于 executescript：新 schema 会在 fp_slot 上建索引，而旧表
    # 还没有这一列——先跑 CREATE INDEX 会直接报「no such column: fp_slot」。
    # _migrate_fp_column 对全新库（连 images 表都没有）直接返回，不影响首建。
    _migrate_fp_column(conn, p)
    conn.executescript(_SCHEMA)
    return conn


def walk_images(roots: Iterable[str | Path], *, follow_symlinks: bool = False,
                on_skip: Any = None) -> list[Path]:
    """递归收集图片文件（跳过系统/依赖目录，不跟软链避免循环）。"""
    out: list[Path] = []
    seen_dirs: set[tuple[int, int]] = set()
    for root in roots:
        rp = Path(root).expanduser()
        if not rp.exists():
            if on_skip:
                on_skip(str(rp), "not_found")
            continue
        for dirpath, dirnames, filenames in os.walk(rp, followlinks=follow_symlinks):
            # 原地改 dirnames 才能阻止 os.walk 下降（返回值过滤无效）
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            # 硬链接/软链导致的重复目录：用 (dev, inode) 去重，否则同一个
            # 目录被两个路径指到就会索引两遍
            try:
                st = os.stat(dirpath)
                key = (st.st_dev, st.st_ino)
                if key in seen_dirs:
                    dirnames[:] = []
                    continue
                seen_dirs.add(key)
            except OSError:
                pass
            for fn in filenames:
                if Path(fn).suffix.lower() in _EXTS:
                    out.append(Path(dirpath) / fn)
    return out


def _run_probe(paths: list[Path], *, parallel: int = 4,
               want_ocr: bool = True, want_fp: bool = True,
               timeout: float = 600) -> list[dict[str, Any]]:
    """调 Swift 辅助批量抽取（多进程并行），返回解析后的记录。

    并行按「进程」而非「线程」：Vision 的请求执行在 Swift 侧是同步的，
    Python 侧用多进程才能真并行（实测 4 路 0.24→0.118 秒/张）。
    """
    if not paths:
        return []
    binp = ensure_probe()
    chunk_n = max(1, parallel)
    chunks = [paths[i::chunk_n] for i in range(chunk_n)]
    args: list[str] = []
    if not want_ocr:
        args.append("--no-ocr")
    if not want_fp:
        args.append("--no-fp")
    procs = []
    for ch in chunks:
        if not ch:
            continue
        procs.append(subprocess.Popen(
            [str(binp), *args, *[str(p) for p in ch]],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace",
        ))
    records: list[dict[str, Any]] = []
    for pr in procs:
        try:
            out, _ = pr.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            pr.kill()
            out, _ = pr.communicate()
        for line in (out or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def _decode_fp(b64: str, dim: int) -> bytes | None:
    """base64 float32 → bytes（写入前解码，随即进 fp.npy）。"""
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    return raw if len(raw) == dim * 4 else None


def _fp_path(db_path) -> Path:
    """指纹矩阵文件：与 index.db 同目录的 fp.npy（派生件，可随时重建）。"""
    return Path(db_path).expanduser().parent / "fp.npy"


def _db_file(conn: sqlite3.Connection) -> Path:
    """当前连接主库的文件路径（指纹矩阵与它同目录）。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return DEFAULT_DB
    f = row["file"] if row is not None else ""
    return Path(f) if f else DEFAULT_DB


def _unit(vec):
    """L2 归一化（写入时归一，检索端点积即余弦）。零向量原样返回。"""
    import numpy as np
    v = np.asarray(vec, dtype="<f4")
    n = float(np.linalg.norm(v))
    return v if n == 0.0 else (v / n)


def _load_fp_matrix(db_path):
    """mmap 只读加载指纹矩阵；不存在/损坏返回 None。

    mmap 是这一步的关键：检索端拿到的是一块连续只读内存，`M @ q` 直接走
    BLAS，无需把 7.7 万个 BLOB 逐行读进 Python（那正是旧实现的瓶颈）。
    """
    import numpy as np
    p = _fp_path(db_path)
    if not p.exists():
        return None
    try:
        return np.load(p, mmap_mode="r")
    except (OSError, ValueError):
        return None


def _write_fp_matrix(db_path, pairs, dim) -> dict:
    """把 (路径, 向量) 序列写成连续 fp.npy，返回 {路径: 行号}。

    只收「有向量且维度匹配」的行；写入前 L2 归一化。原子写（.tmp → replace），
    避免检索端 mmap 到写了一半的文件。
    """
    import numpy as np
    out = _fp_path(db_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    items = [(p, v) for p, v in pairs if v is not None and v.shape[0] == dim]
    if not items:
        try:
            out.unlink()
        except OSError:
            pass
        return {}
    tmp = out.with_name(out.name + ".tmp")
    mm = np.lib.format.open_memmap(tmp, mode="w+", dtype="<f4",
                                   shape=(len(items), dim))
    slots: dict[str, int] = {}
    for i, (p, v) in enumerate(items):
        mm[i] = _unit(v)
        slots[p] = i
    mm.flush()
    del mm
    os.replace(tmp, out)
    return slots


def _rebuild_fp(conn, db_path, fresh, old_slots, old_mat) -> int:
    """按当前 images 表重建 fp.npy，并回填 fp_slot；返回写入行数。

    顺序取 rowid（稳定）。本次新/更新过的行用 fresh 里的向量；未变的行从旧
    矩阵按 fp_slot 原样搬（不重算）。这是「指纹矩阵永远与 SQLite 一致」的
    唯一实现点——所以 fp.npy 可以随时删除，下次索引自动重建。
    """
    rows = list(conn.execute("SELECT path FROM images ORDER BY rowid"))
    dim = None
    for v in fresh.values():
        if v is not None:
            dim = int(v.shape[0])
            break
    if dim is None and old_mat is not None:
        dim = int(old_mat.shape[1])
    pairs = []
    for r in rows:
        p = r["path"]
        v = fresh.get(p)
        if v is None and old_mat is not None:
            s = old_slots.get(p)
            if s is not None and 0 <= s < old_mat.shape[0]:
                v = old_mat[s]
        pairs.append((p, v))
    slots = _write_fp_matrix(db_path, pairs, dim) if dim else {}
    conn.execute("UPDATE images SET fp_slot = NULL")
    if slots:
        conn.executemany("UPDATE images SET fp_slot = ? WHERE path = ?",
                         [(s, p) for p, s in slots.items()])
    conn.commit()
    return len(slots)


def _migrate_fp_column(conn, db_path) -> None:
    """旧库（指纹存 fp BLOB 列）→ 指纹矩阵（一次性）。

    2026-10-06 起指纹移出 SQLite。旧库只补 fp_slot 列并把 BLOB 搬进矩阵，
    不 DROP 旧列——SQLite 老版本不支持 DROP COLUMN，且留着的 NULL 列不占空间。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
    if "fp" not in cols or "fp_slot" in cols:
        return
    conn.execute("ALTER TABLE images ADD COLUMN fp_slot INTEGER")
    rows = [(r["path"], r["fp"], r["fp_dim"])
            for r in conn.execute("SELECT path, fp, fp_dim FROM images")]
    dim = None
    for _p, blob, d in rows:
        if blob and d:
            dim = int(d)
            break
    if dim is None:
        conn.commit()
        return
    import numpy as np
    pairs = []
    for p, blob, d in rows:
        if not blob or int(d or 0) != dim:
            pairs.append((p, None))
            continue
        pairs.append((p, np.frombuffer(blob, dtype="<f4")))
    slots = _write_fp_matrix(db_path, pairs, dim)
    conn.executemany("UPDATE images SET fp_slot = ? WHERE path = ?",
                     [(s, p) for p, s in slots.items()])
    conn.commit()


def index_paths(
    conn: sqlite3.Connection,
    roots: Iterable[str | Path],
    *,
    parallel: int = 4,
    incremental: bool = True,
    batch: int = 256,
    on_progress: Any = None,
    max_images: int | None = None,
) -> dict[str, int]:
    """建库/增量索引。

    增量判据用 **(inode, mtime, size)** 三元组而非单看路径：路径相同但内容
    变了（图被重新生成/编辑过）必须重算指纹，否则以图搜图会返回陈旧结果；
    而单看 mtime 在跨文件系统复制后普遍不可靠，故三者一起比。
    文件被移动（inode 不变，路径变）时也认得出，不必重算指纹。

    返回统计：total / new / updated / unchanged / gone / failed。
    """
    files = walk_images(roots)
    # gone 判定的磁盘全集必须在 max_images 截断**之前**取：截断只会限制
    # 「本次要新算指纹的文件数」，不该把没进本次批次的历史条目判成消失。
    disk_all = {str(p) for p in files}
    if max_images is not None:
        files = files[:max_images]
    stat = {"total": len(files), "new": 0, "updated": 0, "unchanged": 0,
            "gone": 0, "failed": 0}

    # 指纹矩阵与旧 slot：未变的行从旧矩阵搬指纹，不重算。矩阵丢失时无法复用
    # 旧指纹，退化为全量重算（否则未变行的指纹会被静默清空——那是静默数据损坏）。
    db_path = _db_file(conn)
    old_slots: dict[str, int] = {}
    for row in conn.execute("SELECT path, fp_slot FROM images"):
        if row["fp_slot"] is not None:
            old_slots[row["path"]] = row["fp_slot"]
    old_mat = _load_fp_matrix(db_path)
    if incremental and old_slots and old_mat is None:
        incremental = False
        old_slots = {}

    existing: dict[str, tuple[float, int, int]] = {}
    if incremental:
        for row in conn.execute("SELECT path, mtime, size, inode FROM images"):
            existing[row["path"]] = (row["mtime"], row["size"], row["inode"] or 0)

    todo: list[Path] = []
    for p in files:
        sp = str(p)
        try:
            st = p.stat()
        except OSError:
            stat["failed"] += 1
            continue
        cur = (st.st_mtime, st.st_size, st.st_ino)
        prev = existing.get(sp)
        if prev is None:
            stat["new"] += 1
            todo.append(p)
        elif prev != cur:
            # 三元组整体比较（mtime, size, inode）——docstring 说的「三者一起比」。
            # 此前只比前两项，inode 被 SELECT 出来、被存进 existing，却在比较时
            # 被丢弃：跨文件系统复制后 mtime 可能保留，inode 必然不同，
            # 漏比它就漏掉这次重算（返回陈旧指纹）。
            stat["updated"] += 1
            todo.append(p)
        else:
            stat["unchanged"] += 1

    # 消失的文件：清出索引，否则检索会返回打不开的路径
    if incremental:
        # gone 判定必须带 root 维度（2026-09-27 数据丢失修复）：索引是多次
        # 增量累积的，本次只扫了 roots 下的文件。若拿全表路径与本次磁盘集合
        # 做差，上一次索引的其他 root 下的条目会被整批判为消失删光——实测
        # 先索引 ~/Pictures 再索引 ~/Downloads，第一次的条目全部误删。
        # 故只删「属于本次 root 且不在磁盘上」的条目；roots 为空（没扫任何
        # 东西）时同理不做全表差。
        _root_prefixes = tuple(
            str(Path(r).expanduser()).rstrip(os.sep) + os.sep for r in roots if r)
        disk = disk_all
        if _root_prefixes:
            gone = [sp for sp in existing
                    if sp not in disk and sp.startswith(_root_prefixes)]
        else:
            gone = []
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            conn.executemany("DELETE FROM images WHERE path = ?",
                             [(g,) for g in chunk])
        conn.commit()
        stat["gone"] = len(gone)

    fresh: dict[str, Any] = {}
    if todo:
        from PIL import Image  # 只在真正索引时才需要 Pillow

        done = 0
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            recs = _run_probe(chunk, parallel=parallel)
            rows = []
            by_path = {r.get("path"): r for r in recs if isinstance(r, dict)}
            for p in chunk:
                sp = str(p)
                rec = by_path.get(sp)
                if not rec or not rec.get("ok"):
                    stat["failed"] += 1
                    continue
                try:
                    st = p.stat()
                except OSError:
                    stat["failed"] += 1
                    continue
                # 尺寸：Vision 不返回像素尺寸，用 Pillow 读（只读头部，不解码）
                w = h = None
                try:
                    with Image.open(p) as im:
                        w, h = im.size
                except Exception:
                    pass
                labels = rec.get("labels") or []
                ocr = "\n".join(rec.get("ocr") or [])
                blob = _decode_fp(rec.get("fp") or "", int(rec.get("fp_dim") or 0))
                if blob:
                    import numpy as np
                    fresh[sp] = np.frombuffer(blob, dtype="<f4")
                # fp_slot 先写 NULL，末尾 _rebuild_fp 统一回填
                rows.append((
                    sp, st.st_mtime, st.st_size, w, h, st.st_ino,
                    json.dumps(labels, ensure_ascii=False), ocr, None,
                    rec.get("fp_dim"), time.time(),
                ))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO images "
                    "(path,mtime,size,width,height,inode,labels,ocr,fp_slot,fp_dim,indexed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.commit()
            done += len(chunk)
            if on_progress:
                on_progress(done, len(todo))

    # 指纹矩阵重建：把 fp.npy 对齐到当前 images 表。没有变化（todo 空、无 gone）
    # 且矩阵在位时跳过——避免每次 no-op 增量都白写 226MB。
    if todo or stat["gone"] or not _fp_path(db_path).exists():
        _rebuild_fp(conn, db_path, fresh, old_slots, old_mat)
    return stat


# ── 检索 ──────────────────────────────────────────────────────────────────

def _tokens(text: str) -> list[str]:
    """查询切词：ASCII 按词、CJK 按字（中文无空格，字是最小单位）。

    与 argo 主搜索的 `_tokens` 同思路，但这里不做词干化——文件名和标签
    以名词为主，词干化收益低而误配风险高。
    """
    import re
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9_+\-.#]+|[\u4e00-\u9fff]", text or ""):
        out.append(m.group(0).lower())
    return out


# 每字段的打分上限与整串分。
#
# 不变量：**同字段内，整串命中恒高于碎片命中**。靠固定奖励做不到这一点——
# 碎片分随查询长度线性累加（「机器学习」四个字全命中就 7.0），任何固定值的
# 整串奖励都会被更长的查询压过去。故对碎片分设上限，并让整串分高于上限。
#
# 权重次序（跨字段）：文件名整串 > 图中文字整串 > 文件名碎片 > 图中文字碎片
# > 标签碎片。文件名最强是因为它是用户自己起的；OCR 次之因为它是图里真实
# 存在的字符串；标签最弱因为它是机器给的英文泛词。
_SCORE = {
    "name":  {"whole": 6.0, "frag": 3.0, "cap": 4.0},
    "ocr":   {"whole": 5.0, "frag": 1.5, "cap": 3.0},
    "label": {"whole": 0.0, "frag": 1.0, "cap": 2.0},
}


def _score_row(row: sqlite3.Row, q_tokens: list[str], q_raw: str) -> tuple[float, list[str]]:
    """单行文本维度打分 → (分数, 命中维度说明)。

    每个维度**先判整串、再退回碎片**，二者不叠加；碎片分按 `cap` 封顶
    （见 `_SCORE` 注释：不封顶的话长查询的碎片会淹没整串命中）。
    """
    why: list[str] = []
    name = os.path.basename(row["path"]).lower()
    ocr = (row["ocr"] or "").lower()
    try:
        labels = json.loads(row["labels"] or "[]")
    except ValueError:
        labels = []
    label_ids = " ".join(str(l.get("id", "")) for l in labels if isinstance(l, dict))

    q = (q_raw or "").lower()
    whole = len(q) > 1
    score = 0.0

    for field, text, label in (("name", name, "文件名"),
                               ("ocr", ocr, "图中文字"),
                               ("label", label_ids, "标签")):
        cfg = _SCORE[field]
        if whole and cfg["whole"] and q in text:
            score += cfg["whole"]
            why.append(f"{label}整串")
            continue
        frag = 0.0
        for t in q_tokens:
            if not t or t not in text:
                continue
            frag += cfg["frag"]
            if field == "ocr":
                # 出现次数轻微加权：一页里反复出现的词更可能是主题
                frag += min(1.0, text.count(t) * 0.25)
            why.append(f"{label}:{t}")
        if frag:
            score += min(frag, cfg["cap"])
    return score, why


def search_local(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 30,
    roots: Iterable[str | Path] | None = None,
    similar_to: str | None = None,
) -> list[dict[str, Any]]:
    """按文本查本地图片；给 similar_to（图片路径）时按指纹找相似图。

    `similar_to` 走的是「以图找相似」——原反搜图通道（TinEye）上游已 403，
    本地相似检索是这条需求目前唯一可用的实现：不告诉你「这张图从哪来」，
    但能回答「我这套素材里有没有和它像的」。
    """
    if similar_to:
        return _search_by_fingerprint(conn, similar_to, limit=limit)

    q = (query or "").strip()
    if not q:
        return []
    q_tokens = _tokens(q)
    if not q_tokens:
        return []

    # SQL 侧先粗筛：任一 token 出现在 path/ocr/labels 里。避免把 7 万行
    # 全捞进 Python 打分——全表载入 + 逐行解析标签的成本远高于一次 LIKE。
    #
    # 显式列名而非 SELECT *：fp 是指纹 BLOB（768×float32 = 3072 B/行），
    # 而文本检索的消费方（_score_row 与下方 item 组装）只读 path/ocr/labels/
    # width/height/size/mtime。一个 token 命中 1 万行时，SELECT * 会连带
    # 多读约 30 MB 永不使用的 BLOB（2026-09-27 实测口径）。
    where = " OR ".join(
        ["path LIKE ? OR ocr LIKE ? OR labels LIKE ?"] * len(q_tokens))
    params: list[str] = []
    for t in q_tokens:
        like = f"%{t}%"
        params += [like, like, like]
    sql = (
        "SELECT path, ocr, labels, width, height, size, mtime FROM images "
        f"WHERE {where}"
    )
    rows = list(conn.execute(sql, params))

    scored: list[tuple[float, list[str], sqlite3.Row]] = []
    for r in rows:
        s, why = _score_row(r, q_tokens, q)
        if s > 0:
            scored.append((s, why, r))
    scored.sort(key=lambda x: (-x[0], x[2]["path"]))

    out: list[dict[str, Any]] = []
    for s, why, r in scored[:max(1, int(limit))]:
        item: dict[str, Any] = {
            "path": r["path"],
            "name": os.path.basename(r["path"]),
            "score": round(s, 3),
            "match": why[:6],
            "width": r["width"],
            "height": r["height"],
            "size": r["size"],
            "mtime": r["mtime"],
        }
        if r["ocr"]:
            item["ocr_excerpt"] = r["ocr"][:200]
        try:
            item["labels"] = [l.get("id") for l in json.loads(r["labels"] or "[]")
                              if isinstance(l, dict)][:8]
        except ValueError:
            pass
        out.append(item)
    return out


def _search_by_fingerprint(conn: sqlite3.Connection, image_path: str,
                           *, limit: int = 30) -> list[dict[str, Any]]:
    """以图找相似：mmap 指纹矩阵，一次矩阵点积出全部相似度。

    旧实现把每行指纹从 BLOB 取出来，逐行 `frombuffer` + 两次 `np.linalg.norm`
    + 点积——7.7 万张约 180ms。现在矩阵已连续 mmap、行内向量写入时即归一，
    查询向量归一后一次矩阵点积走 BLAS，7.7 万张约 3.7ms（快约 50 倍）。
    """
    import numpy as np
    mat = _load_fp_matrix(_db_file(conn))
    if mat is None or mat.shape[0] == 0:
        return []
    dim = int(mat.shape[1])

    target = str(Path(image_path).expanduser())
    row = conn.execute("SELECT fp_slot FROM images WHERE path = ?",
                       (target,)).fetchone()
    qv = None
    if row is not None and row["fp_slot"] is not None \
            and 0 <= row["fp_slot"] < mat.shape[0]:
        qv = np.array(mat[row["fp_slot"]], dtype="<f4")
    if qv is None:
        recs = _run_probe([Path(target)])
        if not recs or not recs[0].get("ok"):
            return []
        blob = _decode_fp(recs[0].get("fp") or "", int(recs[0].get("fp_dim") or 0))
        if not blob:
            return []
        qv = np.frombuffer(blob, dtype="<f4")
    if qv.shape[0] != dim:
        return []
    norm = float(np.linalg.norm(qv))
    if norm == 0.0:
        return []
    qv = qv / norm

    sims = mat @ qv                       # 一次 BLAS 点积 = 全部行的余弦
    k = min(max(1, int(limit)), int(sims.shape[0]))
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]

    # slot → 路径：只查命中的 k 行，逐条点查（fp_slot 已建索引，<1ms）。
    # 不用 IN (?,?,?) 动态拼占位符——那种写法会被安全扫描判成注入。
    out: list[dict[str, Any]] = []
    for i in idx:
        slot = int(i)
        r = conn.execute("SELECT path, width, height, size, mtime FROM images WHERE fp_slot = ?", (slot,)).fetchone()
        if r is None:
            continue
        out.append({
            "path": r["path"],
            "name": os.path.basename(r["path"]),
            "score": round(float(sims[slot]), 4),
            "width": r["width"], "height": r["height"],
            "size": r["size"], "mtime": r["mtime"],
        })
    return out[:max(1, int(limit))]


# ── 联络表（交给多模态模型判断）───────────────────────────────────────────

def build_contact_sheet(
    items: list[dict[str, Any]],
    out_path: str | Path,
    *,
    cell: int = 240,
    cols: int = 4,
    label: bool = True,
    thumb_cache: str | Path | None = None,
) -> dict[str, Any]:
    """把候选图拼成一张联络表，返回 {path, count, index} 供模型读取。

    为什么拼图：模型看一张拼图能同时判断 12 张图，成本是一次图像调用。
    逐张看 12 次既慢又贵。这让「让模型判断上百张候选」第一次可行。

    index 字段把「拼图上的 #N」映射回文件路径——模型回答用编号，调用方
    换算成路径，不必让模型输出路径（长路径模型容易抄错）。
    """
    from PIL import Image, ImageDraw

    items = [it for it in items if it.get("path")]
    if not items:
        return {"path": None, "count": 0, "index": {}}
    cell = max(80, int(cell))
    cols = max(1, int(cols))
    pad, lab_h = 4, (16 if label else 0)
    rows = (len(items) + cols - 1) // cols
    W = cols * (cell + pad) + pad
    H = rows * (cell + pad + lab_h) + pad
    sheet = Image.new("RGB", (W, H), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)

    index: dict[str, str] = {}
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        src = Path(it["path"])
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((cell, cell))
                x = pad + c * (cell + pad) + (cell - im.width) // 2
                y = pad + r * (cell + pad + lab_h)
                sheet.paste(im, (x, y))
            index[str(i + 1)] = it["path"]
        except Exception:
            continue
        if label:
            draw.text((pad + c * (cell + pad) + 4,
                       pad + r * (cell + pad + lab_h) + cell + 2),
                      f"#{i + 1}", fill=(0, 0, 0))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return {"path": str(out), "count": len(items), "index": index,
            "size": list(sheet.size)}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """索引概况（给用户看「建到哪了/有多少能搜」）。"""
    row = conn.execute(
        "SELECT COUNT(*) n, "
        "SUM(CASE WHEN fp_slot IS NOT NULL THEN 1 ELSE 0 END) with_fp, "
        "SUM(CASE WHEN ocr IS NOT NULL AND ocr != '' THEN 1 ELSE 0 END) with_ocr, "
        "SUM(size) bytes FROM images").fetchone()
    n = row["n"] or 0
    return {
        "indexed": n,
        "with_fingerprint": row["with_fp"] or 0,
        "with_ocr": row["with_ocr"] or 0,
        "total_bytes": row["bytes"] or 0,
    }


# ── CLI ──────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="argo local-image",
        description="本地图片语义检索（Vision 索引：图中文字 + 分类标签 + 特征指纹）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_idx = sub.add_parser("index", help="建库/增量更新索引")
    p_idx.add_argument("roots", nargs="*", default=["~"],
                       help="扫描目录（默认 ~；可给多个）")
    p_idx.add_argument("--db", default=None)
    p_idx.add_argument("--parallel", type=int, default=4,
                       help="并行进程数（默认 4，实测 4 路约 0.12 秒/张）")
    p_idx.add_argument("--full", action="store_true", help="全量重建（忽略增量）")
    p_idx.add_argument("--max", type=int, default=None, help="只索引前 N 张（试跑用）")

    p_q = sub.add_parser("search", help="搜索本地图片")
    p_q.add_argument("query", nargs="?", default="", help="查询词（中英文）")
    p_q.add_argument("--db", default=None)
    p_q.add_argument("-n", "--limit", type=int, default=20)
    p_q.add_argument("--similar-to", default=None,
                     help="给一张图，找本地相似的图（以图找相似）")
    p_q.add_argument("--sheet", default=None,
                     help="把结果拼成联络表存到该路径（交给多模态模型判断）")
    p_q.add_argument("--sheet-cell", type=int, default=240)
    p_q.add_argument("--json", action="store_true", help="JSON 输出")

    p_st = sub.add_parser("stats", help="索引概况")
    p_st.add_argument("--db", default=None)
    p_st.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    db = args.db or DEFAULT_DB

    # 默认关闭：本能力要 macOS+swiftc+numpy 且先建 ~226MB 索引，对多数安装是
    # 纯负担。--help 已在 parse_args 内退出，不会走到这里。
    if not local_image_enabled():
        print(f"本地图片检索默认关闭。启用：export {_ENABLE_ENV}=1"
              f"（或写入 ~/.config/argo/env）", file=sys.stderr)
        return 2

    if args.cmd == "index":
        if not vision_available():
            print("本地图片索引需要 macOS + swiftc（Vision 路线）", file=sys.stderr)
            return 2
        conn = open_db(db)
        roots = [str(Path(r).expanduser()) for r in (args.roots or ["~"])]
        t0 = time.time()

        last = [0.0]

        def _prog(done: int, total: int) -> None:
            now = time.time()
            # 限流显示：索引几万张时每张都打一行会淹没终端
            if now - last[0] < 2.0 and done != total:
                return
            last[0] = now
            pct = 100.0 * done / max(1, total)
            rate = done / max(0.001, now - t0)
            eta = (total - done) / rate / 60 if rate > 0 else 0
            print(f"  索引中 {done}/{total} ({pct:.1f}%) "
                  f"{rate:.1f} 张/秒 剩余约 {eta:.1f} 分钟", file=sys.stderr)

        stat = index_paths(conn, roots, parallel=args.parallel,
                           incremental=not args.full, on_progress=_prog,
                           max_images=args.max)
        el = time.time() - t0
        print(f"扫描 {stat['total']} 张：新增 {stat['new']}、更新 {stat['updated']}、"
              f"未变 {stat['unchanged']}、已删 {stat['gone']}、失败 {stat['failed']}"
              f"  用时 {el:.1f}s")
        print(f"索引文件: {Path(db).expanduser()}")
        return 0

    if args.cmd == "search":
        conn = open_db(db)
        st = stats(conn)
        if not st["indexed"]:
            print("索引为空。先跑：argo local-image index ~/Pictures",
                  file=sys.stderr)
            return 2
        if not args.query and not args.similar_to:
            print("需要查询词，或用 --similar-to 给一张图", file=sys.stderr)
            return 2
        res = search_local(conn, args.query, limit=args.limit,
                           similar_to=args.similar_to)
        sheet_info = None
        if args.sheet and res:
            sheet_info = build_contact_sheet(res, args.sheet, cell=args.sheet_cell)
        if args.json:
            print(json.dumps({"count": len(res), "results": res,
                              "sheet": sheet_info}, ensure_ascii=False))
            return 0
        if not res:
            print("没有匹配的图片")
            return 0
        kind = "相似图" if args.similar_to else "匹配"
        print(f"{len(res)} 张{kind}：\n")
        for i, r in enumerate(res, 1):
            dim = f"{r['width']}x{r['height']}" if r.get("width") else "?"
            mt = time.strftime("%Y-%m-%d", time.localtime(r["mtime"])) if r.get("mtime") else "?"
            print(f"{i:3d}. [{r['score']:>6.2f}] {dim:>10}  {mt}  {r['path']}")
            if r.get("match"):
                print(f"     命中: {', '.join(r['match'])}")
        if sheet_info and sheet_info.get("path"):
            print(f"\n联络表: {sheet_info['path']}（{sheet_info['count']} 张，"
                  f"编号 #1~#{sheet_info['count']} 对应上面顺序）")
        return 0

    if args.cmd == "stats":
        conn = open_db(db)
        st = stats(conn)
        if args.json:
            print(json.dumps(st, ensure_ascii=False))
            return 0
        print(f"已索引 {st['indexed']} 张")
        print(f"  有特征指纹 {st['with_fingerprint']}（以图搜图可用）")
        print(f"  有图中文字 {st['with_ocr']}（文字检索可用）")
        print(f"  原图合计 {_fmt_bytes(st['total_bytes'])}")
        print(f"  索引文件 {Path(db).expanduser()}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
