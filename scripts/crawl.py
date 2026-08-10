#!/usr/bin/env python3
"""crawl.py — 站点级爬取（fetch_v3 降级链：增强 HTTP → TLS 指纹 → Wayback）"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
# fetch_v3 而非裸 urllib fetch：UA 轮换 + Cookie 积累 + curl_cffi 指纹伪造，
# 反爬站（CF 保护等）从必然失败到大概率成功。批量场景禁浏览器降级
# （BFS 可能爬几十页，每页启动 Chrome CDP 会拖慢整体）。
from fetch_v3 import fetch_page_v3 as _fetch_page_v3


def _crawl_fetch(url: str, max_chars: int, timeout: int, raw: bool = True) -> dict:
    """批量爬取用轻量降级链（不升浏览器）：HTTP → TLS 指纹 → Wayback。

    raw 默认 True：爬取需要原始 HTML 提取链接，且 fetch_v3 缓存不存 html，
    必须绕过缓存才能拿到完整页面。通过模块级 fetch_page 转发，
    测试可 monkeypatch crawl.fetch_page 注入假抓取。
    """
    return fetch_page(url, max_chars=max_chars, timeout=timeout, raw=raw)


# 默认实现指向 fetch_v3 的兼容入口；测试可整体替换此名字
fetch_page = _fetch_page_v3

def crawl_sitemap(url, max_pages=20, timeout=10):
    """从 sitemap.xml 爬取"""
    t0 = time.monotonic()
    sitemap_url = urljoin(url, '/sitemap.xml')
    result = _crawl_fetch(sitemap_url, 50000, timeout)
    if not result['success']:
        # 尝试 robots.txt 指定的 sitemap
        robots_url = urljoin(url, '/robots.txt')
        robots_result = _crawl_fetch(robots_url, 10000, timeout)
        if robots_result['success'] and 'Sitemap:' in robots_result.get('html', ''):
            import re as _re
            sitemap_urls = _re.findall(r'Sitemap:\s*(.+)', robots_result['html'])
            if sitemap_urls:
                sitemap_url = sitemap_urls[0].strip()
                result = _crawl_fetch(sitemap_url, 50000, timeout)
    if not result.get('success') or not result.get('html'):
        return {'url': url, 'pages': [], 'total': 0, 'error': 'sitemap not found or empty'}
    html = result.get('html') or result.get('content') or ''
    urls = re.findall(r'<loc>\s*(.*?)\s*</loc>', html)
    urls = urls[:max_pages]
    pages = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_crawl_fetch, u, 2000, timeout): u for u in urls}
        for fut in as_completed(futures, timeout=timeout*2):
            try:
                r = fut.result()
                if r['success']:
                    pages.append({'url': r['url'], 'content': r['content'][:500], 'depth': 0})
            except: pass
    return {'url': url, 'pages': pages, 'total': len(pages), 'elapsed_ms': int((time.monotonic() - t0) * 1000)}

def crawl_bfs(url, max_pages=10, max_depth=2, timeout=8):
    """BFS 爬取"""
    visited = set()
    pages = []
    queue = [(url, 0)]
    while queue and len(pages) < max_pages:
        current_url, depth = queue.pop(0)
        if current_url in visited or depth > max_depth:
            continue
        visited.add(current_url)
        result = _crawl_fetch(current_url, 2000, timeout)
        if result['success']:
            pages.append({'url': current_url, 'content': result['content'][:500], 'depth': depth})
            html = result.get('html') or result.get('content') or ''
            links = re.findall(r'href=["\']([^"\'#]+)', html)
            for link in links[:5]:
                full = urljoin(current_url, link)
                if urlparse(full).netloc == urlparse(url).netloc and full not in visited:
                    queue.append((full, depth+1))
    return {'url': url, 'pages': pages, 'total': len(pages)}

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('url')
    p.add_argument('--strategy', default='bfs', choices=['sitemap','bfs'])
    p.add_argument('--max-pages', type=int, default=10)
    p.add_argument('--max-depth', type=int, default=2)
    args = p.parse_args()
    if args.strategy == 'sitemap':
        r = crawl_sitemap(args.url, args.max_pages)
    else:
        r = crawl_bfs(args.url, args.max_pages, args.max_depth)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2000])
