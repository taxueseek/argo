#!/usr/bin/env python3
"""测试 seek.py 的 Office 办公文档解包透视与 scope 增强。"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

# 把 seek.py 所在目录加入 sys.path
TEST_DIR = Path(__file__).resolve().parent
SEEK_DIR = TEST_DIR.parent / "scripts"
if str(SEEK_DIR) not in sys.path:
    sys.path.insert(0, str(SEEK_DIR))

import seek


class TestSeekOffice(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_fake_docx(self, name: str, text: str) -> Path:
        p = self.root / name
        doc_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>"""
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("word/document.xml", doc_xml)
        return p

    def _create_fake_xlsx(self, name: str, text: str) -> Path:
        p = self.root / name
        shared_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">
  <si><t>{text}</t></si>
</sst>"""
        sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c></row>
  </sheetData>
</worksheet>"""
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("xl/sharedStrings.xml", shared_xml)
            z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        return p

    def test_extract_docx(self):
        docx_path = self._create_fake_docx("test.docx", "人工智能财务预算分析")
        items = seek._extract_office_items(docx_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], 1)
        self.assertIn("人工智能财务预算分析", items[0][1])

    def test_extract_xlsx(self):
        xlsx_path = self._create_fake_xlsx("data.xlsx", "核心利润数据")
        items = seek._extract_office_items(xlsx_path)
        self.assertEqual(len(items), 1)
        self.assertIn("Sheet1:A1", items[0][1])
        self.assertIn("核心利润数据", items[0][1])

    def test_run_query_office_scope(self):
        self._create_fake_docx("contract.docx", "甲方同意支付技术服务费壹佰万元")
        out, rc = seek.run_query(["技术服务费", "--path", str(self.root), "--scope", "office"])
        self.assertEqual(rc, 0)
        self.assertIn("contract.docx:1", out)
        self.assertIn("技术服务费壹佰万元", out)

    def test_run_query_config_scope(self):
        cfg_file = self.root / "app.yaml"
        cfg_file.write_text("database_host: 127.0.0.1\nport: 8080\n", encoding="utf-8")
        out, rc = seek.run_query(["database_host", "--path", str(self.root), "--scope", "config"])
        self.assertEqual(rc, 0)
        self.assertIn("app.yaml", out)


if __name__ == "__main__":
    unittest.main()
