#!/usr/bin/env python3
"""小红书搜索引擎

使用 xhs-cli (xiaohongshu-cli) 工具。
需要先通过 `xhs login` 登录获取 cookies。
"""

import json
import os
import subprocess


# 内部子进程超时（秒）。此前硬编码 15s，比调度层的单引擎墙钟预算（10s）还长，
# 属「内层白跑」：外层 subprocess 会在预算处 kill，但内层已消耗的时间追不回。
# 收到 8s —— 与 execution.default_timeout 一致，且短于预算，
# 使内层能在被 kill 前主动超时返回（走 except 分支），行为更干净。
# 可用 ARGO_XHS_TIMEOUT 覆盖，便于慢网调试。
_DEFAULT_TIMEOUT = 8


def _subprocess_timeout() -> int:
    try:
        v = int(os.environ.get("ARGO_XHS_TIMEOUT", "").strip())
        return v if v > 0 else _DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


def _child_env() -> dict:
    """xhs 子进程环境：UTF-8 + 剔除 NO_PROXY 里的 [::1]（issue #36）。

    两处都会让引擎表现成「平台 0 结果」，且都是环境层问题、与查询无关：
      ① xhs-cli 的输出含 emoji（📕），子进程继承 Windows GBK 控制台编码时抛
         UnicodeEncodeError 直接退出 → 强制 PYTHONUTF8/PYTHONIOENCODING；
      ② 宿主注入 NO_PROXY=...,[::1] 时 xhs 内部 httpx 抛
         InvalidURL: Invalid port ':1]' → 从 NO_PROXY/no_proxy 里剔掉 [::1]。
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for key in ("NO_PROXY", "no_proxy"):
        raw = env.get(key)
        if not raw:
            continue
        kept = [p for p in raw.split(",") if p.strip() and p.strip() != "[::1]"]
        if kept:
            env[key] = ",".join(kept)
        else:
            env.pop(key, None)
    return env


def search(query: str, n: int = 5) -> list[dict]:
    """主搜索入口"""
    try:
        result = subprocess.run(
            # --json 必须传：xhs-cli 的 argparse 接受该 flag 但**不传时只打印
            # 人类可读文本**，_parse_xhs_output 的 json.loads 会失败并静默返回
            # 空列表（2026-11-03 issue #36 实测）。
            ["xhs", "search", "--json", query],
            capture_output=True, text=True, timeout=_subprocess_timeout(),
            encoding="utf-8", errors="replace",  # Windows GBK locale 下 UTF-8 输出会解码崩
            env=_child_env(),  # UTF-8 环境 + 剥 [::1]，见 _child_env（issue #36）
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_xhs_output(result.stdout, n)
        if result.returncode != 0:
            # 未登录不是「平台 0 结果」：带可归因 error 浮上来，避免与「查了没有」
            # 同形（同 zhihu_engine / 社交引擎统一范式）。
            err_msg = (result.stdout or result.stderr or "").strip()
            if "not_authenticated" in err_msg or "NoCookieError" in err_msg:
                return [{"error": "xhs not_authenticated (run `xhs login --qrcode`)",
                         "source": "xiaohongshu"}]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # CLI 未安装/超时不是「平台 0 结果」：带 error 浮上来，与社交引擎
        # 统一范式一致（zhihu_engine 范式，mcp_handlers 的 err=None 设计
        # 依赖引擎抛异常或返 error 占位）。
        return [{"error": f"xhs CLI {type(e).__name__}", "source": "xiaohongshu"}]
    return []


def _parse_xhs_output(raw: str, n: int) -> list[dict]:
    """解析 xhs-cli 输出"""
    results = []
    try:
        data = json.loads(raw)
        items = data.get("items", data.get("data", []))
        if isinstance(items, dict):
            items = items.get("items", [])
        for item in items[:n]:
            if not isinstance(item, dict):
                continue
            note_card = item.get("note_card", item)
            title = note_card.get("display_title", note_card.get("title", ""))
            desc = note_card.get("desc", "")
            interact = note_card.get("interact_info", {})

            # URL 可核验性（关键）：
            # 小红书强制 xsec_token —— 裸 note_id 拼出的 /explore/<id> 会被
            # 302 拦截，无法打开（实测确认）。因此**优先使用上游返回的原始
            # URL/带 token 的链接**，只有在拿不到时才回退到裸 ID 拼接，
            # 并显式标注 url_verifiable=False，让调用方知道这条链接不可复核
            # （与 V2EX 引擎同一契约：宁可不给链接，也不给打不开的链接）。
            raw_url = (item.get("url") or item.get("note_url")
                       or note_card.get("url") or "")
            xsec = (item.get("xsec_token") or note_card.get("xsec_token") or "")
            note_id = item.get("id") or note_card.get("note_id") or ""
            if raw_url:
                url = raw_url
                verifiable = True
            elif note_id and xsec:
                url = (f"https://www.xiaohongshu.com/explore/{note_id}"
                       f"?xsec_token={xsec}")
                verifiable = True
            elif note_id:
                url = f"https://www.xiaohongshu.com/explore/{note_id}"
                verifiable = False   # 裸 ID 必被 302 拦截
            else:
                continue             # 无 ID 无 URL：不可核验，不产出

            results.append({
                "title": title[:100] + ("..." if len(title) > 100 else ""),
                "url": url,
                "snippet": (desc or title)[:300],
                "source": "xiaohongshu",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "xiaohongshu",
                    "content_type": "note",
                    "author": note_card.get("user", {}).get("nickname", ""),
                    "likes": interact.get("liked_count", 0),
                    "comments": interact.get("comment_count", 0),
                    "collects": interact.get("collected_count", 0),
                    "type": note_card.get("type", "normal"),  # normal / video
                    "note_id": note_id,
                    "has_xsec_token": bool(xsec),
                    # False = 该链接为裸 ID 拼接，打开会被平台拦截（不可复核）
                    "url_verifiable": verifiable,
                }
            })
    except (json.JSONDecodeError, ValueError):
        # 尝试文本解析
        pass
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Xiaohongshu search engine")
    parser.add_argument("action", nargs="?", default="search")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        print("[]")
        return

    results = search(args.query, args.n)
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            print(f"### {i}. {r['title']}")
            print(f"- **URL**: {r['url']}")
            print(f"- {r['snippet'][:200]}")
            print()


if __name__ == "__main__":
    main()
