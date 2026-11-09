#!/usr/bin/env python3
"""wechat_article.py — 微信公众号文章多级降级抓取。

用法: argo wx <url> [--json]
      argo wx <url> --json > article.json

降级链（按速度优先）：
  1. HTTP 直取（article.py 解析逻辑，5s 超时）
  2. Chrome CDP 浏览器（复用 fetch_browser.browser_fetch）
  3. 搜狗微信搜索兜底（通过标题间接获取）

输出: 标题 / 作者 / 发布时间 / 正文纯文本 / 图片 URL 列表
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import html as htmllib
from typing import Optional

# 复用 article.py 的解析逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from article import strip_tags, fetch_article

# 复用 fetch_browser.py 的 CDP 通道
from fetch_browser import browser_fetch

# urllib 出口唯一入口（issue #13）：代理解析集中在一处；YAML 解析同理
from net_proxy import http_get
from yaml_load import loads

# 本脚本所在目录：子进程脚本路径必须绝对（本模块可能从任意 CWD 调起）
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _http_fetch(url: str, timeout: int = 5) -> dict:
    """通道1: HTTP 直取（article.py 逻辑，短超时快速失败）。"""
    try:
        return fetch_article(url, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"HTTP 直取失败: {e}"}


def _cdp_fetch(url: str, timeout: int = 15) -> dict:
    """通道2: Chrome CDP 浏览器抓取。"""
    try:
        result = browser_fetch(url, max_chars=20000, timeout=timeout)
        if not result.get("success"):
            return {"ok": False, "error": result.get("error", "CDP 抓取失败")}

        content = result.get("content", "")
        title = result.get("title", "")

        html = result.get("html", "")
        author = ""
        pubtime = ""
        if html:
            m = re.search(r'id="js_name"[^>]*>([^<]+)<', html)
            if m:
                author = m.group(1).strip()
            m = re.search(r'id="publish_time"[^>]*>([^<]+)<', html)
            if m:
                pubtime = m.group(1).strip()

        imgs = []
        if html:
            imgs = re.findall(r'data-src="(https://mmbiz\.qpic\.cn/[^"]+)"', html)
            if not imgs:
                imgs = re.findall(r'src="(https://mmbiz\.qpic\.cn/[^"]+)"', html)

        return {
            "ok": True,
            "url": url,
            "title": title,
            "author": author,
            "publish_time": pubtime,
            "char_count": len(content),
            "image_count": len(imgs),
            "images": imgs,
            "content": content,
            "fetch_method": "chrome_cdp",
        }
    except Exception as e:
        return {"ok": False, "error": f"CDP 通道异常: {e}"}


def _sogou_cmd(title: str) -> list:
    """搜狗兜底的子进程命令——脚本路径绝对化，与调用方 CWD 无关。"""
    return [sys.executable, os.path.join(_SCRIPTS_DIR, "sogou_weixin_browser.py"),
            title, "--n", "3"]


def _sogou_fallback(url: str) -> dict:
    """通道3: 搜狗微信搜索兜底（通过标题间接获取）。"""
    try:
        title = ""
        try:
            raw = http_get(url, {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            }, timeout=5)[0].decode("utf-8", errors="ignore")
            m = re.search(r'<meta property="og:title" content="([^"]+)"', raw)
            if m:
                title = m.group(1).strip()
        except Exception:
            pass

        if not title:
            return {"ok": False, "error": "无法获取标题，搜狗兜底失败"}

        result = subprocess.run(
            _sogou_cmd(title),
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"搜狗搜索失败: {result.stderr[:200]}"}

        try:
            items = loads(result.stdout)
        except Exception:
            return {"ok": False, "error": "搜狗搜索结果解析失败"}

        if not items:
            return {"ok": False, "error": "搜狗搜索无结果"}

        best = items[0]
        return {
            "ok": True,
            "url": best.get("url", url),
            "title": best.get("title", title),
            "author": "",
            "publish_time": best.get("published_at", ""),
            "char_count": len(best.get("snippet", "")),
            "image_count": 0,
            "images": [],
            "content": best.get("snippet", ""),
            "fetch_method": "sogou_fallback",
            "note": "通过搜狗搜索间接获取，内容可能不完整",
        }
    except Exception as e:
        return {"ok": False, "error": f"搜狗兜底异常: {e}"}


# 微信错误页特征串（被删/违规/风控/校验失败都会渲染成短提示页而非常规文章）。
# 这些页面 HTTP 200、可读、有正文——长度判据拦不住，必须按标记识别。
_WECHAT_ERROR_MARKERS = (
    "参数错误", "该内容已被发布者删除", "此内容因违规无法查看", "环境异常",
    "访问过于频繁", "请在微信客户端打开链接", "系统繁忙", "网页包含诱导分享",
    "该公众号已迁移", "此账号已自主注销",
)


def _has_content(result: dict) -> bool:
    """通道结果必须有真实正文才算成功。

    2026-10-12 实测两形态：①被删/不存在的 URL 走 HTTP 直取拿到 HTTP 200 的
    空壳（ok=True、char_count=0）；②CDP 通道会渲染微信「参数错误」提示页
    （char_count=4、title 空）。两者此前都被当成功直接交付（失败伪装成成功），
    后续通道也拿不到机会。

    判据三层（从强到弱）：
      1) 空正文直接失败；
      2) 错误页特征串出现在**正文开头**（前 20 字）即失败——微信提示页都是
         短页、特征串居首；正文中部引用这些词（如讲「环境异常」的文章）
         不误杀，所以不做全文匹配；
      3) 短正文（<60 字）必须至少有一项支撑（标题或图片），否则视为空壳。
    """
    if not result.get("ok"):
        return False
    content = (result.get("content") or "").strip()
    n = result.get("char_count")
    if not isinstance(n, (int, float)) or n <= 0:
        n = len(content)
    if n <= 0:
        return False
    head = content[:20]
    for marker in _WECHAT_ERROR_MARKERS:
        if marker in head:
            return False
    title = (result.get("title") or "").strip()
    if not title and not result.get("images") and len(content) < 60:
        return False
    return True


def fetch_wechat_article(url: str) -> dict:
    """多级降级链主函数。"""
    if not re.match(r"https?://mp\.weixin\.qq\.com/", url):
        return {"ok": False, "error": "仅支持 mp.weixin.qq.com 链接"}

    # 通道1: HTTP 直取（快速失败）
    result = _http_fetch(url, timeout=5)
    if _has_content(result):
        result["fetch_method"] = "http_direct"
        result.setdefault("char_count", len(result.get("content") or ""))
        return result

    # 通道2: Chrome CDP 浏览器
    result = _cdp_fetch(url, timeout=15)
    if _has_content(result):
        return result

    # 通道3: 搜狗搜索兜底
    result = _sogou_fallback(url)
    if _has_content(result):
        return result

    # 全部失败
    return {
        "ok": False,
        "error": "所有通道均失败。建议：1) 稍后重试 2) 用浏览器手动打开",
        "url": url,
    }


def main():
    ap = argparse.ArgumentParser(description="微信公众号文章多级降级抓取")
    ap.add_argument("url", help="mp.weixin.qq.com 文章链接")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    out = fetch_wechat_article(args.url)

    if args.json:
        from cli_io import dumps
        print(dumps(out))
        # 失败必须走非零退出码（与 article.py 同款）：agent/脚本按 rc 判成败，
        # `--json` 分支此前只印 JSON 就返回 0，失败被 rc=0 掩盖。
        if not out.get("ok"):
            sys.exit(1)
    else:
        if not out["ok"]:
            print(f"抓取失败: {out['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"标题: {out.get('title', '')}")
        print(f"作者: {out.get('author', '')}  时间: {out.get('publish_time', '')}  "
              f"正文 {out.get('char_count', 0)} 字 / {out.get('image_count', 0)} 图")
        print(f"通道: {out.get('fetch_method', '')}")
        print("---")
        print(out.get("content", ""))
        if out.get("images"):
            print("---")
            for i, u in enumerate(out["images"], 1):
                print(f"[图{i}] {u}")


if __name__ == "__main__":
    main()
