#!/usr/bin/env python3
"""tweet.py — X 帖子完整打包（`argo tweet`）。

与 `argo search` 的分工：搜索**发现**推文（把推文压成一条结果参与 RRF 融合），
本命令**吸收**推文（给定链接或 ID，把整份内容原样落地）。同一条推文，搜索要
的是「一行」，打包要的是「整份」——所以它是独立命令，不是搜索结果的字段。

按需采用（默认轻、重活要显式要）：
    argo tweet "<url|id>"                    正文打到 stdout
    argo tweet "<url|id>" --json             结构化 JSON（全文+媒体 URL+互动+串/引用）
    argo tweet "<url|id>" --out DIR          落盘四件套（post.md/raw.json/manifest/CHECKLIST）
    argo tweet "<url|id>" --out DIR --media  再下载图片/视频（SSRF 守卫 + 代理感知）
    argo tweet "<url|id>" --mode thread|conversation|quotes|reposts
    argo tweet "<url|id>" --with-quotes --with-reposts --limit 50

零 Key、不碰 Cookie/登录态；媒体下载走 net_proxy.open_url（认 config.yaml 代理）
且每跳过 url_safety.check_url（拒内网/保留地址）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fxtwitter_api as fx  # noqa: E402
from cli_io import dumps, dumps_pretty  # noqa: E402
from net_proxy import open_url  # noqa: E402
from url_safety import check_url  # noqa: E402

MODES = ("status", "thread", "conversation", "quotes", "reposts")
DEFAULT_OUT_PARENT = "argo-tweet"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_name(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.\-]+", "_", s or "", flags=re.U)
    return (s or "item")[:max_len]


def download_file(url: str, dest: Path, *, timeout: float = 60) -> dict[str, Any]:
    """下载一个媒体文件到 dest。返回 {ok, path, bytes, url, error}。

    两道关与全仓出口纪律一致：先 check_url 拒掉非 http(s)/内网目标，再经
    net_proxy.open_url 出网（否则「必须走代理」的环境里会静默连不上）。
    """
    ok, reason = check_url(url)
    if not ok:
        return {"ok": False, "path": str(dest), "bytes": 0, "url": url,
                "error": f"URL 被安全策略拒绝: {reason}"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": fx.USER_AGENT, "Accept": "*/*"})
        with open_url(req, timeout=timeout) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return {"ok": True, "path": str(dest), "bytes": len(data),
                "url": url, "error": None}
    except Exception as e:  # 单张图失败不该拖垮整次打包
        return {"ok": False, "path": str(dest), "bytes": 0, "url": url,
                "error": f"{type(e).__name__}: {e}"}


def _author_handle(status: dict[str, Any], author: Any = None) -> str:
    for src in (author, status.get("author")):
        if isinstance(src, dict) and src.get("screen_name"):
            return str(src["screen_name"])
    return str(status.get("screen_name") or "unknown")


def status_to_md_block(status: dict[str, Any], *, image_rel_paths=None,
                       video_rel_paths=None, heading_level: int = 3) -> str:
    """一条推文 → markdown 区块（含本地媒体相对路径）。"""
    handle = _author_handle(status, status.get("author"))
    author = status.get("author")
    name = author.get("name") if isinstance(author, dict) else ""
    h = "#" * max(2, min(heading_level, 5))
    lines = [
        f"{h} @{handle}" + (f" ({name})" if name else ""),
        "",
        f"- **URL**: {status.get('url') or ''}",
        f"- **ID**: {status.get('id') or ''}",
        f"- **时间**: {status.get('created_at') or ''}",
        f"- **互动**: likes={status.get('likes')} "
        f"reposts={status.get('reposts') or status.get('retweets')} "
        f"replies={status.get('replies')} views={status.get('views')}",
        "",
        fx.status_text(status) or "_(无文本)_",
        "",
    ]
    if image_rel_paths:
        lines += [f"{h}# 图片", ""] + [f"![]({p})" for p in image_rel_paths] + [""]
    if video_rel_paths:
        lines += [f"{h}# 视频", ""] + [f"- [{Path(p).name}]({p})"
                                       for p in video_rel_paths] + [""]
    return "\n".join(lines)


def user_to_md_block(user: dict[str, Any], *, heading_level: int = 3) -> str:
    """转发列表里是 profile 对象，不是推文。"""
    handle = user.get("screen_name") or user.get("username") or "unknown"
    name = user.get("name") or ""
    h = "#" * max(2, min(heading_level, 5))
    lines = [
        f"{h} @{handle}" + (f" ({name})" if name else ""),
        "",
        f"- **ID**: {user.get('id') or ''}",
        f"- **粉丝**: {user.get('followers') or user.get('followers_count')}",
        f"- **关注**: {user.get('following') or user.get('friends_count')}",
        f"- **认证**: {user.get('verified') or user.get('is_blue_verified')}",
        f"- **主页**: https://x.com/{handle}",
        "",
    ]
    bio = (user.get("description") or user.get("bio") or "").strip()
    return "\n".join(lines + ([bio, ""] if bio else []))


def collect_media_jobs(status: dict[str, Any], *, video_quality: str,
                       prefix: str) -> tuple[list[dict], list[dict]]:
    """返回 (photo_jobs, video_jobs)，每项含 url / dest_rel / meta。"""
    photos = []
    for i, p in enumerate(fx.iter_photos(status), 1):
        url = p.get("url") or ""
        if not url:
            continue
        ext = ".png" if ".png" in url else ".webp" if ".webp" in url else ".jpg"
        photos.append({"url": url, "meta": p, "kind": "image",
                       "dest_rel": f"media/images/{prefix}_{i:02d}{ext}"})
    videos = []
    for i, v in enumerate(fx.iter_videos(status), 1):
        url, chosen = fx.pick_video_url(v, quality=video_quality)
        videos.append({"url": url, "meta": v, "chosen": chosen, "kind": "video",
                       "dest_rel": f"media/videos/{prefix}_{i:02d}.mp4",
                       "skip_reason": None if url else "no_downloadable_mp4"})
    return photos, videos


def _dedupe_posts(posts: list[Any]) -> list[dict]:
    """按 id 去重（thread/replies/quotes 交叉重叠是常态）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        out.append(p)
    return out


