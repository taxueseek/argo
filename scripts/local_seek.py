#!/usr/bin/env python3
"""local_seek.py — 本地文件命中（--include-local 用）。

从 search.py 拆出：本地搜索的宽泛根守卫、子进程调用、结果缓存。
search.py 的 _run_local_seek 调用点改为从本模块导入。

三个边界（2026-09-27 实测补齐，此前会拖垮整次搜索）：

1. **宽泛根 / 自身安装树直接不查**（见 `_local_seek_dir`）：cwd 是 home、
    根目录，或落在 argo 自身安装树内时返回 None 并跳过。前者是 22.6 s 事故
    的根因修复（「显式传 --path」解决不了：子进程继承 cwd，显式传与默认
    等价）；后者治「常驻 MCP 进程 cwd = 技能目录」导致结果被自身源码污染。
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

# 最近一次本机检索的耗时与命中数（毫秒级可观测性，进程内单槽）。
# 写侧在 _run_local_seek 收口；读侧由 search.py 在搜索收尾时取走并
# 写进 usage_log 的 query 流——「本地检索实际花了多久、带回几条」此前
# 只能靠猜，现在有账。
_LAST_LOCAL_SEEK_MS = 0
_LAST_LOCAL_SEEK_COUNT = 0
_LAST_LOCAL_SEEK_SKIPPED = False


def local_seek_last_ms() -> int:
    return _LAST_LOCAL_SEEK_MS


def local_seek_last_count() -> int:
    return _LAST_LOCAL_SEEK_COUNT


def local_seek_last_skipped() -> bool:
    return _LAST_LOCAL_SEEK_SKIPPED


def local_seek_accounting() -> dict[str, Any]:
    """最近一次本机检索的账（读侧唯一入口）。

    返回 {} 表示本地轨本次没跑（宽根无候选 / seek.py 缺失 / 被意图预判
    跳过）——空字典与「跑了但 0 命中」在语义上不同，调用方据此决定是否
    写日志字段。字段与 usage_log 的 local_seek 流、响应里的
    local_seek_ms / local_seek_count / local_seek_skipped 三键同口径。
    """
    if _LAST_LOCAL_SEEK_SKIPPED:
        return {"local_seek_skipped": True}
    return {
        "local_seek_ms": _LAST_LOCAL_SEEK_MS,
        "local_seek_count": _LAST_LOCAL_SEEK_COUNT,
    }

# 本机命中条数上限：本地命中是尾部增强项，不该挤压公网结果的排序位置。
# 调用方传入的 max_n 是「本次搜索总结果数」，而本地命中并入后会与公网结果
# 争抢同一个 results 列表；设一个固定上限（而非按 n 比例）让本地命中的
# 占比在 n 变大时自然收缩——n=10 时本地仍最多 3 条，公网结果保住主体。
# 可用 ARGO_LOCAL_SEEK_MAX_HITS 覆盖。
_LOCAL_SEEK_MAX_HITS = 3

# 搜索期告警（issue #34 第 4/7 条）：--json 模式下 stderr 会被调用方
# 2>&1 合并，任何中文告警行都会破坏 JSON 解析。告警统一进这个列表，
# search.py 组装完响应后 drain 并作为 warnings[] 字段随 JSON 返回；
# 非 --json 调用保持写 stderr 的老行为（echo=True）。
_SEARCH_WARNINGS: list[str] = []


def _note_warning(msg: str, echo: bool = True) -> None:
    """记录一条搜索期告警；echo=True 时同时写 stderr（人读模式）。"""
    _SEARCH_WARNINGS.append(msg)
    if echo:
        sys.stderr.write(msg + "\n")


def drain_search_warnings() -> list[str]:
    """取出并清空搜索期告警（search.py 在响应组装完后合并进 warnings[]）。"""
    out = _SEARCH_WARNINGS[:]
    _SEARCH_WARNINGS.clear()
    return out

# 「宽泛根」判定：这些目录**不能**当作本地检索范围。
# 跨平台补齐（2026-09-29）：/mnt（WSL 挂载根）、/cygdrive（Cygwin）——
# 此前是 macOS 视角清单，Windows/WSL 的等价宽根漏网。
_BROAD_LOCAL_ROOTS = ("/", "/tmp", "/private/tmp", "/var", "/usr", "/System",
                      "/Library", "/Applications", "/Volumes", "/Network",
                      "/mnt", "/cygdrive")

# Windows 盘符根（C:\ / C:/ / C:）：与上面清单同义，但形态是「字母+冒号」，
# 用模式判而不用枚举（盘符有 26 个）。
_WIN_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]?$")

# argo 自身安装树的根。常驻 MCP 进程的 cwd 就是它（宿主在技能目录里启动
# 服务），而 `--include-local`（fast/budget 默认开）按 cwd 取范围——于是
# 每次搜索都把本项目自己的源码与测试当作「本机命中」拼进结果。实测一次
# 查询 10 条里 5 条是 `tests/*.py`，score 0.9。它不是「宽泛根」而是
# 「自己的树」：对任何真实查询都只是噪声，故与宽泛根同等跳过。要搜本机
# 文件仍可 cd 到目标目录，或用 local-seek 的 seek.py --path <目录>。
_OWN_INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# 本地文件命中缓存：key = (query, max_n, target) → (写入时间, 结果列表)。
# 文件修改后 TTL 内可能返回旧结果，对本地文件搜索可接受（文件不频繁修改）。
# 容量上限 64 条，超出时整表清空（低频增强项，LRU 的简单替代）。
#
# TTL 300s → 3600s（2026-10-15，B-4）：本地文件命中是「同一台机器上重复问
# 同一件事」的增强项，5 分钟的窗在日常用法里几乎等于没有缓存——改一次笔记
# 之后再问同一个问题，命中率主要被 TTL 而不是被内容变化决定。代价是文件改
# 动后最长 1 小时可能返回旧命中，本地文件搜索可接受（与落盘缓存同一取舍，
# 两者共用这个常量才不会再分叉）。
_LOCAL_SEEK_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LOCAL_SEEK_TTL_S = 3600.0
_LOCAL_SEEK_CACHE_MAX = 64

# H2：seek 模块按解析后真实路径缓存（importlib 显式文件定位，不占通用
# 模块名、不污染 sys.path），进程内只加载一次（实测 0.5ms）。
_SEEK_MODULE_CACHE: dict[str, Any] = {}

# include-local 的本地搜索 worker 单例（住在被导入模块而非 search.py 顶层，
# 防 daemon runpy.run_module 重跑 search 顶层时覆写泄漏线程池）。
_LOCAL_SEEK_EXECUTOR = None


def get_local_seek_executor():
    global _LOCAL_SEEK_EXECUTOR
    if _LOCAL_SEEK_EXECUTOR is None:
        import concurrent.futures
        _LOCAL_SEEK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="local-seek")
    return _LOCAL_SEEK_EXECUTOR

# H3：跨进程落盘缓存。CLI 每次新进程，进程内缓存救不了 CLI 重复查询；
# 落盘缓存让 TTL 内的重复查询（同 query+dir+max）零成本。陈旧语义与
# 进程内缓存一致，**共用 `_LOCAL_SEEK_TTL_S`（现 3600s）**——两处各写一个字
# 面量正是它们会分叉的原因。写侧原子替换 + 全异常吞掉：缓存永不破坏搜索。
_SEEK_DISK_CACHE_MAX = 64
# 条目形状版本：跨版本改 results 条目字段时，TTL 内的旧形状条目按 miss 处理
_SEEK_DISK_SCHEMA = 1


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
        if rec.get("v") != _SEEK_DISK_SCHEMA:
            return None
        if time.time() - rec.get("ts", 0) >= _LOCAL_SEEK_TTL_S:
            return None
        results = rec.get("results")
        if not isinstance(results, list):
            return None
        # 返回拷贝：缓存持有所有权，调用方就地改写（_engine 标记等）不得污染缓存
        return [dict(r) if isinstance(r, dict) else r for r in results]
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
        data[key] = {"v": _SEEK_DISK_SCHEMA, "ts": now, "results": results}
        if len(data) > _SEEK_DISK_CACHE_MAX:
            for k in sorted(data, key=lambda k: data[k].get("ts", 0)
                            )[:len(data) - _SEEK_DISK_CACHE_MAX]:
                data.pop(k, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            import argo_paths
            argo_paths.atomic_write_json(p, data, indent=None)  # mkstemp 唯一 tmp，失败自清理
        except (ImportError, AttributeError, OSError):
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, p)  # 原子替换：并发写不会留下半截文件
    except Exception:
        pass


# 本地代码/工程/配置意图正则模式
# 判据与 local_intent._LOCAL_SIGNAL_RE 逐字一致（红绿锁见
# tests/test_local_intent.py::test_intent_pattern_matches_local_seek）。
_LOCAL_INTENT_PATTERN = re.compile(
    r"(?i)(?:\b(?:def|class|function|fn|import|export|const|var|struct|enum)\b|"
    r"\.(?:py|js|ts|json|yaml|yml|md|rs|go|c|cpp|h|sh|sql)\b|"
    r"(?:readme|config|package\.json|cargo\.toml|dockerfile|makefile)|"
    r"(?:本工程|本项目|代码库|本地文件|本地脚本|脚本文件|架构实现|函数|方法|源码|接口定义))"
)


def has_local_code_intent(query: str) -> bool:
    """是否包含本地工程/代码/配置检索意图。"""
    if not query:
        return False
    return bool(_LOCAL_INTENT_PATTERN.search(query))


def resolve_include_local(include_local: bool | None, mode: str, query: str = "",
                          domain: str | None = None) -> bool:
    """include-local 三态解析：True/False 显式，None=自动。

    双轨检索设计：任意搜索默认同时执行本地与联网检索（include_local=True），
    除非显式指定 --no-local / include_local=False。

    自动路径唯一的例外：查询命中**公域答案型域**（天气/行情/汇率/宏观/
    新闻等，判据见 local_intent）且不含本地工程意图信号时跳过——本机文件
    对这类查询是纯噪声，省下的是一次 rg/fd 遍历。显式 True 恒尊重，
    用户点名要搜就搜。
    """
    if include_local is not None:
        return bool(include_local)
    try:
        from local_intent import should_skip_local
        if should_skip_local(domain, query):
            return False
    except Exception:
        pass  # 判据失败 fail-open：宁可多搜一次本地，也不误杀
    return True


def _max_local_hits() -> int:
    """本机命中条数上限（ARGO_LOCAL_SEEK_MAX_HITS 可覆盖）。"""
    try:
        raw = os.environ.get("ARGO_LOCAL_SEEK_MAX_HITS")
        if raw is not None and raw.strip() != "":
            return max(0, int(raw))
    except (TypeError, ValueError):
        pass
    return _LOCAL_SEEK_MAX_HITS


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


def _is_own_install_root(path: str) -> bool:
    """path 是否落在 argo 自身安装树内（含其根目录本身）。"""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real == _OWN_INSTALL_ROOT or real.startswith(_OWN_INSTALL_ROOT + os.sep)


def _detect_project_root(start_dir: str | None = None) -> str | None:
    """从当前目录向上回溯探测工程根目录（根据 .git / 常见项目清单识别）。

    避免用户在项目深度子目录执行检索时，因范围过窄漏掉顶层架构文档与同级模块。
    """
    try:
        cur = Path(start_dir or os.getcwd()).resolve()
        markers = {".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"}
        for parent in [cur] + list(cur.parents):
            if any((parent / m).exists() for m in markers):
                p_str = str(parent)
                if not _is_broad_local_root(p_str) and not _is_own_install_root(p_str):
                    return p_str
    except Exception:
        pass
    return None


def _local_seek_dir() -> str | None:
    """本地检索的适用范围；宽泛根或自身安装树返回 None（据此跳过本地命中）。

    当前处于某个具体工程或其子目录时，优先自适应溯源至工程根（覆盖全局文档）；
    若处于宽泛根或自身安装树则返回 None，由调用方进入定向私域资产探测。
    """
    cwd = os.getcwd()
    if _is_broad_local_root(cwd) or _is_own_install_root(cwd):
        return None
    return _detect_project_root(cwd) or cwd


def _candidate_targeted_roots() -> list[str]:
    """宽泛根场景下的定向检索候选目录（既防全盘扫崩，又避免漏掉核心私域资产）。

    彻底消除特定机器/个人工作区硬编码（如 Documents/GPT），优先环境变量覆盖，
    自适应感知云端/容器工作区与跨平台 Agent 技能规范。
    """
    raw = os.environ.get("ARGO_LOCAL_SEEK_ROOTS", "").strip()
    if raw:
        cands = [p for p in re.split(r"[:;]", raw) if p.strip()]
        return [c for c in cands if os.path.isdir(c) and not _is_own_install_root(c) and not _is_broad_local_root(c)]

    cands: list[str] = []

    # 1. 容器与 CI/CD 环境工作区感知（Docker / K8s / Cloud Shell）
    for env_var in ("WORKSPACE", "PROJECT_DIR", "CI_PROJECT_DIR"):
        v = os.environ.get(env_var, "").strip()
        if v and os.path.isdir(v):
            cands.append(v)
    for container_root in ("/workspace", "/app", "/project"):
        if os.path.isdir(container_root):
            cands.append(container_root)

    # 2. 跨平台通用标准私域目录（基于用户 home 规范派生，零特定主机字面量）
    home = os.path.realpath(os.path.expanduser("~"))
    standard_roots = [
        os.path.join(home, ".agents", "skills"),  # Agent Skills 行业规范目录
        os.path.join(home, ".claude", "skills"),  # Claude 客户端规范目录
        os.path.join(home, ".codex", "skills"),   # Codex 客户端规范目录
        os.path.join(home, "workspace"),          # 通用工作区目录
        os.path.join(home, "projects"),           # 通用工程目录
        os.path.join(home, "notes"),              # 通用笔记目录
    ]
    cands.extend(standard_roots)

    seen = set()
    valid_roots = []
    for c in cands:
        try:
            real_c = os.path.realpath(os.path.expanduser(c))
        except OSError:
            continue
        if real_c in seen:
            continue
        seen.add(real_c)
        if os.path.isdir(real_c) and not _is_broad_local_root(real_c) and not _is_own_install_root(real_c):
            valid_roots.append(real_c)
    return valid_roots


def _run_local_seek(query: str, max_n: int = 5,
                    search_dir: str | None = None,
                    warn: bool = True, echo: bool = True) -> list[dict[str, Any]]:
    """本机文件命中（include-local 用）：进程内调 seek.run_query，JSON 并入。

    双轨并发支持：非宽泛根搜当前项目；处于宽泛根时定向扫描高频私域目录。
    """
    global _LAST_LOCAL_SEEK_MS, _LAST_LOCAL_SEEK_COUNT, _LAST_LOCAL_SEEK_SKIPPED
    # 用 time.time 而非 perf_counter：本模块的时间源是 time（TTL 判定、
    # 落盘缓存共用），换钟会让「耗时」与「缓存新鲜度」在测试冻结时钟时
    # 分叉（test_b4_knobs 的 _FrozenClock 只冻 time）。差值语义不变。
    _t0 = time.time()
    _LAST_LOCAL_SEEK_SKIPPED = False
    target = search_dir or _local_seek_dir()
    targets = [target] if target else _candidate_targeted_roots()
    if not targets:
        _LAST_LOCAL_SEEK_SKIPPED = True
        if warn:
            _note_warning(
                "  [include-local] 当前目录处于宽泛根且未发现预设私域资产目录，已跳过本地检索；"
                "要搜特定目录请指定 --path 或切换到目标目录重试",
                echo)
        _LAST_LOCAL_SEEK_MS = int((time.time() - _t0) * 1000)
        return []

    # seek.py 存在性检查**下沉到缓存未命中分支**（2026-11-10）：缓存命中不
    # 需要 seek.py——把检查前置会让「子技能未装/工具暂时缺失」时 TTL 内的缓存
    # 结果也拿不到，而缓存的全部意义正是省掉工具调用。前置检查还让
    # test_b4_knobs 的进程内缓存 TTL 用例无法验证（它故意 mock 掉 seek.py
    # 来短路真检索，结果连缓存都读不到）。
    from seek_locator import resolve_seek_py
    seek_py = resolve_seek_py()
    _seek_py_ok = bool(seek_py) and os.path.isfile(seek_py)

    combined_hits: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    _cap = min(max_n, _max_local_hits())

    for tgt in targets:
        cache_key = f"{query}|{max_n}|{tgt}"
        cached = _LOCAL_SEEK_CACHE.get(cache_key)
        if cached is not None and time.time() - cached[0] < _LOCAL_SEEK_TTL_S:
            hits = cached[1]
        else:
            disk_hit = _seek_disk_cache_get(cache_key)
            if disk_hit is not None:
                if len(_LOCAL_SEEK_CACHE) >= _LOCAL_SEEK_CACHE_MAX:
                    _LOCAL_SEEK_CACHE.clear()
                _LOCAL_SEEK_CACHE[cache_key] = (time.time(), disk_hit)
                hits = disk_hit
            else:
                if not _seek_py_ok:
                    # 缓存未命中 + 工具缺失：本次无法检索（缓存已是最后手段）。
                    # 不在这里 return——多目标场景下其余目标可能仍有缓存。
                    continue
                payload = None
                try:
                    payload = seek_query_payload(query, tgt, max_n,
                                                 time_budget=_LOCAL_SEEK_TIMEOUT_S,
                                                 warn=warn, echo=echo)
                except Exception:
                    payload = None
                if payload is None:
                    payload = _seek_query_subprocess(seek_py, query, tgt, max_n)
                if payload is None or payload.get("_error"):
                    hits = []
                else:
                    hits = _payload_to_hits(payload, max_n, cache_key)
                    _seek_disk_cache_put(cache_key, hits)

        for h in hits:
            u = h.get("url")
            if u not in seen_urls:
                seen_urls.add(u)
                combined_hits.append(h)
                if len(combined_hits) >= _cap:
                    break
        if len(combined_hits) >= _cap:
            break

    # 工具缺失且**所有**目标都无缓存命中：此时确实是「检索未执行」，必须
    # 留可归因说明（不能与「查了没有」同形）。缓存命中的目标不算——它们
    # 已经交付了结果，只是没走工具。
    if not _seek_py_ok and not combined_hits:
        _LAST_LOCAL_SEEK_SKIPPED = True
        if warn:
            _note_warning(
                "  [include-local] 未找到 local-seek 的 seek.py 且无缓存命中，"
                "本机文件检索未执行", echo)

    _LAST_LOCAL_SEEK_MS = int((time.time() - _t0) * 1000)
    _LAST_LOCAL_SEEK_COUNT = len(combined_hits)
    return combined_hits[:_cap]


def _load_seek_module(seek_py: str):
    """按解析后真实路径加载 seek 模块（importlib 显式文件定位，不占通用
    模块名），按路径缓存——ARGO_LOCAL_SEEK_PATH 指向不同文件时各自独立
    加载。失败返回 None。

    seek.py 模块级有 sibling 导入（from seek_rank import ...），而
    spec_from_file_location 不像脚本运行那样把文件所在目录放进 sys.path
    ——干净进程里 exec_module 必 ModuleNotFoundError，进程内路径名存实亡
    （2026-10-31 实测：CI 与本机 sys.path 污染顺序不同，同一条测试一边绿
    一边红）。加载前把 seek.py 所在目录补进 sys.path（去重），进程内加载
    从此确定性可用；模块名仍走 argo_seek_impl，不占通用名。"""
    mod = _SEEK_MODULE_CACHE.get(seek_py)
    if mod is not None:
        return mod
    import importlib.util
    seek_dir = os.path.dirname(os.path.abspath(seek_py))
    if seek_dir and seek_dir not in sys.path:
        sys.path.insert(0, seek_dir)
    spec = importlib.util.spec_from_file_location("argo_seek_impl", seek_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SEEK_MODULE_CACHE[seek_py] = mod
    return mod


def seek_query_payload(query: str, target: str, max_n: int,
                       exact: bool = False, time_budget: float | None = None,
                       warn: bool = False, echo: bool = True
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
        # rc=1 有两类：
        # 1) 「未找到匹配」：seek.run_query 返回 "local-seek: 未找到匹配（...）"。
        #    这是正常零命中，不是进程内异常——返回空结果避免子进程回退把同一搜索
        #    再跑一遍（消除双跑税），并让 0 命中写入缓存。
        # 2) 确定性环境/参数错误（如 Windows 缺 rg/grep、目录不存在）：
        #    子进程重跑结果完全一样；转为结构化告警并停止无意义回退（issue #34.4）。
        stripped = text.strip()
        if rc == 1 and (not stripped or "未找到匹配" in stripped):
            return {"results": [], "total": 0}
        if rc == 1 and stripped.startswith("local-seek:"):
            if warn:
                _note_warning(f"  [include-local] {stripped}", echo)
            return {"results": [], "total": 0, "_error": stripped}
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
    """seek payload → include-local 结果条目（明确来源标记）。"""
    hits = payload.get("results") or payload.get("files") or []
    seek_mode = payload.get("mode", "fast")
    hit_score = 0.9 if seek_mode == "fast" else 0.7
    out = []
    for h in hits[:max_n]:
        if not isinstance(h, dict):
            continue
        path = h.get("path") or h.get("file") or ""
        line = h.get("line") or h.get("lineno") or 1
        line_str = f":{line}" if str(line).isdigit() and int(line) > 0 else ""
        url = f"file://{path}" + (f"#{line}" if str(line).isdigit() and int(line) > 0 else "")
        title = f"[本机] {path}{line_str}"
        snippet = (h.get("snippet") or h.get("text") or h.get("line_text") or "")[:200]
        out.append({
            "title": title,
            "url": url,
            "snippet": f"[本机文件] {snippet}" if not snippet.startswith("[本机") else snippet,
            "source": "local_files",
            "source_type": "local",
            "channel": "local_filesystem",
            "is_local": True,
            "score": hit_score,
            "kind": "local",
        })
    if len(_LOCAL_SEEK_CACHE) >= _LOCAL_SEEK_CACHE_MAX:
        _LOCAL_SEEK_CACHE.clear()
    _LOCAL_SEEK_CACHE[cache_key] = (time.time(), out)
    return out
