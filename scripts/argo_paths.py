#!/usr/bin/env python3
"""argo_paths.py — argo 本地状态目录的唯一来源。

背景：此前 11 个模块各自拼 ~/.cache/unified-search，构造方式分裂成 4 种
（Path.home()/".cache"/...、expanduser("~/.cache/...")、字面量字符串、
config 默认值），导致 config.yaml 的 cache.db_path 管不住 quota.json、
health.db 等文件，测试也难以整体隔离。

现在所有状态路径统一由本模块派生：
  - 根目录可被 ARGO_STATE_DIR 覆盖（测试隔离 / 只读环境 / 显式换目录）
  - 其次认 config.yaml 的 cache.db_path 所在目录，保持向后兼容
  - 再次：历史目录已存在就继续用（不迁移），否则落**平台惯例**目录
    （Windows %LOCALAPPDATA%/unified-search；POSIX 遵循 XDG_CACHE_HOME）
  - 各模块只声明「文件名」，不再各自拼目录
  - `python3 scripts/argo_paths.py [--json]` 可打印关键路径的实际来源

注意：User-Agent 里的 unified-search@local 是邮箱标识，与状态目录无关，
不在此处管理。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from cli_io import dumps, dumps_pretty

# 环境变量覆盖：优先级最高，用于测试隔离与只读环境
ENV_STATE_DIR = "ARGO_STATE_DIR"
# 平台惯例变量（只读，不新增 argo 私有名字）
ENV_XDG_CACHE = "XDG_CACHE_HOME"
ENV_LOCALAPPDATA = "LOCALAPPDATA"

# 历史默认目录（也是 config.yaml 中 db_path 的默认前缀）
_LEGACY_ROOT = "~/.cache/unified-search"

# 缓存配置段未就绪时的保底（config 不可用、PyYAML 缺失等场景）
_FALLBACK_ROOT = _LEGACY_ROOT

# 应用名（平台惯例目录里的子目录名）
_APP_DIRNAME = "unified-search"


def _platform_cache_root(env: Mapping[str, str] | None = None,
                         platform: str | None = None) -> Path | None:
    """平台惯例的缓存根（不含应用名）：Windows → %LOCALAPPDATA%；其余 → $XDG_CACHE_HOME。

    未设置对应变量时返回 None，由调用方回落到 Unix 惯例 ~/.cache。抽成纯函数
    （可注入 env 与 platform）是为了能在 macOS 上直接单测 Windows 分支——否则
    「Windows 上取哪个目录」永远只有真机才能验证。
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        base = str(env.get(ENV_LOCALAPPDATA) or "").strip()
    else:
        base = str(env.get(ENV_XDG_CACHE) or "").strip()
    return Path(os.path.expanduser(base)) if base else None


def platform_cache_default() -> Path:
    """平台惯例的默认状态目录（不含覆盖与配置影响）。

    Windows：%LOCALAPPDATA%\\unified-search；POSIX（Linux/BSD/macOS）：遵循
    XDG_CACHE_HOME，未设置时 ~/.cache/unified-search（XDG 默认值，与历史一致）。
    """
    root = _platform_cache_root()
    if root is not None:
        return root / _APP_DIRNAME
    return Path(os.path.expanduser(_LEGACY_ROOT))


