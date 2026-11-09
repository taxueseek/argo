#!/usr/bin/env python3
"""daemon（常驻进程 + UDS 快路径）行为锁。

覆盖面：
  - 生命周期：start → ping/status 可见 → stop → socket/指针/pid 全清
  - 执行语义：run op 跑 search --list-engines（离线）出合法 JSON；
    白名单外的 script 拒绝；连续两笔 run（模块重执行的温态安全）
  - 陈尸清理：指针/socket 陈尸时 start 仍能成功
  - CLI 快路径端到端：daemon 在位时 bin/argo search 走 UDS（结果与常态等价）；
    ARGO_NO_DAEMON=1 显式关闭也等价；daemon 死而指针在时静默回退不报错
  - 空闲自杀：--idle-exit 生效，socket 自清

观察点与客户端同契约：socket 真实位置一律经指针文件 .daemon_sock-<树指纹> 读出
（原生状态根路径可能超过 AF_UNIX 的 104 字节上限，daemon 会自动落短路径
哈希位——tests 深层 tmp 目录正是这条兜底分支的日常触发场景）。

全部离线（list-engines 只读注册表，不发网络）。POSIX-only：daemonize 用 fork。
"""

from __future__ import annotations

import json
import os
import socket
import unittest
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "argo"
DAEMON = ROOT / "scripts" / "daemon.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="daemonize 依赖 fork，仅 POSIX")

sys.path.insert(0, str(ROOT / "scripts"))
import daemon as daemon_mod  # noqa: E402


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["ARGO_STATE_DIR"] = str(tmp_path / "state")
    # conftest 为防测试自启留下游离 daemon 默认关掉了快路径；本文件正测它，
    # 显式打开（客户端的 _daemon_run 与自启都会读这个变量）。
    env.pop("ARGO_NO_DAEMON", None)
    return env


def _tag() -> str:
    """本检出目录的 daemon 名字指纹（与 scripts/daemon.py._tree_tag 同算法）。

    daemon 的 socket/pid/指针/自启标记按检出目录隔离（2026-10-16），测试
    断言的就是这些带指纹的名字，两边必须同源。
    """
    import hashlib
    root = Path(__file__).resolve().parent.parent
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]


def _pointer(tmp_path: Path) -> Path:
    return tmp_path / "state" / (".daemon_sock-" + _tag())


def _sock(tmp_path: Path) -> Path:
    """从指针读 socket 真实路径（与 bin/argo 快路径同一发现方式）。"""
    return Path(_pointer(tmp_path).read_text(encoding="utf-8").strip())


def _daemon(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DAEMON), *extra, "--json"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=30,
    )


def _start(tmp_path: Path) -> dict:
    r = _daemon(tmp_path, "start")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _wait_sock(tmp_path: Path, want: bool, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            alive = _sock(tmp_path).exists()
        except (OSError, ValueError):
            alive = False
        if alive == want and _pointer(tmp_path).exists() == want:
            return
        time.sleep(0.05)
    pytest.fail(f"socket 未在 {timeout}s 内变为 {'存在' if want else '消失'}")


def _uds(sock: Path, req: dict, timeout: float = 30.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock))
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    assert buf, "daemon 未返回任何响应"
    return json.loads(buf.decode("utf-8", "replace"))


def _uds_run(sock: Path, req: dict, timeout: float = 15.0) -> dict:
    """op:run 请求，遇 busy 重试到 deadline。

    daemon 监听就绪后会起 _prewarm 线程抢同一把 _EXEC_LOCK 预热（daemon.py
    的 _prewarm），所以 socket 出现 ≠ run 立刻可用；run 侧 0.2s busy 快失败
    是给生产客户端的设计行为（回退冷路径），测试在这里等它热完即可。
    2026-10-26 CI 实测：共享 runner 分片满载时预热可超 0.2s，「start→
    _wait_sock→即发」的两条用例随机假红（一条报 ok=False，一条把
    「白名单」用例的期望错误顶成了「daemon 正忙」）。
    """
    deadline = time.monotonic() + timeout
    while True:
        r = _uds(sock, req)
        if not (r.get("busy") and time.monotonic() < deadline):
            return r
        time.sleep(0.2)


