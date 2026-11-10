#!/usr/bin/env python3
"""seek_fts — seek 内容搜索的 FTS5 持久倒排索引（opt-in，ARGO_LOCAL_FTS=1）。

定位与边界
----------
rg 每次全树扫描，大范围（万级文件）内容搜索要 0.9-1.3s；本模块把「文件清单 +
正文」落进 SQLite FTS5，查询走毫秒级 MATCH，只在索引过期（TTL）时付一次增量
刷新。**rg 永远保留为降级主路**：索引不可用/未建成/查询带正则语义/零命中时
全部回退 rg，本模块只做加速层，不改变 seek 的能力面。

运行现状（2026-10-11 终态拍板 = C）：首次全量索引（GPT 知识库 + ~/.zcode）
建到 963MB 后已删除；ARGO_LOCAL_FTS 未设且库不存在，查询恒走 rg（0.6-0.7s/次）。
要重启加速层按终态 B 重建：只索引 GPT 知识库（~/.zcode 整体排除）+ 正文上限
收紧，预计 300-400MB；重建约 16-35s/根，一遍即 complete=1。

与 rg 的结果契约（测试在 tests/test_seek_fts.py）
  1. 行粒度一致：输出同为 (路径, 行号, 截断片段)，行号是文件真实行号。
  2. 大小写一致：rg 恒定 -i，行抽取同样大小写不敏感子串匹配。
  3. 覆盖面一致：文件清单用 `rg --files` 生成（同样尊重 .gitignore 与
     hidden/follow 旗标）；二进制（8KB 探测含 NUL）与 rg 搜索期行为一致地跳过。
  4. 建成门槛：索引「完整建成」前一律回退 rg（宁可慢不可漏）；建成后 TTL 内
     新建/修改的文件是已知的新鲜度缺口，零命中时自动回 rg 兜底。
  5. 已知偏差（opt-in 换速的明码标价，SKILL.md 同步）：超过 _MAX_FILE_BYTES
     的文件整体不入索引、索引正文截到 _MAX_BODY_BYTES——更深行与更大文件
     只有 rg 能找到；文件集合按 bm25 相关度取前
     _MAX_FILES_PER_QUERY 个（rg 是全集合）——「相关性优先于遍历序」的有意
     偏差，最终次序仍由 seek 的重排层定。

CJK 分词（关键坑，模式沿用 wechat-digger/scripts/cjk.py 的实战件）
  unicode61 把连续 CJK 串当**一个** token，中文子串查询永远 0 命中；SQLite
  trigram 对 2 字中文词同样无效。解法：写侧 split_cjk 逐字插空格，查侧
  build_match_query 把查询改写成逐字短语——两侧不对称就静默漏召回，改动
  必须两侧同改。纯 ASCII 语料走免正则快路（首建性能的关键）。

并发：CLI 与 MCP 进程可能同时读写——WAL + busy_timeout，写失败按索引不可用
处理（回退 rg），绝不放大成搜索故障。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── 配置常量 ──────────────────────────────────────────────────────────────────

_DB_NAME = "seek-fts.db"
_TTL_S = 120.0            # 索引新鲜度窗口；过期才做增量刷新
_REFRESH_BUDGET_S = 1.5   # 查询路径上单次刷新的墙钟上界（清单 + 改动文件重读）
_REBUILD_BUDGET_S = 30.0  # 显式 rebuild 的墙钟上界（大树没建完就再跑一次续做）
_MAX_BODY_BYTES = 128 * 1024    # 单文件索引正文上限 128KB（瘦身治理：99.5% 源代码与文档均覆盖）
_MAX_FILE_BYTES = 256 * 1024    # 超过 256KB 坚决不入索引（防大 json、打包代码挤占）
_MAX_FILES_PER_QUERY = 160      # MATCH 按相关度取前 N 个文件再抽行
_MAX_LINES_PER_FILE = 200       # 单文件最多贡献的行（防单文件挤占）
_MAX_LINES_TOTAL = 4000         # 候选行总量上限（喂给池选择前的有界池）
_BUSY_TIMEOUT_MS = 8000

# 白名单扩展名：仅对源代码、规范与关键文档建索引（奥卡姆剃刀：从源头杜绝大体积数据/日志入库）
_INDEXABLE_EXTS = frozenset({
    ".py", ".ts", ".js", ".jsx", ".tsx", ".rs", ".go", ".c", ".cpp", ".h", ".hpp",
    ".java", ".kt", ".swift", ".md", ".markdown", ".mdx", ".yaml", ".yml", ".toml",
    ".sh", ".bash", ".zsh", ".sql", ".html", ".css", ".json", ".txt", ".rst"
})
_NON_INDEXABLE_EXTS = frozenset({
    ".jsonl", ".csv", ".tsv", ".npy", ".npz", ".pkl", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".tar", ".gz", ".db", ".sqlite",
    ".sqlite3", ".min.js", ".min.css", ".map", ".bundle.js"
})

# 扩展排除目录：过滤机器输出、会话转写与缓存临时目录
#
# 不含裸 `data`：那是极常见目录名（argo 自己的 data/ 里是 countries.json、
# regions_cn.json 这类知识文件），按目录名一刀切会误伤大量项目的知识目录。
# 真要排除机器数据目录，靠扩展名白名单（_JSONL/_CSV 等已排除）与体积门禁。
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".trash", "dist",
    "build", "target", ".gradle", ".idea", ".vscode", ".mimosa",
    "rollout", "logs", "log", "backups", "tmp", "temp", ".cache",
    "transcript", "brain", "datasets", "site-packages",
})

_TEXT_SNIFF_BYTES = 8192

# ── CJK 对称分词（沿用 wechat-digger/scripts/cjk.py，四函数成对使用契约）──────

_CJK_CLASS = (
    "\u3040-\u30ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\uac00-\ud7af"
    "\U00020000-\U0002fa1f"
)
_CJK_RUN = re.compile(f"[{_CJK_CLASS}]+")
_TOKEN = re.compile(f"[{_CJK_CLASS}]+|[A-Za-z0-9_]+")
_SPLIT = re.compile(
    f"(?<=[{_CJK_CLASS}])(?=[{_CJK_CLASS}])"
    f"|(?<=[A-Za-z0-9])(?=[{_CJK_CLASS}])"
    f"|(?<=[{_CJK_CLASS}])(?=[A-Za-z0-9])"
    f"|(?<=[{_CJK_CLASS}A-Za-z0-9])(?=[^\\s{_CJK_CLASS}A-Za-z0-9])"
    f"|(?<=[^\\s{_CJK_CLASS}A-Za-z0-9])(?=[{_CJK_CLASS}A-Za-z0-9])"
)


def _index_body(raw: bytes) -> str:
    """字节 → 索引正文。ASCII 快路免正则（代码语料的大头）；CJK 才逐字切分。"""
    if raw.isascii():
        return raw.decode("ascii", "replace")
    return _SPLIT.sub(" ", raw.decode("utf-8", "replace"))


def _build_match_query(keyword: str, max_terms: int = 12):
    """查侧改写：CJK 逐字短语 + ASCII 前缀星；无 token 可用时返回 None。

    丢弃标点与 FTS5 操作符（AND/NEAR/引号/冒号），同时关掉查询注入面。
    与写侧 _index_body 的切分口径不对称会静默漏召回——两侧必须同改。
    """
    if not keyword or not keyword.strip():
        return None
    parts = _TOKEN.findall(keyword)
    if not parts or len(parts) > max_terms:
        return None
    out = []
    for part in parts:
        if _CJK_RUN.fullmatch(part):
            out.append('"' + " ".join(part) + '"')
        else:
            out.append(f'"{part}"*')
    return " ".join(out)


# ── 路径与开关 ────────────────────────────────────────────────────────────────

def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """总开关：ARGO_LOCAL_FTS=1 才启用（对齐 ARGO_LOCAL_IMAGE 的 opt-in 先例）。"""
    return _truthy(os.environ.get("ARGO_LOCAL_FTS", ""))


def index_db_path() -> Path:
    """索引库路径：ARGO_STATE_DIR 优先（与 argo 状态同域），否则历史默认目录。"""
    root = os.environ.get("ARGO_STATE_DIR", "").strip() or "~/.cache/unified-search"
    return Path(os.path.expanduser(root)) / _DB_NAME


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(index_db_path()), timeout=_BUSY_TIMEOUT_MS / 1000)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    _ensure_schema(con)
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        " path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL,"
        " root TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS files_root ON files(root)")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5("
        " path UNINDEXED, body, tokenize='unicode61')")
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta ("
        " root TEXT PRIMARY KEY, updated_ts REAL, complete INTEGER, files INTEGER)")


# ── 索引构建 / 增量刷新 ───────────────────────────────────────────────────────

def _list_files(root: str, dots: bool) -> list[str] | None:
    """`rg --files` 生成文件清单（与 rg 搜索覆盖面 parity 的关键）。

    返回 None 表示清单不可得（rg 缺失/超时）——调用方按索引不可用降级。
    """
    cmd = ["rg", "--files"]
    if dots:
        # seek 同款旗标；--hidden 会把 .git 等清单进来，用忽略表兜住
        cmd += ["--hidden", "--follow"]
    for d in sorted(_SKIP_DIRS):
        cmd += ["-g", f"!{d}"]
    cmd.append(root)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_REFRESH_BUDGET_S * 4)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # rc=2 = 个别条目遍历时报错（实测：tmp 竞态消失的软链）。已输出的清单
    # 照常可用，增量 diff 会在后续刷新里自然修正消失/新增；仅当完全无输出
    # 才按清单不可得降级。
    if proc.returncode not in (0, 1, 2):
        return None
    lines = [ln for ln in proc.stdout.splitlines() if ln]
    return lines if lines else None


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return True  # 读不了 = 当二进制跳过（软链断、权限洞都不放大成故障）


def refresh(root: str, dots: bool = False, budget_s: float | None = None,
            force: bool = False) -> dict:
    """增量刷新一个根的索引：清单 diff → 重读改动文件 → 删除消失文件。

    预算到点即停（本轮覆盖到的已入库）；清单是全量的，没读到的文件下次
    查询/重建续做。返回 {root, indexed, changed, deleted, complete, elapsed_ms}，
    complete=1 表示本轮把清单走完（首建完成的标志）。
    """
    t0 = time.monotonic()
    budget = budget_s if budget_s is not None else _REFRESH_BUDGET_S
    root_abs = os.path.abspath(os.path.expanduser(root))
    out = {"root": root_abs, "indexed": 0, "changed": 0, "deleted": 0,
           "complete": 0, "elapsed_ms": 0}
    listing = _list_files(root_abs, dots)
    if listing is None:
        return out
    con = _connect()
    try:
        old = dict(con.execute("SELECT path, mtime FROM files WHERE root = ?",
                               (root_abs,)).fetchall())
        want = set(listing)
        deadline = time.monotonic() + budget
        rows = []
        for p in listing:
            try:
                st = os.stat(p)  # stat 而非 lstat：--follow 语义下链目标为准
            except OSError:
                continue
            mtime, size = st.st_mtime, st.st_size
            if not force and p in old and abs(old[p] - mtime) < 1e-6:
                continue
            if size == 0 or size > _MAX_FILE_BYTES:
                continue
            ext = os.path.splitext(p)[1].lower()
            # 白名单优先：不在白名单内的一律不入索引（无扩展名的文件如 Dockerfile、
            # Makefile 也不入——它们的内容价值低且已被 rg 主路覆盖）。
            if ext not in _INDEXABLE_EXTS:
                continue
            if ext in _NON_INDEXABLE_EXTS:
                continue
            path_obj = Path(p)
            if _is_binary(path_obj):
                continue
            try:
                with open(path_obj, "rb") as f:
                    raw = f.read(_MAX_BODY_BYTES)
            except OSError:
                continue
            rows.append((p, mtime, size, raw))
            out["changed"] += 1
            if time.monotonic() >= deadline:
                break
        complete = time.monotonic() < deadline
        if rows:
            con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                            [(p, m, s, root_abs) for p, m, s, _ in rows])
            for p, _m, _s, raw in rows:
                if p in old:  # 只在确实有旧行时才删——首建时逐条 DELETE 是 O(n²)
                    con.execute("DELETE FROM content WHERE path = ?", (p,))
                con.execute("INSERT INTO content(path, body) VALUES (?, ?)",
                            (p, _index_body(raw)))
            out["indexed"] = len(rows)
        gone = [p for p in old if p not in want]
        if gone:
            con.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
            con.executemany("DELETE FROM content WHERE path = ?", [(p,) for p in gone])
            out["deleted"] = len(gone)
        total = con.execute("SELECT COUNT(*) FROM files WHERE root = ?",
                            (root_abs,)).fetchone()[0]
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?,?)",
                    (root_abs, time.time(), 1 if complete else 0, total))
        con.commit()
        out["complete"] = 1 if complete else 0
    except sqlite3.Error:
        out["complete"] = 0  # 写失败按索引不可用降级，不放大成搜索故障
    finally:
        con.close()
    out["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return out


# ── 查询 ─────────────────────────────────────────────────────────────────────

_REGEX_META = set("\\^$.[]|()*+?{}")


def query_suitable(query: str) -> bool:
    """FTS 快路径的查询门：无正则元字符、有可检索 token。不满足则走 rg。"""
    if not enabled() or not query or any(c in _REGEX_META for c in query):
        return False
    return _build_match_query(query) is not None


def content_candidates(query: str, root: str, dots: bool = False,
                       since_ts: float | None = None,
                       until_ts: float | None = None) -> list | None:
    """FTS 候选行：(路径, 行号, 片段) 列表；None = 索引不可用，回退 rg。

    行抽取是大小写不敏感的整查询子串匹配（对齐 rg 恒定 -i 的语义）；路径
    前缀过滤让子目录查询直接复用父根的索引。
    """
    root_abs = os.path.abspath(os.path.expanduser(root))
    match = _build_match_query(query)
    if match is None or not enabled():
        return None
    try:
        con = _connect()
    except sqlite3.Error:
        return None
    try:
        # 覆盖查询根的已建索引优先（子目录查询复用父根索引）；找不到才把
        # 查询根当新根刷新——否则同一个文件会在父子两个 root 下各入一次
        # content 表，JOIN 后重复出现（pytest 实测踩中）。
        row = _covering_meta(con, root_abs)
        if row is None or (time.time() - row[1]) >= _TTL_S:
            refresh(root_abs, dots=dots)
            row = _covering_meta(con, root_abs)
        if row is None or not row[2]:
            return None  # 建不起来或未建成 → rg 主路（宁可慢不可漏）
        where = ["f.path LIKE ? ESCAPE '\\'"]
        params: list = [_like_prefix(root_abs)]
        if since_ts is not None:
            where.append("f.mtime >= ?")
            params.append(since_ts)
        if until_ts is not None:
            where.append("f.mtime <= ?")
            params.append(until_ts)
        sql = (
            "SELECT f.path FROM content JOIN files f ON f.path = content.path "
            "WHERE content MATCH ? AND " + " AND ".join(where) +
            " ORDER BY bm25(content) LIMIT ?")
        hits = [r[0] for r in
                con.execute(sql, [match] + params + [_MAX_FILES_PER_QUERY])]
        if not hits:
            return []  # 索引完整且新鲜 → 这是真没有
        q_low = query.lower()
        out: list = []
        total = 0
        for path in hits:
            per_file = 0
            try:
                with open(path, "rb") as f:
                    # 与索引正文同界：更深行只有 rg 能找到（契约第 5 条）
                    data = f.read(_MAX_BODY_BYTES)
            except OSError:
                continue
            for ln, line in enumerate(
                    data.decode("utf-8", "replace").splitlines(), 1):
                if q_low in line.lower():
                    t = line.strip()  # 截断口径与 seek.truncate 逐位一致
                    out.append((path, ln,
                                t if len(t) <= 120 else t[:119] + "…"))
                    per_file += 1
                    total += 1
                    if (per_file >= _MAX_LINES_PER_FILE
                            or total >= _MAX_LINES_TOTAL):
                        break
            if total >= _MAX_LINES_TOTAL:
                break
        return out
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _covering_meta(con: sqlite3.Connection, root_abs: str):
    """找已覆盖查询根的索引 meta：root 等于查询根，或查询根在其子树下。

    返回 (root, updated_ts, complete) 或 None；多个命中取最短 root（覆盖面
    最大、最不易漏）。索引根数量级是个位数，线性扫足够。
    """
    best = None
    for r, ts, comp in con.execute(
            "SELECT root, updated_ts, complete FROM meta").fetchall():
        if root_abs == r or root_abs.startswith(r.rstrip(os.sep) + os.sep):
            if best is None or len(r) < len(best[0]):
                best = (r, ts, comp)
    return best


def _like_prefix(root_abs: str) -> str:
    """路径前缀的 LIKE 模式；把 LIKE 通配与转义符按 SQLite 语义转义。

    目录根必须是**目录前缀**（尾随 /），否则 `/a/b` 会误配兄弟树
    `/a/bc/x.py`（2026-10-12 修复）；根路径 `/` 特判为 `/%`——旧实现产出
    `//%`，永不匹配任何绝对路径，表现为「索引完整 → 真没有」的静默假空。
    目标是单个文件时按精确路径匹配（rg 接受文件做搜索根，目录语义会落空）。
    """
    esc = root_abs.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    try:
        is_dir = os.path.isdir(root_abs)
    except OSError:
        is_dir = True  # 判不了按目录处理：宁多不少，零命中仍可回退 rg
    if not is_dir:
        return esc
    if esc.endswith(os.sep):
        return esc + "%"  # 文件系统根（或调用方传入的尾随分隔符）：直接挂 %
    return esc + os.sep + "%"


# ── 独立 CLI：预热 / 状态（大树先跑 rebuild 预热，查询路径才不吃首建成本）────

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="seek FTS 索引管理")
    ap.add_argument("action", choices=["rebuild", "status"])
    ap.add_argument("path", nargs="?", default=".", help="索引根目录")
    ap.add_argument("--dots", action="store_true", help="含隐藏目录与软链")
    args = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(args.path))
    if args.action == "rebuild":
        info = refresh(root, dots=args.dots, budget_s=_REBUILD_BUDGET_S, force=True)
        print(json.dumps(info, ensure_ascii=False))
        return 0 if info["complete"] else 1
    try:
        con = _connect()
    except sqlite3.Error as e:
        print(json.dumps({"root": root, "error": str(e)[:120]}, ensure_ascii=False))
        return 1
    row = con.execute("SELECT updated_ts, complete FROM meta WHERE root = ?",
                      (root,)).fetchone()
    n = con.execute("SELECT COUNT(*) FROM files WHERE root = ?",
                    (root,)).fetchone()[0]
    if row is None:
        print(json.dumps({"root": root, "indexed": n, "status": "empty"},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"root": root, "indexed": n,
                      "age_s": round(time.time() - row[0], 1),
                      "complete": row[1]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
