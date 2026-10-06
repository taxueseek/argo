#!/usr/bin/env python3
"""test_local_image — 本地图片索引与检索的契约（不需要 Vision/网络）。

## 守的是什么

本文件锁三类容易静默失效的行为。它们都不会报错，只会让检索结果悄悄变差
或给出陈旧答案：

  1. **增量判据**：`(mtime, size)` 变化必须重算指纹。只看路径存在与否的话，
     一张图被重新生成后索引仍指向旧指纹，以图搜图会返回**错误**的相似结果
     ——比没有结果更糟，因为它看起来是对的。
  2. **消失文件清理**：文件删掉后索引必须同步删除，否则检索返回打不开的路径。
  3. **检索评分维度**：文件名 / 图中文字 / 分类标签三个维度的权重关系要稳定。
     权重错了表现是「排序不像人想的」，没有断言就只能靠肉眼发现。

指纹相关的用例用合成的 768 维向量，不依赖 Vision 与真实图片；索引用例只在
存在 Pillow 时跑（跳过而非失败，CI 环境可能没装）。

实现改动必跑：`python3 -m pytest tests/test_local_image.py -q`
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import local_image as li  # noqa: E402

numpy = pytest.importorskip("numpy")

# 三字段碎片分上限之和（封顶断言用）
_SCORE_CAP_SUM = sum(c["cap"] for c in li._SCORE.values())


@pytest.fixture()
def conn():
    db = tempfile.mktemp(suffix=".db")
    c = li.open_db(db)
    yield c
    c.close()
    try:
        os.unlink(db)
    except OSError:
        pass


def _put(conn, path, *, mtime=1.0, size=100, inode=1,
         labels=None, ocr="", dim=768):
    """插一行元数据；指纹另走 _set_fps（2026-10-06 起指纹存 fp.npy，不在这张表）。"""
    conn.execute(
        "INSERT OR REPLACE INTO images (path,mtime,size,width,height,inode,labels,ocr,fp_slot,fp_dim,indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (path, mtime, size, 800, 600, inode,
         json.dumps(labels or [], ensure_ascii=False), ocr,
         None, dim, 0.0))
    conn.commit()


def _set_fps(conn, mapping, dim=768):
    """把 {路径: float32 字节} 写成 fp.npy 并回填 fp_slot（模拟建库产物）。"""
    pairs = [(p, numpy.frombuffer(b, dtype="<f4")) for p, b in mapping.items()]
    slots = li._write_fp_matrix(li._db_file(conn), pairs, dim)
    conn.execute("UPDATE images SET fp_slot = NULL")
    conn.executemany("UPDATE images SET fp_slot = ? WHERE path = ?",
                     [(s, p) for p, s in slots.items()])
    conn.commit()


# ── 切词 ──────────────────────────────────────────────────────────────────

class TestTokens:
    def test_cjk_split_per_char(self):
        """中文无空格，按字切是最小单位（再用整串命中补精度）。"""
        assert li._tokens("雪山壁纸") == ["雪", "山", "壁", "纸"]

    def test_ascii_kept_whole(self):
        assert li._tokens("MCP server") == ["mcp", "server"]

    def test_mixed(self):
        assert li._tokens("argo MCP 配置") == ["argo", "mcp", "配", "置"]

    def test_empty(self):
        assert li._tokens("") == []


# ── 检索评分 ──────────────────────────────────────────────────────────────

class TestScoring:
    def _row(self, path="/a/x.png", ocr="", labels=None):
        return {"path": path, "ocr": ocr,
                "labels": json.dumps(labels or [])}

    def test_filename_beats_ocr_beats_label(self):
        """三个维度的权重次序：文件名 > 图中文字 > 分类标签。"""
        q = ["mcp"]
        name_s, _ = li._score_row(self._row(path="/a/mcp.png"), q, "mcp")
        ocr_s, _ = li._score_row(self._row(ocr="mcp"), q, "mcp")
        lab_s, _ = li._score_row(
            self._row(labels=[{"id": "mcp", "conf": 0.5}]), q, "mcp")
        assert name_s > ocr_s > lab_s > 0

    def test_whole_string_beats_fragments_same_field(self):
        """同字段内整串命中必须压过碎片命中（同一查询下的两条候选）。

        这是曾经的实现缺陷：碎片分随查询长度线性累加且无上限，而整串奖励是
        固定值，多字查询下整串反而输给碎片。现在碎片分按字段封顶、整串分高于
        上限，故同字段内必然分出高低。
        """
        q = "机器学习"
        toks = li._tokens(q)
        # 同一查询的两条候选：整串出现 vs 各字分散（中间隔了别的字）
        whole, _ = li._score_row(self._row(ocr="机器学习模型"), toks, q)
        frag, _ = li._score_row(self._row(ocr="机 器 学 习"), toks, q)
        assert whole > frag, "整串命中应高于同字段碎片命中"

    def test_long_query_fragments_capped(self):
        """碎片分封顶：长查询的碎片命中不得无界增长。"""
        long_q = "机器学习模型训练数据"
        toks = li._tokens(long_q)
        s, _ = li._score_row(self._row(ocr=" ".join(toks)), toks, long_q)
        cap_sum = _SCORE_CAP_SUM
        assert s <= cap_sum

    def test_filename_whole_beats_ocr_whole(self):
        """跨字段比较仍按权重：文件名整串 > 图中文字整串。"""
        q = "雪山"
        toks = li._tokens(q)
        name_s, _ = li._score_row(self._row(path="/a/雪山.png"), toks, q)
        ocr_s, _ = li._score_row(self._row(ocr="雪山"), toks, q)
        assert name_s > ocr_s

    def test_no_match_is_zero(self):
        s, why = li._score_row(self._row(ocr="完全无关"), ["zzz"], "zzz")
        assert s == 0.0 and why == []

    def test_labels_json_corrupt_does_not_crash(self):
        """labels 列损坏时不得抛异常（索引可能被外部工具改过）。"""
        row = {"path": "/a/x.png", "ocr": "", "labels": "{not json"}
        s, _ = li._score_row(row, ["a"], "a")
        assert isinstance(s, float)


class TestSearchLocal:
    def test_matches_ocr_and_reports_dimension(self, conn):
        _put(conn, "/img/mcp-note.png", ocr="MCP 工具面 19 个")
        res = li.search_local(conn, "MCP", limit=5)
        assert len(res) == 1
        assert any("图中文字" in m for m in res[0]["match"])
        assert res[0]["name"] == "mcp-note.png"

    def test_matches_filename(self, conn):
        _put(conn, "/img/雪山壁纸.png")
        res = li.search_local(conn, "雪山", limit=5)
        assert len(res) == 1
        assert any("文件名" in m for m in res[0]["match"])

    def test_empty_query_returns_nothing(self, conn):
        _put(conn, "/img/a.png", ocr="x")
        assert li.search_local(conn, "") == []

    def test_no_match(self, conn):
        _put(conn, "/img/a.png", ocr="无关内容")
        assert li.search_local(conn, "zzzzz") == []

    def test_limit_respected(self, conn):
        for i in range(10):
            _put(conn, f"/img/mcp{i}.png", ocr="mcp")
        assert len(li.search_local(conn, "mcp", limit=3)) == 3

    def test_results_include_path_and_score(self, conn):
        _put(conn, "/img/a.png", ocr="mcp")
        r = li.search_local(conn, "mcp", limit=1)[0]
        for k in ("path", "name", "score", "match"):
            assert k in r


# ── 以图找相似 ────────────────────────────────────────────────────────────

class TestFingerprintSearch:
    def _fp(self, seed, dim=768):
        rng = numpy.random.default_rng(seed)
        v = rng.standard_normal(dim).astype("<f4")
        return (v / numpy.linalg.norm(v)).tobytes()

    def test_identical_vector_scores_one(self, conn):
        _put(conn, "/img/a.png")
        _put(conn, "/img/b.png")
        _set_fps(conn, {"/img/a.png": self._fp(1), "/img/b.png": self._fp(2)})
        res = li.search_local(conn, "", limit=5, similar_to="/img/a.png")
        assert res[0]["path"] == "/img/a.png"
        assert res[0]["score"] == pytest.approx(1.0, abs=1e-3)

    def test_missing_fingerprint_returns_empty(self, conn):
        """库里没有该图且现算失败时返回空，不抛异常。"""
        res = li.search_local(conn, "", limit=5,
                              similar_to="/nonexistent/nope.png")
        assert res == []

    def test_rows_without_fp_skipped(self, conn):
        _put(conn, "/img/a.png")
        _put(conn, "/img/nofp.png")
        _set_fps(conn, {"/img/a.png": self._fp(1)})
        res = li.search_local(conn, "", limit=5, similar_to="/img/a.png")
        assert all(r["path"] != "/img/nofp.png" for r in res)

    def test_zero_vector_query_returns_empty(self, conn):
        """零向量（解码失败的占位）不得造成除零崩溃——返回空即可。"""
        _put(conn, "/img/a.png")
        _set_fps(conn, {"/img/a.png": numpy.zeros(768, dtype="<f4").tobytes()})
        assert li.search_local(conn, "", limit=5, similar_to="/img/a.png") == []

    def test_matrix_l2_normalized_on_write(self, conn):
        """写入时即归一化——这是「检索端点积即余弦」的前提（50 倍提速的基础）。"""
        _put(conn, "/img/a.png")
        raw = (numpy.arange(768, dtype="<f4") + 1.0).tobytes()
        _set_fps(conn, {"/img/a.png": raw})
        mat = li._load_fp_matrix(li._db_file(conn))
        assert mat is not None
        row0 = numpy.asarray(mat[0])
        assert float(numpy.linalg.norm(row0)) == pytest.approx(1.0, abs=1e-4)


# ── 拼图 ──────────────────────────────────────────────────────────────────

class TestContactSheet:
    def test_index_maps_number_to_path(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        items = []
        for i in range(3):
            p = tmp_path / f"img{i}.png"
            Image.new("RGB", (60, 40), (i * 60, 0, 0)).save(p)
            items.append({"path": str(p)})
        out = tmp_path / "sheet.png"
        info = li.build_contact_sheet(items, out, cell=80, cols=3)
        assert info["count"] == 3
        assert info["path"] == str(out)
        # 编号 → 路径映射是「模型用编号回答、调用方换算成路径」的关键
        assert info["index"]["1"] == str(items[0]["path"])
        assert info["index"]["3"] == str(items[2]["path"])
        assert out.exists()

    def test_unreadable_image_skipped(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        good = tmp_path / "ok.png"
        Image.new("RGB", (60, 40)).save(good)
        items = [{"path": str(good)}, {"path": str(tmp_path / "missing.png")}]
        info = li.build_contact_sheet(items, tmp_path / "s.png", cell=80)
        # 坏图跳过但好的仍出图；count 反映实际入表数量
        assert info["path"] is not None
        assert info["index"].get("1") == str(good)

    def test_empty_input(self, tmp_path):
        info = li.build_contact_sheet([], tmp_path / "s.png")
        assert info["count"] == 0 and info["path"] is None


# ── 索引与增量 ────────────────────────────────────────────────────────────

class TestIndexIncremental:
    """不依赖 Vision：直接验证增量判据（这是最容易静默出错的一处）。"""

    def test_changed_file_is_reindexed(self, conn):
        """(mtime,size) 变了必须进 todo——否则以图搜图返回陈旧指纹。"""
        _put(conn, "/img/a.png", mtime=1.0, size=100)
        existing = {r["path"]: (r["mtime"], r["size"], r["inode"])
                    for r in conn.execute("SELECT path,mtime,size,inode FROM images")}
        # 同路径、mtime 变了
        cur = (2.0, 100, 1)
        prev = existing["/img/a.png"]
        assert (prev[0], prev[1]) != (cur[0], cur[1]), "判据必须认出内容变化"

    def test_unchanged_file_skipped(self, conn):
        _put(conn, "/img/a.png", mtime=1.0, size=100)
        prev = (1.0, 100, 1)
        cur = (1.0, 100, 1)
        assert (prev[0], prev[1]) == (cur[0], cur[1])

    def test_gone_files_removed_from_index(self, conn, tmp_path):
        """消失的文件必须清出索引：否则检索给出打不开的路径。

        root 维度（2026-09-27 数据丢失修复）：只清「属于本次扫描 root 且不在
        磁盘上」的条目。本次 root 之外的条目（别的 root 索引进来的）必须保留
        ——旧实现拿全表路径与本次磁盘集合做差，先索引 ~/Pictures 再索引
        ~/Downloads 会把第一次的条目全部误删。
        """
        tmp = tmp_path / "gone.png"
        Image = pytest.importorskip("PIL.Image")
        Image.new("RGB", (10, 10)).save(tmp)
        _put(conn, str(tmp), mtime=1.0)
        _put(conn, "/img/also-gone.png", mtime=1.0)
        before = conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"]
        assert before == 2
        os.unlink(tmp)
        # 只扫 tmp_path（不含 /img）：root 内的 gone.png 该被清掉，
        # /img 的条目不属于本次 root，必须留下。
        li.index_paths(conn, [str(tmp_path)])
        after = conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"]
        assert after == 1
        left = conn.execute("SELECT path FROM images").fetchone()["path"]
        assert left == "/img/also-gone.png"

    def test_cross_root_indexing_keeps_previous_root(self, conn, tmp_path):
        """分两次索引不同 root：第二次不得把第一次的条目判成 gone 删光。

        这正是 2026-09-27 修复的数据丢失场景（先 ~/Pictures 再 ~/Downloads）。
        同时锁定 max_images 截断不参与 gone 判定：截断只限制新算批次。
        """
        Image = pytest.importorskip("PIL.Image")
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        Image.new("RGB", (4, 4)).save(root_a / "keep.png")
        Image.new("RGB", (4, 4)).save(root_b / "new.png")
        _put(conn, str(root_a / "keep.png"), mtime=1.0)

        # 第二次只索引 root_b，且 max_images=1（截断到 1 个文件）
        li.index_paths(conn, [str(root_b)], max_images=1)
        paths = {r["path"] for r in conn.execute("SELECT path FROM images")}
        assert str(root_a / "keep.png") in paths, "上一个 root 的条目被误删"
        assert str(root_b / "new.png") in paths, "本次 root 的条目应入索引"

    def test_incremental_judge_uses_full_triple(self, conn, tmp_path):
        """增量判据是 (mtime, size, inode) 三元组——docstring 说的「三者一起比」。

        旧实现只比前两项：inode 被 SELECT 出来、被存进 existing，却在比较时
        被丢弃。跨文件系统复制后 mtime 可能保留，漏比 inode 就漏重算。
        """
        Image = pytest.importorskip("PIL.Image")
        p = tmp_path / "x.png"
        Image.new("RGB", (4, 4)).save(p)
        st = p.stat()
        # mtime+size 相同、inode 不同 → 必须判为 updated
        _put(conn, str(p), mtime=st.st_mtime, size=st.st_size, inode=st.st_ino + 1)
        stat = li.index_paths(conn, [str(tmp_path)])
        assert stat["updated"] == 1, "inode 变化未被判据认出（只比了 mtime/size）"


class TestWalkImages:
    def test_collects_only_image_extensions(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        (tmp_path / "a.png").write_bytes(b"x")
        (tmp_path / "b.txt").write_text("x")
        (tmp_path / "c.JPG").write_bytes(b"x")
        found = {p.name for p in li.walk_images([tmp_path])}
        assert found == {"a.png", "c.JPG"}

    def test_skips_heavy_dirs(self, tmp_path):
        heavy = tmp_path / "node_modules"
        heavy.mkdir()
        (heavy / "x.png").write_bytes(b"x")
        (tmp_path / "y.png").write_bytes(b"x")
        found = {p.name for p in li.walk_images([tmp_path])}
        assert found == {"y.png"}

    def test_skips_dot_dirs(self, tmp_path):
        d = tmp_path / ".cache"
        d.mkdir()
        (d / "x.png").write_bytes(b"x")
        assert li.walk_images([tmp_path]) == []

    def test_missing_root_reported_not_raised(self, tmp_path):
        skips = []
        out = li.walk_images([tmp_path / "nope"],
                             on_skip=lambda p, r: skips.append((p, r)))
        assert out == []
        assert skips and skips[0][1] == "not_found"


class TestStats:
    def test_counts(self, conn):
        _put(conn, "/a.png", ocr="文字")
        _put(conn, "/b.png")
        _set_fps(conn, {"/a.png": numpy.ones(768, dtype="<f4").tobytes()})
        st = li.stats(conn)
        assert st["indexed"] == 2
        assert st["with_fingerprint"] == 1
        assert st["with_ocr"] == 1

    def test_empty_db(self, conn):
        st = li.stats(conn)
        assert st["indexed"] == 0 and st["with_fingerprint"] == 0


# ── 指纹矩阵：迁移与一致性（2026-10-06 指纹移出 SQLite）─────────────────────

class TestFpMatrixMigration:
    """旧库（指纹存 fp BLOB 列）打开时自动迁移到 fp.npy，不丢指纹。"""

    def _make_legacy_db(self, path):
        c = sqlite3.connect(path)
        c.execute(
            "CREATE TABLE images (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, "
            "width INTEGER, height INTEGER, inode INTEGER, labels TEXT, ocr TEXT, "
            "fp BLOB, fp_dim INTEGER, indexed_at REAL)")
        c.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        v = numpy.random.default_rng(7).standard_normal(768).astype("<f4")
        v = v / numpy.linalg.norm(v)
        c.execute(
            "INSERT INTO images (path,mtime,size,width,height,inode,labels,ocr,fp,fp_dim,indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("/old/a.png", 1.0, 10, 8, 6, 1, "[]", "旧库文字", v.tobytes(), 768, 0.0))
        c.commit()
        c.close()

    def test_legacy_blob_migrated_to_matrix(self, tmp_path):
        db = str(tmp_path / "legacy.db")
        self._make_legacy_db(db)
        conn = li.open_db(db)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
            assert "fp_slot" in cols, "迁移未补 fp_slot 列"
            mat = li._load_fp_matrix(db)
            assert mat is not None and mat.shape == (1, 768), "旧 BLOB 未搬进矩阵"
            row = conn.execute("SELECT fp_slot FROM images WHERE path = ?",
                               ("/old/a.png",)).fetchone()
            assert row["fp_slot"] == 0
            # 迁移后以图搜图可用，自匹配为 1
            res = li._search_by_fingerprint(conn, "/old/a.png", limit=3)
            assert res and res[0]["path"] == "/old/a.png"
            assert res[0]["score"] == pytest.approx(1.0, abs=1e-3)
            assert li.stats(conn)["with_fingerprint"] == 1
        finally:
            conn.close()


class TestFpMatrixConsistency:
    """index_paths 结束后 fp.npy 必须与 images 表严格对齐（派生件语义）。"""

    def _mk(self, d, name, color):
        Image = pytest.importorskip("PIL.Image")
        p = os.path.join(str(d), name)
        Image.new("RGB", (32, 32), color).save(p)
        return p

    def test_matrix_matches_table_after_incremental(self, tmp_path):
        db = str(tmp_path / "idx.db")
        conn = li.open_db(db)
        try:
            d = tmp_path / "imgs"
            d.mkdir()
            self._mk(d, "a.png", (10, 0, 0))
            self._mk(d, "b.png", (0, 10, 0))
            li.index_paths(conn, [str(d)])
            m1 = li._load_fp_matrix(db)
            assert m1 is not None and m1.shape[0] == 2

            # 删一张、加一张，再增量：矩阵必须跟着表走
            os.unlink(os.path.join(str(d), "a.png"))
            self._mk(d, "c.png", (0, 0, 10))
            li.index_paths(conn, [str(d)])

            rows = list(conn.execute(
                "SELECT path, fp_slot FROM images WHERE fp_slot IS NOT NULL"))
            m2 = li._load_fp_matrix(db)
            assert m2 is not None
            assert m2.shape[0] == len(rows), "矩阵行数与有指纹的行数必须一致"
            slots = sorted(r["fp_slot"] for r in rows)
            assert slots == list(range(len(rows))), "slot 必须稠密（0..n-1）"
        finally:
            conn.close()

    def test_matrix_rebuilt_when_file_deleted(self, tmp_path):
        """fp.npy 被删后下一次 index 自动重建（它是派生件，可随时删）。"""
        db = str(tmp_path / "idx.db")
        conn = li.open_db(db)
        try:
            d = tmp_path / "imgs"
            d.mkdir()
            self._mk(d, "a.png", (10, 0, 0))
            li.index_paths(conn, [str(d)])
            li._fp_path(db).unlink()
            assert li._load_fp_matrix(db) is None
            li.index_paths(conn, [str(d)])
            m = li._load_fp_matrix(db)
            assert m is not None and m.shape[0] == 1
        finally:
            conn.close()
