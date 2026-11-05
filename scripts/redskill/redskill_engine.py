#!/usr/bin/env python3
"""redskill_engine.py — 小红书 REDSkill 排行榜与全量技能检索（argo 引擎，type=cli）

数据源: https://cowork.xiaohongshu.com/s/redskill-rank/data.json
  - 公开静态 JSON（14.6MB），每日 08:05 更新，无鉴权
  - 结构: dataDate / genTime / useList(使用榜100) / newList(7天新增100)
          / todayList(当日新增100) / authorList(作者发布榜100) / allSkills(全量47650)
  - 注意: 页面搜索框为纯前端过滤（无 ?q= 深链），榜单技能才带 note_id

缓存: ~/.cache/argo-redskill/data.json（按 dataDate 判断过期，过期自动刷新）

用法:
  python3 redskill_engine.py search "小红书封面" -n 5
  python3 redskill_engine.py rank [use|new|today|author] -n 10
  python3 redskill_engine.py update
  python3 redskill_engine.py stats

输出: JSON {"results": [{title,url,snippet,score,source,...}]}（argo cli builder 兼容）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 复用 scripts/ 的原子写唯一来源（本文件在 scripts/redskill/ 子目录，
# 父目录不在 sys.path 上，需显式加）。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import argo_paths  # noqa: E402
from net_proxy import open_url  # noqa: E402  出口调度唯一入口（issue #13 同类修复）

DATA_URL = "https://cowork.xiaohongshu.com/s/redskill-rank/data.json"
PAGE_URL = "https://cowork.xiaohongshu.com/s/redskill-rank/"
EXPLORE_URL = "https://www.xiaohongshu.com/explore/{}"
SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={}"
PROFILE_URL = "https://www.xiaohongshu.com/user/profile/{}"
def _cache_dir() -> Path:
    """缓存根：模块属性（测试 patch 的真名）优先，否则按当前 env 解析。"""
    patched = globals().get("CACHE_DIR")
    if patched is not None:
        return patched
    return Path(os.environ.get("ARGO_REDSKILL_CACHE")
                or (argo_paths.platform_cache_default() / "argo-redskill"))


def _cache_file() -> Path:
    return _cache_dir() / "data.json"


def __getattr__(name: str):
    """`CACHE_DIR`/`CACHE_FILE` 按当前 ARGO_REDSKILL_CACHE 解析（PEP 562，290d3d8 同口径）。

    模块内裸名查找不触发本函数（LOAD_GLOBAL 语义），函数体一律走
    _cache_dir()/_cache_file()；这里只维持外部属性访问契约。
    """
    if name == "CACHE_DIR":
        return _cache_dir()
    if name == "CACHE_FILE":
        return _cache_file()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
USER_AGENT = "argo-search/1.0 (+https://github.com/taxueseek/argo; redskill)"
DOWNLOAD_TIMEOUT = 120  # 14.6MB 全量，首次下载需要更长时间

RANK_LISTS = {
    "use": "useList",
    "new": "newList",
    "today": "todayList",
    "author": "authorList",
}

# 「非可安装」这个关键信息为什么进摘要前缀，而不是 metadata：
# argo 的 CLI builder 用 image_ops._cli_result_row 做字段白名单，只保留
# title/url/snippet/source/published_at + 图片字段——metadata 与 upstream
# **都不透传**（实测 redskill 输出带 metadata，Argo 结果里是 null）。
# snippet 是 CLI 引擎唯一能自由填充的字段，故把使用方式写在最前面。
_USAGE_PREFIX = "【小红书技能·APP 内使用】"
RANK_LABELS = {
    "use": "使用人数榜",
    "new": "7天新增榜",
    "today": "当日新增榜",
    "author": "作者发布榜",
}

# 中文技能名可能带 .skill 后缀等，检索时归一化
_NAME_NORM_RE = re.compile(r"[\s._\-]+")


def _log(msg: str) -> None:
    print(f"[redskill] {msg}", file=sys.stderr)


def _http_get(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with open_url(req, timeout=timeout) as resp:
        return resp.read()


def _load_data(force: bool = False) -> dict[str, Any]:
    """加载数据：缓存新鲜则直接用，过期或缺失则下载。

    新鲜判定：dataDate 距今 <= 2 天（genTime 每日 08:05，允许跨一天缓冲）。
    """
    cached = _read_cache()
    if not force and cached:
        data_date = cached.get("dataDate", "")
        if _is_fresh(data_date):
            return cached
        _log(f"缓存过期（dataDate={data_date}），刷新中…")
    return _download(force=force)


def _is_fresh(data_date: str) -> bool:
    if not data_date:
        return False
    try:
        y, m, d = (int(x) for x in data_date.split("-"))
        from datetime import date

        gap = (date.today() - date(y, m, d)).days
        return gap <= 2
    except Exception:
        return False


def _read_cache() -> dict[str, Any] | None:
    try:
        cf = _cache_file()
        if cf.exists() and cf.stat().st_size > 0:
            with open(cf, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        _log(f"缓存读取失败: {e}")
    return None


def _download(force: bool = False) -> dict[str, Any]:
    cache_file = _cache_file()
    _cache_dir().mkdir(parents=True, exist_ok=True)
    raw = _http_get(DATA_URL)
    data = json.loads(raw.decode("utf-8"))
    if "allSkills" not in data:
        raise ValueError("data.json 结构异常：缺少 allSkills")
    # 原子写走 argo_paths 唯一来源（唯一 tmp 名）；旧实现固定 `.tmp` 名，
    # 并发刷新（手动 update 撞上搜索自动刷新）会互相搬走临时文件而写失败。
    # 14.6MB 缓存用 indent=None 省掉格式化的时间与体积。
    argo_paths.atomic_write_json(cache_file, data, indent=None)
    if force:
        _log(f"已强制刷新缓存: {cache_file} ({cache_file.stat().st_size / 1e6:.1f}MB, dataDate={data.get('dataDate')})")
    return data


def _note_map(data: dict[str, Any]) -> dict[int, str]:
    """从三个榜单合并 skill_id -> note_id 映射（allSkills 无 note_id）。"""
    m: dict[int, str] = {}
    for key in ("useList", "newList", "todayList"):
        for item in data.get(key, []):
            sid = item.get("skill_id")
            note = item.get("note_id")
            if sid and note:
                m[int(sid)] = note
    return m


def _tokenize(query: str) -> list[str]:
    """切词：英文/数字按空白与连字符，中文保留整串 + 逐字（用于子串覆盖）。"""
    q = query.strip().lower()
    tokens = re.split(r"[\s,，。；;、/\\|:：().（）]+", q)
    tokens = [t for t in tokens if t]
    return tokens


def _search_all(data: dict[str, Any], query: str, n: int) -> list[dict[str, Any]]:
    """全量技能关键词检索（对比 allSkills 的 skill_name + skill_description）。"""
    all_skills = data.get("allSkills", [])
    note_map = _note_map(data)
    # 榜单热力榜：useList skill_id -> 排名（用于热门加成）。
    # 单条记录缺 skill_id 不致命，跳过而非让整个搜索抛错
    hot_rank: dict[int, int] = {}
    for i, item in enumerate(data.get("useList", [])):
        sid = item.get("skill_id")
        if sid:
            hot_rank[int(sid)] = i

    q = query.strip().lower()
    tokens = _tokenize(q) or [q]
    no_space_q = re.sub(r"[\s]+", "", q)

    scored: list[tuple[float, dict[str, Any]]] = []
    for s in all_skills:
        name = str(s.get("skill_name", ""))
        desc = str(s.get("skill_description", ""))
        name_l = name.lower()
        desc_l = desc.lower()
        score = 0.0
        # 整串命中
        if q in name_l:
            score += 10.0
        elif q in desc_l:
            score += 6.0
        # 去空白串命中（中文查询常见「小红书封面」）
        if no_space_q and no_space_q != q and (no_space_q in name_l or no_space_q in desc_l):
            score += 6.0
        # 子词命中
        hit_terms = 0
        for t in tokens:
            if t in name_l:
                score += 3.0
                hit_terms += 1
            elif t in desc_l:
                score += 1.5
                hit_terms += 1
        if hit_terms == 0:
            continue
        # 词覆盖率加成
        score += 2.0 * hit_terms / max(len(tokens), 1)
        # 热门加成（榜单技能优先）
        sid = s.get("skill_id")
        if sid and int(sid) in hot_rank:
            score += 1.0
        # 描述越短、名称越匹配，越像「直接答案」
        if score > 0:
            scored.append((score, s))

    scored.sort(key=lambda x: -x[0])
    results: list[dict[str, Any]] = []
    for score, s in scored[:n]:
        sid = s.get("skill_id")
        note = note_map.get(int(sid)) if sid else None
        name = s.get("skill_name", "")
        if note:
            url = EXPLORE_URL.format(note)
        else:
            # 无笔记链接时给站内搜索（URL 随技能名唯一，避免同 URL 去重误伤）
            url = SEARCH_URL.format(urllib.parse.quote(name))
        results.append({
            "title": name,
            "url": url,
            "snippet": f"{_USAGE_PREFIX}{str(s.get('skill_description', ''))}"[:500],
            "score": round(score, 3),
            "source": "redskill",
            "skill_id": sid,
            # 小红书技能是**提示词/方法论**，不是可安装的代码包。上游数据
            # （allSkills 52314 条）只有 skill_id / skill_name /
            # skill_description 三个字段，**零安装地址**——实测 githubUrl /
            # url / installUrl / repoUrl / note_id 全部为 0 命中；只有 300 条
            # 榜单技能带 note_id。明确标注，避免用户以为「搜到就能装」。
            "metadata": {
                "platform": "xiaohongshu",
                "installable": False,
                "note_id": note,
                "usage": "小红书站内技能：在小红书 APP 内使用，或复制描述作提示词",
            },
        })
    return results


def _rank_list(data: dict[str, Any], rank_type: str, n: int) -> list[dict[str, Any]]:
    items = data.get(RANK_LISTS[rank_type], [])
    results = []
    for i, item in enumerate(items[:n]):
        if rank_type == "author":
            results.append({
                "title": item.get("nickname", ""),
                "url": PROFILE_URL.format(item.get("author_id", "")),
                "snippet": f"发布技能 {item.get('skill_cnt', 0)} 个",
                "score": round(1.0 - i * 0.01, 3),
                "source": "redskill",
                "rank": i + 1,
                "author_id": item.get("author_id"),
            })
        else:
            note = item.get("note_id")
            url = EXPLORE_URL.format(note) if note else PAGE_URL

            snippet = f"{_USAGE_PREFIX}{str(item.get('skill_description', ''))}"[:500]
            results.append({
                "title": item.get("skill_name", ""),
                "url": url,
                "snippet": snippet,
                "score": round(1.0 - i * 0.01, 3),
                "source": "redskill",
                "rank": i + 1,
                "skill_id": item.get("skill_id"),
                "use_cnt": item.get("use_cnt") if rank_type in ("use", "today") else item.get("new7_cnt"),
                "use_users": item.get("use_users") if rank_type in ("use", "today") else None,
                # 榜单技能有 note_id（可跳笔记），但笔记里也没有「安装包」——
                # 小红书技能的使用入口是 APP 本身，不是下载。同 _search_all。
                "metadata": {
                    "platform": "xiaohongshu",
                    "installable": False,
                    "note_id": note,
                    "rank": i + 1,
                    "usage": "小红书站内技能：在小红书 APP 内使用，或复制描述作提示词",
                },
            })
    return results


def _stats(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataDate": data.get("dataDate"),
        "genTime": data.get("genTime"),
        "counts": {
            "useList": len(data.get("useList", [])),
            "newList": len(data.get("newList", [])),
            "todayList": len(data.get("todayList", [])),
            "authorList": len(data.get("authorList", [])),
            "allSkills": len(data.get("allSkills", [])),
        },
        "cache_file": str((cf := _cache_file())),
        "cache_mb": round(cf.stat().st_size / 1e6, 1) if cf.exists() else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书 REDSkill 引擎（排行榜 + 全量技能检索）")
    parser.add_argument("action", nargs="?", default="search", help="search | rank | update | stats")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--force-update", action="store_true")
    args = parser.parse_args()

    try:
        if args.action == "update":
            data = _download(force=True)
            print(json.dumps({"results": [{"title": f"{RANK_LABELS[k]} {len(data.get(v, []))} 条",
                                           "url": PAGE_URL,
                                           "snippet": f"dataDate={data.get('dataDate')} allSkills={len(data.get('allSkills', []))}",
                                           "score": 1.0, "source": "redskill"} for k, v in RANK_LISTS.items()]},
                             ensure_ascii=False))
            return 0

        if args.action == "stats":
            data = _read_cache()
            if not data:
                print(json.dumps({"results": [], "error": "无缓存，先运行 update"}), ensure_ascii=False)
                return 0
            s = _stats(data)
            print(json.dumps({"results": [{"title": f"{s['dataDate']} 全量 {s['counts']['allSkills']}",
                                           "url": PAGE_URL,
                                           "snippet": json.dumps(s, ensure_ascii=False)[:300],
                                           "score": 1.0, "source": "redskill"}]}, ensure_ascii=False))
            return 0

        data = _load_data(force=args.force_update)

        if args.action == "rank" or (args.action in RANK_LISTS and not args.query):
            rank_type = args.action if args.action in RANK_LISTS else (args.query or "use")
            if rank_type not in RANK_LISTS:
                rank_type = "use"
            results = _rank_list(data, rank_type, args.n)
        else:
            q = args.query or args.action
            if not q or q in ("search", "s"):
                print(json.dumps({"results": [], "error": "缺少查询词（用法: search <query> -n N）"}), ensure_ascii=False)
                return 0
            results = _search_all(data, q, args.n)

        print(json.dumps({"results": results}, ensure_ascii=False))
        return 0
    except urllib.error.URLError as e:
        print(json.dumps({"results": [], "error": f"下载失败: {e}（可稍后重试或先运行 update）"}),
              ensure_ascii=False)
        return 1
    except Exception as e:  # noqa: BLE001 - 引擎出口统一 JSON 化
        print(json.dumps({"results": [], "error": str(e)}), ensure_ascii=False)
        return 1


if __name__ == "__main__":
    sys.exit(main())
