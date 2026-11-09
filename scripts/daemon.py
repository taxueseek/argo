#!/usr/bin/env python3
"""daemon.py — argo 常驻进程：把每次 CLI 调用的固定成本变成本次 UDS 往返。

背景（2026-10-11 实测，见仓库外分析底稿）：一次 web 搜索 ~800ms 里，
dispatch（引擎网络等待）占 97%；真正的 Python 计算只有几十毫秒。但**每次
CLI 调用都要重付一遍「解释器启动 + 重模块 import」约 85–100ms**——engines
全家（builders/urllib/http）26ms、search_entry 9.9ms、recovery 2.8ms……
agent 循环里一次研究任务调 15–20 次 argo，这笔固定成本就放大成 1.5–2s。

对照 OpenAI Habitat（Python→Rust）案例的结论是：argo 的可省成本来自
「每次调用的固定开销」，不是 Python 语言本身——所以正解是常驻进程把
import 付一次，而不是重写语言。收益相同，成本差两个数量级。

设计：
  - 协议：UDS 上逐连接一行 JSON 请求 / 一行 JSON 响应（stdlib socket，无新依赖）
  - 执行：runpy.run_module 复刻 bin/argo 的原地执行语义（sys.argv 置换 +
    stdout/stderr 重定向捕获），daemon 与 CLI 的行为严格同源
  - 串行：请求执行持全局锁。原因：redirect_stdout 是进程级全局态，并发
    会串流；且 argo 内部本就高并发（并行效率 >1），CLI 层串行不损吞吐。
    连接接受与响应发送仍在多线程——排队不阻塞心跳/停止。
  - 快路径：bin/argo 侧零 import 直连（socket 指针文件 `.daemon_sock-<树指纹>` 落在
    bin/argo 可预测的缓存根下，解耦 argo_paths 的深层解析）
  - 生命周期：`argo daemon start|status|stop|restart`；空闲 --idle-exit 秒
    自杀（默认 1800），不留僵尸；SIGTERM 同样清场
  - 安全：socket chmod 0600（UDS 连接权限跟随文件权限，默认 umask 会放出
    0755 让同机其他用户连上）；只放行白名单脚本

第一笔 run 请求会多付 ~100ms（search 全家首次 import），此后全部热路径。
daemon 自身启动刻意轻：只 import argo_paths/engine_env，不预热引擎栈。

运行：
  python3 scripts/daemon.py start [--foreground] [--idle-exit 1800] [--json]
  python3 scripts/daemon.py status [--json]
  python3 scripts/daemon.py stop [--json]
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import glob
import io
import json
import os
import runpy
import signal
import socket
import sys
import threading
import time

SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
ARGO_HOME = os.path.dirname(SCRIPTS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from argo_paths import state_path, state_root  # noqa: E402
import engine_states  # 状态词表唯一真源（2026-11-09 收口）

SOCK_NAME = "daemon.sock"
PID_NAME = "daemon.pid"
LOG_NAME = "daemon.log"
POINTER_NAME = ".daemon_sock"  # 指针文件：落 bin/argo 可预测的缓存根，内容=真实 socket 路径
MARKER_NAME = ".daemon_autostart"  # 自启节流文件：落 _cache_root()
MAX_REQ_BYTES = 1 << 20  # 单请求上限 1MB（args 很小，此值纯防失控）
# 发送侧超时的兜底值（客户端未在请求里带 send_timeout 时的旧客户端路径）。
# 正常路径由客户端按自己的 ARGO_DAEMON_TIMEOUT+5 下达，见 handle()。
_SEND_TIMEOUT_S = 300.0

# 可执行脚本白名单（键=客户端传的 script 名，值=runpy 模块名）。
# 与 bin/argo.scripts 同源收窄：daemon 只收 CLI 映射表里真实存在的目标，
# 客户端 v1 只路由 search；表先备全，后续放行只需在此加行 + bin/argo 改路由。
RUNNABLE = {
    "search": "search",
}


_fp_memo: tuple[float, str] | None = None
_FP_TTL_S = 1.0  # 与 config_stamp() 同口径：1s 内突发请求免扫 220 个文件


def _code_fingerprint(force: bool = False) -> str:
    """仓库 Python 源码指纹：scripts/*.py + sub-skills 关键子技能 + bin/argo。

    为什么需要它：daemon 是长驻进程，但 `runpy.run_module` 只重执行脚本顶层，
    首次请求 import 进 sys.modules 的整棵依赖树（search_cli/search/search_pipeline/
    engines*/sub-skills/local-search/search_v3 …）**不会被刷新**（2026-10-12
    审计实证：顶层体重执行、依赖仍是旧对象）。于是 `git pull` / 改动脚本后，
    daemon 存活期内每次 search 都在静默跑旧代码，最长到空闲自杀（默认 30
    分钟）；若新版脚本 import 了旧模块里不存在的符号，顶层 ImportError 还会
    变成「ok=True + exit 1」的硬失败。
    覆盖面必须包含 sub-skills：daemon 进程内经 engines.py 把 search_v3 固化
    进 sys.modules，只 hash scripts/ 会让「只改子技能」的变更漏出保护面。
    指纹不一致时客户端必须回退冷路径（见 _daemon_run），daemon 自身也退出，
    下一笔冷调用会按需重启新代码。
    """
    global _fp_memo
    now = time.monotonic()
    if not force and _fp_memo is not None and now - _fp_memo[0] < _FP_TTL_S:
        return _fp_memo[1]
    import hashlib
    h = hashlib.sha1()
    targets: list[str] = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "*.py")))
    base = os.path.dirname(SCRIPTS_DIR)
    for rel in ("sub-skills/local-search", "sub-skills/local-seek",
                "sub-skills/ego-search"):
        targets.extend(sorted(glob.glob(os.path.join(base, rel, "**", "*.py"),
                                        recursive=True)))
    for p in targets:
        try:
            st = os.stat(p)
        except OSError:
            continue
        h.update(f"{os.path.relpath(p, base)}:{st.st_mtime_ns}:{st.st_size}\n"
                 .encode("utf-8"))
    # bin/argo 也在热路径上（客户端自己读），变更同样必须触发回退
    try:
        st = os.stat(os.path.join(ARGO_HOME, "bin", "argo"))
        h.update(f"bin/argo:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
    except OSError:
        pass
    digest = h.hexdigest()
    _fp_memo = (now, digest)
    return digest



_TREE_TAG: str | None = None


def _tree_tag() -> str:
    """本检出目录（ARGO_HOME realpath）的短指纹：daemon 落盘名字全部按树隔离。

    为什么需要（2026-10-16 实证）：socket/pid/指针/自启标记/日志此前落在
    共享状态根（~/.cache/unified-search）的固定名上，同机多个检出目录
    （git worktree、多宿主软链、他人另一份 clone）互相踩：
      - B 树 daemon 抢到固定 socket 名后，A 树 CLI 经指针连上的是 B 的进程
        ——跑的是 B 的代码与配置；而指纹检测在 B 进程内只比对 B 自己的源码
        树，A 的改动永远触发不了 stale，「改了不生效」且无法解释；
      - pid 文件互相覆盖：A 树 `argo daemon stop` 杀的是 B 树的 daemon；
      - 自启节流标记共享：A 树刚自启过，B 树 60s 内拉不起自己的 daemon。
    名字带 ARGO_HOME 指纹后每树一个 daemon，互不抢、互不误杀。
    兼容：旧固定名的在跑实例照常服务到空闲自杀（默认 30 分钟），新客户端按
    新名字自启本树实例，无迁移动作；stop/status 只作用于本树实例。
    两侧必须同算法同输入：bin/argo._tree_tag 与这里逐字一致（sha1(ARGO_HOME)
    前 12 hex），测试 test_daemon.py 锁这份契约。
    """
    global _TREE_TAG
    if _TREE_TAG is None:
        import hashlib
        _TREE_TAG = hashlib.sha1(ARGO_HOME.encode("utf-8")).hexdigest()[:12]
    return _TREE_TAG


def _cache_root() -> str:
    """bin/argo 可预测的缓存根（与 bin/argo._default_cache_root 同一逻辑）。

    指针文件必须落在客户端**不 import 任何模块**就能算出的位置——entrypoint
    的零 import 纪律不可破，所以这里反向迁就它的简化解析（env → XDG → 历史
    目录），而不是客户端迁就 argo_paths 的完整解析（config.yaml db_path 分支
    客户端不读 yaml 就算不出来）。两边不一致时快路径静默失效退回常态执行，
    不会错。
    """
    # ARGO_STATE_DIR 与 bin/argo 的 _PY_CACHE 同一优先级：env 覆盖是测试隔离
    # 的硬开关，指针文件必须跟 socket 一起落进被隔离的根，两端才对得上。
    env_dir = os.environ.get("ARGO_STATE_DIR", "").strip()
    if env_dir:
        return env_dir
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "unified-search")
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return os.path.join(xdg, "unified-search")
    return os.path.expanduser("~/.cache/unified-search")


def _sock_path() -> str:
    """socket 路径：默认与状态根同目录；超长时落短路径哈希位。

    macOS/Linux 的 AF_UNIX 路径上限是 104 字节（sun_path），pytest 的深层
    临时目录、或用户把 ARGO_STATE_DIR 挪进深嵌套目录时，原生路径会超——
    实测 bind 直接 OSError「AF_UNIX path too long」。此时退到
    /tmp/argo-daemon-<uid>/<state_root 哈希>.sock：目录 0700 保用户隔离，
    哈希保不同状态根互不冲突；可预测性由指针文件承担（客户端只读指针，
    不算 socket 路径），所以这里怎么挪都不影响两端契约。
    """
    native = str(state_path(SOCK_NAME + "-" + _tree_tag()))
    if os.name == "nt" or len(native.encode("utf-8")) <= 100:
        return native
    import hashlib
    h = hashlib.sha1(str(state_root()).encode("utf-8")).hexdigest()[:10]
    d = f"/tmp/argo-daemon-{os.getuid()}"
    return os.path.join(d, f"{h}-{_tree_tag()}.sock")


def _pointer_path() -> str:
    return os.path.join(_cache_root(), POINTER_NAME + "-" + _tree_tag())


def _marker_path() -> str:
    return os.path.join(_cache_root(), MARKER_NAME + "-" + _tree_tag())


def _pid_path() -> str:
    return str(state_path(PID_NAME + "-" + _tree_tag()))


def _pid_is_daemon(pid: int) -> bool:
    """核实 pid 的命令行确实是本 daemon（macOS/Linux ps，Windows 视为不是）。

    误杀的代价不对称：漏杀只是留一个下次 start 时 ping 不到、自己会空闲退出的
    进程；错杀是 SIGTERM 一个无关进程。所以判据从紧——单独出现 "daemon.py"
    子串不够（编辑器/别仓同名脚本都会中）。

    必须识别两种启动形态（2026-10-13 实测发现第二种此前漏判）：
      A. `python <本仓>/scripts/daemon.py start`——直接调用与 autostart 派生；
      B. `./bin/argo daemon start`——bin/argo 经 runpy 原地执行时，Python 层的
         sys.argv 被改写但**内核记录的 argv 不变**，ps 里始终是 bin/argo 形态。
         此前只认 A，B 形态下 stop 的 pid 兜底补刀会漏判，留孤儿进程。
    """
    if os.name == "nt":
        return False
    import subprocess
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
        cmd = (r.stdout or "").strip()
    except Exception:
        return False
    if not cmd:
        return False
    toks = cmd.split()
    if "start" not in toks:  # 两种形态都以 start 收尾；无 start 一律排除
        return False
    if any(t.endswith("daemon.py") for t in toks) and SCRIPTS_DIR in cmd:
        return True  # 形态 A
    # 形态 B：bin/argo 入口（绝对/相对路径均可能，按 basename 认）
    has_argo = any(os.path.basename(t.strip("'\"")) in ("argo", "argo.exe")
                   for t in toks)
    return has_argo and "daemon" in toks


def _log_path() -> str:
    return str(state_path(LOG_NAME + "-" + _tree_tag()))


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ── 状态查询 ────────────────────────────────────────────────────────────────


def ping(sock_path: str, timeout: float = 1.0) -> dict:
    """对 socket 发 ping；活着返回响应 dict，死了抛 OSError。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(b'{"op":"ping"}\n')
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8", "replace"))
    finally:
        s.close()


