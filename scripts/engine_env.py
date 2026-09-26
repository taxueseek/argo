#!/usr/bin/env python3
"""engine_env.py — 引擎密钥与启用策略（环境变量优先）

规范：
  - 敏感信息只走环境变量，禁止硬编码
  - 新名优先：ARGO_<ENGINE>_API_KEY / ARGO_<NAME>
  - 兼容旧名：TAVILY_API_KEY、EXA_API_KEY、QWEATHER_KEY 等
  - 启用控制：ARGO_ENABLE_ENGINES / ARGO_DISABLE_ENGINES（逗号分隔）

对外：
  resolve_env_name / get_env / expand_placeholders
  required_env_for / missing_env_for / env_ready
  parse_engine_list_env / is_engine_allowed_by_env
"""

from __future__ import annotations

import os
import re
import json
import shlex
import sys
import threading
from pathlib import Path
from typing import Any

# ── 密钥文件热读（平台惯例位置，多候选合并）─────────────────────────────────
# os.environ 优先（显式覆盖/测试注入），文件保底：进程未经 mcp_launch.sh
# 注入密钥时仍能拿到，且改文件无需重启——多候选签名缓存，任一变更即重读。
#
# 位置按平台惯例解析（候选按优先级排列）：
#   1. ARGO_ENV_FILE：显式指定，**只读这一个**，不做合并（权威覆盖）
#   2. Windows：%APPDATA%\argo\env；POSIX：$XDG_CONFIG_HOME/argo/env（若该变量设置了）
#   3. 历史路径 ~/.config/argo/env（POSIX 上它就是 XDG 默认值）
# 读取时**合并**全部候选（靠前者逐键优先），而不是只认第一个存在的文件：
# 否则「用户在 XDG 目录放了一份、历史路径还留着一份」会让一部分密钥读得到、
# 一部分读不到——那正是本仓反复踩过的「装了没通电」形态（引擎按状态层可用、
# 实际取不到 key，静默 0 结果）。
_envfile_lock = threading.Lock()
_envfile_cache: dict[str, str] = {}
_envfile_sig: tuple = ()

ENV_ENV_FILE = "ARGO_ENV_FILE"
ENV_XDG_CONFIG = "XDG_CONFIG_HOME"
ENV_APPDATA = "APPDATA"
_LEGACY_ENVFILE = "~/.config/argo/env"