def _fetch_bundle(status_id: str, mode: str, *, timeout: float,
                  limit: int) -> dict[str, Any]:
    """按 mode 拉取，返回统一 bundle（raw/author/root_status/thread/replies/quotes/repost_users/posts）。"""
    bundle: dict[str, Any] = {
        "raw": {}, "author": None, "root_status": None, "thread": [],
        "replies": [], "quotes": [], "repost_users": [], "posts": [],
    }

    root_error: list[str] = []

    def _root() -> tuple[dict | None, Any]:
        """主帖拉不到不致命（quotes/reposts 仍可交付），但**不静默**：原因记进
        root_error，随 raw 落进产物，否则「为什么没有主帖」只能靠猜。"""
        try:
            b = fx.get_status(status_id, timeout=timeout)
            return (b.get("status") if isinstance(b.get("status"), dict) else None,
                    b.get("raw"))
        except fx.FxTwitterError as e:
            root_error.append(str(e))
            return None, None

    def _raw(raw: dict) -> dict:
        return {**raw, "status_error": root_error[0]} if root_error else raw

    if mode == "quotes":
        q = fx.get_quotes(status_id, limit=limit, timeout=timeout)
        root, root_raw = _root()
        quotes = [r for r in (q.get("results") or []) if isinstance(r, dict)]
        return {**bundle, "raw": _raw({"quotes": q.get("raw"), "status": root_raw}),
                "author": (root or {}).get("author"), "root_status": root,
                "quotes": quotes,
                "posts": _dedupe_posts(([root] if root else []) + quotes)}

    if mode == "reposts":
        r = fx.get_reposts(status_id, limit=limit, timeout=timeout)
        root, root_raw = _root()
        users = [u for u in (r.get("results") or []) if isinstance(u, dict)]
        return {**bundle, "raw": _raw({"reposts": r.get("raw"), "status": root_raw}),
                "author": (root or {}).get("author"), "root_status": root,
                "repost_users": users, "posts": [root] if root else []}

    if mode == "thread":
        b = fx.get_thread(status_id, timeout=timeout)
        root = b.get("status") if isinstance(b.get("status"), dict) else None
        thread = [i for i in (b.get("thread") or []) if isinstance(i, dict)]
        root_id = str((root or {}).get("id") or "")
        posts = ([root] if root else []) + [
            t for t in thread if str(t.get("id") or "") != root_id]
        return {**bundle, "raw": b["raw"], "author": b.get("author"),
                "root_status": root or (posts[0] if posts else None),
                "thread": thread, "posts": _dedupe_posts(posts)}

    if mode == "conversation":
        b = fx.get_conversation(status_id, timeout=timeout)
        root = b.get("status") if isinstance(b.get("status"), dict) else None
        thread = [i for i in (b.get("thread") or []) if isinstance(i, dict)]
        replies = [i for i in (b.get("replies") or []) if isinstance(i, dict)]
        return {**bundle, "raw": b["raw"], "author": b.get("author"),
                "root_status": root, "thread": thread, "replies": replies,
                "posts": _dedupe_posts(([root] if root else []) + thread + replies)}

    # status（默认）：单条；响应里带串时尽量并入
    b = fx.get_status(status_id, timeout=timeout)
    root = b.get("status") if isinstance(b.get("status"), dict) else None
    thr = b.get("thread")
    thread = [i for i in thr if isinstance(i, dict)] if isinstance(thr, list) and len(thr) > 1 else []
    root_id = str((root or {}).get("id") or "")
    posts = ([root] if root else []) + [
        t for t in thread if str(t.get("id") or "") != root_id]
    return {**bundle, "raw": b["raw"], "author": b.get("author"),
            "root_status": root, "thread": thread,
            "posts": _dedupe_posts(posts)}


