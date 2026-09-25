#!/usr/bin/env python3
"""config_cache.py — 配置的跨进程磁盘缓存（性能层，可整体关闭）。

## 为什么单独成模块

配置加载有两条完全不同的路径：

  1. **正确性路径**（config.py）：解析 config.yaml + 合并 60 多个外置声明，产物
     被当作**事实**（db_path、引擎表…），必须逐字节正确；
  2. **性能路径**（本模块）：把上面那次解析的结果按 (mtime, size, ctime, 内容摘要)
     存成 JSON，让下一个进程省掉整笔解析（实测 50–82 ms → 16–18 ms）。

两者的失效判据、schema 版本、清理策略都是独立的——混在一个文件里时，改缓存逻辑
要先在 1000 行配置代码里找到「跨进程配置缓存」那一段。这里的产物只是**优化结果**：
缓存错了最坏是多解析一次，绝不改变语义（判据见 `_config_content_digest` 的说明）。

`ARGO_CONFIG_DISK_CACHE=0` 可整体关闭；只读环境、schema 不匹配、摘要不可用时
一律 fail-open 回落到解析路径。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from except_sets import OPT_IMPORT, SHAPE_BENIGN

# 状态目录唯一来源。argo_paths 只在函数体内反向 import config，此处模块级导入
# 不会成环（与 config.py 的同一处理，见那边的注释）。
import argo_paths  # noqa: E402


def _config_path() -> Path:
    """config.yaml 路径：**延迟**从 config 取（config 在模块级导入本模块，成环）。"""
    from config import CONFIG_PATH
    return CONFIG_PATH


# ── 跨进程配置缓存（写入文件 JSON）─────────────────────────────────────────────────
#
# 为什么需要：每条命令都是一次新进程，config.yaml + 63 个外置声明的解析合并
# 在**每个** CLI 调用里重付一遍；进程内记忆化救不了跨进程的重复。输入几乎从不
# 变化，故把「归一化后」的配置按输入指纹写入文件，命中时用 json.loads 取代整条链。
# 实测（新进程 min of 4，macOS/Python 3.14）：load_config 关缓存 50–82 ms、命中
# 16–18 ms；其中 read+json.loads 约 2 ms，余下是每次都必须现算的外置声明扫描与
# CLI 路径校验（那两项是「结论随环境变，不能连结论一起缓存」的部分）。
#
# 键必须同时覆盖「输入」与「解释输入的代码」：只按 config.yaml 的 mtime 命中，
# 升级 argo（归一化逻辑改了）后仍会读回旧逻辑写下的缓存，表现是「升级了没生效」。
# 故把 config.py 与 yaml_load.py 的源码 mtime 也算进键里（见 _loader_sig）。
_CONFIG_DISK_CACHE_SCHEMA = 3
_loader_sig_cache: int | None = None


def _loader_sig() -> int:
    """本模块源码的 mtime_ns —— 磁盘缓存的「代码版本」键（进程内记忆一次）。

    归一化链不只在 config.py：`_load_yaml` 走 yaml_load.py（loader 选择直接
    影响解析结果）。两处都进签名，改任一处缓存立刻失效——不这么做的话，
    升级后读回的是旧逻辑写下的结果，表现是「改了没生效」。
    """
    global _loader_sig_cache
    if _loader_sig_cache is None:
        sig = 0
        for mod in (Path(__file__), Path(__file__).parent / "yaml_load.py"):
            try:
                sig = (sig * 1_000_003 + mod.stat().st_mtime_ns) % (2 ** 62)
            except OSError:
                continue
        _loader_sig_cache = sig
    return _loader_sig_cache


def _config_disk_cache_enabled() -> bool:
    """写入文件缓存开关（默认开）；ARGO_CONFIG_CACHE=0/false/no/off 关闭。

    走 engine_env.env_flag 而不是自己判真假：全仓布尔开关只有这一套计算方式（它
    同时认 env 文件里的写法），再写一份 `in {"0","false",...}` 就是又一处会
    漂移的第二计算方式。engine_env 只依赖标准库，懒导入避免与 config 成环。
    """
    try:
        from engine_env import env_flag
        return env_flag("ARGO_CONFIG_CACHE", default=True)
    except OPT_IMPORT + SHAPE_BENIGN:
        # 判定链不可用时按「开」处理：缓存是纯性能优化，关掉它不影响正确性，
        # 而误判为「关」只会让每次调用回到全量解析（旧的既定行为）。
        return True


def _config_disk_cache_path() -> Path:
    """写入文件缓存位置：**引导根目录**，按 config 路径分槽，不依赖配置解析。

    这是解开「自锁」的关键：状态目录（argo_paths.state_root）要靠 config.yaml
    的 cache.db_path 才能算出来，而缓存的意义正是省掉这次解析——把缓存放进
    状态目录，就变成了「为了找缓存必须先解析配置」。故放在一个静态可推导的
    引导位置：ARGO_STATE_DIR（测试/多环境隔离）优先，否则历史默认根。

    文件名按 config.yaml 的绝对路径分槽：同机多份 argo（skills/argo 与一份
    backup 副本、或两个不同 checkout）共用引导根时，此前会互相顶掉对方的缓存，
    每次调用都退化成全量解析——命中率归零，收益归零（审计实测 5 次运行 5 次
    全解析）。分槽后各写各的，键里的 config_path 仍留作第二道校验。
    """
    override = os.environ.get(argo_paths.ENV_STATE_DIR, "").strip()
    root = (Path(os.path.expanduser(override)) if override
            else argo_paths.legacy_root())
    slot = hashlib.sha256(str(_config_path()).encode("utf-8")).hexdigest()[:16]
    return root / f"config-cache-{slot}.json"


def _json_round_trip_safe(obj: Any) -> bool:
    """配置能否无损过一遍 JSON：字典键必须都是字符串。

    YAML 允许 `1: x` 这样的非字符串键，json.dumps 会静默写成 "1"，回读时键
    类型已变——缓存不得改变语义，发现这类键就整个不写缓存（退回每次解析）。
    """
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _json_round_trip_safe(v)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return all(_json_round_trip_safe(v) for v in obj)
    return True


def _config_content_digest(st: os.stat_result) -> str | None:
    """config.yaml 的**内容摘要**（blake2b/16 hex），进程内按 stat 记忆。

    为什么用内容而不是 stat 字段：这是跨平台正确性的分水岭。
    - `st_mtime`/`st_size` 可以被 `touch -r`、`cp -p`、`tar -x` 原样还原；
    - `st_ctime` 在 POSIX 是「元数据变更时间」（能兜住还原 mtime 的写法），
      但**在 Windows 是创建时间**——改写内容后它根本不变，于是同一份代码在
      两个平台上给出不同的失效保真度，Windows 上「改了不生效」会静默复现。
    内容摘要没有这个问题：两个平台都只认字节。代价是 121 KB 读 + 哈希实测
    0.47 ms，而它换来的是省下 19–35 ms/次，且决定状态目录（cache.db_path）
    的那份文件从此不可能读到旧版本。

    读不到（权限/被删）返回 None，调用方按「无摘要」处理——宁可不命中缓存，
    也不要拿一份源文件不明的摘要当命中依据。
    """
    global _content_digest_memo
    key = _config_db_path_key(st)
    if _content_digest_memo is not None and _content_digest_memo[0] == key:
        return _content_digest_memo[1]
    try:
        digest: str | None = hashlib.blake2b(
            _config_path().read_bytes(), digest_size=16).hexdigest()
    except OSError:
        digest = None
    _content_digest_memo = (key, digest)
    return digest


def _config_disk_cache_key(st: os.stat_result,
                           scan: tuple[float, int, int, str],
                           digest: str | None) -> dict[str, Any]:
    """缓存指纹：凡能改变「解析结果」的输入都要进键。

    - config_digest：config.yaml 的**内容摘要**。这是唯一的强判据——mtime 能被
      `cp -p`/`touch -r` 还原，ctime 在 Windows 是创建时间（改写不变），只有
      内容不会骗人。摘要取不到（None）时键必然不等于任何已存载荷，等价于
      「不使用缓存」，这是刻意的保守选择。
    - ext_digest：外置声明的集合摘要（每文件的相对路径/大小/mtime/ctime 聚合）。
      只取 max_mtime 对「删除声明」与「拷入旧时间戳的声明」是盲的，会让缓存继续
      给出一份「引擎删了还在 / 加了不生效」的配置。
    - loader_sig：解释这份配置的代码变了（归一化逻辑改了）也要失效，
      否则升级后读回的是旧逻辑写下的结果。
    - home：`~` 展开依赖 HOME，同一个 ARGO_STATE_DIR 下换 HOME 会拿到
      上一个 HOME 的展开路径。
    """
    return {
        "config_path": str(_config_path()),
        "config_digest": digest,
        "config_size": st.st_size,
        "ext_digest": scan[3],
        "ext_files": scan[1],
        "loader_sig": _loader_sig(),
        "home": os.path.expanduser("~"),
    }


def _config_db_path_key(st: os.stat_result) -> tuple[str, int, int, int]:
    """config.yaml 的**进程内记忆键**（stat 三元 + ctime）——只用于避免同一进程
    重复读盘，不承担跨进程失效判据（那是 config_digest 的职责）。

    它自身不跨进程，也就没有平台语义问题：Windows 上 ctime 是创建时间，这里
    退化为 (路径, mtime, size) 三重判据，而进程内改写文件必然同时改 mtime。
    """
    return (str(_config_path()), st.st_mtime_ns, st.st_size, st.st_ctime_ns)


def _looks_like_config(cfg: Any) -> bool:
    """缓存载荷的结构自检：不可信输入不得让加载链崩掉。

    缓存文件可能被外部改写、写坏、或来自结构已变的版本（schema 只防我们自己
    升格式，防不了手工编辑）。`engines` 被改成字符串这类结构损坏，会让下游
    `cfg.get("engines", {}).items()` 抛 AttributeError——表现是「每条命令都崩
    且看不出跟缓存有关」，用户无从知道要删哪个文件。结构不符一律当「没有缓存」
    退回解析链（解析链随后会重写这份缓存，自愈）。
    """
    if not isinstance(cfg, dict):
        return False
    for key in ("engines", "domains", "cache", "execution", "network", "output"):
        if key in cfg and not isinstance(cfg[key], (dict, list)):
            return False
    return True


_disk_cache_memo: tuple[tuple[str, int, int, int], dict[str, Any] | None] | None = None
_content_digest_memo: tuple[tuple[str, int, int, int], str | None] | None = None


def _disk_cache_payload() -> dict[str, Any] | None:
    """读写入文件缓存文件的原始载荷（进程内按 config.yaml 的 stat 记忆）。

    peek 与 load_config 都从这里取，一次进程最多读盘一次；读取失败（无缓存 /
    损坏 / 被裁剪）一律返回 None，由调用方走完整解析链。
    """
    global _disk_cache_memo
    if not _config_disk_cache_enabled():
        return None
    try:
        st = _config_path().stat()
    except OSError:
        return None
    key = _config_db_path_key(st)
    if _disk_cache_memo is not None and _disk_cache_memo[0] == key:
        return _disk_cache_memo[1]
    payload: dict[str, Any] | None = None
    try:
        raw = json.loads(_config_disk_cache_path().read_text(encoding="utf-8"))
        if (isinstance(raw, dict)
                and raw.get("schema") == _CONFIG_DISK_CACHE_SCHEMA
                and isinstance(raw.get("key"), dict)
                and isinstance(raw.get("config"), dict)):
            payload = raw
    except (OSError, ValueError):
        payload = None
    _disk_cache_memo = (key, payload)
    return payload


def _load_config_disk_cache(st: os.stat_result,
                           scan: tuple[float, int, int, str],
                           digest: str | None) -> dict[str, Any] | None:
    """取写入文件配置缓存；指纹不完全匹配或结构不对则返回 None（走完整解析链）。"""
    if digest is None:
        return None          # 摘要取不到 → 不信任任何已存载荷（保守优先）
    raw = _disk_cache_payload()
    if raw is None or raw.get("key") != _config_disk_cache_key(st, scan, digest):
        return None
    cfg = raw.get("config")
    return cfg if _looks_like_config(cfg) else None


def _peek_disk_cache_db_path(st: os.stat_result,
                             digest: str | None) -> tuple[bool, str | None]:
    """从写入文件缓存里取 cache.db_path → (命中, db_path)。

    命中时 import 链上的路径派生（argo_paths）连 YAML 都不用解析——这是冷启动
    固定开销里最后一块可省的重复劳动：config.yaml 有 121 KB，C 版 loader 解析
    一遍约 20 ms，而派生只需要里面一个标量。

    **必须校验 config_digest**：db_path 是状态目录的源头（argo_paths.state_root），
    只比 config_path 会漏掉「配置改了但缓存文件还在」的窗口——peek 拿到旧路径、
    load_config 拿到新路径，两处计算方式分裂，长驻进程整个生命周期都会把状态文件
    写到旧目录（审计实测复现）。用内容摘要而不是 stat 字段，是因为 stat 可被
    `touch -r`/`cp -p` 还原、ctime 在 Windows 又只是创建时间（同一份代码在两个
    平台上失效保真度不同）。外置声明摘要不在此校验：db_path 只由 config.yaml
    本身决定。

    返回的路径与走 YAML 时**同为展开后的形式**（~ 已展开）：两条分支计算方式必须
    一致，否则同一个进程里先命中缓存、后走解析会拿到两种形态，给未来的调用者
    埋雷（当前唯一消费者 argo_paths 会再 expanduser 一次，所以现在看不出来）。
    """
    if digest is None:
        return False, None
    raw = _disk_cache_payload()
    if raw is None:
        return False, None
    key = raw.get("key") or {}
    if (key.get("config_path"), key.get("config_digest")) != (
            str(_config_path()), digest):
        return False, None
    cfg = raw.get("config")
    if not _looks_like_config(cfg):
        return False, None
    cache_cfg = cfg.get("cache")
    if not isinstance(cache_cfg, dict):
        return True, None
    db_path = cache_cfg.get("db_path")
    return True, (os.path.expanduser(str(db_path)) if db_path else None)


def _save_config_disk_cache(st: os.stat_result, scan: tuple[float, int, int, str],
                            digest: str | None,
                            config: dict[str, Any]) -> None:
    """原子写写入文件缓存；任何失败静默（只读环境退回每次解析的老路）。

    存的是**归一化后、校验前**的配置：`_validate_engine_paths` 的结论依赖
    运行环境（该机器 PATH 上有没有这个 CLI、文件在不在），必须每次现算——
    把「已禁用」的结论一起缓存，会让在 A 机器（缺某 CLI）写下的判定被 B 机器
    读成事实，正是这套配置里最忌讳的「结论当输入存」。
    """
    global _disk_cache_memo
    if digest is None or not _config_disk_cache_enabled() \
            or not _json_round_trip_safe(config):
        return
    payload = {
        "schema": _CONFIG_DISK_CACHE_SCHEMA,
        "key": _config_disk_cache_key(st, scan, digest),
        "config": config,
    }
    path = _config_disk_cache_path()
    try:
        # 原子写走 argo_paths 的唯一来源（mkstemp 唯一 tmp + os.replace 的
        # 正确性在一处维护，多进程并行写同一目标不会互相搬走 tmp）
        argo_paths.atomic_write_json(path, payload, indent=None)
    except (OSError, TypeError, ValueError):
        # TypeError：YAML 里的日期/自定义类型无法进 JSON——整份跳过，不降级
        # 成 default=str（那会静默改变类型语义）
        _disk_cache_memo = None
        return
    # 刚写的这份就是本进程的最新真值，直接填入记忆，省掉一次回读
    _disk_cache_memo = (_config_db_path_key(st), payload)
    _sweep_orphan_cache_slots(path)


def _sweep_orphan_cache_slots(keep: Path) -> None:
    """回收「config_path 已不存在」的缓存槽；任何失败静默。

    why（2026-09-17 实测）：引导根里 8 个槽中有 6 个属于 `/tmp` 下的临时
    checkout（测试与基准跑出来的），每个约 142 KB 且永久留存，合计 1.13 MB。

    判据只能是「那份 config.yaml 还在不在」：槽按 config_path 分是**多 checkout
    安全所必需的**（见 _config_disk_cache_path 的说明），所以不能按「同一个
    config 的旧摘要」来删——那会误删另一个 checkout 正在用的槽。只删 config_path
    在盘上已消失的槽，既不误伤活着的 checkout，又能自愈任何来源的临时副本。

    时机：只在写缓存时顺带扫（缓存未命中才写，属低频）。临时 checkout 自己写槽
    时就会回收上一批的遗留，所以污染源活跃时回收也随之发生。
    """
    if not _config_disk_cache_enabled():
        return
    try:
        siblings = list(keep.parent.glob("config-cache-*.json"))
    except OSError:
        return
    for path in siblings:
        if path == keep:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cfg_path = str(((payload.get("key") or {}).get("config_path")) or "").strip()
        except (OSError, ValueError, AttributeError):
            # 读不动或结构不认识：一律不动。判定不了就不删，是这里的保守选择。
            continue
        if cfg_path and not Path(cfg_path).exists():
            try:
                path.unlink()
            except OSError:
                continue
