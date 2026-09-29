#!/usr/bin/env python3
"""local_seek.py — 本地文件命中（--include-local 用）。

从 search.py 拆出：本地搜索的宽泛根守卫、子进程调用、结果缓存。
search.py 的 _run_local_seek 调用点改为从本模块导入。

三个边界（2026-09-27 实测补齐，此前会拖垮整次搜索）：

1. **宽泛根直接不查**（见 `_local_seek_dir`）：cwd 是 home 或根目录时
   返回 None 并跳过。这是 22.6 s 事故的根因修复，「显式传 --path」
   解决不了（子进程继承 cwd，显式传与默认等价）。
2. **超时从 20 s 收到 _LOCAL_SEEK_TIMEOUT_S**。本地命中是增强项，不是
   主结果；它的等待上限必须显著小于用户对一次搜索的耐心。
3. **TimeoutExpired 必须在这里吞掉**。此前没有 try，该异常一路冒泡到
   CLI/MCP 调用方（实测栈打到 `subprocess.py:1268`）；上层虽有兜底，
   但本地命中失败本就不该让整次搜索承担异常路径。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# 本机文件命中子进程超时（秒）。**必须显著小于 fast/auto 的总预算**：
# 本地命中是尾部增强项，不是主结果，它的等待不配与搜索引擎同等。
_LOCAL_SEEK_TIMEOUT_S = 3.0

# 「宽泛根」判定：这些目录**不能**当作本地检索范围。
# 跨平台补齐（2026-09-29）：/mnt（WSL 挂载根）、/cygdrive（Cygwin）——
# 此前是 macOS 视角清单，Windows/WSL 的等价宽根漏网。
_BROAD_LOCAL_ROOTS = ("/", "/tmp", "/private/tmp", "/var", "/usr", "/System",
                      "/Library", "/Applications", "/Volumes", "/Network",
                      "/mnt", "/cygdrive")

# Windows 盘符根（C:\ / C:/ / C:）：与上面清单同义，但形态是「字母+冒号」，
# 用模式判而不用枚举（盘符有 26 个）。
_WIN_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]?$")

# 本地文件命中缓存：key = (query, max_n, target) → (写入时间, 结果列表)。
# 文件修改后 TTL 内可能返回旧结果，对本地文件搜索可接受（文件不频繁修改）。
# 容量上限 64 条，超出时整表清空（低频增强项，LRU 的简单替代）。
_LOCAL_SEEK_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LOCAL_SEEK_TTL_S = 300.0
_LOCAL_SEEK_CACHE_MAX = 64

# H2：seek 模块按解析后真实路径缓存（importlib 显式文件定位，不占通用
# 模块名、不污染 sys.path），进程内只加载一次（实测 0.5ms）。
_SEEK_MODULE_CACHE: dict[str, Any] = {}

# H3：跨进程落盘缓存。CLI 每次新进程，进程内缓存救不了 CLI 重复查询；
# 落盘缓存让 TTL 内的重复查询（同 query+dir+max）零成本。陈旧语义与
# 进程内缓存一致（300s TTL——文件不频繁修改场景可接受）。写侧原子替换
# + 全异常吞掉：缓存永不破坏搜索。
_SEEK_DISK_CACHE_MAX = 64


def _seek_disk_cache_path() -> Path:
    try:
        import argo_paths
        return Path(argo_paths.state_path("local_seek_cache.json"))
    except Exception:
        return Path(os.path.expanduser(
            "~/.cache/unified-search/local_seek_cache.json"))


def _seek_disk_cache_get(key: str) -> list[dict[str, Any]] | None:
    try:
        p = _seek_disk_cache_path()
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        rec = data.get(key) if isinstance(data, dict) else None
        if not isinstance(rec, dict):
            return None
        if time.time() - rec.get("ts", 0) >= _LOCAL_SEEK_TTL_S:
            return None
        results = rec.get("results")
        return results if isinstance(results, list) else None
    except Exception:
        return None


def _seek_disk_cache_put(key: str, results: list[dict[str, Any]]) -> None:
    try:
        p = _seek_disk_cache_path()
        now = time.time()
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}  # 损坏文件：整表重建（缓存可牺牲，搜索不可牺牲）
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and now - v.get("ts", 0) < _LOCAL_SEEK_TTL_S}
        data[key] = {"ts": now, "results": results}
        if len(data) > _SEEK_DISK_CACHE_MAX:
            for k in sorted(data, key=lambda k: data[k].get("ts", 0)
                            )[:len(data) - _SEEK_DISK_CACHE_MAX]:
                data.pop(k, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)  # 原子替换：并发写不会留下半截文件
    except Exception:
        pass


def _is_broad_local_root(path: str) -> bool:
    """path 是否是「不能当作本地检索范围」的宽泛根。

    判据从 _local_seek_dir 提取为可复用函数：MCP 的 argo_local_search
    默认 path="~"，与 CLI 的 cwd 缺省是同一类事故形态（2026-09-27 实测
    MCP 侧对整个 home 跑 rg，30 s 后报错返回），两侧必须共用同一判据。
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return True
    home = os.path.realpath(os.path.expanduser("~"))
    if real == home:
        return True
    # 盘符根同时查原始与 realpath 形态：realpath 在 POSIX 上会把 "C:\"
    # 当相对路径拼接（cwd + "/C:\"），只有 raw 形态还能保住判据。
    if _WIN_DRIVE_ROOT_RE.match(path) or _WIN_DRIVE_ROOT_RE.match(real):
        return True
    for root in _BROAD_LOCAL_ROOTS:
        if real == root or real == os.path.realpath(root):
            return True
    return False