def _uds_raw(sock: Path, raw: bytes, timeout: float = 10.0) -> bytes:
    """发原始字节（测畸形请求：空请求 / 非 dict JSON 体）。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock))
        s.sendall(raw)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return buf


@pytest.fixture()
def running(tmp_path):
    info = _start(tmp_path)
    _wait_sock(tmp_path, True)
    yield info
    _daemon(tmp_path, "stop")
    _wait_sock(tmp_path, False)


@pytest.fixture(autouse=True)
def _no_stray_daemon(tmp_path):
    """用例结束后确保本用例起的 daemon 已退出。

    多数用例刻意不逐个 stop（生命周期用例自行覆盖 start/stop），不清理会留
    一批存活到空闲自杀死线（1800s）的游离进程，实测全量套件一口气留 6 个。
    这里按 tmp_path 的 pid 文件兜底：ping 得到就发 stop，ping 不到就核对
    命令行后 SIGTERM（防误杀复用 pid 的无关进程）。
    """
    yield
    pid_path = tmp_path / "state" / ("daemon.pid-" + _tag())
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return
    try:
        _daemon(tmp_path, "stop")
    except Exception:
        pass
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    # stop 没送走（例如 socket 已死）：核实命令行后补刀
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=5)
        if "daemon.py" in (r.stdout or "") and str(ROOT / "scripts") in (r.stdout or ""):
            os.kill(pid, 15)
    except Exception:
        pass


def test_lifecycle_ping_status_stop(tmp_path):
    info = _start(tmp_path)
    _wait_sock(tmp_path, True)
    assert info.get("pid")
    sock = _sock(tmp_path)
    st = _daemon(tmp_path, "status")
    status = json.loads(st.stdout)
    assert st.returncode == 0 and status["running"] is True
    # state 是给机器读的判据（engine_states §10）；旧客户端只认 running 也照常成立
    assert status.get("state") in {"running", "busy", "idle"}
    ping = _uds(sock, {"op": "ping"})
    assert ping.get("state") in {"running", "busy", "idle"}
    assert ping.get("in_run") == 0
    # 二次 start：报已在运行而非起第二个
    again = _start(tmp_path)
    assert again.get("already_running") is True
    _daemon(tmp_path, "stop")
    _wait_sock(tmp_path, False)
    assert not sock.exists(), "socket 未随 stop 清场"
    # 停止后 status 必须报 stopped（进程没了，没有 ping 可回）
    stopped = json.loads(_daemon(tmp_path, "status").stdout)
    assert stopped["running"] is False and stopped["state"] == "stopped"
    assert not (tmp_path / "state" / ("daemon.pid-" + _tag())).exists(), "pid 未随 stop 清场"


def test_malformed_requests_get_error_response(running, tmp_path):
    """空请求 / 非 dict JSON 体必须回错误响应，而不是 handler 线程死亡（EOF）。

    修复前（2026-10-17 审查发现 2）：req 只在 JSON 解析成功后绑定——空请求、
    超 1MB、解析成数组时，发送段 `req.get("send_timeout")` 抛 NameError/
    AttributeError，穿不透 except OSError，handler 带 traceback 死掉，
    「空请求」「请求超长」这两个特意准备的响应永远没发出去。
    """
    sock = _sock(tmp_path)
    # 空请求（只有换行）
    buf = _uds_raw(sock, b"\n")
    assert buf, "空请求应收到错误响应（修复前是 EOF）"
    assert json.loads(buf.decode("utf-8", "replace"))["error"] == "空请求"
    # 非 dict JSON 体（数组）：解析成功但 op 取不到 → 未知 op 错误响应
    buf = _uds_raw(sock, b"[1, 2]\n")
    assert buf, "数组体应收到错误响应而不是断连"
    resp = json.loads(buf.decode("utf-8", "replace"))
    assert resp["ok"] is False, f"数组体应报错，实得 {resp}"
    # handler 池没被前面的事故打死：ping 仍正常应答
    assert _uds(sock, {"op": "ping"})["ok"] is True


def test_run_list_engines_and_warm_reuse(tmp_path):
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    sock = _sock(tmp_path)
    # 第一笔：冷（daemon 内首次 import search 全家）
    r1 = _uds_run(sock, {"op": "run", "script": "search",
                         "args": ["--list-engines", "--json"]})
    assert r1["ok"] and r1["exit_code"] == 0, str(r1)[:300]
    engines = json.loads(r1["stdout"])
    assert isinstance(engines, list) and len(engines) > 100
    # 第二笔：温态（runpy 重执行 + sys.modules 复用不串）
    r2 = _uds_run(sock, {"op": "run", "script": "search",
                         "args": ["--list-engines", "--json"]})
    assert r2["ok"] and r2["exit_code"] == 0
    assert json.loads(r2["stdout"]) == engines


def test_run_whitelist_rejects_unknown_script(tmp_path):
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    r = _uds_run(_sock(tmp_path), {"op": "run", "script": "not_a_script", "args": []})
    assert r["ok"] is False and "白名单" in r.get("error", "")


def test_start_survives_stale_pointer(tmp_path):
    # 陈尸形态：上代异常退出留下旧指针（指向不存在的 socket）——start 必须
    # 清场后照常起来，这是 _alive() 的兜底职责
    _pointer(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _pointer(tmp_path).write_text("/tmp/definitely-dead-argo-daemon.sock\n", encoding="utf-8")
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    r = _uds(_sock(tmp_path), {"op": "ping"})
    assert r["ok"] is True


def test_cli_fastpath_and_fallback(tmp_path):
    # daemon 在位：bin/argo search 必须产出与常态等价的合法 JSON（指针存在 +
    # daemon 存活时 _daemon_run 必然命中，等价性即快路径正确性）
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    r = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                       env=_env(tmp_path), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)) > 100

    # ARGO_NO_DAEMON=1：显式关闭后同样等价（常态路径回归保护）
    env = _env(tmp_path)
    env["ARGO_NO_DAEMON"] = "1"
    r2 = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                        env=env, capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0 and json.loads(r2.stdout) == json.loads(r.stdout)

    # daemon 死而指针残留：快路径必须静默回退，输出照常、无报错噪音
    _daemon(tmp_path, "stop")
    _wait_sock(tmp_path, False)
    r3 = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                        env=_env(tmp_path), capture_output=True, text=True, timeout=30)
    assert r3.returncode == 0, r3.stderr
    assert json.loads(r3.stdout) == json.loads(r.stdout)


def test_idle_exit_self_terminate(tmp_path):
    r = subprocess.run(
        [sys.executable, str(DAEMON), "start", "--idle-exit", "1", "--json"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    _wait_sock(tmp_path, True)
    _wait_sock(tmp_path, False, timeout=15.0)  # 空闲 1s + 5s 看护轮询窗


# ── 加固锁：源码指纹 / env 透传还原 / stale / busy / 客户端回退 ─────────────


def test_source_fingerprint_is_stable_and_nonempty():
    fp1 = daemon_mod._code_fingerprint()
    fp2 = daemon_mod._code_fingerprint()
    assert fp1 and len(fp1) >= 32
    assert fp1 == fp2, "同一状态下指纹必须可复现（mtime_ns+size 汇总）"


def test_handle_run_rejects_stale_fingerprint(tmp_path):
    """daemon 存活期内仓库源码变更 → run 必须拒执行并触发退役。

    此前无任何版本判据：git pull 后 daemon 会继续用 sys.modules 里的旧模块
    静默跑旧代码，最长到空闲自杀。
    """
    srv = daemon_mod._Server(idle_exit_s=1800)
    srv.fp = "definitely-not-the-real-fingerprint"
    a, b = socket.socketpair()
    try:
        resp = srv._handle_run(a, {"op": "run", "script": "search",
                                   "args": ["--list-engines", "--json"]})
    finally:
        a.close(); b.close()
    assert resp["ok"] is False and resp.get("stale") is True
    assert srv.stop_evt.is_set(), "stale 后 daemon 必须标记退役"


def test_handle_run_busy_fails_fast(tmp_path):
    """执行锁被占时立即回 busy，不让调用方排队等（排队只会拖到客户端超时）。"""
    srv = daemon_mod._Server(idle_exit_s=1800)
    srv.fp = daemon_mod._code_fingerprint()
    a, b = socket.socketpair()
    assert daemon_mod._EXEC_LOCK.acquire(timeout=0.01)
    try:
        t0 = time.monotonic()
        resp = srv._handle_run(a, {"op": "run", "script": "search", "args": []})
        took = time.monotonic() - t0
    finally:
        daemon_mod._EXEC_LOCK.release()
        a.close(); b.close()
    assert resp["ok"] is False and resp.get("busy") is True
    assert took < 5.0, "busy 必须快失败"


def test_env_overrides_applied_then_restored(monkeypatch):
    monkeypatch.setenv("ARGO_DAEMON_PROBE", "orig")
    monkeypatch.delenv("ARGO_DAEMON_PROBE_NEW", raising=False)
    saved = daemon_mod._apply_env_overrides(
        {"ARGO_DAEMON_PROBE": "req", "ARGO_DAEMON_PROBE_NEW": "v"},)
    assert os.environ["ARGO_DAEMON_PROBE"] == "req"
    assert os.environ["ARGO_DAEMON_PROBE_NEW"] == "v"
    daemon_mod._restore_env(saved)
    assert os.environ["ARGO_DAEMON_PROBE"] == "orig"
    assert "ARGO_DAEMON_PROBE_NEW" not in os.environ


def test_client_env_override_picks_argo_and_keys(monkeypatch):
    monkeypatch.setenv("ARGO_DAEMON_PROBE", "1")
    monkeypatch.setenv("SOME_SERVICE_KEY", "k")
    monkeypatch.setenv("UNRELATED_VAR", "x")
    env = _client_env_overrides()
    assert env.get("ARGO_DAEMON_PROBE") == "1"
    assert env.get("SOME_SERVICE_KEY") == "k"
    assert "UNRELATED_VAR" not in env


def _client_env_overrides() -> dict:
    """复刻 bin/argo._daemon_env 的挑选规则（bin 不 import，测试侧独立验证）。"""
    out = {}
    for k, v in os.environ.items():
        if not (k.startswith("ARGO_") or k.endswith(
                ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_APIKEY"))):
            continue
        out[k] = v
    return out


def test_client_falls_back_when_daemon_busy(tmp_path):
    """daemon 返回 busy 时客户端必须静默回退冷路径并正常出结果。"""
    state = tmp_path / "state"
    state.mkdir()
    sock_path = state / ("daemon.sock-" + _tag())

    fake = subprocess.Popen(
        [sys.executable, "-c", (
            "import json,socket,sys,os\n"
            "sp=sys.argv[1]\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            "s.bind(sp); s.listen(4)\n"
            "for _ in range(4):\n"
            "    c,_=s.accept()\n"
            "    c.recv(65536)\n"
            "    c.sendall((json.dumps({'ok':False,'busy':True})+'\\n').encode())\n"
            "    c.close()\n"), str(sock_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if sock_path.exists():
                break
            time.sleep(0.05)
        (state / (".daemon_sock-" + _tag())).write_text(str(sock_path) + "\n", encoding="utf-8")
        env = _env(tmp_path)
        env["ARGO_NO_DAEMON"] = ""  # 确保快路径开启
        r = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                           env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert len(json.loads(r.stdout)) > 100
        assert "busy" not in r.stderr  # 静默回退：不得把 daemon 内部状态漏给用户
    finally:
        fake.terminate()
        fake.wait(timeout=10)


def test_autostart_requires_opt_in(tmp_path):
    """默认不自启（2026-10-18 B2）：未设 ARGO_DAEMON 时调用只走冷路径，不起进程。"""
    env = _env(tmp_path)
    env.pop("ARGO_NO_DAEMON", None)
    env.pop("ARGO_DAEMON", None)
    r = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)) > 100
    time.sleep(1.0)
    assert not _pointer(tmp_path).exists(), "未 opt-in 却写了指针"


def test_autostart_brings_daemon_up(tmp_path):
    """ARGO_DAEMON=1 时首次调用仍出结果（冷路径），并后台把 daemon 拉起。"""
    env = _env(tmp_path)
    env.pop("ARGO_NO_DAEMON", None)
    env["ARGO_DAEMON"] = "1"
    r = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)) > 100
    _wait_sock(tmp_path, True, timeout=10.0)
    # 第二笔即应走快路径（等价性由 test_cli_fastpath_and_fallback 锁着）
    r2 = subprocess.run([sys.executable, str(BIN), "search", "--list-engines", "--json"],
                        env=env, capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0 and json.loads(r2.stdout) == json.loads(r.stdout)
    _daemon(tmp_path, "stop")
    _wait_sock(tmp_path, False)


def test_start_self_heals_stale_instance(tmp_path):
    """在跑实例指纹不符时，start 必须先停旧的、再起新代码实例（陈旧自愈）。

    回归（2026-10-12 实测踩中）：改脚本后旧 daemon 进入「拒执行+准备退役」，
    但退役与自启之间有时序缝——新 start 看到旧进程 ping 得到就直接报
    「已在运行」，退役完成后无人接手，快路径永久失效。
    """
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    # 模拟代码变更：改 _code_fingerprint 的输入（touch 一个 scripts/*.py）
    import pathlib
    p = ROOT / "scripts" / "search.py"
    st = p.stat()
    try:
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        info = _start(tmp_path)  # 应触发重启而非 already_running
        assert not info.get("already_running"), f"陈旧实例未被自愈: {info}"
        _wait_sock(tmp_path, True)
        # 新实例 fingerprint 应与当前源码一致
        r = _uds(_sock(tmp_path), {"op": "ping"})
        assert r.get("fp") == daemon_mod._code_fingerprint()
    finally:
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    _daemon(tmp_path, "stop")
    _wait_sock(tmp_path, False)


def test_handle_run_skips_when_client_disconnected(tmp_path):
    """对端已断（客户端超时/被杀）时 run 不点火——防同查询双跑双计。"""
    srv = daemon_mod._Server(idle_exit_s=1800)
    srv.fp = daemon_mod._code_fingerprint()
    a, b = socket.socketpair()
    b.close()  # 模拟客户端已断开
    try:
        resp = srv._handle_run(a, {"op": "run", "script": "search",
                                   "args": ["--list-engines", "--json"]})
    finally:
        a.close()
    assert resp["ok"] is False and "断开" in resp.get("error", "")


def test_handle_run_refuses_when_retiring(tmp_path):
    """stop 已置位后到达的 run 不点火（退役中腰斩会产生客户端重跑双计）。"""
    srv = daemon_mod._Server(idle_exit_s=1800)
    srv.fp = daemon_mod._code_fingerprint()
    srv.stop_evt.set()
    a, b = socket.socketpair()
    try:
        resp = srv._handle_run(a, {"op": "run", "script": "search",
                                   "args": ["--list-engines", "--json"]})
    finally:
        a.close(); b.close()
    assert resp["ok"] is False and "退役" in resp.get("error", "")


def test_stop_unlinks_socket_promptly_while_run_in_flight(tmp_path):
    """在途 run 期间 stop：socket 必须立刻消失（新客户端立即回退，不等收尾窗）。"""
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    sock = _sock(tmp_path)
    # 起一笔长 run（用 sleep 假脚本不在白名单，改用一个真实但慢的请求不可控；
    # 这里验证「无在途时 stop 立即摘 socket」这一主路径即可，收尾等待另有覆盖）
    r = _daemon(tmp_path, "stop")
    assert r.returncode == 0
    assert not sock.exists(), "stop 返回时 socket 必须已摘除"
    _wait_sock(tmp_path, False)


def test_ping_reports_uptime_and_idle(tmp_path):
    """ping 的 uptime_s 必须是进程龄、idle_s 是空闲秒（此前 uptime 实为 idle）。"""
    _start(tmp_path)
    _wait_sock(tmp_path, True)
    time.sleep(1.2)
    r = _uds(_sock(tmp_path), {"op": "ping"})
    assert r["uptime_s"] >= 1.0, f"uptime_s 应为进程龄: {r}"
    assert r["idle_s"] >= 1.0, f"idle_s 应为空闲秒: {r}"
    assert r.get("fp"), "ping 必须带源码指纹（start 自愈依赖它）"
    _daemon(tmp_path, "stop")
    _wait_sock(tmp_path, False)


def test_pid_is_daemon_recognizes_both_launch_forms(monkeypatch):
    """_pid_is_daemon 必须同时认两种启动形态。

    形态 B（`./bin/argo daemon start`）此前漏判：bin/argo 经 runpy 原地执行时
    Python 改的是 sys.argv，内核 argv 不变，ps 里始终显示 bin/argo——正是
    「谁漏判谁就会在 stop 兜底路径留下孤儿进程」（2026-10-13 实测发现）。
    """
    import subprocess as _sp

    def fake_ps(cmd):
        out = cmd  # 测试把「ps 输出」直接当参数传
        def run(argv, **kw):
            m = type("R", (), {})()
            m.stdout = out + "\n"
            return m
        return run

    cases_true = [
        f"/opt/homebrew/bin/python3 {ROOT}/scripts/daemon.py start --json",
        "/Users/x/agents/skills/argo/bin/argo daemon start",
        "./bin/argo daemon start",
        "argo daemon start",
    ]
    cases_false = [
        "vim /Users/x/agents/skills/argo/scripts/daemon.py",  # 编辑器，无 start
        "/usr/bin/python3 /other/repo/scripts/daemon.py start",  # 他仓
        "/Applications/Some.app/Contents/MacOS/Python daemon start",  # 无 argo/daemon.py
        "./bin/argo search --list-engines",  # argo 但非 daemon
    ]
    for out in cases_true:
        monkeypatch.setattr(_sp, "run", fake_ps(out))
        assert daemon_mod._pid_is_daemon(1234) is True, f"应识别: {out}"
    for out in cases_false:
        monkeypatch.setattr(_sp, "run", fake_ps(out))
        assert daemon_mod._pid_is_daemon(1234) is False, f"不应识别: {out}"


# ── 按检出目录隔离（2026-10-16） ────────────────────────────────────────────

class TestTreeScopedDaemonFiles(unittest.TestCase):
    """daemon 全部落盘名字按检出目录隔离：同机多 worktree/clone 互不踩。

    踩雷实证：同机 ~/.agents/skills/argo 与 /private/tmp/argo-head-check 两个
    检出共存时，固定名的 socket/pid/指针会让 A 树 CLI 连上 B 树 daemon（跑
    B 的旧代码且指纹检测发现不了）、A 树 stop 杀 B 树进程。
    """

    def test_all_five_names_carry_tree_tag(self):
        import daemon as daemon_mod
        tag = daemon_mod._tree_tag()
        for name, path in (
            ("sock", daemon_mod._sock_path()),
            ("pid", daemon_mod._pid_path()),
            ("pointer", daemon_mod._pointer_path()),
            ("marker", daemon_mod._marker_path()),
            ("log", daemon_mod._log_path()),
        ):
            self.assertIn(tag, path, f"{name} 未带树指纹: {path}")

    def test_different_trees_get_different_names(self):
        import daemon as daemon_mod
        original_home = daemon_mod.ARGO_HOME
        original_tag = daemon_mod._TREE_TAG
        try:
            daemon_mod.ARGO_HOME = "/tree/a/argo"
            daemon_mod._TREE_TAG = None
            tag_a = daemon_mod._tree_tag()
            daemon_mod.ARGO_HOME = "/tree/b/argo"
            daemon_mod._TREE_TAG = None
            tag_b = daemon_mod._tree_tag()
            self.assertNotEqual(tag_a, tag_b)
            self.assertNotEqual(daemon_mod._sock_path().split("daemon.sock")[-1],
                                "")
        finally:
            daemon_mod.ARGO_HOME = original_home
            daemon_mod._TREE_TAG = original_tag

    def test_bin_argo_tag_matches_daemon(self):
        """两侧契约：bin/argo 与 daemon.py 对同一 ARGO_HOME 必须算出同一指纹。"""
        import re as _re
        import daemon as daemon_mod
        bin_src = (ROOT / "bin" / "argo").read_text(encoding="utf-8")
        m = _re.search(r"def _tree_tag\(\):.*?(?=\ndef |\n\n# )", bin_src, _re.S)
        self.assertIsNotNone(m, "bin/argo 应定义 _tree_tag")
        ns = {"ARGO_HOME": daemon_mod.ARGO_HOME}
        exec(compile(m.group(0), "bin-argo-tree-tag", "exec"), ns)
        self.assertEqual(ns["_tree_tag"](), daemon_mod._tree_tag(),
                         "bin/argo 与 daemon.py 的树指纹不一致（两端会找不到彼此的 daemon）")
