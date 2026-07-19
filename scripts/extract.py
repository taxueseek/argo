#!/usr/bin/env python3
"""extract.py — 结构化数据提取"""
import json, re, sys
from html.parser import HTMLParser
from fetch import fetch_page

class TableExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._in_table = False
        self._rows = []
        self._row = []
        self._cell = []
        self._in_cell = False
    def handle_starttag(self, tag, attrs):
        if tag == 'table': self._in_table = True; self._rows = []
        elif tag in ('td','th') and self._in_table: self._in_cell = True; self._cell = []
        elif tag == 'tr' and self._in_table: self._row = []
    def handle_endtag(self, tag):
        if tag in ('td','th') and self._in_cell:
            self._in_cell = False; self._row.append(''.join(self._cell).strip())
        elif tag == 'tr' and self._in_table and self._row:
            self._rows.append(self._row)
        elif tag == 'table' and self._in_table:
            self._in_table = False
            if self._rows:
                headers = self._rows[0]
                table = [{headers[i]: row[i] if i < len(row) else '' for i in range(len(headers))} for row in self._rows[1:]]
                self.tables.append(table)
    def handle_data(self, data):
        if self._in_cell: self._cell.append(data)

def extract_tables(html):
    ext = TableExtractor(); ext.feed(html); return ext.tables

def extract_metadata(html):
    meta = {}
    title_m = re.search(r'<title>(.*?)</title>', html, re.I|re.S)
    if title_m: meta['title'] = title_m.group(1).strip()
    for pattern in [r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', r'<meta\s+content=["\'](.*?)["\']\s+name=["\']description["\']']:
        m = re.search(pattern, html, re.I)
        if m: meta['description'] = m.group(1).strip(); break
    for prop in ['og:title','og:description','og:image']:
        m = re.search(rf'property=["\']{prop}["\']\s+content=["\'](.*?)["\']', html, re.I)
        if m: meta[prop] = m.group(1).strip()
    return meta

def extract_jsonld(html):
    results = []
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S|re.I):
        try: results.append(json.loads(m.group(1)))
        except: pass
    return results

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--url', required=True)
    p.add_argument('--mode', default='all', choices=['tables','metadata','jsonld','all'])
    args = p.parse_args()
    result = fetch_page(args.url, 50000)
    if not result['success']: print(json.dumps({'error': result.get('error')})); sys.exit(1)
    html = result['content']
    output = {}
    if args.mode in ('tables','all'): output['tables'] = extract_tables(html)
    if args.mode in ('metadata','all'): output['metadata'] = extract_metadata(html)
    if args.mode in ('jsonld','all'): output['jsonld'] = extract_jsonld(html)
    print(json.dumps(output, ensure_ascii=False, indent=2)[:3000])