def _local_seek_dir() -> str | None:
    """本地检索的适用范围；宽泛根返回 None（调用方据此跳过本地命中）。

    宁可返回 None 也不返回一个「象征性收窄」的路径：本地命中是增强项，
    在范围不合理时**不产出**比产出噪音 + 20 s 等待更符合用户预期。
    用户想搜特定树时 cd 到目标目录，或用 local-seek 的 seek.py 显式给
    --path（网络类查询另有 argo search --local-first 走本地引擎聚合），
    那条路径不受此守卫影响。
    """
    cwd = os.getcwd()
    if _is_broad_local_root(cwd):
        return None
    return cwd


def _run_local_seek(query: str, max_n: int = 5,
                    search_dir: str | None = None) -> list[dict[str, Any]]:
    """本机文件命中（include-local 用）：进程内调 seek.run_query，JSON 并入。

    fast/budget 模式下由 super_search 自动调用（_resolve_include_local），
    auto/deep 需显式 --include-local / MCP include_local。评分与
    MCP argo_local_search 同口径（0.9 精确 / 0.7 扩展）。
    进程内失败回退子进程形态（_seek_query_subprocess），能力不回退。
    """
    target = search_dir or _local_seek_dir()
    if not target:
        # 宽泛根：不查，但必须留下可归因的痕迹（docstring 第 1 条）。
        # 「跳过」与「查了没有」在返回值上都是空列表，但含义完全不同——
        # 静默跳过会让用户以为本地没匹配，而事实是根本没查。返回一个
        # 占位「结果」又会污染 results（它只是个说明，不是命中），所以
        # 说明走 stderr：与 CLI 既有的 include-local 异常提示同一条通道。
        sys.stderr.write(
            "  [include-local] 当前目录过宽（home / 根目录），本地检索会扫全盘，已跳过；"
            "要搜本机文件请 cd 到目标目录后重试，或用 local-seek 的 seek.py --path <目录>\n")
        return []

    # 缓存：同 query+target 在 TTL 内直接命中，避免重复冷启动 seek.py 子进程。
    # 文件修改后 TTL 内可能返回旧结果，对本地文件搜索可接受（文件不频繁修改）。
    cache_key = f"{query}|{max_n}|{target}"
    cached = _LOCAL_SEEK_CACHE.get(cache_key)
    if cached is not None and time.time() - cached[0] < _LOCAL_SEEK_TTL_S:
        return cached[1]

    # H3：跨进程落盘缓存（CLI 每次新进程，进程内缓存救不了 CLI 重复查询；
    # MCP 常驻进程通常在内存层就命中，落盘层对它是兜底）。命中后回填内存层。
    disk_hit = _seek_disk_cache_get(cache_key)
    if disk_hit is not None:
        if len(_LOCAL_SEEK_CACHE) >= _LOCAL_SEEK_CACHE_MAX:
            _LOCAL_SEEK_CACHE.clear()
        _LOCAL_SEEK_CACHE[cache_key] = (time.time(), disk_hit)
        return disk_hit

    # 安装感知 + 唯一来源：委托 seek_locator 统一发现 local-seek/scripts/seek.py
    # （打包子技能优先，ARGO_LOCAL_SEEK_PATH / ARGO_LOCAL_SEEK_ROOTS 承载自定义/遗留）。
    # 不硬编码 ~/.agents/skills|~/.claude/skills 主机路径（SKILL.md 明令禁止）。
    from seek_locator import resolve_seek_py
    seek_py = resolve_seek_py()
    if not seek_py or not os.path.isfile(seek_py):
        return []

    # H2（2026-09-29）：进程内直调 seek.run_query——省掉子进程解释器启动
    # （实测 30-110ms/次，子进程总成本 60-140ms vs 进程内 ~30ms，落点即 rg
    # 本体）。seek 模块 stdlib-only，import 实测 0.5ms；time_budget 下传到
    # 内部 rg/fd 子进程（子进程可硬杀，线程不可杀——不传会占死单线程 executor）。
    # 进程内失败（模块加载/执行异常）回退子进程路径：能力不回退，只是慢。
    payload = None
    try:
        payload = seek_query_payload(query, target, max_n,
                                     time_budget=_LOCAL_SEEK_TIMEOUT_S)
    except Exception:
        payload = None
    if payload is None:
        payload = _seek_query_subprocess(seek_py, query, target, max_n)
    if payload is None:
        return []
    out = _payload_to_hits(payload, max_n, cache_key)
    _seek_disk_cache_put(cache_key, out)  # H3：写穿到落盘层
    return out


