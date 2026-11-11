#!/usr/bin/env python3
"""test_public_config_compliance — 对外产物不得出现盗版/影子图书馆类站名。

## 守的是什么

2026-09-13 审查发现：`scholar_search` 域的触发词里写着 `sci-hub`。触发词只该
描述**检索意图**，不该引用具体站点；配置是公开产物，出现影子图书馆站名既无
功能价值（意图词「论文下载」已足够触发同一路由），又把本仓牵进它并不提供的
服务——实测该查询本来就只送往 google_scholar/arxiv，argo 从不取那些站的内容。

## 判据与范围

扫描**对外产物**：config.yaml、engines/**/*.yaml（含 specs）、README ×5、
SKILL.md、package.json。不含 `tests/`（本文件自身必须持有禁用名单）与
`docs/`（内部文档讨论该议题是正常且必要的）。

## 双向锁定

只禁站名不够——把整条 pattern 删掉也能过。故同时锁定「检索意图仍在」：
scholar_search 必须仍能被子查询「论文下载」命中。一边禁、一边必须留，
这样「修掉站名」才不会被偷换成「删掉能力」。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 已确认的盗版站 / 影子图书馆名（小写比较）。只收「下载受版权保护内容」的
# 站点，不误伤合法学术源（arxiv/openreview/biorxiv 等不在列）。
_SHADOW_LIBRARY_NAMES = (
    "sci-hub", "scihub", "sci_hub",
    "libgen", "library genesis", "z-lib", "zlib", "z-library", "zlibrary",
    "annas-archive", "annas archive", "annas_archive",
    "1337x", "thepiratebay", "piratebay", "rarbg", "nyaa", "torrentz",
    "bookfi", "libros", "avaxhome", "zlibrary-asia",
    "磁力链接", "种子下载",
)

# 对外产物（含 glob）
_PUBLIC_GLOBS = (
    "config.yaml",
    "package.json",
    "SKILL.md",
    "README.md",
    "README.zh.md",
    "README.es.md",
    "README.ja.md",
    "README.ko.md",
    "engines/*.yaml",
    "engines/specs/*.yaml",
)


def _public_files() -> list[Path]:
    out: list[Path] = []
    for pattern in _PUBLIC_GLOBS:
        out.extend(sorted(ROOT.glob(pattern)))
    return out


class TestNoShadowLibraryNames(unittest.TestCase):
    def test_public_files_exist(self):
        """扫描面不能是空的——glob 写错会让本检查静默全绿。"""
        files = _public_files()
        self.assertGreaterEqual(len(files), 60,
                                f"仅扫到 {len(files)} 个对外文件，glob 可能失效")

    def test_no_shadow_library_name_in_public_artifacts(self):
        hits: list[str] = []
        for f in _public_files():
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
            for name in _SHADOW_LIBRARY_NAMES:
                if name in text:
                    line = next(
                        (i + 1 for i, ln in enumerate(
                            f.read_text(encoding="utf-8", errors="ignore").splitlines())
                         if name in ln.lower()), 0)
                    hits.append(f"{f.relative_to(ROOT)}:{line} 出现 {name!r}")
        self.assertFalse(hits, (
            "对外产物里出现盗版/影子图书馆站名——触发词请改用检索意图词"
            "（如「论文下载/文献获取」），不要引用具体站点：\n  "
            + "\n  ".join(hits)
        ))


class TestPaperIntentStillRouted(unittest.TestCase):
    """反向锁定：删掉站名之后，「想拿论文全文」的意图仍须命中学术域。"""

    def test_download_intent_routes_to_academic(self):
        from route import route_query
        for q in ("论文下载", "文献获取", "学位论文", "学术文献"):
            d = route_query(q, mode="auto", depth="balanced")
            engs = list(d.get("engines") or [])
            self.assertTrue(engs, f"{q} 未选出任何引擎")
            self.assertTrue(
                any(e in ("google_scholar", "arxiv", "crossref",
                          "semantic_scholar", "openalex") for e in engs),
                f"{q} 命中域 {d.get('domain')} 但引擎 {engs} 里没有学术源"
                f"——触发词改窄后丢掉了原有能力",
            )

    def test_scholar_search_declares_download_intent(self):
        """scholar_search 必须仍有下载意图触发词（防「禁站名」被偷换成「删能力」）。"""
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        pats = []
        for d in cfg.get("domains") or []:
            if isinstance(d, dict) and d.get("name") == "scholar_search":
                pats = list(d.get("patterns") or [])
        self.assertTrue(pats, "scholar_search 域不存在或没有 patterns")
        blob = " ".join(pats)
        self.assertTrue(
            any(w in blob for w in ("论文下载", "下载论文", "文献下载", "文献获取")),
            f"scholar_search 缺少下载意图触发词：{pats}",
        )


if __name__ == "__main__":
    unittest.main()