def state_root() -> Path:
    """返回 argo 本地状态根目录（已 expanduser，不保证存在）。

    优先级（**存量优先**，避免静默搬家丢掉用户的缓存与配额计数）：

      1. ARGO_STATE_DIR 环境变量（显式覆盖，测试隔离与只读环境都用它）
      2. config.yaml cache.db_path 的父目录（保证与主缓存同域）
      3. 历史目录 ~/.cache/unified-search（**已存在**时继续用；不迁移）
      4. 平台惯例目录：Windows → %LOCALAPPDATA%\\unified-search；
         POSIX → $XDG_CACHE_HOME/unified-search（该变量设置时）
      5. 保底：~/.cache/unified-search（即 POSIX 惯例的 XDG 默认值）

    为什么「已存在的历史目录」排在平台惯例之前：平台变量（XDG_CACHE_HOME /
    LOCALAPPDATA）在 Windows 上恒有值、在部分 Linux 桌面也常被设置，若它们无条件
    优先，存量用户升级后会发现搜索缓存、配额计数、准入记录全部"归零"——那是比
    「目录不够惯例」严重得多的问题。想让 argo 换目录的用户用 ARGO_STATE_DIR
    显式指定（或直接改 config.yaml 的 cache.db_path），一步到位且可预期。
    """
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        return Path(os.path.expanduser(override))

    db_path = _config_db_path()
    if db_path:
        expanded = os.path.expanduser(db_path)
        parent = os.path.dirname(expanded)
        if parent:
            return Path(parent)

    legacy = Path(os.path.expanduser(_LEGACY_ROOT))
    try:
        if legacy.is_dir():
            return legacy
    except OSError:
        pass
    return platform_cache_default()


def resolved_paths() -> dict[str, str]:
    """诊断用：把关键路径的**实际来源**摊开（`python3 scripts/argo_paths.py`）。

    支持类问题的第一步永远是「你到底在读哪个文件」；此前这些路径散在
    state_root / config.peek_cache_db_path / engine_env._envfile_paths 三处，
    只能靠读代码推。这里只做只读汇报，不产生任何副作用。
    """
    out: dict[str, str] = {}
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        out["state_source"] = f"{ENV_STATE_DIR}={override}"
    elif _config_db_path():
        out["state_source"] = "config.yaml cache.db_path"
    elif Path(os.path.expanduser(_LEGACY_ROOT)).is_dir():
        out["state_source"] = "历史默认目录（已存在，未迁移）"
    else:
        out["state_source"] = "平台惯例默认"
    out["state_root"] = str(state_root())
    out["platform_cache_default"] = str(platform_cache_default())
    out["legacy_root"] = str(Path(os.path.expanduser(_LEGACY_ROOT)))
    try:
        import engine_env
        env_paths = [str(p) for p in engine_env._envfile_paths()]
        out["env_files"] = os.pathsep.join(env_paths)
        out["env_file_in_use"] = next(
            (p for p in env_paths if Path(p).is_file()), "(均不存在)")
    except Exception as e:      # engine_env 不可用不该让诊断本身失败
        out["env_files"] = f"(不可用：{e})"
    return out


