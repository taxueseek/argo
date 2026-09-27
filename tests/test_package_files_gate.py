#!/usr/bin/env python3
"""npm 打包产物门禁：不得包含 __pycache__ 与主机路径（2026-09-27）。

守的缺陷：`package.json` 的 `files` 字段里，`!` 排除规则曾被写在目录项
**之前**：

    "files": ["scripts/", "!**/__pycache__/**", "!**/*.pyc", "sub-skills/", ...]

npm 的 `files` 字段语义是**后者覆盖前者**：`"sub-skills/"` 这个目录项出现
在排除规则之后，于是 `sub-skills/**/__pycache__` 又被重新包含进来。实测
最小复现（两条命令，只差一个顺序）：

    $ echo '{"files":["scripts/","!**/__pycache__/**","sub-skills/"]}'  → 含 .pyc
    $ echo '{"files":["scripts/","sub-skills/","!**/__pycache__/**"]}'  → 干净

后果有两层：
  1. **能力**：发布包里混进 15 个 .pyc，跨 Python 版本/架构是无效文件；
  2. **隐私**：.pyc 的 co_filename 里带着构建机的绝对路径与用户名
     （`/Users/<user>/.agents/skills/argo/...`），对外发布的包不该包含它。

注意 `.npmignore` 对此**无效**：`files` 字段存在时 npm 只认 `files`，
所以修法只能是把 `!` 规则移到数组末尾（本测试即守这条不变量）。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_package_files_gate.py -q
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "package.json"


class TestFilesFieldOrdering(unittest.TestCase):
    """静态门禁：`files` 里所有 `!` 排除规则必须排在所有包含项之后。"""

    @classmethod
    def setUpClass(cls):
        cls.files = json.loads(PKG.read_text(encoding="utf-8"))["files"]

    def test_excludes_come_last(self):
        """核心不变量：排除项必须整体位于包含项之后。

        一旦有人在末尾追加新的目录项（`"tests/"` 之类），这条会红——
        正是这类追加会静默复活 __pycache__。
        """
        neg = [i for i, f in enumerate(self.files) if f.startswith("!")]
        pos = [i for i, f in enumerate(self.files) if not f.startswith("!")]
        self.assertTrue(neg, "files 里必须有排除规则（否则 __pycache__ 会进包）")
        self.assertGreater(min(neg), max(pos),
                           "有排除规则写在了包含项之前：npm 会按后者覆盖前者，"
                           "被后面的目录项重新包含（__pycache__ 泄露的根因）")

    def test_pyc_pycache_are_excluded(self):
        """必须真的排除 .pyc 与 __pycache__（不是只写了个名字）。"""
        joined = "\n".join(self.files)
        self.assertIn("**/*.pyc", joined, "缺少 .pyc 排除规则")
        self.assertIn("__pycache__", joined, "缺少 __pycache__ 排除规则")

    def test_sub_skills_is_included(self):
        """反向：排除规则不得误伤 sub-skills 本体（它是能力的一部分）。"""
        self.assertIn("sub-skills/", self.files,
                      "sub-skills/ 必须在包含列表里：local-seek/local-search 都在其中")


class TestPackedTarballIfPresent(unittest.TestCase):
    """动态门禁：仓库里若已有打包产物，它不得含 .pyc 或主机路径。

    只在产物存在时运行（tgz 是 .gitignore 的本地产物，CI 上可能没有）。
    只读 tgz 的**条目名**即可发现 .pyc；主机路径检查需要解包内容，
    故用 tarfile 逐条读小文件内容做子串扫描，避免整包解压到磁盘。
    """

    def _tarball(self) -> Path | None:
        cands = sorted(REPO.glob("*.tgz"))
        return cands[0] if cands else None

    def test_no_pyc_in_tarball(self):
        tg = self._tarball()
        if tg is None:
            self.skipTest("仓库内无 .tgz 产物，跳过（打包门禁由 CI 或发布前执行）")
        import tarfile
        with tarfile.open(tg) as tf:
            names = tf.getnames()
        bad = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        self.assertEqual(bad, [], f"打包产物含 {len(bad)} 个 pyc 条目：{bad[:3]}")

    def test_no_host_path_in_tarball(self):
        """发布包不得含**具体主机路径**（真实用户名/主目录）。

        判据说明：不能只搜 `/Users/`——脱敏模块 `archive_run.py` 里就带着
        一条**用来清除路径的正则**，它是安全设施而非泄露。真正的泄露形态是
        `co_filename` 那种带真实用户名的绝对路径（原缺陷里 .pyc 携带的就是它）。
        所以判据取「/Users/<具体名>/」形态，并排除正则片段与占位符。
        """
        tg = self._tarball()
        if tg is None:
            self.skipTest("仓库内无 .tgz 产物，跳过")
        import re
        import tarfile
        # 泄露形态：/Users/<具体名>/... ；排除正则片段与占位符
        leak_re = re.compile(rb"/Users/(?![\[\]\^\\\s\"':])(?!\$)"
                             rb"(?!example|user|username|me\b)[A-Za-z0-9._-]{2,}/")
        leaks: list[str] = []
        with tarfile.open(tg) as tf:
            for m in tf.getmembers():
                if not m.isfile() or m.size > 2_000_000:
                    continue
                if not m.name.endswith((".py", ".pyc", ".json", ".yaml", ".yml", ".js")):
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                if leak_re.search(f.read()):
                    leaks.append(m.name)
        self.assertEqual(leaks, [], f"打包产物含具体主机路径：{leaks[:3]}")


if __name__ == "__main__":
    unittest.main()