def build_bundle(target: str, *, mode: str = "status", timeout: float = 15,
                 with_quotes: bool = False, with_reposts: bool = False,
                 limit: int = 20) -> tuple[str, dict[str, Any]]:
    """拉取并叠加 flags，返回 (status_id, bundle)。"""
    status_id = fx.extract_status_id(target)
    if not status_id:
        raise ValueError(f"无法解析 status id: {target!r}（给推文 URL 或纯数字 ID）")
    mode = (mode or "status").lower()
    if mode not in MODES:
        raise ValueError(f"未知 mode: {mode}（可选 {', '.join(MODES)}）")

    limit = max(1, min(int(limit), 100))
    bundle = _fetch_bundle(status_id, mode, timeout=timeout, limit=limit)
    raw_extra: dict[str, Any] = {}

    if with_quotes and mode != "quotes":
        try:
            q = fx.get_quotes(status_id, limit=limit, timeout=timeout)
            bundle["quotes"] = [r for r in (q.get("results") or []) if isinstance(r, dict)]
            raw_extra["quotes"] = q.get("raw")
        except fx.FxTwitterError as e:
            raw_extra["quotes_error"] = str(e)

    if with_reposts and mode != "reposts":
        try:
            r = fx.get_reposts(status_id, limit=limit, timeout=timeout)
            bundle["repost_users"] = [u for u in (r.get("results") or []) if isinstance(u, dict)]
            raw_extra["reposts"] = r.get("raw")
        except fx.FxTwitterError as e:
            raw_extra["reposts_error"] = str(e)

    if raw_extra:
        base = bundle.get("raw")
        bundle["raw"] = ({**base, **raw_extra} if isinstance(base, dict)
                         else {"primary": base, **raw_extra})
    bundle["_extra_quotes"] = bundle.get("quotes") or []
    bundle["_extra_repost_users"] = bundle.get("repost_users") or []
    return status_id, bundle


def _render_text(status_id: str, mode: str, bundle: dict[str, Any]) -> str:
    """默认 stdout：可读正文（全文，不截断）。"""
    root = bundle.get("root_status") or {}
    handle = _author_handle(root, bundle.get("author"))
    out = [f"@{handle} · {root.get('created_at') or '?'} · mode={mode}",
           f"{root.get('url') or 'https://x.com/i/status/' + status_id}",
           f"互动: likes={root.get('likes')} reposts={root.get('reposts') or root.get('retweets')} "
           f"replies={root.get('replies')} views={root.get('views')}",
           ""]
    posts = bundle.get("posts") or []
    for i, p in enumerate(posts, 1):
        if len(posts) > 1:
            out.append(f"--- [{i}/{len(posts)}] ---")
        out.append(status_to_md_block(p, heading_level=2))
    users = bundle.get("_extra_repost_users") or []
    if users:
        out.append(f"--- 转发用户 {len(users)} ---")
        for u in users[:50]:
            out.append(user_to_md_block(u, heading_level=3))
    photos = sum(len(fx.iter_photos(p)) for p in posts)
    videos = sum(len(fx.iter_videos(p)) for p in posts)
    if photos or videos:
        out.append("")
        out.append(f"[媒体] images={photos} videos={videos}"
                   f"（用 --out DIR --media 下载到本地）")
    return "\n".join(out)


