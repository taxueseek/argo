#!/usr/bin/env python3
"""local_image_fts.py — local_image 的 FTS5 中文索引、内容哈希去重与批量同步辅助。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


def fts_split_cjk(text: str) -> str:
    """将连续 CJK 字符逐字拆开，适配 SQLite FTS5 unicode61 分词。"""
    if not text:
        return ""
    if text.isascii():
        return text.lower()
    return re.sub(r"([\u4e00-\u9fff])", r" \1 ", text).lower()


def fts_body_for_row(path: str, labels_json: str, ocr: str) -> str:
    try:
        labels = json.loads(labels_json or "[]")
    except ValueError:
        labels = []
    label_ids = " ".join(
        str(l.get("id", "")) for l in labels if isinstance(l, dict)
    )
    return fts_split_cjk(f"{os.path.basename(path)} {path} {label_ids} {ocr or ''}")


def ensure_migrations(conn: sqlite3.Connection) -> None:
    """为旧库补齐 content_hash 列。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
    if cols and "content_hash" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN content_hash TEXT")
        conn.commit()


def sync_fts_if_empty(conn: sqlite3.Connection) -> None:
    """旧库升级后 images 有数据但 images_fts 为空时，自动一次性回填。"""
    img_cnt = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    if not img_cnt:
        return
    fts_cnt = conn.execute("SELECT COUNT(*) FROM images_fts").fetchone()[0]
    if fts_cnt >= img_cnt:
        return
    conn.execute("DELETE FROM images_fts")
    batch_rows: list[tuple[str, str]] = []
    for r in conn.execute("SELECT path, labels, ocr FROM images"):
        batch_rows.append((r["path"], fts_body_for_row(r["path"], r["labels"], r["ocr"])))
        if len(batch_rows) >= 500:
            conn.executemany("INSERT INTO images_fts(path, body) VALUES (?,?)", batch_rows)
            batch_rows.clear()
    if batch_rows:
        conn.executemany("INSERT INTO images_fts(path, body) VALUES (?,?)", batch_rows)
    conn.commit()


def build_reused_rows(
    items: list[tuple[Path, os.stat_result, str]],
    hash_cache: dict[str, dict[str, Any]],
    fresh: dict[str, Any],
) -> tuple[list[tuple[Any, ...]], list[tuple[str, str]], int]:
    """将批内命中相同 content_hash 的图片组装为复用写入行，返回 (rows, fts_rows, failed_cnt)。"""
    import time
    rows: list[tuple[Any, ...]] = []
    fts_rows: list[tuple[str, str]] = []
    failed = 0
    for p, st, ch in items:
        sp = str(p)
        cached = hash_cache.get(ch)
        if not cached:
            failed += 1
            continue
        if cached.get("fp_vec") is not None:
            fresh[sp] = cached["fp_vec"]
        rows.append((
            sp, st.st_mtime, st.st_size, cached["width"], cached["height"],
            st.st_ino, cached["labels"], cached["ocr"], None,
            cached["fp_dim"], time.time(), ch,
        ))
        fts_rows.append((sp, fts_body_for_row(sp, cached["labels"], cached["ocr"])))
    return rows, fts_rows, failed


def upsert_images_with_fts(
    conn: sqlite3.Connection,
    rows: list[tuple[Any, ...]],
    fts_rows: list[tuple[str, str]],
) -> None:
    """批量写入 images 并同步更新 images_fts。"""
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO images "
        "(path,mtime,size,width,height,inode,labels,ocr,fp_slot,fp_dim,indexed_at,content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany("DELETE FROM images_fts WHERE path = ?", [(r[0],) for r in rows])
    conn.executemany("INSERT INTO images_fts(path, body) VALUES (?,?)", fts_rows)
    conn.commit()


def query_candidates(conn: sqlite3.Connection, q_tokens: list[str]) -> list[sqlite3.Row]:
    """优先走 FTS5 倒排索引粗筛（毫秒级），零命中或异常时回退 LIKE 兜底。"""
    fts_terms = [
        '"' + fts_split_cjk(t).strip().replace('"', '""') + '"'
        for t in q_tokens if fts_split_cjk(t).strip()
    ]
    if fts_terms:
        match_expr = " OR ".join(fts_terms)
        try:
            rows = list(conn.execute(
                "SELECT i.path, i.ocr, i.labels, i.width, i.height, i.size, i.mtime "
                "FROM images_fts f JOIN images i ON i.path = f.path "
                "WHERE images_fts MATCH ? LIMIT 2000",
                (match_expr,),
            ))
            if rows:
                return rows
        except sqlite3.OperationalError:
            pass

    where = " OR ".join(
        ["path LIKE ? OR ocr LIKE ? OR labels LIKE ?"] * len(q_tokens))
    params: list[str] = []
    for t in q_tokens:
        like = f"%{t}%"
        params += [like, like, like]
    return list(conn.execute(
        "SELECT path, ocr, labels, width, height, size, mtime FROM images "
        f"WHERE {where}",
        params,
    ))


def quick_content_hash(path: Path, size: int) -> str:
    """计算文件快速内容指纹（size + 头 64KB + 尾 64KB），毫秒级去重。"""
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    chunk = 65536
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(size - chunk)
            h.update(f.read(chunk))
    return h.hexdigest()[:24]


def build_contact_sheet(
    items: list[dict[str, Any]],
    out_path: str | Path,
    *,
    cell: int = 240,
    cols: int = 4,
    label: bool = True,
    thumb_cache: str | Path | None = None,
) -> dict[str, Any]:
    """把候选图拼成一张联络表，返回 {path, count, index} 供模型读取。"""
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


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


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