def _alive() -> dict | None:
    """daemon 存活则返回 ping 响应，否则 None。

    清场判据从紧（2026-10-12 审计修正）：只在「连接被明确拒绝」时摘除
    socket/指针——ping 超时（daemon 被 GIL 硬占/负载高）不代表它死了，
    此前任何异常都 unlink，会把活着 daemon 的入口摘掉，其监听 socket 成为
    永久不可达的孤儿，直到空闲自杀。宁可不摘（下次 start 会再探），不误摘。
    """
    sp = _sock_path()
    if not os.path.exists(sp):
        with contextlib.suppress(OSError):
            os.unlink(_pointer_path())
        return None
    try:
        return ping(sp)
    except OSError as e:
        if e.errno in (errno.ECONNREFUSED, errno.ENOENT):
            for p in (sp, _pointer_path()):
                with contextlib.suppress(OSError):
                    os.unlink(p)
        return None
    except Exception:
        return None


# ── 请求执行 ────────────────────────────────────────────────────────────────

# 执行锁：stdout 重定向是进程级全局态，运行请求必须串行。持有者超时前
# 未释放时新请求不排队等待而是快速返回 busy——排队等待会让每个调用方的
# 客户端超时预算含上排队时间，冷路径本来就能并行跑，排队在这儿没有任何
# 收益（audit 2026-10-12）。
_EXEC_LOCK = threading.Lock()
_EXEC_WAIT_S = 0.2