def to_json(status_id: str, mode: str, bundle: dict[str, Any]) -> dict[str, Any]:
    """结构化输出：全文 + 媒体 URL + 互动 + 串/引用/转发，原样可消费。"""
    def _brief(st: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(st.get("id") or ""),
            "url": st.get("url"),
            "author": (st.get("author") or {}).get("screen_name") if isinstance(st.get("author"), dict) else None,
            "created_at": st.get("created_at"),
            "text": fx.status_text(st),
            "likes": st.get("likes"), "reposts": st.get("reposts") or st.get("retweets"),
            "replies": st.get("replies"), "views": st.get("views"),
            "images": [p.get("url") for p in fx.iter_photos(st) if p.get("url")],
            "videos": [v.get("url") for v in fx.iter_videos(st) if v.get("url")],
            "is_note_tweet": st.get("is_note_tweet"),
        }

    root = bundle.get("root_status") or {}
    return {
        "schema_version": "argo-tweet/1.0",
        "status_id": status_id,
        "mode": mode,
        "entry_url": root.get("url"),
        "author": bundle.get("author") if isinstance(bundle.get("author"), dict) else None,
        "post_count": len(bundle.get("posts") or []),
        "posts": [_brief(p) for p in (bundle.get("posts") or [])],
        "thread": [_brief(t) for t in (bundle.get("thread") or [])],
        "replies": [_brief(r) for r in (bundle.get("replies") or [])],
        "quotes": [_brief(q) for q in (bundle.get("quotes") or [])],
        "repost_users": [
            {"screen_name": u.get("screen_name") or u.get("username"),
             "name": u.get("name"), "followers": u.get("followers")}
            for u in (bundle.get("repost_users") or []) if isinstance(u, dict)
        ],
        "provider": "fxtwitter",
    }