def migrate_legacy_state(*, yes: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """把历史目录里的状态搬到**平台惯例目录**（可选命令，默认只报告不执行）。

    为什么需要它：解析顺序刻意让「已存在的历史目录」优先（避免升级后状态归零），
    代价是**存量安装永远留在历史路径**——Windows 用户想要 `%LOCALAPPDATA%`、
    Linux 用户想要 `$XDG_CACHE_HOME` 时，此前只能设 `ARGO_STATE_DIR`（还要自己
    把旧状态搬过去）。这个命令把「搬 + 让惯例生效」合成一步。

    安全边界（这是会动用户数据的命令，默认什么都不做）：

    - 仅当根目录由**历史默认**决定时才可迁移。`ARGO_STATE_DIR` 或 config.yaml 的
      `cache.db_path` 说了算时直接拒绝——那是用户明确指定的位置，不该被搬家。
    - 目标目录已存在且有内容时拒绝（绝不覆盖）。
    - 必须显式传 `yes=True`/`--yes` 才会动数据（不做 TTY 探测：那既不是可靠判据，
      也让脚本与交互两种场景行为不一致）。
    - 搬完尝试 `rmdir` 历史目录（只在空时成功）。这一步是关键：留着空目录会让
      `state_root()` 继续判「历史存在」而停在旧路径，用户会以为数据丢了。
      目录里还有别的文件（不是 argo 的）时 rmdir 失败，此时明确提示用户设
      `ARGO_STATE_DIR` 或自行清理。
    - 返回结构化结果（moved/skipped/errors），供 CLI 打印与测试断言。
    """
    legacy = Path(os.path.expanduser(_LEGACY_ROOT))
    target = platform_cache_default()
    result: dict[str, Any] = {
        "legacy_root": str(legacy), "target": str(target),
        "moved": [], "errors": [], "status": "", "dry_run": bool(dry_run),
    }

    if os.environ.get(ENV_STATE_DIR, "").strip():
        result["status"] = f"跳过：{ENV_STATE_DIR} 已显式指定状态目录（那是权威，不改）"
        return result
    if _config_db_path():
        result["status"] = "跳过：config.yaml 的 cache.db_path 已指定位置（那是权威，不改）"
        return result
    if legacy == target:
        result["status"] = "无需迁移：历史目录就是平台惯例目录（POSIX 默认情形）"
        return result
    if not legacy.is_dir():
        result["status"] = "无需迁移：历史目录不存在（新安装直接走平台惯例）"
        return result

    entries = sorted(p for p in legacy.iterdir() if p.name != "MIGRATED_TO.txt")
    if not entries:
        result["status"] = "无需迁移：历史目录是空的"
        return result
    target_occupied = target.is_dir() and any(target.iterdir())
    if target_occupied:
        result["status"] = f"拒绝：目标目录已有内容 {target}（不覆盖现有状态）"
        return result
    if dry_run:
        # 预演是只读的，不需要 --yes：先看清楚会搬什么，再决定执不执行
        result["status"] = "预演：以下内容将被移动（未执行）"
        result["moved"] = [p.name for p in entries]
        return result
    if not yes:
        # 不用 TTY 探测做交互确认：一来「是不是 tty」不是「要不要执行」的可靠判据
        # （本仓有专门检查挡 isatty），二来数据搬迁命令在脚本里也该有完全一致的
        # 行为——要么显式 --yes，要么不动。
        result["status"] = "拒绝：需显式加 --yes（该命令会移动用户数据；--dry-run 可先看内容）"
        return result

    import shutil as _shutil
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result["status"] = f"失败：无法创建目标目录 {target}（{e}）"
        return result

    for p in entries:
        try:
            _shutil.move(str(p), str(target / p.name))   # 跨设备时自动退化复制+删除
            result["moved"].append(p.name)
        except (OSError, _shutil.Error) as e:
            result["errors"].append(f"{p.name}: {e}")

    try:
        legacy.rmdir()          # 仅在空了之后成功——留着会让惯例不生效
        result["legacy_removed"] = True
    except OSError:
        result["legacy_removed"] = False
        try:
            (legacy / "MIGRATED_TO.txt").write_text(
                f"argos 状态已迁移到：{target}\n本目录剩余内容不属于 argo 或无法移动；"
                f"可自行清理，或用 {ENV_STATE_DIR} 显式指定状态目录。\n",
                encoding="utf-8")
        except OSError:
            pass

    if result["errors"]:
        result["status"] = f"部分完成：{len(result['moved'])} 项已移动，{len(result['errors'])} 项失败"
    else:
        result["status"] = (
            f"完成：{len(result['moved'])} 项已移动到 {target}"
            + ("" if result.get("legacy_removed") else "（历史目录未清空，见其中的 MIGRATED_TO.txt）"))
    return result


def _lock_impl_name() -> str:
    """当前平台实际可用的锁实现——真机验证时第一眼要看的东西。"""
    try:
        import fcntl
        if hasattr(fcntl, "flock"):
            return "flock（POSIX）"
    except ImportError:
        pass
    try:
        import msvcrt
        if hasattr(msvcrt, "locking"):
            return "msvcrt（Windows）"
    except ImportError:
        pass
    return "无（fail-open：不阻断主路径，但多进程状态下会有丢更新）"


def _check_lock_roundtrip(hold_s: float) -> tuple[str, str]:
    """真起一个子进程握住锁，父进程再抢一次——验证互斥真的生效。

    这是平台相关的关键自检：Windows 的 msvcrt 分支在 macOS 上只能用假模块测契约，
    真机行为（锁区间语义、跨进程可见性）只有在目标机器上跑一次才算验过。
    """
    import subprocess
    import tempfile as _tempfile
    lock_path = state_path(".lock-check")
    # 锁文件建不出来时(file_lock 会按设计 fail-open)必须说清原因，否则这条自检
    # 看起来像"锁的实现坏了"，而真正的问题在目录可写性上。
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
    except OSError as e:
        return "warn", (f"锁文件无法创建（{lock_path.parent} 不可写：{e}）——"
                        f"按设计 fail-open，先解决状态目录可写性再看这项")
    child = _tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    child.write(
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from pathlib import Path\n"
        "import argo_paths\n"
        f"with argo_paths.file_lock(Path({str(lock_path)!r}), timeout=5.0):\n"
        "    print('LOCKED', flush=True)\n"
        f"    time.sleep({hold_s!r})\n")
    child.close()
    proc = None
    try:
        proc = subprocess.Popen([sys.executable, child.name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        line = proc.stdout.readline().strip() if proc.stdout else ""
        if line != "LOCKED":
            err = (proc.stderr.read() if proc.stderr else "").strip()[:200]
            return "fail", f"子进程没能持锁（stdout={line!r} stderr={err!r}）"
        t0 = time.monotonic()
        with file_lock(lock_path, timeout=3.0):
            waited = (time.monotonic() - t0) * 1000
        if waited < 100:
            return "fail", f"第二个持有者没被挡住（{waited:.0f} ms 就拿到锁）"
        return "pass", f"互斥生效：第二个持有者等待 {waited:.0f} ms"
    finally:
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for p in (child.name, str(lock_path)):
            try:
                os.unlink(p)
            except OSError:
                pass


def run_checks(lock_hold_s: float = 0.6) -> list[dict[str, Any]]:
    """在**当前机器**上实测一遍路径解析与关键能力（`argo paths --check`）。

    为什么要有它：跨平台分支（Windows 用哪个目录、msvcrt 锁是否真互斥、解释器候选
    最终落到谁）在开发机上只能做契约级测试，真机验证此前得靠人肉翻代码。这里把
    「这台机器上实际发生了什么」一次打印清楚——任何平台一条命令自行验证。
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    info = resolved_paths()
    add("平台", "info",
        f"{sys.platform} | 状态目录来源：{info['state_source']} | 锁实现：{_lock_impl_name()}")

    root = state_root()
    add("状态目录解析", "pass", f"{root}（来源：{info['state_source']}）")
    if (str(root) == info["legacy_root"]
            and str(root) != info["platform_cache_default"]):
        add("惯例提示", "warn",
            f"当前用历史目录，平台惯例目录是 {info['platform_cache_default']}；"
            f"想切换先 `argo paths --migrate --dry-run` 看内容")

    probe = root / ".write-check"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("状态目录可写", "pass", str(root))
    except OSError as e:
        add("状态目录可写", "fail",
            f"{root} 不可写：{e}（可设 ARGO_STATE_DIR 指向可写目录）")

    env_files = [p for p in info["env_files"].split(os.pathsep) if p]
    if any(Path(p).is_file() for p in env_files):
        try:
            import engine_env
            keys = engine_env._envfile_load()
            add("密钥文件", "pass",
                f"在读 {info['env_file_in_use']}（{len(keys)} 个键；值不回显）")
        except Exception as e:
            add("密钥文件", "fail", f"{info['env_file_in_use']} 解析失败：{e}")
    else:
        add("密钥文件", "warn",
            f"候选均不存在：{'、'.join(env_files)}（未配置密钥时属正常）")

    try:
        from config import load_config
        cfg = load_config()
        n = len([k for k, v in (cfg.get("engines") or {}).items() if isinstance(v, dict)])
        add("配置加载", "pass", f"engines={n}")
    except Exception as e:
        add("配置加载", "fail", f"load_config 失败：{e}")

    try:
        from importlib.machinery import SourceFileLoader
        import importlib.util as _ilu
        bin_path = Path(__file__).parent.parent / "bin" / "argo"
        spec = _ilu.spec_from_file_location(
            "argo_bin_check", bin_path,
            loader=SourceFileLoader("argo_bin_check", str(bin_path)))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if mod._self_capable():
            add("解释器", "pass", f"当前解释器就地执行（{sys.executable}）")
        else:
            picked = mod._pick_python()
            add("解释器", "pass" if picked else "fail",
                f"探测结果：{picked or '未找到可用解释器'}")
    except Exception as e:
        add("解释器", "warn", f"未能检查（{e}）")

    try:
        status, detail = _check_lock_roundtrip(lock_hold_s)
        add("跨进程锁", status, detail)
    except Exception as e:
        add("跨进程锁", "fail", f"自检异常：{e}")

    return checks


def _cli() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="argo 路径诊断：状态目录与密钥文件到底解析到了哪里")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--check", action="store_true",
                        help="实测本机解析与能力（目录可写性 / 跨进程锁 / 配置 / 解释器）")
    parser.add_argument("--migrate", action="store_true",
                        help="把历史状态目录搬到平台惯例目录（默认只报告）")
    parser.add_argument("--yes", action="store_true",
                        help="配合 --migrate：确认执行（该命令会移动数据）")
    parser.add_argument("--dry-run", action="store_true",
                        help="配合 --migrate：只列出将移动的内容")
    args = parser.parse_args()

    if args.check:
        checks = run_checks()
        if args.json:
            print(dumps(checks))
        else:
            marks = {"pass": "通过", "fail": "失败", "warn": "注意", "info": "信息"}
            print(f"argo 路径自检（{sys.platform}）")
            for c in checks:
                print(f"  [{marks.get(c['status'], c['status'])}] {c['check']}：{c['detail']}")
        return 0 if all(c["status"] != "fail" for c in checks) else 1

    if args.migrate:
        result = migrate_legacy_state(yes=args.yes, dry_run=args.dry_run)
        if args.json:
            print(dumps(result))
        else:
            print(f"状态：{result['status']}")
            for name in result["moved"]:
                print(f"  移动 {name} → {result['target']}")
            for err in result["errors"]:
                print(f"  失败 {err}")
            if not args.dry_run and result["moved"]:
                print(f"此后 state_root 解析为：{state_root()}")
        return 0 if not result["errors"] else 1

    info = resolved_paths()
    if args.json:
        print(dumps(info))
    else:
        for k, v in info.items():
            print(f"{k:22} {v}")
    return 0


def _config_db_path() -> str | None:
    """从 config.yaml 读 cache.db_path；不可用时返回 None。

    config 模块本身可能不可用（PyYAML 缺失 / 配置文件损坏），
    此处必须 fail-open，否则路径派生会连带崩溃。

    走 peek_cache_db_path() 轻量读取：get_cache_config() 会触发 load_config()
    合并全部外置引擎 spec，而 import cache 时就会调到本函数——为一个标量付
    整轮合并不值得。

    （勘误 2026-09-15：此处的「约 1.7s」无法复现。实测 load_config() 为
    纯 Python loader 107 ms / C 版 15 ms；真实问题是 import 链上这条派生被
    连调 4 次，且每次都用慢的 loader 解析 123 KB 配置。）
    """
    try:
        from config import peek_cache_db_path
        return peek_cache_db_path()
    except Exception:
        return None


def state_path(*parts: str) -> Path:
    """返回状态根目录下的路径（不自动创建目录）。

    state_path("health.db")        → <root>/health.db
    state_path("admission", "x.json") → <root>/admission/x.json
    """
    return state_root().joinpath(*parts)


def ensure_state_dir(*parts: str) -> Path:
    """返回状态根目录下的子目录，并确保其存在。

    目录不可创建时（只读挂载 / 权限受限）fail-open 返回目标路径——
    由调用方在写入时处理，避免 import 期就崩。
    """
    d = state_path(*parts)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def legacy_root() -> Path:
    """历史默认目录（仅用于迁移/兼容判断，新代码请用 state_root）。"""
    return Path(os.path.expanduser(_LEGACY_ROOT))


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 10.0):
    """跨进程排他锁（阻塞获取；超时按 fail-open 放行，见下方「fail-open」段）。

    为什么需要：状态文件的「读-改-写」序列只在**进程内**加锁，
    CLI / MCP server / 评测脚本三者并行时，进程 A 读到旧状态、
    进程 B 也读到旧状态，各自 +1 后依次覆盖，后写者抹掉前者的增量
    （实测 6 进程 × 60 次 record 状态丢失 77%）。

    实现：flock 锁在独立的 .lock 文件上，不与被保护的数据文件共享
    inode——数据文件靠 os.replace 整体替换，若锁与数据同 inode，
    替换后新进程会锁到另一个 inode 而形同无锁。

    fail-open：两种情况都会直接放行、绝不因观测层问题阻断搜索主路径——
    (1) 平台不支持任何锁实现；(2) 超时抢不到锁（此时会 log warning 到
    `argo.file_lock`，让运维看得到「无锁执行」发生过，但不抛异常）。
    契约由 tests/test_argo_paths.py::TestFileLock::test_timeout_fails_open 锁死。
    """
    lock_path = path.parent / f".{path.name}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return

    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return

    try:
        import fcntl  # POSIX
    except ImportError:
        fcntl = None

    lock_impl = _make_lock_impl(fcntl)
    if lock_impl is None:
        # 两个平台都没有可用锁（不认识的系统）→ fail-open，绝不阻断主路径
        try:
            yield
        finally:
            os.close(fd)
        return

    acquire, release = lock_impl
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            acquire(fd)
            acquired = True
            break
        except OSError:
            if time.monotonic() >= deadline:
                # 有意 fail-open：锁是保护层，不该成为单点故障（见
                # tests/test_argo_paths.py::TestFileLock::test_timeout_fails_open）。
                # 但也不能纯静默——让运维看得到「无锁执行」发生过。
                import logging
                logging.getLogger("argo.file_lock").warning(
                    "file_lock(%s) timed out after %.1fs; proceeding without lock "
                    "(fail-open by design; read-modify-write races possible)",
                    path, timeout,
                )
                break
            time.sleep(0.002)
    try:
        yield
    finally:
        if acquired:
            try:
                release(fd)
            except OSError:
                pass
        os.close(fd)


def _make_lock_impl(fcntl: Any):
    """选一套可用的文件锁实现 → (acquire(fd), release(fd))；都不可用返回 None。

    为什么要两套：Windows 没有 `fcntl`，此前这里直接 fail-open——于是
    「6 进程 × 60 次 record 状态丢失 77%」这类丢更新在 Windows 上原样复现，
    而调用方完全看不出来（没有任何日志，锁看起来"加了"）。Windows 上的对应
    设施是 `msvcrt.locking`：对文件的一段字节区间加锁，语义与 flock 足够接近。

    差异点（都在这里吸收掉，不让调用方感知）：
    - msvcrt 锁的是**相对当前文件位置**的字节区间 → 每次加/解锁前 seek(0)；
    - 空文件上锁区间为空，先写一个字节把它变成合法区间（内容是占位，无人读）；
    - msvcrt 的 LK_NBLCK 抢不到时抛 OSError，与 flock 的 LOCK_NB 一致，
      所以外层重试循环两种实现可以共用。
    """
    if fcntl is not None and hasattr(fcntl, "flock"):
        def _acq(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _rel(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)

        return _acq, _rel

    try:
        import msvcrt  # Windows
    except ImportError:
        return None

    def _acq_win(fd: int) -> None:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")      # 让锁区间非空
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _rel_win(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    return _acq_win, _rel_win


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """原子写文本文件的唯一来源（任意内容，非仅 JSON）。

    临时文件用 mkstemp 取**进程内唯一**名字（同目录，保证同文件系统
    rename 语义），失败路径只清理自己的 tmp。`mode` 非空时对最终文件
    收紧权限（密钥类配置写 0600）。
    """
    import tempfile  # 延迟导入：tempfile→shutil→random 子树实测 ~6ms，
    # 纯缓存命中的搜索一次原子写都不做，没理由为它每次进程启动都付这笔钱
    # （与 config.py 的惰性 logging 同一范式；_check_lock_roundtrip 已在用）。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        # 只清理自己创建的 tmp；别人的 tmp 不归本进程管
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = 2) -> None:
    """原子写 JSON 状态的唯一来源。

    why：此前 quota / circuit_breaker / lang_pref / v2ex_nodes / job /
    fetch_v3 / redskill 多处各自手写 `p.with_suffix(".tmp")` + replace，
    **临时文件名固定**。多进程（CLI 与 MCP server 并行、或评测脚本）
    同时写同一个文件时，A 的 replace 会把 B 的 tmp 一起搬走/删掉，B 再
    replace 就抛 FileNotFoundError。实测 6 进程 × 60 次 record：崩溃 235
    次、成功仅 125 次、状态丢失 68%——配额计数因此系统性偏低，且
    errors > used 的反常正是这么来的。

    修法：临时文件用 mkstemp 取**进程内唯一**名字（同目录，保证同文件
    系统 rename 语义），失败路径清理自己的 tmp，绝不触碰别人的。
    实现在 atomic_write_text；本函数只负责序列化。
    """
    content = dumps_pretty(payload) if indent else dumps(payload)
    atomic_write_text(path, content)


# WAL 回缩上限与自动检查点页数：argo 状态库的统一 WAL 策略。
#
# 为什么需要（2026-09-17 实测）：SQLite 默认 `journal_size_limit=-1`，检查点后
# 不回缩 `-wal`，文件长期停在自动检查点阈值（1000 页 ≈ 3.94 MB）上。实测
# 「limit=1 MB + autocheckpoint=256 页」把稳态压到 0.95 MB；只设 limit 不降
# 检查点阈值无效（仍 2.84 MB）——**真正起作用的是检查点频率**。
# 本机 cache.db + adaptive.db 两库合计省约 6 MB。
WAL_SIZE_LIMIT_BYTES = 1024 * 1024
WAL_AUTOCHECKPOINT_PAGES = 256


def apply_state_pragmas(conn: Any) -> None:
    """argo 状态库连接的统一设置：WAL + 回缩上限 + 检查点阈值。

    why：此前 cache.py / adaptive.py / health_probe.py 各自写一份
    `PRAGMA journal_mode=WAL`，三份必然漂移——新增状态库时只会照抄
    journal_mode，漏掉回缩上限；改策略时也要改三处。收敛到一处的代价是
    多一次函数调用，收益是「WAL 策略」这件事只有一个定义点。

    只改 PRAGMA，不碰连接生命周期（各模块的连接复用方式不同，刻意不统一）。
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT_BYTES}")
    conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")


def isolate_state_dir(tag: str = "argo-dev") -> Path:
    """把状态目录重定向到独立临时目录，并返回该目录。

    **必须在 import 任何 argo 状态模块之前调用**——quota / circuit_breaker /
    cache / adaptive 等在模块级就按当时的 ARGO_STATE_DIR 定下路径常量，
    之后再改环境变量不生效。

    为什么需要：开发/评测脚本（ab_eval、matrix_search_eval、benchmark…）
    会走真实搜索路径，从而写生产状态。实测污染后果——真实
    circuit_breaker.json 里混进 190 个 `eng_<8hex>` 夹具条目、`probe…`、
    `bad` 等测试引擎，熔断统计被稀释，且这些脏条目永久留在生产状态里。
    """
    import tempfile  # 延迟导入（同 atomic_write_text）：隔离状态目录才需要
    d = Path(tempfile.mkdtemp(prefix=f"{tag}-state-"))
    os.environ[ENV_STATE_DIR] = str(d)
    return d


def db_path() -> Path:
    """主缓存库路径。

    与 state_path("cache.db") 的差别只在「config.yaml 里用户显式改写了
    db_path」这一种情况：此时尊重用户的显式配置，不放回状态根目录。

    ARGO_STATE_DIR 一旦设置，一律优先——它是测试隔离与只读环境的硬开关，
    不能被磁盘上的 config.yaml 盖掉（否则 env 形同虚设）。
    """
    if os.environ.get(ENV_STATE_DIR, "").strip():
        return state_path("cache.db")
    raw = _config_db_path()
    if raw:
        return Path(os.path.expanduser(raw))
    return state_path("cache.db")


if __name__ == "__main__":
    raise SystemExit(_cli())
