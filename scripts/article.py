#!/usr/bin/env python3
"""article.py — 微信公众号文章全文抓取。

用法: argo article <url> [--json]
      argo article <url> --json > article.json

绕过 robots.txt 限制：公众号 robots 禁爬，但文章页对真实浏览器开放，
用手机 UA 直连（curl/urllib 不遵守 robots），解析 js_content 提取正文。

输出: 标题 / 作者 / 发布时间 / 正文纯文本 / 图片 URL 列表
"""
import argparse, json, re, sys, html as htmllib, urllib.request

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://mp.weixin.qq.com/",
}


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def strip_tags(s: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</(p|div|section|h\d|li|tr|blockquote)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s)
    lines = [l.strip() for l in s.split("\n")]
    return "\n".join(l for l in lines if l)


def fetch_article(url: str, timeout: int = 30) -> dict:
    """抓取并解析公众号文章，返回结构化 dict（CLI 与 MCP 共用主链）。

    URL 非 mp.weixin.qq.com 时抛 ValueError（CLI 走 stderr 退出，
    MCP 转 isError）；抓取失败/反爬拦截返回 {"ok": False, "error": ...}。
    """
    if not re.match(r"https?://mp\.weixin\.qq\.com/", url):
        raise ValueError("仅支持 mp.weixin.qq.com 链接")

    try:
        raw = fetch(url, timeout)
    except Exception as e:
        return {"ok": False, "error": f"抓取失败: {e}"}

    if "js_content" not in raw and "环境异常" in raw:
        return {"ok": False, "error": "微信反爬拦截（环境异常），请稍后重试"}

    def grab(pattern, default=""):
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else default

    title = grab(r'<meta property="og:title" content="([^"]+)"') or \
        grab(r'<h1[^>]*class="rich_media_title"[^>]*>([\s\S]*?)</h1>')
    author = grab(r'<meta property="og:article:author" content="([^"]+)"') or \
        grab(r'id="js_name"[^>]*>([^<]+)<')
    pubtime = grab(r'id="publish_time"[^>]*>([^<]+)<')

    m = re.search(r'id="js_content"[^>]*>([\s\S]*?)</div>\s*<script', raw)
    if not m:
        m = re.search(r'id="js_content"[^>]*>([\s\S]*?)</div>', raw)
    body_html = m.group(1) if m else ""

    text = strip_tags(body_html)
    imgs = re.findall(r'data-src="(https://mmbiz\.qpic\.cn/[^"]+)"', body_html)
    if not imgs:
        imgs = re.findall(r'src="(https://mmbiz\.qpic\.cn/[^"]+)"', body_html)

    return {
        "ok": True,
        "url": url,
        "title": title,
        "author": author,
        "publish_time": pubtime,
        "char_count": len(text),
        "image_count": len(imgs),
        "images": imgs,
        "content": text,
    }


def main():
    ap = argparse.ArgumentParser(description="微信公众号文章全文抓取")
    ap.add_argument("url", help="mp.weixin.qq.com 文章链接")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    try:
        out = fetch_article(args.url)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not out["ok"]:
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(1)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"标题: {out['title']}")
        print(f"作者: {out['author']}  时间: {out['publish_time']}  "
              f"正文 {out['char_count']} 字 / {out['image_count']} 图")
        print("---")
        print(text)
        if imgs:
            print("---")
            for i, u in enumerate(imgs):
                print(f"[图{i}] {u}")


if __name__ == "__main__":
    main()