# 环境变量透传白名单判据在客户端（bin/argo._daemon_env）：daemon 侧只负责
# 应用/还原请求携带的覆盖。此处不再放第二份挑选规则——双份实现必然漂移，
# 且 daemon 侧那份此前全仓无人调用（2026-10-12 审查发现）。
_MAX_ENV_OVERRIDE_BYTES = 32768


def _apply_env_overrides(overrides) -> dict:
    """请求级应用客户端环境；返回还原快照（键 → 旧值或 None）。"""
    saved: dict = {}
    if not isinstance(overrides, dict):
        return saved
    total = 0
    for k, v in overrides.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k:
            continue
        total += len(k) + len(v)
        if total > _MAX_ENV_OVERRIDE_BYTES:
            return saved  # 超限拒绝（防畸形请求灌爆环境）；已应用的不回滚
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    return saved


def _restore_env(saved: dict) -> None:
    for k, prev in saved.items():
        if prev is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = prev


def _run_request(req: dict) -> dict:
    """执行一笔 run 请求：与 bin/argo 原地路径严格同语义。

    run_module 而非直接调 main()：与 bin/argo 的 runpy 路径一字不差，脚本
    的 `if __name__ == "__main__"` / argparse / sys.exit 语义原样生效。代价
    是每次重执行脚本顶层（__pycache__ 命中下 ~几 ms）——相对引擎 RTT 可忽略，
    换来的是零特判的「和 CLI 完全一样」。

    调用方必须已持有 _EXEC_LOCK（锁在 handle() 层做超时快失败）。
    """
    script = req.get("script")
    module = RUNNABLE.get(script or "")
    args = req.get("args") or []
    if module is None:
        return {"ok": False, "error": f"script 不在白名单: {script!r}"}
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return {"ok": False, "error": "args 必须是字符串列表"}

    script_path = os.path.join(SCRIPTS_DIR, f"{script}.py")
    t0 = time.perf_counter()
    out, err = io.StringIO(), io.StringIO()
    code = 0
    old_argv, old_cwd = sys.argv, os.getcwd()
    saved_env = _apply_env_overrides(req.get("env"))
    try:
        cwd = req.get("cwd")
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)
        sys.argv = [script_path] + args
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                runpy.run_module(module, run_name="__main__")
            except SystemExit as e:
                # 与 CLI 退出码契约一致：None/0 → 0，整数原样，其余 → 1
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException as e:  # 引擎栈漏出的非 SystemExit：按 CLI 语义退出码 1
        code = 1
        err.write(f"\n[daemon] {type(e).__name__}: {e}\n")
    finally:
        _restore_env(saved_env)
        sys.argv = old_argv
        with contextlib.suppress(OSError):
            os.chdir(old_cwd)
    return {
        "ok": True,
        "exit_code": code,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ── 服务循环 ────────────────────────────────────────────────────────────────


def _peer_closed(conn: socket.socket) -> bool:
    """对端是否已断开（不消费数据，MSG_PEEK 探测）。

    用途：客户端超时/被杀后 socket 已关，但请求如果恰在排队或还没点火，
    daemon 不应该再把它跑一遍——那是同一查询的付费调用与 usage 双计
    （audit 2026-10-12）。已开始的执行不中断（引擎有各自超时且有缓存兜底），
    只挡住「还没开跑」这一类。
    """
    import select
    try:
        r, _, _ = select.select([conn], [], [], 0)
        if not r:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


# stop/SIGTERM 时等在途 run 收尾的上限：宁可 stop 慢几秒，也不腰斩一笔已
# 开始的执行（腰斩会让客户端拿到 EOF 回退冷跑，同一查询双跑双计）。
_STOP_GRACE_S = 20.0


class _Server:
    def __init__(self, idle_exit_s: float) -> None:
        self.idle_exit_s = idle_exit_s
        self.stop_evt = threading.Event()
        self.active = 0
        self.active_lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.started_at = time.monotonic()
        self.in_run = 0  # 正在执行的 run 数（0/1，受 _EXEC_LOCK 约束）
        self.fp = ""  # 启动时的源码指纹；请求时比对，变更即 stale

    def handle(self, conn: socket.socket) -> None:
        with self.active_lock:
            self.active += 1
        try:
            conn.settimeout(60)  # 兼容旧值 300s：哑连接不应把空闲退出推迟五分钟
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < MAX_REQ_BYTES:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            resp: dict = {"ok": False, "error": "空请求"}
            # req 必须先绑定再使用：空请求 / 超 1MB / 解析失败 / 非 dict 体
            # （如 JSON 数组）时 475 行的 `req.get("send_timeout")` 会
            # NameError/AttributeError——两者都穿不透下面的 except OSError，
            # handler 线程带 traceback 死掉，上面特意准备的错误响应永远没
            # 发送，客户端拿 EOF 回退冷跑（2026-10-17 审查发现 2）。
            req: dict = {}
            if len(buf) > MAX_REQ_BYTES:
                resp = {"ok": False, "error": "请求超长"}
                buf = b""
            if buf.strip():
                try:
                    parsed = json.loads(buf.decode("utf-8", "replace"))
                    if isinstance(parsed, dict):
                        req = parsed
                    op = req.get("op")
                    if op == "ping":
                        now = time.monotonic()
                        # state 是给机器读的唯一判据（词表见 engine_states §10）；
                        # 保留 ok/running 形态给旧客户端，新增字段不改旧语义。
                        resp = {"ok": True, "pid": os.getpid(),
                                "state": (engine_states.DAEMON_BUSY
                                          if self.in_run
                                          else engine_states.DAEMON_RUNNING),
                                "in_run": self.in_run,
                                "uptime_s": round(now - self.started_at, 1),
                                "idle_s": round(now - self.last_activity, 1),
                                "fp": self.fp}
                    elif op == "stop":
                        resp = {"ok": True, "stopping": True}
                        self.stop_evt.set()
                    elif op == "run":
                        resp = self._handle_run(conn, req)
                    else:
                        resp = {"ok": False, "error": f"未知 op: {op!r}"}
                except Exception as e:
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            # sendall 用的超时与 recv 的那把（60s）分开，且以**客户端声明
            # 的预算**为准：一笔 run 超过 60s 时 sendall 抛 socket.timeout
            # 会丢响应、逼客户端冷跑重来（同一查询双跑双计，2026-10-16 审查
            # 发现）。请求里的 send_timeout 由客户端按自己的
            # ARGO_DAEMON_TIMEOUT+5 填——服务端任何固定值都猜不到用户把
            # 客户端等限调到了多大；缺失（旧客户端）时回退模块常量。
            try:
                _send_tmo = float(req.get("send_timeout"))
            except (TypeError, ValueError):
                _send_tmo = _SEND_TIMEOUT_S
            conn.settimeout(_send_tmo if _send_tmo > 0 else None)
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass  # 客户端超时先走：响应丢弃即可，引擎侧照常完成
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            with self.active_lock:
                self.active -= 1

    def _handle_run(self, conn: socket.socket, req: dict) -> dict:
        """run 分支：忙时快失败；源码变更判 stale；对端已断不点火。"""
        if not _EXEC_LOCK.acquire(timeout=_EXEC_WAIT_S):
            return {"ok": False, "busy": True,
                    "error": "daemon 正忙（客户端应回退冷路径）"}
        try:
            if self.stop_evt.is_set():
                # 已进入退役：不再点火（点火后进程退出会把它腰斩，客户端
                # 拿不到响应只能冷跑重来——正是本次要消除的双跑）。
                return {"ok": False, "error": "daemon 退役中（客户端应回退冷路径）"}
            fp = _code_fingerprint()
            if self.fp and fp and fp != self.fp:
                self.stop_evt.set()  # 旧代码 daemon 退役，下次冷调用按需起新
                return {"ok": False, "stale": True,
                        "error": "仓库源码已变更：daemon 退出，客户端回退冷路径"}
            if _peer_closed(conn):
                return {"ok": False, "error": "客户端已断开，跳过执行"}
            self.last_activity = time.monotonic()
            self.in_run += 1
            try:
                resp = _run_request(req)
            finally:
                self.in_run -= 1
            self.last_activity = time.monotonic()
            return resp
        finally:
            _EXEC_LOCK.release()

    def serve_forever(self) -> None:
        sp = _sock_path()
        # bind 前建目录：UDS bind 会在 socket 路径上落文件，目录不存在即
        # FileNotFoundError（测试隔离/新机器首次启动必踩——上一版就栽在这）
        d = os.path.dirname(sp)
        if d:
            os.makedirs(d, exist_ok=True)
        if d.startswith("/tmp/argo-daemon-"):
            os.chmod(d, 0o700)  # 短路径位落在共享 /tmp：目录必须锁回本用户
        listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listen_sock.bind(sp)
        except OSError as e:
            # 父进程只能看到「启动超时」，死因必须自己写进日志
            print(f"[daemon] bind 失败 {sp}: {e}", flush=True)
            raise
        os.chmod(sp, 0o600)  # UDS 连接权限=文件权限；默认 umask 会放出组/其他可连
        listen_sock.listen(16)
        self.fp = _code_fingerprint()  # 请求期比对；源码一变即 stale（见 _handle_run）
        _atomic_write(_pid_path(), f"{os.getpid()}\n")
        _atomic_write(_pointer_path(), sp + "\n")

        def _on_term(_sig, _frm) -> None:
            self.stop_evt.set()

        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)

        def _watchdog() -> None:
            while not self.stop_evt.wait(5.0):
                if self.idle_exit_s > 0 and self.active == 0 \
                        and time.monotonic() - self.last_activity > self.idle_exit_s:
                    print(f"[daemon] 空闲 {self.idle_exit_s:.0f}s，自杀退出", flush=True)
                    self.stop_evt.set()

        threading.Thread(target=_watchdog, daemon=True).start()

        def _prewarm() -> None:
            # 监听就绪后后台预热核心模块与域正则（与 mcp_handlers._warm_core_async 同构）：
            # 既不拖慢 start 握手（~30ms），又消除首笔 search 请求的 ~118ms 冷加载税。
            if not _EXEC_LOCK.acquire(timeout=0.05):
                return
            try:
                if self.stop_evt.is_set():
                    return
                import search  # noqa: F401
                import search_cli  # noqa: F401
                from route_domains import warm_compiled_domains
                warm_compiled_domains()
            except Exception:
                pass
            finally:
                _EXEC_LOCK.release()

        threading.Thread(target=_prewarm, name="argo-daemon-warm", daemon=True).start()
        print(f"[daemon] pid={os.getpid()} socket={sp} 就绪", flush=True)
        try:
            listen_sock.settimeout(2.0)
            while not self.stop_evt.is_set():
                try:
                    conn, _ = listen_sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
            # 收尾第一步：立刻关闭并摘除监听 socket——退役期间新客户端不再
            # 连进 backlog 干等（旧实现把 listen_sock 开到退出才关，
            # stop 后紧跟的 search 要等整个收尾窗口才拿到 RST）。此后新连接
            # 立即 ECONNREFUSED → 客户端静默回退冷路径。
            with contextlib.suppress(OSError):
                listen_sock.close()
            for p in (_sock_path(), _pointer_path(), _marker_path()):
                with contextlib.suppress(OSError):
                    os.unlink(p)
            # 第二步：等在途 run 收尾（上限 _STOP_GRACE_S）——腰斩已开始的
            # 执行会逼客户端回退冷跑、同查询双计。无在途执行时立即退出。
            deadline = time.monotonic() + _STOP_GRACE_S
            while time.monotonic() < deadline and self.in_run > 0:
                time.sleep(0.05)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(_pid_path())
            print("[daemon] 已退出", flush=True)


