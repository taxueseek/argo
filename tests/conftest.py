#!/usr/bin/env python3
"""pytest 全局配置。

存量测试大量 mock urllib.request.urlopen 验证引擎 URL 构造（setlang/lang 等），
HttpClient 接入后这些 mock 不再生效。默认回退 urllib 路径，保证存量检查行为
不变；HttpClient 新行为的专项测试显式 monkeypatch.setenv 开启。

另：把 argo 状态目录隔离到临时目录。部分模块（如 v2ex_nodes 的节点表缓存）
默认写 argo_paths.state_path()，测试若不隔离会**污染生产缓存**——
实测曾把测试 fixture（1 个假节点）写进 ~/.cache/unified-search/，
导致真实调用全部路由失败。ARGO_STATE_DIR 是 argo 既有的一级开关，
在这里设一次即可覆盖所有遵循该约定的模块。
"""

import atexit
import os
import shutil
import tempfile

import pytest

os.environ.setdefault("ARGO_ENGINE_HTTP_CLIENT", "0")

# 会话级临时根：tests/ 里 30+ 处 `tempfile.mkdtemp` 都不自行清理（各自的用例
# 只关心目录内容、不管善后），逐处补 cleanup 既易漏——新增用例会再漏——也难
# 维持。这里把 `tempfile.tempdir` 与 `TMPDIR` 一并指向会话目录：本进程所有
# mkdtemp/mkstemp、以及测试 spawn 出的子进程里新建的临时目录，都收在同一个
# 根下，退出时一次 rmtree 清干净。**一处修改覆盖整类泄漏**（实测本机曾积
# 1135 个 `argo-*` 残留目录 / ~40MB，且每跑一次测试只增不减）。
_SESSION_TMP = tempfile.mkdtemp(prefix="argo-test-session-")
tempfile.tempdir = _SESSION_TMP
os.environ["TMPDIR"] = _SESSION_TMP
atexit.register(shutil.rmtree, _SESSION_TMP, ignore_errors=True)

# 路由决策缓存默认关闭，理由与上面的 HTTP_CLIENT 同类：它是跨进程的持久缓存，
# 而本会话的状态目录**整轮共享**——于是「A 用例路由过 Q」会把决策留给「B 用例
# 换过夹具后再路由 Q」，用例之间互相串味，且结果与执行顺序相关。关掉后退回每次
# 实算，存量检查的行为与引入缓存前逐位一致；缓存自身的行为由
# tests/test_route_cache.py 显式打开开关验证。
os.environ["ARGO_ROUTE_CACHE"] = "0"

# 状态目录隔离（必须在任何 argo 模块 import 前设置）
_STATE_DIR = tempfile.mkdtemp(prefix="argo-test-state-")
os.environ["ARGO_STATE_DIR"] = _STATE_DIR

# 密钥文件隔离（2026-09-29）：get_env 在 os.environ 之后会热读
# ~/.config/argo/env——开发者机器上的真实密钥会渗进测试：未配 key 的
# 「必须显式报错」用例（seltz/you/parallel 等）拿到真 key 后静默通过，
# 而 CI 上同样是红的——方向相反的假绿/假红。与 ARGO_STATE_DIR 同一理由：
# 测试不得读取开发者的真实配置。指向临时目录里一个不存在的文件 = 空 env
# 文件；需要验证 env 文件行为的测试自行显式设置 ARGO_ENV_FILE。
os.environ["ARGO_ENV_FILE"] = os.path.join(_STATE_DIR, "nonexistent-env")


# HTTP 连接池（conn_pool）是**进程级**可变状态：某个用例期间建立的空闲连接
# 会被后续用例复用，改变它们看到的网络形态。实测（2026-09-27）：
# test_fetch_md_negotiate 的用例给 example.com 留了一条经代理的活连接，
# test_stop_signal 的 mobile fetch 复用它拿到真实内容、链条在移动端短路，
# 断言随之失败——而单独跑两个文件都绿。与 ARGO_STATE_DIR 同一理由：
# 跨用例共享的进程级状态必须每用例清空。
@pytest.fixture(autouse=True)
def _isolate_conn_pool():
    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import conn_pool
    except ImportError:
        yield
        return
    conn_pool.clear()
    yield
    conn_pool.clear()