def _platform_config_root(env: "os._Environ[str] | dict[str, str] | None" = None,
                          platform: str | None = None) -> Path | None:
    """平台惯例的配置根（不含应用名）：Windows → %APPDATA%；其余 → $XDG_CONFIG_HOME。

    未设置对应变量时返回 None，由调用方回落到 Unix 惯例 ~/.config。抽成纯函数
    （可注入 env 与 platform）是为了能在 macOS 上直接单测 Windows 分支——否则
    「Windows 上取哪个目录」永远只有真机才能验证。
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        base = str(env.get(ENV_APPDATA) or "").strip()
    else:
        base = str(env.get(ENV_XDG_CONFIG) or "").strip()
    return Path(os.path.expanduser(base)) if base else None


def _envfile_paths() -> list[Path]:
    """密钥文件的候选路径，按优先级排列（去重保序）。

    这里是**唯一**的位置解析入口：读取、签名、诊断都走它，任何新增候选都只
    改这一处（此前写死 ~/.config/argo/env，Windows 与自定义 XDG 都无处安放）。
    """
    explicit = os.environ.get(ENV_ENV_FILE, "").strip()
    if explicit:
        return [Path(os.path.expanduser(explicit))]
    cands: list[Path] = []
    root = _platform_config_root()
    if root is not None:
        cands.append(root / "argo" / "env")
    cands.append(Path(os.path.expanduser(_LEGACY_ENVFILE)))
    out: list[Path] = []
    for p in cands:
        if p not in out:
            out.append(p)
    return out


def _envfile_path() -> Path:
    """首选（最高优先级）密钥文件路径——展示与错误提示用。

    读取请用 `_envfile_paths()`：只认这一个会漏掉另一份候选里的密钥。
    """
    return _envfile_paths()[0]


def _parse_envfile(text: str) -> dict[str, str]:
    """解析 env 文件文本（export 前缀、引号、注释都按既有计算方式处理）。"""
    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if not k or not v:
            continue
        try:
            parts = shlex.split(v)
            val = parts[0] if parts else ""
        except ValueError:
            val = v.strip("'\"")
        data[k] = val
    return data


def _envfile_load() -> dict[str, str]:
    global _envfile_cache, _envfile_sig
    paths = _envfile_paths()
    stats: list[tuple[str, int, int]] = []
    for p in paths:
        try:
            st = p.stat()
            stats.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    sig = tuple(stats)
    with _envfile_lock:
        if sig == _envfile_sig:
            return _envfile_cache
        data: dict[str, str] = {}
        # 靠前的候选优先：先读的键不被后面的覆盖（显式覆盖 > 平台惯例 > 历史）
        for p in paths:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for k, v in _parse_envfile(text).items():
                data.setdefault(k, v)
        _envfile_cache = data
        _envfile_sig = sig
        return data


def reset_envfile_cache() -> None:
    """清空 env 文件缓存，强制下次读取重新读盘。

    缓存内容与签名是一对状态，必须一起清。只把签名改回旧值会让本进程内的
    密钥读取**永久失效**：签名与真实文件对得上，函数就直接返回那份不属于
    该文件的缓存。2026-09-15 实测到的现场——某测试用「改签名 + 临时路径」
    强制重读，结束时只恢复签名，于是此后 get_env 再也读不到
    ~/.config/argo/env 里的任何密钥，路由把带密钥的引擎全判为不可用，
    表现为「单独跑通过、和别的测试一起跑就失败」。

    需要强制重读的调用方（测试隔离、密钥热更新）都走这里，别再各自操作
    那两个全局变量：只做一半的状态在测试里几乎看不出来，排查成本极高。
    """
    global _envfile_cache, _envfile_sig
    with _envfile_lock:
        _envfile_cache = {}
        _envfile_sig = ()

# 引擎 → 候选环境变量（从左到右优先）
# 第一项为推荐新名（ARGO_ 前缀），后续为历史兼容名
KNOWN_ENV_ALIASES: dict[str, list[str]] = {
    "tavily": ["ARGO_TAVILY_API_KEY", "TAVILY_API_KEY"],
    "bocha": ["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"],
    "bocha_ai": ["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"],
    "brave": ["ARGO_BRAVE_API_KEY", "BRAVE_API_KEY"],
    "byted": ["ARGO_BYTED_API_KEY", "ARGO_WEB_SEARCH_API_KEY", "WEB_SEARCH_API_KEY"],
    "exa": ["ARGO_EXA_API_KEY", "EXA_API_KEY"],
    "octen": ["ARGO_OCTEN_API_KEY", "OCTEN_API_KEY"],
    "felo": ["ARGO_FELO_API_KEY", "FELO_API_KEY"],
    "metaso": ["ARGO_METASO_API_KEY", "METASO_API_KEY"],
    "qweather": ["ARGO_QWEATHER_KEY", "QWEATHER_KEY"],
    "github": ["ARGO_GITHUB_TOKEN", "GITHUB_TOKEN"],
    "wolframalpha": ["ARGO_WOLFRAM_APPID", "WOLFRAM_APPID"],
    "zhihu": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "zhihu_global": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "zhihu_hot": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "zhihu_user": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "anysearch": ["ARGO_ANYSEARCH_API_KEY", "ANYSEARCH_API_KEY"],  # 可选
    "firecrawl": ["ARGO_FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY"],  # 可选（keyless 免费层）
    "weread": ["ARGO_WEREAD_API_KEY", "WEREAD_API_KEY"],  # 微信读书 Agent Gateway
    "em_miaoxiang": ["ARGO_EASTMONEY_APIKEY", "EASTMONEY_APIKEY"],  # 东财妙想（可选）
}

# 占位符名 → 候选 env（用于 config 中 {TAVILY_API_KEY} 展开）
PLACEHOLDER_ALIASES: dict[str, list[str]] = {
    "TAVILY_API_KEY": ["ARGO_TAVILY_API_KEY", "TAVILY_API_KEY"],
    "BOCHA_API_KEY": ["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"],
    "BRAVE_API_KEY": ["ARGO_BRAVE_API_KEY", "BRAVE_API_KEY"],
    "WEB_SEARCH_API_KEY": ["ARGO_BYTED_API_KEY", "ARGO_WEB_SEARCH_API_KEY", "WEB_SEARCH_API_KEY"],
    "EXA_API_KEY": ["ARGO_EXA_API_KEY", "EXA_API_KEY"],
    "OCTEN_API_KEY": ["ARGO_OCTEN_API_KEY", "OCTEN_API_KEY"],
    "FELO_API_KEY": ["ARGO_FELO_API_KEY", "FELO_API_KEY"],
    "METASO_API_KEY": ["ARGO_METASO_API_KEY", "METASO_API_KEY"],
    "QWEATHER_KEY": ["ARGO_QWEATHER_KEY", "QWEATHER_KEY"],
    "GITHUB_TOKEN": ["ARGO_GITHUB_TOKEN", "GITHUB_TOKEN"],
    "WOLFRAM_APPID": ["ARGO_WOLFRAM_APPID", "WOLFRAM_APPID"],
    "ZHIHU_ACCESS_SECRET": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "ANYSEARCH_API_KEY": ["ARGO_ANYSEARCH_API_KEY", "ANYSEARCH_API_KEY"],
    "FIRECRAWL_API_KEY": ["ARGO_FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY"],
    "WEREAD_API_KEY": ["ARGO_WEREAD_API_KEY", "WEREAD_API_KEY"],
    "UNPAYWALL_EMAIL": ["ARGO_UNPAYWALL_EMAIL", "UNPAYWALL_EMAIL"],
}

_PLACEHOLDER_RE = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")
# 动态生成的非密钥占位符，不算 required_env
# （UUID = engines_base._proc_uuid 的进程级一次性身份，见其 docstring）
_NON_SECRET_PLACEHOLDERS = {"QUERY", "N", "TIMESTAMP", "MODE", "DEPTH", "UUID"}

# 可选密钥：有则更好，缺失不阻断自动路由
OPTIONAL_ENV_ENGINES: set[str] = {
    "github",       # GITHUB_TOKEN 仅提高限频
    "anysearch",    # ANYSEARCH_API_KEY 可选
    "firecrawl",    # FIRECRAWL_API_KEY 可选：官方免费层匿名（keyless）即可搜索
}


def _name_variants(name: str) -> list[str]:
    """单个名字展开为候选链：原名 + 前缀规范名（去重保序）。

    密钥有两套写法：推荐名 `ARGO_<NAME>` 与历史裸名 `<NAME>`，用户只写其中
    一套是常态（文档推 ARGO_ 名、老集成留裸名）。所以「单名」入口必须两套
    都认——否则同一把 key 会出现「一部分读得到、一部分读不到」的分裂：
    `get_env("TINYFISH_API_KEY")` 只查裸名，而 config.yaml / ENGINE_CATALOG
    都要求配 `ARGO_TINYFISH_API_KEY`，实测结果是 fetch 渲染层被悄悄关掉
    （`_api_key()` 拿到空串）而 search 三引擎正常。
    """
    if not name:
        return []
    if name.startswith("ARGO_"):
        return [name, name[len("ARGO_"):]]
    return [name, f"ARGO_{name}"]


def get_env(names: str | list[str], default: str = "") -> str:
    """按优先级读取第一个非空环境变量；os.environ 优先，~/.config/argo/env
    热读保底（改文件即生效，无需重启）。

    单名字符串会展开为「原名 + 前缀变体」候选链（见 _name_variants）；显式
    列表按调用方给定顺序与内容原样使用（调用方已写全两个名字时不做二次展开，
    保持既有优先级语义）。"""
    if isinstance(names, str):
        names = _name_variants(names)
    for name in names:
        if not name:
            continue
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return str(val)
    try:
        file_env = _envfile_load()
    except Exception:
        file_env = {}
    for name in names:
        if not name:
            continue
        val = file_env.get(name)
        if val is not None and str(val).strip() != "":
            return str(val)
    return default


# 布尔开关的「关」值集合（统一定义在这一处，见 env_flag）
_FALSY_VALUES = frozenset({
    "0", "false", "no", "off", "n", "disable", "disabled", "none",
})

# 授权位的「开」值白名单（strict=True 时使用）：只有明确写出的真值算放行
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})


def env_flag(name: str, default: bool = True, *, expand: bool = True,
             strict: bool = False) -> bool:
    """布尔环境开关的统一解析（全仓只有这一处）。

    此前全仓有四套互不兼容的判断规则，同一写法在不同开关上行为不同：
      - `not in ("0","false","False")`      → 不认 no/off（ARGO_FETCH_PARALLEL、
                                               ARGO_USAGE_LOG）
      - `not in ("0","false","False","no")` → 不认 off（fetch_v3 的四个开关、
                                               引擎 HTTP、robots、tinyfish、search 两处）
      - `not in {"","0","false","off","no"}` → 空串算关（recompute，语义不同）
    用户写 `ARGO_FETCH_JINA=off` 关得掉，写 `ARGO_FETCH_PARALLEL=off` 却关不掉
    ——「开关看着生效、其实没生效」是最难查的一类问题。

    判定规则：忽略大小写与首尾空白，以下算关：0/false/no/off/n/disable/disabled/none。
    未设置或值为空 → default。读取走 get_env：os.environ 优先，随后
    ~/.config/argo/env 热读——此前这些开关只认 os.environ，把开关写进 env
    文件（密钥的规范位置）是不生效的。

    expand=False 只认**字面名字**，不做别名展开。授权类开关（决定「是否放行
    一个受限动作」的开关，如 ARGO_ALLOW_RECOMPUTE）必须用它：别名展开是给
    密钥用的便利（`X_API_KEY` 与 `ARGO_X_API_KEY` 都认），一旦落在授权位上就
    变成了授权扩张——环境里任何一个工具随手设 `ALLOW_RECOMPUTE=1`（少写了
    ARGO_ 前缀的一个无关变量）就等于替用户放行了受限子进程执行脚本。授权只
    认推荐名这一条明确信号，误触不了；能力开关（超时、并发、渲染降级）继续
    保留两套写法的容忍度。

    strict=True 只认**明确写出的真值**（1/true/yes/on/y/t），其余一律算关。
    授权位必须同时用 expand=False + strict=True：默认计算方式是「非关即开」，那对
    能力开关没问题（最坏是换个行为），对授权位就是「任何拼错的值都放行」——
    `ARGO_ALLOW_RECOMPUTE=0x0`、`=maybe`、`=ture` 全都会授权。检查见
    tests/test_static_lint_gate.py（漏写任一项即报红）。
    """
    raw = get_env(_name_variants(name) if expand else [name])
    val = str(raw).strip().lower()
    if val == "":
        return default
    if strict:
        return val in _TRUE_VALUES
    return val not in _FALSY_VALUES


def sync_envfile_to_environ() -> list[str]:
    """把 ~/.config/argo/env 同步进 os.environ（只填缺失，不覆盖已有值）。

    兼容计算方式：读取方保持标准 os.environ 直读不动（含其他 AGT/客户端的
    既有集成，零改动零破坏），由入口（bin/argo / mcp_server）调用本函数
    把文件密钥「同步一份过去」。os.environ 已有变量永远优先——显式覆盖
    与测试注入不受影响。重复执行结果一致，可重复调用。
    """
    try:
        file_env = _envfile_load()
    except Exception:
        return []
    injected: list[str] = []
    for k, v in file_env.items():
        cur = os.environ.get(k)
        if cur is None or cur.strip() == "":
            os.environ[k] = v
            injected.append(k)
    return injected


def resolve_env_name(engine_id: str, logical: str = "api_key") -> list[str]:
    """返回某引擎逻辑密钥的候选环境变量名列表。"""
    aliases = KNOWN_ENV_ALIASES.get(engine_id)
    if aliases:
        return list(aliases)
    upper = engine_id.upper().replace("-", "_")
    if logical == "api_key":
        return [f"ARGO_{upper}_API_KEY", f"{upper}_API_KEY"]
    upper_logic = logical.upper()
    return [f"ARGO_{upper}_{upper_logic}", f"{upper}_{upper_logic}"]


def expand_placeholders(value: Any) -> Any:
    """递归展开字符串中的 {ENV_NAME}，支持 ARGO_ 别名。"""
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key in _NON_SECRET_PLACEHOLDERS:
                return m.group(0)
            candidates = PLACEHOLDER_ALIASES.get(key, [f"ARGO_{key}", key])
            resolved = get_env(candidates, "")
            return resolved if resolved else m.group(0)

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, list):
        return [expand_placeholders(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_placeholders(v) for k, v in value.items()}
    return value


def _placeholders_in_spec(spec: dict[str, Any]) -> list[str]:
    """从引擎 spec 中提取 {ENV} 占位符名（不含非密钥）。"""
    try:
        blob = json.dumps(spec, ensure_ascii=False, default=str)
    except Exception:
        blob = str(spec)
    found = []
    for m in _PLACEHOLDER_RE.finditer(blob):
        key = m.group(1)
        if key in _NON_SECRET_PLACEHOLDERS:
            continue
        if key not in found:
            found.append(key)
    return found


def required_env_for(engine_id: str, spec: dict[str, Any] | None = None) -> list[str]:
    """推断引擎必填环境变量（返回「逻辑/主推荐」名列表，用于展示与检查）。

    优先级：
      1. spec.required_env（显式列表，元素可以是 str 或候选列表）
      2. KNOWN_ENV_ALIASES
      3. 从 spec 占位符推断
      4. 专用 type 映射
    """
    spec = spec or {}
    if engine_id in OPTIONAL_ENV_ENGINES and not spec.get("require_api_key"):
        # 仍可在 detail 中展示 optional，但不作为 required
        return []

    explicit = spec.get("required_env")
    if explicit:
        out: list[str] = []
        for item in explicit:
            if isinstance(item, list) and item:
                out.append(item[0])
            elif isinstance(item, str) and item:
                out.append(item)
        return out

    if engine_id in KNOWN_ENV_ALIASES:
        return [KNOWN_ENV_ALIASES[engine_id][0]]

    # type 级默认
    type_defaults = {
        "exa": ["ARGO_EXA_API_KEY"],
        "octen": ["ARGO_OCTEN_API_KEY"],
        "qweather": ["ARGO_QWEATHER_KEY"],
    }
    t = str(spec.get("type", ""))
    if t in type_defaults:
        return type_defaults[t]

    placeholders = _placeholders_in_spec(spec)
    # 把占位符映射到推荐 ARGO 名
    mapped: list[str] = []
    for p in placeholders:
        candidates = PLACEHOLDER_ALIASES.get(p, [f"ARGO_{p}", p])
        mapped.append(candidates[0])
    return mapped


def missing_env_for(engine_id: str, spec: dict[str, Any] | None = None) -> list[str]:
    """返回缺失的环境变量（用候选链检查：任一候选有值即视为满足）。"""
    spec = spec or {}
    if engine_id in OPTIONAL_ENV_ENGINES and not spec.get("require_api_key"):
        return []

    missing: list[str] = []

    explicit = spec.get("required_env")
    if explicit:
        for item in explicit:
            if isinstance(item, list):
                candidates = item
                display = item[0] if item else "?"
            else:
                display = str(item)
                if display in PLACEHOLDER_ALIASES:
                    candidates = PLACEHOLDER_ALIASES[display]
                elif display.startswith("ARGO_"):
                    legacy = display[len("ARGO_"):]
                    candidates = [display, legacy]
                else:
                    candidates = [f"ARGO_{display}", display]
            if not get_env(candidates):
                missing.append(display)
        return missing

    if engine_id in KNOWN_ENV_ALIASES:
        aliases = KNOWN_ENV_ALIASES[engine_id]
        if not get_env(aliases):
            missing.append(aliases[0])
        return missing

    t = str((spec or {}).get("type", ""))
    type_map = {
        "exa": KNOWN_ENV_ALIASES["exa"],
        "octen": KNOWN_ENV_ALIASES["octen"],
        "qweather": KNOWN_ENV_ALIASES["qweather"],
    }
    if t in type_map:
        if not get_env(type_map[t]):
            missing.append(type_map[t][0])
        return missing

    for p in _placeholders_in_spec(spec or {}):
        candidates = PLACEHOLDER_ALIASES.get(p, [f"ARGO_{p}", p])
        if not get_env(candidates):
            missing.append(candidates[0])
    return missing


def env_ready(engine_id: str, spec: dict[str, Any] | None = None) -> bool:
    return len(missing_env_for(engine_id, spec)) == 0


def parse_engine_list_env(var_name: str) -> set[str] | None:
    """解析逗号分隔引擎列表；未设置返回 None。"""
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return None
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_engine_allowed_by_env(engine_id: str) -> bool:
    """ARGO_ENABLE_ENGINES 白名单 / ARGO_DISABLE_ENGINES 黑名单。

    - 仅设置 ENABLE：不在名单内 → False
    - DISABLE 命中 → False
    - 都未设置 → True
    """
    enabled = parse_engine_list_env("ARGO_ENABLE_ENGINES")
    disabled = parse_engine_list_env("ARGO_DISABLE_ENGINES")
    if disabled and engine_id in disabled:
        return False
    if enabled is not None and engine_id not in enabled:
        return False
    return True


def env_status_for(engine_id: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """单引擎 env 状态摘要（供 list/validate）。"""
    required = required_env_for(engine_id, spec)
    missing = missing_env_for(engine_id, spec)
    return {
        "required_env": required,
        "missing_env": missing,
        "env_ready": len(missing) == 0,
        "allowed_by_env": is_engine_allowed_by_env(engine_id),
    }
