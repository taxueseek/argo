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

# usage_log 同理（2026-10-06）：测试发出的真实查询（如「测试查询」类 canary）
# 曾写入生产 usage_log/query.jsonl，污染用量统计与延迟分布基准。同一命令关掉；
# usage_log 自身行为由 tests/test_usage_log.py 显式开启验证。
os.environ.setdefault("ARGO_USAGE_LOG", "0")

# routable memo 同理关闭（2026-09-30）：它是进程内 1s 时窗缓存，而全量套件的
# 状态目录整轮共享——A 用例改引擎可用性（ENABLE/DISABLE 开关、准入拉黑）后
# 时窗内的 B 用例会拿到脏路由集，实测导致 zhihu_hot/scenario_thresholds 等
# 6 个路由形态用例串味失败。ARGO_ADMISSION_TTL_S=0 是 get_engines 侧同一
# 语义层的既有旋钮，memo 随 admission 缓存一并关闭，回退逐次精确过滤；
# memo 自身的行为由 tests/test_routable_memo.py 显式开启旋钮验证。
os.environ.setdefault("ARGO_ADMISSION_TTL_S", "0")

# daemon 快路径默认关闭（2026-10-12）：bin/argo 现在对缺失指针做「后台按需
# 自启」，测试里随手一次 `bin/argo search` 都会 spawn 一个常驻进程——全量套件
# 会留下一批存活 30 分钟的游离 daemon（实测 6 个），既污染本机又让后续用例
# 的延迟分布失真。需要验证快路径的用例在 tests/test_daemon.py 内显式打开
# （其 _env 会 pop 掉这个变量）。
os.environ.setdefault("ARGO_NO_DAEMON", "1")

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

# 仓库自带入口上 PATH（2026-10-22，第一次在 CI 上跑全量才发现）：一批用例
# subprocess 调用 `argo …`——开发机上它是 `~/.local/bin/argo` 的符号链接，
# CI 上没有这层安装，于是 3 条用例红成 FileNotFoundError: 'argo'。
# 这里把 repo/bin 放到 PATH 最前，等价于「装过」的环境；不改安装方式，
# 也不把测试改成直接调 scripts/*.py（那会漏掉入口包装层的行为）。
_BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if os.path.isdir(_BIN_DIR):
    os.environ["PATH"] = _BIN_DIR + os.pathsep + os.environ.get("PATH", "")


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


@pytest.fixture(autouse=True)
def _isolate_adaptive_and_breaker(tmp_path_factory, monkeypatch):
    """自适应成绩与熔断状态都是**跨用例共享的进程级/磁盘状态**。

    实测（2026-10-23）：`test_full.py` 等真跑 super_search 的用例会把
    local_bing 的低分写进共享 adaptive 库；几十个用例之后，这个分数足以
    把它挤出路由 combo，表现为 test_multilingual / test_route_golden 只在
    全量套件里红、单跑全绿。熔断状态同理（状态目录整轮共享）。

    每个用例换一份临时 adaptive.db / circuit_breaker.json，并清掉模块级
    单例；`route_combo` 也缓存了 learner，必须一并回到未解析哨兵。
    临时目录用 tmp_path_factory 另开，**不能复用用例自己的 tmp_path**——
    「基准跑完不得留文件」一类断言会把它当成被测代码的残留。
    """
    try:
        import adaptive
        import circuit_breaker
        import route_combo
    except ImportError:
        yield
        return
    state_dir = tmp_path_factory.mktemp("engine-state")
    monkeypatch.setattr(adaptive, "DB_PATH", state_dir / "adaptive.db")
    adaptive._learner = None
    route_combo._adaptive_learner = route_combo._UNRESOLVED
    _old_breaker = circuit_breaker._breaker
    circuit_breaker._breaker = circuit_breaker.CircuitBreaker(
        state_path=str(state_dir / "circuit_breaker.json"))
    yield
    adaptive._learner = None
    route_combo._adaptive_learner = route_combo._UNRESOLVED
    circuit_breaker._breaker = _old_breaker


@pytest.fixture
def allow_fake_hosts(monkeypatch):
    """让「假域名」不再真的走 DNS：`docs.example.com` 这类地址在本地可能被
    ISP 劫持解析成公网 IP（实测本机解析到 198.18.0.x），在 CI 上则是 NXDOMAIN，
    SSRF 防护 fail-closed 后整条 fetch 链返回 `fetch_method="blocked"`
    ——同一份用例「本地绿、CI 红」（2026-10-22 第一次跑 CI 时 12 条一起红）。

    SSRF 本身由 tests/test_tls_spoof.py 与 url_safety 的用例覆盖；这里只把
    「能不能访问」放行，fetch 链的其它分支逻辑（协商、md 变体、移动端、tinyfish）
    照常真跑。只在用假域名的用例上显式声明，不做全局放宽。
    """
    import url_safety

    monkeypatch.setattr(url_safety, "check_url", lambda url: (True, ""))
