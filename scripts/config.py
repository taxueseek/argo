#!/usr/bin/env python3
"""
config.py — Unified Search v2 配置加载器

职责：
  - 从项目根目录的 config.yaml 加载统一配置
  - 支持热加载（按 mtime 缓存）
  - 使用 PyYAML 解析（缺失时给出明确安装提示）
  - 将 ~ 展开为实际用户目录
  - 提供类型化访问接口（引擎列表、域规则、成本分级、预算配置）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

# ── 路径 ──────────────────────────────────────────────────────────────────────

# logging 延迟导入：config.py 的 import 链实测 31ms，其中 logging 占 13ms。
# _logger 仅 1 处 warning 使用，模块级导入会让所有 import config 的路径白付。
# 延迟到首次调用时 import，--list-engines / --help 等路径省 13ms。
# shutil 保留模块级：仅 5ms，且测试用 patch.object(config_mod.shutil, ...) 依赖它。

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
# 外置引擎声明目录：engines/*.yaml（不含 plugins/、templates/、_ 前缀）
ENGINES_DIR = Path(__file__).parent.parent / "engines"

# 本地状态目录唯一来源。argo_paths 只在函数体内反向 import config，
# 因此此处模块级导入不会成环（config 未就绪时 argo_paths 会回落到历史目录）。
import argo_paths  # noqa: E402

# 跨进程配置磁盘缓存（性能层）住在 config_cache；这里同名转出：load_config /
# peek_cache_db_path / _cli 与既有测试（config._config_content_digest …）无需改。
# 打桩点若落在这些符号上，必须打在**读取处**（config_cache）。
from config_cache import (  # noqa: E402
    _CONFIG_DISK_CACHE_SCHEMA,
    _config_disk_cache_enabled,
    _config_disk_cache_path,
    _config_disk_cache_key,
    _config_db_path_key,
    _config_content_digest,
    _disk_cache_payload,
    _json_round_trip_safe,
    _load_config_disk_cache,
    _looks_like_config,
    _loader_sig,
    _peek_disk_cache_db_path,
    _save_config_disk_cache,
    _sweep_orphan_cache_slots,
)
from cli_io import dumps


# ── 默认配置 ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "engines": {
        "anysearch": {
            "enabled": True, "type": "anysearch",
            # 进程内 JSON-RPC builder（保持一致 config.yaml 来源）；不调用主机上的
            # anysearch-skill CLI，避免写死 ~/.agents/skills 主机路径（纪律：禁止）。
            "label": "AnySearch", "cost_tier": "free",
            "search_args": ["search", "{query}", "--max_results", "{n}"],
            "env": {},
        },
    },
    "domains": [
        {
            "name": "general_search", "desc": "通用搜索（兜底）",
            "patterns": [], "primary": "anysearch",
            "fallback": "anysearch", "parallel": True,
        },
    ],
    # db_path 由 argo_paths 唯一来源派生，不再字面量拼 ~/.cache/unified-search。
    # 无 ARGO_STATE_DIR 时展开结果与历史默认一致，存量缓存不失效。
    "cache": {"enabled": True, "db_path": str(argo_paths.db_path()), "ttl": 3600, "max_size_mb": 200},
    "execution": {"default_timeout": 8, "parallel_timeout": 6, "max_parallel_engines": 3, "retry_count": 0},
    "budget": {
        "fast": {"max_cost_per_query": 0.0, "allow_paid": False},
        "auto": {"max_cost_per_query": 0.01, "allow_paid": True},
        "deep": {"max_cost_per_query": 1.0, "allow_paid": True},
        "budget": {"max_cost_per_query": 0.005, "allow_paid": False, "quota_threshold": 0.2},
    },
    "output": {"format": "auto", "include_scores": True, "include_routing_decision": True},
}


# ── YAML 加载 ─────────────────────────────────────────────────────────────────

def _require_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError as e:
        raise ImportError("缺少 PyYAML，请安装：pip install pyyaml") from e


def _load_yaml(text: str) -> dict[str, Any]:
    """解析 YAML —— 统一走 yaml_load（优先 libyaml C 版 loader）。

    此前直接调 `yaml.safe_load`，走的是纯 Python 扫描器：解析 123 KB 的
    config.yaml 实测 79 ms，C 版同内容 10 ms。冷启动链上同一份配置会被解析
    多次，差距被成倍放大。详见 yaml_load.py。
    """
    from yaml_load import loads
    parsed = loads(text)
    return parsed if isinstance(parsed, dict) else {}


# ── 配置加载与缓存 ─────────────────────────────────────────────────────────────

_config_cache: dict[str, Any] | None = None
_config_mtime: float = 0.0
_config_load_error: str | None = None


def _expand_value(value: Any) -> Any:
    """递归展开字符串中的 ~ 为用户目录。"""
    if isinstance(value, str):
        return os.path.expanduser(value)
    if isinstance(value, list):
        return [_expand_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_value(v) for k, v in value.items()}
    return value


def _resolve_relative_paths(config: dict[str, Any]) -> dict[str, Any]:
    """将 cli 引擎 cmd/domain_search 中的相对路径解析为 config.yaml 所在目录的绝对路径。"""
    base = CONFIG_PATH.parent
    for name, spec in config.get("engines", {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "cli":
            continue
        for key in ("cmd", "domain_search"):
            if key not in spec:
                continue
            items = spec[key]
            if not isinstance(items, list):
                continue
            resolved: list[Any] = []
            for item in items:
                if isinstance(item, str) and item and not item.startswith(("/", "~", "http://", "https://")) and not item.startswith("{"):
                    candidate = base / item
                    if candidate.exists() or ("/" in item or "\\" in item):
                        resolved.append(str(candidate.resolve()))
                    else:
                        resolved.append(item)
                else:
                    resolved.append(item)
            spec[key] = resolved
    return config


def _validate_engine_paths(config: dict[str, Any]) -> dict[str, Any]:
    """验证引擎 CLI 路径，不存在则标记为 disabled。

    相对路径一律以 config.yaml 所在目录为基准解析（不是进程 CWD）。外置 spec
    （engines/specs/*.yaml）的 cmd 是相对路径，而 _resolve_relative_paths 在
    合并外置声明之前就已执行，故这些路径不会被转成绝对路径。若这里用
    Path(cmd[-1]).exists() 判定，结果就随 CWD 变——实测同一份配置在仓库根
    目录下 165 个引擎可用、在 /tmp 下只剩 163（train/weather 被静默停用），
    一致性检查也随之红/绿漂移。
    """
    base = CONFIG_PATH.parent
    for name, spec in config.get("engines", {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "cli":
            continue
        cmd = spec.get("cmd", [])
        if not cmd or cmd[0] in ("npx", "node"):
            continue
        cmd_path_str = cmd[-1]
        if cmd_path_str.startswith("--"):
            continue
        cmd_path = Path(cmd_path_str).expanduser()
        if not cmd_path.is_absolute():
            cmd_path = base / cmd_path
        if cmd_path.exists():
            continue
        # 裸命令（如 PATH 中的可执行文件）：用 shutil.which 查 PATH，查不到才禁用
        if not (shutil.which(cmd_path_str) or shutil.which(cmd_path.name)):
            spec["enabled"] = False
    return config


def _load_external_engine_specs() -> dict[str, dict[str, Any]]:
    """加载 engines/*.yaml 与 engines/specs/*.yaml 外置声明。

    文件名（去后缀）默认为 engine_id；文件内 engine_id / name 可覆盖。
    跳过：_ 前缀文件。
    """
    result: dict[str, dict[str, Any]] = {}
    if not ENGINES_DIR.is_dir():
        return result
    try:
        yaml = _require_yaml()
    except ImportError:
        return result
    from yaml_load import loads as _yaml_loads

    def _load_dir(directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            try:
                data = _yaml_loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            # 允许 {engine_id, ...} 或直接是 engine spec
            engine_id = data.get("engine_id") or data.get("name") or path.stem
            if not isinstance(engine_id, str) or not engine_id:
                continue
            spec = dict(data)
            spec.pop("engine_id", None)
            # name 字段若是展示名且与 id 不同，保留；type 必须有
            if "type" not in spec and "url" in spec:
                spec["type"] = "http"
            if "enabled" not in spec:
                spec["enabled"] = True
            spec["_external_spec"] = str(path)
            result[engine_id] = spec

    _load_dir(ENGINES_DIR)
    _load_dir(ENGINES_DIR / "specs")
    return result


def _merge_external_engines(config: dict[str, Any]) -> dict[str, Any]:
    """外置 YAML 覆盖/新增引擎；cost_tiers 可选追加。"""
    external = _load_external_engine_specs()
    if not external:
        return config
    engines = config.setdefault("engines", {})
    if not isinstance(engines, dict):
        engines = {}
        config["engines"] = engines
    for eid, spec in external.items():
        # 外置优先覆盖同名（便于本地试验）
        base = dict(engines.get(eid) or {})
        base.update(spec)
        engines[eid] = base
    # 注：不再维护独立的 cost_tiers 段。成本分级由 get_cost_tiers() 从 engines
    # 段的 cost_tier 字段聚合，独立段是第二份计算方式（曾与声明矛盾：zhihu_global
    # 列在 free 而声明 api、tavily 列在 paid 而声明 api），且无任何读取方。
    return config


_ext_scan_cache: tuple[float, tuple[float, int, int, str]] | None = None  # (monotonic 时刻, 指纹)


def _external_engines_scan_uncached() -> tuple[float, int, int, str]:
    """扫一遍外置声明目录 → (最新 mtime, 文件数, 总字节, 声明集摘要)。

    为什么不能只报 mtime：**max 对「删除」与「回填旧时间」是盲的**——删掉一个
    不是最新的声明（engines/specs/train.yaml 之类），max 不变；`cp -p` 拷进来
    一个新声明（mtime 被保留成旧值），max 也不变。两者都会让写入文件配置缓存继续
    命中，表现为「引擎删了还在 / 加了不生效」（审计实测）。文件数与总字节对
    集合变化敏感；摘要按**每个文件**的 (相对路径, 大小, mtime_ns, ctime_ns)
    聚合成一个值，能识别「总量不变但换了文件」这类集合替换。

    这里刻意**不读文件内容**：65 个声明逐个 read_bytes 实测 9.5 ms，等于把省下
    的时间吃掉一半。跨平台语义保底交给 config.yaml 的内容摘要（它是决定状态
    目录的那份文件，见 _config_content_digest）：Windows 上 st_ctime 是创建
    时间而非元数据变更时间，本摘要因此退化为「路径集 + 大小 + mtime」，仍能
    覆盖编辑器改文件（mtime 变）与增删声明；只有「改写内容并还原 mtime」这种
    刻意手法在 Windows 上漏检，而 POSIX 上 ctime 仍然兜着。
    """
    latest = 0.0
    count = 0
    total = 0
    digest = hashlib.blake2b(digest_size=16)
    if not ENGINES_DIR.is_dir():
        return latest, count, total, digest.hexdigest()
    for path in sorted(ENGINES_DIR.rglob("*.yaml")):
        if "plugins" in path.parts:
            continue
        try:
            st = path.stat()
            rel = path.relative_to(ENGINES_DIR)
        except (OSError, ValueError):
            continue
        count += 1
        total += st.st_size
        latest = max(latest, st.st_mtime)
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ctime_ns}\n".encode())
    return latest, count, total, digest.hexdigest()


def _ttl_memo(cache: tuple[float, float] | None, ttl: float,
              compute: Callable[[], float]) -> tuple[tuple[float, float] | None, float]:
    """TTL 记忆化的唯一实现：TTL 内复用 cache，否则调 compute 并回填。

    同一个形状（`if ttl>0 and cache and now-cache[0]<ttl: return cache[1]`）
    此前在外置引擎 mtime 与 config_stamp 各写一遍，失效语义靠人工保持一致——
    一处改了 force 语义、另一处没改，就是这类漂移的温床（review 实测点出）。
    返回 (新 cache, 取值)：ttl<=0 时不写记忆（关闭记忆化的逃生门）。
    """
    now = time.monotonic()
    if ttl > 0 and cache is not None and now - cache[0] < ttl:
        return cache, cache[1]
    value = compute()
    if ttl > 0:
        return (now, value), value
    return cache, value


def _remember_ext_scan(scan: tuple[float, int, int, str]) -> None:
    """把刚测到的外置声明指纹回填记忆——force 重读后不必立刻再扫一遍。"""
    global _ext_scan_cache
    if _stamp_ttl() > 0:
        _ext_scan_cache = (time.monotonic(), scan)


def _external_engines_scan() -> tuple[float, int, int, str]:
    """外置声明目录指纹，按与 config_stamp 相同的 TTL 记忆。"""
    global _ext_scan_cache
    _ext_scan_cache, value = _ttl_memo(
        _ext_scan_cache, _stamp_ttl(), _external_engines_scan_uncached)
    return value


def _external_engines_mtime() -> float:
    """外置引擎声明目录的最新 mtime，按与 config_stamp 相同的 TTL 记忆。

    扫一遍要 rglob 并 stat 全部外置声明（约 60 个文件、60 多次系统调用）。
    load_config() 与 config_stamp() 在一次搜索里会被调用十几次，此前每次都
    重新扫盘，实测一次搜索因此产生约 900 次 stat，绝大多数是在重复读不会变
    的文件。外置声明新增/改动晚 1 秒生效没有实际影响，所以在 TTL 内只扫一次
    （TTL 由 ARGO_CONFIG_STAMP_TTL_S 控制，设 0 即关闭记忆、每次即时扫描）。
    """
    return _external_engines_scan()[0]


_stamp_cache: tuple[float, float] | None = None  # (monotonic 时刻, stamp)


def _stamp_ttl() -> float:
    """stamp 记忆化的 TTL（秒）。每次读，便于测试与现场调参。

    走 engine_env.get_env 而非 os.environ 直读：开关写进 ~/.config/argo/env
    （密钥与开关的规范位置）也要生效——这正是 env_flag 统一布尔计算方式时定下的
    契约，同一个旋钮不该有两套可读位置。engine_env 只依赖标准库，懒导入避免
    与 config 成环；导入或读取失败一律回落到 os.environ 与默认值。
    """
    name = "ARGO_CONFIG_STAMP_TTL_S"
    raw = os.environ.get(name, "")
    if not raw.strip():
        try:
            from engine_env import get_env
            raw = get_env(name)
        except Exception:
            raw = ""
    try:
        return float(raw or 1.0)
    except ValueError:
        return 1.0


def config_stamp() -> float:
    """config.yaml + 外置引擎声明的综合 mtime（供 registry 等热加载判断）。

    按 TTL 记忆化（默认 1 s，`ARGO_CONFIG_STAMP_TTL_S` 可调，设 0 即关闭）：
    取值要 stat 全部 63 个外置声明文件，而 `engines.get_registry()` **每访问
    一次**注册表就会调它一次——实测一次搜索触发 28 次调用 = 1764 次 stat，
    99% 是重复劳动。热加载不要求亚秒级感知（配置改动晚 1 秒生效无实际影响），
    故折叠到 TTL 内一次。
    """
    global _stamp_cache
    _stamp_cache, value = _ttl_memo(_stamp_cache, _stamp_ttl(),
                                    _stamp_uncached)
    return value


def _stamp_uncached() -> float:
    try:
        combined = CONFIG_PATH.stat().st_mtime
    except OSError:
        combined = 0.0
    return max(combined, _external_engines_mtime())




def load_config(force: bool = False) -> dict[str, Any]:
    """加载配置，支持热加载；合并 engines/*.yaml 外置声明。"""
    global _config_cache, _config_mtime, _config_load_error, _stamp_cache
    try:
        st = CONFIG_PATH.stat()
        mtime = st.st_mtime
    except OSError:
        if _config_cache is None:
            base = _validate_engine_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG))))
            _config_cache = _merge_external_engines(base)
        return _config_cache

    # force=True 表示调用方明确要立即重读（如 --check、测试改写配置后），
    # 此时绕过 TTL 记忆直接扫盘，保证强制刷新语义不被记忆化延迟。
    scan = (_external_engines_scan_uncached() if force
            else _external_engines_scan())
    ext_mtime = scan[0]
    combined_mtime = max(mtime, ext_mtime)

    if force:
        # force 语义必须贯穿**所有**同级缓存，否则重读与快照脱节：外置声明
        # 指纹记忆留着旧值 → 本函数下一次非 force 调用按旧值判定「没变」；
        # stamp 记忆留着旧值 → 1 秒内 get_registry() 仍按旧 stamp 决定要不要
        # 重建，于是「--check 之后立刻搜索」拿到的注册表与刚重读的配置不是
        # 一个版本。两者都已在本函数里算出来了，直接回填，不额外扫盘。
        _remember_ext_scan(scan)
        if _stamp_ttl() > 0:
            _stamp_cache = (time.monotonic(), combined_mtime)

    if not force and _config_cache is not None and combined_mtime == _config_mtime:
        return _config_cache

    # 跨进程缓存：force 不读（调用方明确要求重读，不许拿旧结论）。
    # 整段包 try：缓存是加速器不是依赖，结构被外部改写之类的问题必须退化成
    # 「走一遍完整解析」，绝不能把异常抛给每个调用方（那种表现是「CLI 每次都崩、
    # 且看不出跟缓存有关」，用户无从知道要删哪个文件）。
    if not force:
        try:
            # 内容摘要也在缓存判断之前算：它是唯一的强判据（见 _config_content_digest）
            disk = _load_config_disk_cache(st, scan, _config_content_digest(st))
            if disk is not None:
                _config_cache = _validate_engine_paths(disk)
                _config_mtime = combined_mtime
                _config_load_error = None
                return _config_cache
        except Exception:
            pass

    try:
        # 与 peek_cache_db_path 共享同一次解析（同一份 123 KB 文本此前各解析
        # 一遍，纯属重复劳动）
        _ok, parsed, err = _read_config_yaml()
        if err:
            _config_load_error = err
        if parsed:
            expanded = _expand_value(parsed)
            resolved = _resolve_relative_paths(expanded)
            resolved = _merge_external_engines(resolved)
            # 外置 spec 的 cmd 是相对路径 → 合并后必须再解析一次，
            # 否则它们永远保持相对（且校验随 CWD 漂移）
            resolved = _resolve_relative_paths(resolved)
            _save_config_disk_cache(st, scan, _config_content_digest(st), resolved)
            _config_cache = _validate_engine_paths(resolved)
            _config_mtime = combined_mtime
            _config_load_error = err
        elif _config_cache is None:
            base = _validate_engine_paths(_resolve_relative_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))))
            _config_cache = _merge_external_engines(base)
    except ImportError as e:
        _config_load_error = str(e)
        import sys as _sys
        print(f"[unified-search] PyYAML 未安装，使用内置默认配置。安装：pip install pyyaml。原因：{e}", file=_sys.stderr)
        if _config_cache is None:
            base = _validate_engine_paths(_resolve_relative_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))))
            _config_cache = _merge_external_engines(base)
        return _config_cache
    except Exception as e:
        _config_load_error = str(e)
        if _config_cache is None:
            base = _validate_engine_paths(_resolve_relative_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))))
            _config_cache = _merge_external_engines(base)
    return _config_cache


def last_load_error() -> str | None:
    return _config_load_error


# ── 类型化访问接口 ─────────────────────────────────────────────────────────────

def get_engines(config: dict[str, Any] | None = None,
                *,
                routable_only: bool = False) -> dict[str, dict[str, Any]]:
    """返回启用的引擎配置字典。

    routable_only=True 时额外过滤：
      - ARGO_ENABLE/DISABLE_ENGINES
      - 缺 API Key
      - admission blocked
    """
    cfg = config if config is not None else load_config()
    engines = cfg.get("engines", {})
    result = {name: spec for name, spec in engines.items() if spec.get("enabled", True)}
    if not routable_only:
        return result
    try:
        from engine_env import is_engine_allowed_by_env, env_ready
        from engine_admission import is_blocked
    except ImportError as e:
        # 单机可选依赖（engine_env / engine_admission）缺失时按历史语义返回
        # 未过滤集，但**必须留痕**：静默返回意味着 blocked 与 env 未就绪的
        # 引擎重新可路由，是最难查的一类故障。注意这里与上面两处不同——
        # 上面两处是同一仓内的死回退（已删），这里是真会发生的降级（保留）。
        import logging
        logging.getLogger("unified_search.config").warning(
            "routable_only 过滤不可用（%s），本次返回未过滤引擎集", e)
        return result
    filtered: dict[str, dict[str, Any]] = {}
    for name, spec in result.items():
        if not is_engine_allowed_by_env(name):
            continue
        if not env_ready(name, spec):
            continue
        if is_blocked(name):
            continue
        filtered[name] = spec
    return filtered


def get_domains(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """返回按优先级排序的域规则列表。"""
    cfg = config if config is not None else load_config()
    return cfg.get("domains", [])


# 解析结果记忆化：key = config.yaml 的 (mtime_ns, size)。
# import 期的状态目录派发会连调 4 次（quota / adaptive / argo_engine_registry /
# cache 各自的模块级常量），此前每次重解析一遍 123 KB 配置——实测 4×~79 ms。
# 配置被改写（mtime 或 size 变）即失效，语义与逐次读盘一致。
#
# 这份解析结果是 peek 与 load_config **共用**的：两处此前各解析一遍同一份
# 123 KB 文本（C 版 loader 实测约 20 ms/遍），等于每个进程白等一遍。
_parsed_yaml_cache: tuple[tuple[int, int], dict[str, Any] | None] | None = None


def _read_config_yaml() -> tuple[bool, dict[str, Any] | None, str | None]:
    """读并解析 config.yaml，按 (mtime_ns, size) 记忆化。

    返回 (可记忆, 解析结果, 错误信息)：

    - 可记忆=False 表示读取/解析链不可用（文件缺失、配置损坏），调用方不得
      写记忆——一次瞬时故障不该固化成本进程的永久 None（原 peek 契约）。
    - 解析结果按原样缓存，**调用方只读**：load_config 先 _expand_value 出副本
      再归一化，所以缓存对象不会被就地改写。
    - PyYAML 缺失仍抛 ImportError，由 load_config 给出安装提示（原先它对
      ImportError 与 YAMLError 是两种处置）。
    """
    global _parsed_yaml_cache
    try:
        st = CONFIG_PATH.stat()
    except OSError as e:
        return False, None, f"config.yaml 不可读：{e}"
    key = (st.st_mtime_ns, st.st_size)
    if _parsed_yaml_cache is not None and _parsed_yaml_cache[0] == key:
        return True, _parsed_yaml_cache[1], None
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return False, None, f"config.yaml 读取失败：{e}"
    try:
        parsed = _load_yaml(text)
    except ImportError:
        raise
    except Exception as e:
        # 含 yaml.YAMLError（ParserError 等）——它不是 ValueError 的子类。
        # 此前只 catch (ImportError, ValueError) 会让「配置损坏」直接抛出，
        # 与 fail-open 契约不符（2026-09-15 实测确认）。
        return False, None, f"config.yaml 解析失败：{type(e).__name__}: {e}"
    if not isinstance(parsed, dict):
        parsed = None
    _parsed_yaml_cache = (key, parsed)
    return True, parsed, None


def peek_cache_db_path() -> str | None:
    """轻量读取 config.yaml 的 cache.db_path，**不合并外置引擎 spec**。

    专用场景：import 期的路径派生（argo_paths._config_db_path → cache.py 的
    DEFAULT_DB_PATH）。load_config() 会合并 engines/*.yaml 的全部外置声明，
    而这里只需要一个标量——为它付整轮合并不值得。

    （勘误 2026-09-15：本文档与 65e6d2e 提交信息曾记「load_config 实测约
    1.7s」，该数字无法复现。同一台机器实测 load_config() 为纯 Python loader
    107 ms / C 版 15 ms（含 63 个 yaml、220 个引擎），因此当时的真实成本是
    「一次合并约 0.1 s，而 import 链上被连调 4 次」而非单次 1.7 s。数字已按
    可复现计算方式改写。）
    语义与 get_cache_config() 的 db_path 字段保持一致：用户未配置返回 None
    （由调用方回退 state_path）。返回值统一是**展开后的形式**（`~` 已展开）：
    缓存命中与走解析两条分支必须同一计算方式，调用方再 expanduser 一次也无副作用。
    PyYAML 缺失 / 配置损坏时 fail-open 返回 None，与 _config_db_path 契约一致。

    结果按 config.yaml 的 (mtime_ns, size, ctime_ns) 记忆化：同一进程内多次
    调用只解析一次（与 load_config 共享同一份解析结果，见 _read_config_yaml）。
    解析链不可用时（PyYAML 缺失 / 配置损坏）不写记忆，下次调用仍会重试，避免
    把一次瞬时故障固化成本进程的永久 None。

    写入文件缓存命中时**连 YAML 都不解析**：db_path 只取决于 config.yaml 本身，
    而缓存键里已经记着这份文件的 stat，与逐次读盘语义一致。缓存未命中或停用
    （ARGO_CONFIG_CACHE=0）则照旧走解析链。
    """
    try:
        st = CONFIG_PATH.stat()
    except OSError:
        return None
    hit, db_path = _peek_disk_cache_db_path(st, _config_content_digest(st))
    if hit:
        return db_path
    try:
        ok, parsed, _err = _read_config_yaml()
    except ImportError:
        return None
    if not ok or not isinstance(parsed, dict):
        return None
    cache_cfg = parsed.get("cache")
    if not isinstance(cache_cfg, dict):
        return None
    db_path = cache_cfg.get("db_path")
    return os.path.expanduser(str(db_path)) if db_path else None


def get_cache_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    return cfg.get("cache", DEFAULT_CONFIG["cache"])


def get_execution_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    return cfg.get("execution", DEFAULT_CONFIG["execution"])


def get_output_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    return cfg.get("output", DEFAULT_CONFIG["output"])


def get_cost_tiers(config: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """返回成本分级：从引擎声明的 cost_tier 字段聚合（唯一来源 config.yaml engines 段）。

    自 v2.6 起 cost_tiers 不再是独立配置段，新增引擎只需在 engines 段声明
    cost_tier 字段，此处自动归入对应分级。
    """
    cfg = config if config is not None else load_config()
    tiers: dict[str, list[str]] = {}
    for name, spec in cfg.get("engines", {}).items():
        if not isinstance(spec, dict):
            continue
        tier = spec.get("cost_tier", "free")
        tiers.setdefault(tier, []).append(name)
    return tiers


def get_budget_config(mode: str = "auto") -> dict[str, Any]:
    """返回预算模式配置。"""
    cfg = load_config()
    budgets = cfg.get("execution", {}).get("budget", DEFAULT_CONFIG["budget"])
    return budgets.get(mode, budgets.get("auto", {}))


def cost_tier_of(engine: str) -> str:
    """引擎的成本档位：free / low / api / paid（未声明按 free 保底）。"""
    tiers = get_cost_tiers()
    for tier in ("free", "low", "api", "paid"):
        if engine in tiers.get(tier, []):
            return tier
    return "free"


# 成本因子唯一来源：越低越少被优先选中。
# 语义打分（tfidf_router）与 n 桶化判定（engines）都从这里取，不再各维护一套表
# ——此前 config 是 {free 1.0, low 0.7, paid 0.3}、tfidf_router 是
# {free 1.0, low 0.85, paid 0.6}，同一个概念两个值。
# api 档（需密钥、按量计费，如 exa/octen/tavily/zhihu_global）保持 1.0：
# 额度消耗由配额表按 limit/period 跟踪并降权，这里不叠加惩罚；但**它们不参与
# n 桶化**（见 engines._free_engine）——按量计费的源把 n 从 5 放大到 10 是直接放大账单。
_COST_FACTOR_BY_TIER = {"free": 1.0, "low": 0.85, "api": 1.0, "paid": 0.6}


def get_cost_factor(engine: str) -> float:
    """获取引擎的 cost_factor（按 cost_tier 查表，见上）。"""
    return _COST_FACTOR_BY_TIER.get(cost_tier_of(engine), 1.0)


# ── CLI 调试用 ─────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search v2 配置查看器")
    parser.add_argument("--engines", action="store_true", help="显示引擎配置")
    parser.add_argument("--domains", action="store_true", help="显示域规则")
    parser.add_argument("--cost-tiers", action="store_true", help="显示成本分级")
    parser.add_argument("--check", action="store_true", help="检查配置")
    args = parser.parse_args()
    cfg = load_config(force=True)
    if args.engines:
        print(dumps(get_engines(cfg)))
    elif args.domains:
        print(dumps(get_domains(cfg)))
    elif args.cost_tiers:
        print(dumps(get_cost_tiers(cfg)))
    elif args.check:
        err = last_load_error()
        print(dumps({
            "path": str(CONFIG_PATH), "ok": err is None, "error": err,
            "engines": list(get_engines(cfg).keys()),
            "domains": [d.get("name") for d in get_domains(cfg)],
        }))
    else:
        print(dumps(cfg))


if __name__ == "__main__":
    _cli()