# ── 子命令 ──────────────────────────────────────────────────────────────────


def _quiet_stop(sp: str) -> bool:
    """向在跑 daemon 发 stop 并等 socket 消失；成功 True。

    用于 start 的陈旧自愈：指纹不符的旧实例先请走，再起新代码的实例。
    connect 失败不再直接判失败——旧实例可能恰在自行退役（accept 循环已退出、
    socket 文件尚未清理），此时「连不上」= 已在停，视同成功（2026-10-12
    审查：假阴性会让陈旧自愈在最需要它的时刻拒绝启动）。
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(sp)
        s.sendall(b'{"op":"stop"}\n')
        s.close()
    except OSError:
        # 连不上：确认它确实不在响应（_alive 为 None）即视同已停
        return _alive() is None
    deadline = time.time() + 10
    while time.time() < deadline:
        if not os.path.exists(sp):
            return True
        time.sleep(0.1)
    return False


def _start(args: argparse.Namespace) -> int:
    alive = _alive()
    if alive:
        # 陈旧自愈：在跑的实例指纹与当前源码不符（git pull / 改脚本后）时，
        # 老实例已进入「拒执行 + 准备退役」状态，但退役与重新拉起之间有时序
        # 缝——客户端自启的新 start 会因「ping 得到旧进程」直接报已在运行并
        # 退出，结果退役完成后无人接手（实测踩中：touch 脚本后 status 永为
        # 未运行）。这里对指纹不符的实例先停、等它出场，再走正常启动。
        cur_fp = _code_fingerprint(force=True)
        running_fp = alive.get("fp") or ""
        if running_fp and cur_fp and running_fp != cur_fp:
            if not args.json:
                print(f"argo daemon 检出旧代码实例（pid={alive.get('pid')}），重启",
                      file=sys.stderr)
            if _quiet_stop(_sock_path()):
                alive = None  # 让流程继续到启动
            else:
                print(json.dumps({"ok": False, "error": "旧实例未能停止"},
                                 ensure_ascii=False) if args.json
                      else "旧代码 daemon 未能停止；先 `argo daemon stop`", file=sys.stderr)
                return 1
        else:
            print(json.dumps({"ok": True, "already_running": True, **alive},
                             ensure_ascii=False) if args.json
                  else f"argo daemon 已在运行 pid={alive.get('pid')}")
            return 0

    # 启动互斥（stub 文件 O_EXCL）：两个并发 start 都通过 _alive 检查时会
    # 双双 fork，输家 bind 失败、赢家被后到的父进程误报；更糟的是赢家在
    # bind→listen 窗口内会被输家的 _alive 当陈尸。stub 让后来者等待赢家。
    stub = _pid_path() + ".starting"
    try:
        os.makedirs(os.path.dirname(stub), exist_ok=True)
    except OSError:
        pass
    acquired = False
    for _attempt in range(3):
        try:
            fd = os.open(stub, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            # 旧 stub 可能是上次崩溃残留（>20s 视为陈尸）：清掉后立刻重抢
            try:
                if time.time() - os.stat(stub).st_mtime > 20:
                    os.unlink(stub)
                    continue
            except OSError:
                pass
            # 活着的启动者：等它把 daemon 拉起来（或它中途放行 stub）
            deadline = time.time() + 10
            while time.time() < deadline:
                info = _alive()
                if info:
                    print(json.dumps({"ok": True, "already_running": True, **info},
                                     ensure_ascii=False) if args.json
                          else f"argo daemon 已在运行 pid={info.get('pid')}")
                    return 0
                if not os.path.exists(stub):
                    break  # 对方放行：回到循环重抢 stub
                time.sleep(0.1)
        except OSError:
            break  # stub 建不了（只读状态目录等）：退回无互斥的老路，不阻断启动
    else:
        # 三次都没抢到：对方仍在启动流程里，如实报告
        print(json.dumps({"ok": False, "error": "另一进程正在启动 daemon，等待超时"},
                         ensure_ascii=False) if args.json
              else "另一进程正在启动 argo daemon（等待超时）", file=sys.stderr)
        return 1

    try:
        # socket 还在但 ping 不通：可能是在途请求把 GIL 占满的活 daemon。
        # 不抢跑（bind 必失败、还可能把它的入口当陈尸摘掉），如实报告。
        sp = _sock_path()
        if os.path.exists(sp):
            try:
                ping(sp, 2.0)
            except OSError as e:
                if e.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                    print(json.dumps({"ok": False, "error": "daemon socket 存在但无响应"},
                                     ensure_ascii=False) if args.json
                          else "argo daemon socket 存在但无响应；先 `argo daemon stop`",
                          file=sys.stderr)
                    return 1
            except Exception:
                pass

        if args.foreground or os.name == "nt":  # Windows 无 fork：只支持前台
            _serve_child(args)
            return 0

        pid = os.fork()
        if pid > 0:
            # 父进程：等 socket 就绪后报告（子进程 bind 完成即视为成功）
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    info = ping(_sock_path(), 0.5)
                    msg = {"ok": True, "pid": info.get("pid")}
                    print(json.dumps(msg, ensure_ascii=False) if args.json
                          else f"argo daemon 已启动 pid={info.get('pid')} socket={_sock_path()}")
                    return 0
                except Exception:
                    time.sleep(0.05)
            print(json.dumps({"ok": False, "error": "启动超时", "log": _log_path()},
                             ensure_ascii=False) if args.json
                  else f"argo daemon 启动超时，看日志：{_log_path()}", file=sys.stderr)
            return 1
        os.setsid()
        # 重定向 stdio 到日志：daemon 不再依赖终端（日志目录同样可能还不存在）
        log_dir = os.path.dirname(_log_path())
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        log = open(_log_path(), "ab", buffering=0)
        os.dup2(log.fileno(), 0)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        with contextlib.suppress(OSError):
            log.close()
        _serve_child(args)
        return 0
    finally:
        with contextlib.suppress(OSError):
            os.unlink(stub)


def _serve_child(args: argparse.Namespace) -> None:
    try:
        from engine_env import sync_envfile_to_environ
        sync_envfile_to_environ()
    except Exception:
        pass  # env 文件读不到不拦 daemon：引擎侧按各自缺 key 的既有口径失败
    _Server(args.idle_exit).serve_forever()


def _stop(args: argparse.Namespace) -> int:
    sp = _sock_path()
    try:
        ping(sp, 1.0)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(sp)
        s.sendall(b'{"op":"stop"}\n')
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        # 等 socket 文件消失（退役第一步就会 unlink，正常毫秒级）；socket 消
        # 失后再给在途 run 一个收尾观察窗——文档口径是「上限 20s」，此前只
        # 等 5s 就无条件报成功，在途收尾还没完时紧随的 start 会因 socket
        # 仍在而报「先 stop」（用户被指回刚跑过的命令）。
        deadline = time.time() + 5
        while time.time() < deadline and os.path.exists(sp):
            time.sleep(0.05)
        pid = None
        try:
            with open(_pid_path(), encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pass
        if pid:
            grace = time.time() + _STOP_GRACE_S
            while time.time() < grace:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break  # 进程已退出：真停
                time.sleep(0.1)
        with contextlib.suppress(OSError):
            os.unlink(_marker_path())
        print(json.dumps({"ok": True, "stopped": True, "pid": pid}, ensure_ascii=False)
              if args.json else "argo daemon 已停止")
        return 0
    except Exception:
        # socket 已死：靠 pid 文件兜底补刀，避免进程活着却没有入口的孤儿态。
        # kill 前先核实命令行确实是本 daemon——pid 文件可能陈旧，而 OS 的 pid
        # 会复用：裸 SIGTERM 一个恰好复用 pid 的无关进程是真实伤害。
        killed = None
        try:
            with open(_pid_path(), encoding="utf-8") as f:
                pid = int(f.read().strip())
            if _pid_is_daemon(pid):
                os.kill(pid, signal.SIGTERM)
                killed = pid
        except (OSError, ValueError):
            pass
        for p in (sp, _pointer_path(), _pid_path(), _marker_path()):
            with contextlib.suppress(OSError):
                os.unlink(p)
        print(json.dumps({"ok": True, "stopped": True, "killed": killed},
                         ensure_ascii=False) if args.json else "argo daemon 已清理")
        return 0


def _status(args: argparse.Namespace) -> int:
    alive = _alive()
    state = (str(alive.get("state") or engine_states.DAEMON_RUNNING)
             if alive else engine_states.DAEMON_STOPPED)
    payload = {"ok": True, "running": bool(alive), "state": state,
               "socket": _sock_path(), "pid": (alive or {}).get("pid")}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if alive:
            print(f"argo daemon 运行中 pid={alive.get('pid')} socket={_sock_path()}")
        else:
            print(f"argo daemon 未运行（socket={_sock_path()}）")
    return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description="argo 常驻 daemon（消 CLI 每次调用的启动+import 固定成本）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="启动 daemon（默认后台化）")
    p_start.add_argument("--foreground", action="store_true", help="前台运行（调试用）")
    p_start.add_argument("--idle-exit", type=float, default=1800.0,
                         help="空闲 N 秒后自杀退出；0=常驻不退（默认 1800）")
    for p in (p_start, sub.add_parser("status"), sub.add_parser("stop")):
        p.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()
    if args.cmd == "start":
        return _start(args)
    if args.cmd == "stop":
        return _stop(args)
    return _status(args)


if __name__ == "__main__":
    raise SystemExit(_cli())