def pack(target: str, *, mode: str = "status", out_dir: Path | None = None,
         download_media: bool = False, video_quality: str = "max",
         timeout: float = 15, with_quotes: bool = False,
         with_reposts: bool = False, limit: int = 20) -> dict[str, Any]:
    """完整打包：落盘 post.md / raw.json / manifest.json / CHECKLIST.md（可选媒体）。"""
    t0 = time.time()
    status_id, bundle = build_bundle(
        target, mode=mode, timeout=timeout, with_quotes=with_quotes,
        with_reposts=with_reposts, limit=limit)

    posts = bundle.get("posts") or []
    users = bundle.get("_extra_repost_users") or []
    if not posts and not users:
        raise ValueError("API 返回空（无帖子、无转发用户）")

    root = (Path(out_dir).expanduser() if out_dir
            else Path.cwd() / DEFAULT_OUT_PARENT / status_id).resolve()
    root.mkdir(parents=True, exist_ok=True)

    media_results: list[dict[str, Any]] = []
    md_parts: list[str] = []
    root_status = bundle.get("root_status") or (posts[0] if posts else None)
    handle = _author_handle(root_status or {}, bundle.get("author"))
    summary = fx.status_text(root_status or {})[:60].replace("\n", " ")

    md_parts += [f"# X 打包 · @{handle}", "",
                 f"- **入口 ID**: `{status_id}`", f"- **模式**: `{mode}`",
                 f"- **帖子条数**: {len(posts)}"]
    if bundle.get("quotes"):
        md_parts.append(f"- **引用条数**: {len(bundle['quotes'])}")
    if users:
        md_parts.append(f"- **转发用户数**: {len(users)}")
    md_parts += [f"- **打包时间**: {_now_iso()}"]
    if summary:
        md_parts.append(f"- **摘要**: {summary}")
    md_parts += ["", "---", ""]

    def render_one(st: dict[str, Any]) -> str:
        sid = str(st.get("id") or "unknown")
        photos, videos = collect_media_jobs(st, video_quality=video_quality,
                                            prefix=_safe_name(sid))
        img_rels, vid_rels = [], []
        for job in photos + videos:
            if download_media and job.get("url"):
                dest = root / job["dest_rel"]
                res = download_file(job["url"], dest,
                                    timeout=max(timeout * 4, 120))
                res.update(kind=job["kind"], status_id=sid)
                media_results.append(res)
                if res["ok"]:
                    (img_rels if job["kind"] == "image" else vid_rels).append(job["dest_rel"])
            else:
                # 未下载：ok=None 表示「没尝试」，不是成功也不是失败——否则
                # 「有 URL」会被记成 images_ok，把「按需」的账算成「已下好」。
                media_results.append({
                    "ok": None, "path": None, "bytes": 0,
                    "url": job.get("url"), "kind": job["kind"], "status_id": sid,
                    "skipped_download": True, "has_url": bool(job.get("url")),
                    "error": job.get("skip_reason"), "chosen": job.get("chosen"),
                })
        return status_to_md_block(st, image_rel_paths=img_rels,
                                  video_rel_paths=vid_rels, heading_level=3)

    def section(title: str, items: list[dict], *, as_users: bool = False) -> None:
        if not items:
            return
        md_parts.append(f"## {title}")
        md_parts.append("")
        for it in items:
            md_parts.append(user_to_md_block(it, heading_level=3) if as_users
                            else render_one(it))
            # 用 extend 不用 +=：嵌套函数里 `md_parts += [...]` 会把 md_parts
            # 变成 section 的**局部名**（列表的 += 也是赋值），前面的 append
            # 随即 UnboundLocalError——所有走 section 的 mode 会当场崩。
            md_parts.extend(["---", ""])

    if mode == "conversation":
        root_id = str((root_status or {}).get("id") or "")
        thread = [t for t in (bundle.get("thread") or [])
                  if str(t.get("id") or "") != root_id]
        tids = {str(t.get("id") or "") for t in thread}
        replies = [r for r in (bundle.get("replies") or [])
                   if str(r.get("id") or "") not in tids]
        if root_status:
            section("主帖", [root_status])
        section("Thread", thread)
        section("回复", replies)
    elif mode == "quotes":
        if root_status:
            section("主帖", [root_status])
        section("引用", bundle.get("quotes") or [])
    elif mode == "reposts":
        if root_status:
            section("主帖", [root_status])
        section("转发用户", users, as_users=True)
    elif mode == "thread":
        section("Thread", posts)
    else:
        for st in posts:
            md_parts.append(render_one(st))
            md_parts += ["---", ""]

    if with_quotes and mode != "quotes" and bundle.get("quotes"):
        section("引用", bundle["quotes"])
    if with_reposts and mode != "reposts" and users:
        section("转发用户", users, as_users=True)

    post_md = root / "post.md"
    post_md.write_text("\n".join(md_parts), encoding="utf-8")
    raw_path = root / "raw.json"
    raw_path.write_text(dumps_pretty(bundle.get("raw")), encoding="utf-8")

    def _ok(kind: str, flag: bool) -> list[dict]:
        """只统计**真尝试过下载**的条目（skipped 的 ok=None，两侧都不算）。"""
        return [m for m in media_results
                if m.get("kind") == kind and not m.get("skipped_download")
                and bool(m.get("ok")) is flag]

    images_ok, images_fail = _ok("image", True), _ok("image", False)
    videos_ok, videos_fail = _ok("video", True), _ok("video", False)
    text_ok = any(fx.status_text(p) for p in posts) or bool(users)
    quotes = bundle.get("quotes") or []

    manifest = {
        "schema_version": "argo-tweet/1.0",
        "status_id": status_id, "mode": mode, "packed_at": _now_iso(),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "entry_url": (root_status or {}).get("url"),
        "author": bundle.get("author") if isinstance(bundle.get("author"), dict) else None,
        "post_count": len(posts),
        "post_ids": [str(p.get("id")) for p in posts],
        "quote_count": len(quotes),
        "quote_ids": [str(q.get("id")) for q in quotes],
        "repost_user_count": len(users),
        "repost_handles": [str(u.get("screen_name") or u.get("username") or "")
                           for u in users],
        "with_quotes": with_quotes or mode == "quotes",
        "with_reposts": with_reposts or mode == "reposts",
        "limit": limit, "download_media": download_media,
        "video_quality": video_quality, "media": media_results,
        "counts": {
            "posts": len(posts), "quotes": len(quotes), "repost_users": len(users),
            "images_ok": len(images_ok), "images_fail": len(images_fail),
            "videos_ok": len(videos_ok), "videos_fail": len(videos_fail),
            "text_ok": text_ok,
        },
        "paths": {"root": str(root), "post_md": str(post_md),
                  "raw_json": str(raw_path)},
        "provider": "fxtwitter",
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(dumps_pretty(manifest), encoding="utf-8")

    n_images = sum(1 for m in media_results if m.get("kind") == "image")
    n_videos = sum(1 for m in media_results if m.get("kind") == "video")

    def _media_check(label: str, n: int, ok: list, fail: list) -> tuple:
        """「没下载」与「没媒体」要分开报——前者是用户选择，后者是客观事实。"""
        if not download_media:
            return (label, True,
                    f"未下载（{n} 待下，--media 开启）" if n else "无媒体（skip）")
        return (label, not fail,
                "无媒体（skip）" if n == 0 else f"ok={len(ok)} fail={len(fail)}")

    checks = [
        ("正文/内容非空", text_ok, "至少一条有 text 或有效列表"),
        ("raw.json 已写", raw_path.is_file() and raw_path.stat().st_size > 2, str(raw_path)),
        ("post.md 已写", post_md.is_file() and post_md.stat().st_size > 10, str(post_md)),
        _media_check("图片下载", n_images, images_ok, images_fail),
        _media_check("视频下载", n_videos, videos_ok, videos_fail),
    ]
    all_pass = all(c[1] for c in checks)
    cl = [f"# CHECKLIST · {status_id}", "",
          f"- 打包时间: {_now_iso()}", f"- 模式: {mode}",
          f"- 结果: **{'PASS' if all_pass else 'PARTIAL'}**", "",
          "| 项 | 状态 | 说明 |", "|----|------|------|"]
    cl += [f"| {n} | {'✅' if ok else '❌'} | {d} |" for n, ok, d in checks]
    if images_fail or videos_fail:
        cl += ["", "## 失败明细", ""]
        cl += [f"- `{m.get('kind')}` {m.get('url') or m.get('path')}: {m.get('error')}"
               for m in images_fail + videos_fail]
    checklist_path = root / "CHECKLIST.md"
    checklist_path.write_text("\n".join(cl + [""]), encoding="utf-8")
    manifest["paths"]["checklist"] = str(checklist_path)
    manifest["checklist_pass"] = all_pass
    manifest_path.write_text(dumps_pretty(manifest), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="argo tweet",
        description="X 帖子完整打包（FxTwitter，零 Key；搜索用 argo search）")
    ap.add_argument("target", help="推文 URL 或纯数字 status id")
    ap.add_argument("--mode", choices=list(MODES), default="status",
                    help="status（默认，带串时并入）| thread | conversation | quotes | reposts")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"落盘目录（默认 ./{DEFAULT_OUT_PARENT}/<id>）；给了就写四件套")
    ap.add_argument("--media", action="store_true",
                    help="下载图片/视频（需 --out；默认只记 URL 不下载）")
    ap.add_argument("--video-quality", choices=["max", "min", "720"], default="max")
    ap.add_argument("--with-quotes", action="store_true", help="额外附加「引用」分区")
    ap.add_argument("--with-reposts", action="store_true", help="额外附加「转发用户」分区")
    ap.add_argument("--limit", type=int, default=20, help="quotes/reposts 上限（默认 20，上限 100）")
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON（全文+媒体 URL+互动）")
    args = ap.parse_args(argv)

    try:
        if args.out is not None or args.media:
            manifest = pack(
                args.target, mode=args.mode, out_dir=args.out,
                download_media=args.media, video_quality=args.video_quality,
                timeout=args.timeout, with_quotes=args.with_quotes,
                with_reposts=args.with_reposts, limit=args.limit)
            if args.json:
                print(dumps(manifest))
            else:
                c = manifest["counts"]
                extra = "".join(
                    f" {k}={c[k]}" for k in ("quotes", "repost_users") if c.get(k))
                print(f"packed → {manifest['paths']['root']}")
                print(f"posts={c['posts']}{extra} "
                      f"images={c['images_ok']}/{c['images_ok'] + c['images_fail']} "
                      f"videos={c['videos_ok']}/{c['videos_ok'] + c['videos_fail']} "
                      f"checklist={'PASS' if manifest.get('checklist_pass') else 'PARTIAL'} "
                      f"({manifest['elapsed_ms']}ms)")
                for key in ("post_md", "raw_json", "checklist"):
                    print(f"  {key:<10} {manifest['paths'][key]}")
            return 0

        status_id, bundle = build_bundle(
            args.target, mode=args.mode, timeout=args.timeout,
            with_quotes=args.with_quotes, with_reposts=args.with_reposts,
            limit=args.limit)
        if not bundle.get("posts") and not bundle.get("_extra_repost_users"):
            print("API 返回空（无帖子、无转发用户）", file=sys.stderr)
            return 1
        if args.json:
            print(dumps(to_json(status_id, args.mode, bundle)))
        else:
            print(_render_text(status_id, args.mode, bundle))
        return 0
    except ValueError as e:
        print(f"argo tweet: {e}", file=sys.stderr)
        return 1
    except fx.FxTwitterError as e:
        print(f"argo tweet: FxTwitter 错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
