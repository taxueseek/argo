#!/usr/bin/env python3
"""tests/test_mcp_setup.py — 多客户端 MCP 注入/还原单测（隔离 home，不碰真实配置）"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import mcp_setup as ms  # noqa: E402


class TestMcpSetupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argo-mcp-test-")
        # 隔离 home：配置路径 + 备份目录均落在 tmp，不污染真实 ~/.argo 与配置文件
        os.environ["ARGO_HOME_OVERRIDE"] = self.tmp
        # 保证 mcp/clients.yaml 真源可被加载（真实路径）
        os.environ["ARGO_CLIENTS_PATH"] = str(
            Path(__file__).resolve().parents[1] / "mcp" / "clients.yaml"
        )

    def tearDown(self):
        os.environ.pop("ARGO_HOME_OVERRIDE", None)
        os.environ.pop("ARGO_CLIENTS_PATH", None)


class TestSelect(TestMcpSetupBase):
    def _clients(self):
        return [
            {"id": "codex", "name": "Codex", "config_path": ".codex/config.toml",
             "servers_key": "mcp_servers", "format": "toml", "detect": [".codex"]},
            {"id": "cursor", "name": "Cursor", "config_path": ".cursor/mcp.json",
             "servers_key": "mcpServers", "format": "json", "detect": [".cursor"]},
        ]

    def test_all_selects_detected(self):
        os.makedirs(os.path.join(self.tmp, ".codex"))
        os.makedirs(os.path.join(self.tmp, ".cursor"))
        got = ms._select(self._clients(), ["all"])
        self.assertEqual({c["id"] for c in got}, {"codex", "cursor"})

    def test_comma_split(self):
        got = ms._select(self._clients(), ["codex,cursor"])
        self.assertEqual({c["id"] for c in got}, {"codex", "cursor"})

    def test_unknown_ignored(self):
        os.makedirs(os.path.join(self.tmp, ".codex"))
        got = ms._select(self._clients(), ["codex,ghost"])
        self.assertEqual({c["id"] for c in got}, {"codex"})


class TestJsonInjectRemove(TestMcpSetupBase):
    def test_inject_then_remove_preserves_siblings(self):
        cfg = Path(self.tmp) / ".cursor" / "mcp.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"mcpServers": {"other": {"url": "x"}}}), encoding="utf-8")
        client = {"id": "cursor", "config_path": ".cursor/mcp.json",
                  "servers_key": "mcpServers", "format": "json", "url_key": "url"}
        entry = {"command": "python3", "args": ["a.py"], "env": {"K": "v"}}

        new = ms._inject_json(client, entry)
        ms.atomic_write(cfg, new, secret=False)
        data = json.loads(new)
        self.assertIn("argo", data["mcpServers"])
        self.assertIn("other", data["mcpServers"])  # 兄弟保留

        removed = ms._remove_json(client)
        assert removed is not None
        data2 = json.loads(removed)
        self.assertNotIn("argo", data2["mcpServers"])
        self.assertIn("other", data2["mcpServers"])  # 兄弟保留

    def test_remove_missing_returns_none(self):
        client = {"id": "cursor", "config_path": ".cursor/mcp.json",
                  "servers_key": "mcpServers", "format": "json"}
        self.assertIsNone(ms._remove_json(client))


class TestTomlInjectRemove(TestMcpSetupBase):
    def test_toml_preserves_comment_and_appends_block(self):
        cfg = Path(self.tmp) / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("# 手写注释\nmodel = \"gpt-5\"\n", encoding="utf-8")
        client = {"id": "codex", "config_path": ".codex/config.toml",
                  "servers_key": "mcp_servers", "format": "toml"}
        entry = {"command": "python3", "args": ["mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8"}}

        new = ms._inject_toml(client, entry)
        ms.atomic_write(cfg, new, secret=False)
        self.assertIn("# 手写注释", new)
        self.assertIn("model = \"gpt-5\"", new)
        self.assertIn("[mcp_servers.argo]", new)
        self.assertIn("command = \"python3\"", new)

        removed = ms._remove_toml(client)
        assert removed is not None
        self.assertIn("# 手写注释", removed)
        self.assertIn("model = \"gpt-5\"", removed)
        self.assertNotIn("[mcp_servers.argo]", removed)

    def test_toml_duplicate_block_raises(self):
        cfg = Path(self.tmp) / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[mcp_servers.argo]\ncommand = \"x\"\n", encoding="utf-8")
        client = {"id": "codex", "config_path": ".codex/config.toml",
                  "servers_key": "mcp_servers", "format": "toml"}
        entry = {"command": "python3", "args": ["a.py"]}
        with self.assertRaises(ValueError):
            ms._inject_toml(client, entry)

    def test_toml_remove_no_marker_returns_none(self):
        cfg = Path(self.tmp) / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("model = \"x\"\n", encoding="utf-8")
        client = {"id": "codex", "config_path": ".codex/config.toml",
                  "servers_key": "mcp_servers", "format": "toml"}
        self.assertIsNone(ms._remove_toml(client))


class TestBackupRestore(TestMcpSetupBase):
    def test_backup_created_and_restored(self):
        cfg = Path(self.tmp) / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("model = \"x\"\n", encoding="utf-8")

        backup = ms.backup_file(cfg)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        # 修改后从备份恢复
        cfg.write_text("model = \"y\"\n", encoding="utf-8")
        backups = ms.list_backups("config.toml")
        self.assertGreaterEqual(len(backups), 1)
        shutil_copy = __import__("shutil")
        shutil_copy.copy2(backups[0], cfg)
        self.assertIn("model = \"x\"", cfg.read_text(encoding="utf-8"))

    def test_backup_missing_file_returns_none(self):
        self.assertIsNone(ms.backup_file(Path(self.tmp) / "nope.json"))


class TestAtomicWrite(TestMcpSetupBase):
    def test_atomic_write_creates_parent_and_content(self):
        p = Path(self.tmp) / "a" / "b" / "c.json"
        ms.atomic_write(p, '{"k":1}\n', secret=True)
        self.assertEqual(p.read_text(encoding="utf-8"), '{"k":1}\n')

    def test_atomic_write_secret_mode(self):
        p = Path(self.tmp) / "secret.json"
        ms.atomic_write(p, "x", secret=True)
        mode = p.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