def _load_seek_module(seek_py: str):
    """按解析后真实路径加载 seek 模块（importlib 显式文件定位，不污染
    sys.path、不占通用模块名），按路径缓存——ARGO_LOCAL_SEEK_PATH 指向
    不同文件时各自独立加载。失败返回 None。"""
    mod = _SEEK_MODULE_CACHE.get(seek_py)
    if mod is not None:
        return mod
    import importlib.util
    spec = importlib.util.spec_from_file_location("argo_seek_impl", seek_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SEEK_MODULE_CACHE[seek_py] = mod
    return mod


def seek_query_payload(query: str, target: str, max_n: int,
                       exact: bool = False, time_budget: float | None = None
                       ) -> dict[str, Any] | None:
    """H2 公共入口：进程内调 seek.run_query 取 JSON payload。

    include-local（_run_local_seek）与 MCP argo_local_search 共用——两条
    调用路径同一实现（单一来源）。失败/无匹配返回 None，调用方自行回退
    （子进程形态或直接报无结果）。
    """
    from seek_locator import resolve_seek_py
    seek_py = resolve_seek_py()
    if not seek_py or not os.path.isfile(seek_py):
        return None
    mod = _load_seek_module(seek_py)
    if mod is None:
        return None
    argv = [query, "--json", "--max", str(max(max_n, 1)), "--path", target]
    if exact:
        argv.append("--exact")
    text, rc = mod.run_query(argv, time_budget=time_budget)
    if rc != 0:
        # rc=1 是「搜了没命中」，不是「真失败」——区分两者避免子进程回退
        # 把同一搜索再跑一遍（实测 787ms vs 命中路径 12ms，双跑税 ~775ms）。
        # 无命中返回空结果（不回退）；真失败返回 None（调用方回退子进程）。
        if rc == 1 and not text.strip():
            return {"results": [], "total": 0}
        return None
    if not text.strip():
        return None
    return json.loads(text)


def _seek_query_subprocess(seek_py: str, query: str, target: str,
                           max_n: int) -> dict[str, Any] | None:
    """子进程形态取 payload（H2 前的唯一路径，现为进程内失败的回退）。"""
    import subprocess as _sp
    cmd = [sys.executable, seek_py, query, "--json", "--max", str(max(max_n, 1)),
           "--path", target]
    try:
        r = _sp.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_LOCAL_SEEK_TIMEOUT_S,
            env={**os.environ, "PYTHONUTF8": "1"},  # 子进程是自家 seek.py，双向显式 UTF-8
        )
    except _sp.TimeoutExpired:
        return None  # 本地命中是增强项：超时即弃，不冒泡（docstring 第 2、3 条）
    except OSError:
        return None  # 解释器/脚本不可执行等环境问题：同样不该拖垮主搜索
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _payload_to_hits(payload: dict[str, Any], max_n: int,
                     cache_key: str) -> list[dict[str, Any]]:
    """seek payload → include-local 结果条目（评分与 MCP argo_local_search
    同口径：0.9 精确命中 / 0.7 扩展召回），并写入进程内缓存。"""
    hits = payload.get("results") or payload.get("files") or []
    seek_mode = payload.get("mode", "fast")
    hit_score = 0.9 if seek_mode == "fast" else 0.7
    out = []
    for h in hits[:max_n]:
        if not isinstance(h, dict):
            continue
        path = h.get("path") or h.get("file") or ""
        line = h.get("line") or h.get("lineno") or 1
        url = f"file://{path}" + (f"#{line}" if str(line).isdigit() else "")
        out.append({
            "title": path,
            "url": url,
            "snippet": (h.get("snippet") or h.get("text") or h.get("line_text") or "")[:160],
            "source": "local_files",
            "score": hit_score,
            "kind": "local",
        })
    if len(_LOCAL_SEEK_CACHE) >= _LOCAL_SEEK_CACHE_MAX:
        _LOCAL_SEEK_CACHE.clear()
    _LOCAL_SEEK_CACHE[cache_key] = (time.time(), out)
    return out
