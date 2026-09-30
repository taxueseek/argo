#!/usr/bin/env python3
"""get_engines(routable_only=True) 进程内记忆化（2026-09-30 route 回归修复）。

背景：route_query 每 query 连调 get_engines(routable_only=True) 多次，每次
对全部引擎逐个跑 env_ready/is_blocked——cProfile 实测占 route 耗时约一半，
基准捕获回归 6.5→8.8 ms/query。修复为进程内记忆化，失效键四件套：
config_stamp、env 文件签名、进程内 ENABLE/DISABLE 开关、1s 时窗。

本文件锁三件事（前两条在未修复代码上必红）：
  1. 同秒内重复调用不重算（性能回归锁，红绿主断言）；
  2. 显式传 config 的调用方不走 memo（契约测试语义保留）；
  3. 失效键各分量变化会触发重算（防「修过头缓存住脏结果」）。

conftest 默认关 memo（同一旋钮 ARGO_ADMISSION_TTL_S=0，防套件串味），
本文件用 autouse fixture 显式开回 memo 并清状态。

引用纪律：本文件内对 engine_env / engine_admission 一律**函数级 import**，
不绑模块级名字——test_state_integrity 等用例会 importlib.reload 这些模块，
模块级绑定会指向旧实例（准入目录、缓存各一套），与生产路径（config 内
函数级 import 解析到 sys.modules 当前实例）对不上，产生本文件自身与
被测对象不在同一世界的假故障（实测：拉黑写入旧实例目录，过滤读新实例，
轮询 5s 仍不见生效，探针 filter_mod != test_mod 才定位）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from config import get_engines, load_config  # noqa: E402


@pytest.fixture(autouse=True)
def _memo_on(monkeypatch):
    """本文件内开启 memo，并每用例前清缓存状态（不影响其他文件）。"""
    monkeypatch.setenv("ARGO_ADMISSION_TTL_S", "1")
    monkeypatch.setattr(config, "_routable_memo", None)
    yield
    config._routable_memo = None


class _CallCounter:
    """包装 env_ready / is_blocked，统计真实调用次数。

    config.get_engines 的底层 import 是函数内执行（每次调用时从模块取当前
    属性），所以 setattr sys.modules 当前实例即可生效。
    """

    def __init__(self):
        import engine_admission
        import engine_env
        self.env = 0
        self.blocked = 0
        self._orig_ready = engine_env.env_ready
        self._orig_blocked = engine_admission.is_blocked

    def install(self, monkeypatch):
        import engine_admission
        import engine_env

        def counting_ready(*a, **k):
            self.env += 1
            return self._orig_ready(*a, **k)

        def counting_blocked(*a, **k):
            self.blocked += 1
            return self._orig_blocked(*a, **k)

        monkeypatch.setattr(engine_env, "env_ready", counting_ready)
        monkeypatch.setattr(engine_admission, "is_blocked", counting_blocked)


def test_memo_second_call_does_not_recompute(monkeypatch):
    """同秒内两次调用：第二次不得重新逐引擎判定（性能回归锁）。

    未修复代码每次调用都全量过滤 → 计数增长 → 本测试红；修复后第二次
    命中 memo → 计数不变 → 绿。
    """
    c = _CallCounter()
    c.install(monkeypatch)

    first = get_engines(routable_only=True)
    assert first, "routable 过滤结果不应为空"
    n1 = c.env

    second = get_engines(routable_only=True)
    assert second == first
    assert c.env == n1, (
        f"第二次调用重新计算了逐引擎判定（{n1}→{c.env}），"
        "route 每 query 连调多次会被放大成上百万次判定"
    )


def test_explicit_config_bypasses_memo(monkeypatch):
    """显式传 config 的调用方保留逐次精确语义（test_engine_catalog 依赖）。"""
    c = _CallCounter()
    c.install(monkeypatch)
    cfg = load_config()

    first = get_engines(cfg, routable_only=True)
    n1 = c.env
    assert n1 > 0
    second = get_engines(cfg, routable_only=True)
    assert second == first
    assert c.env > n1, "显式传 config 不应命中 memo，必须逐次重算"


def test_invalidation_on_process_env_switch(monkeypatch):
    """进程内 ARGO_DISABLE_ENGINES 变化必须立即反映（失效键覆盖开关）。"""
    first = get_engines(routable_only=True)
    assert first

    monkeypatch.setenv("ARGO_DISABLE_ENGINES", "hackernews")
    second = get_engines(routable_only=True)
    assert "hackernews" in first
    assert "hackernews" not in second, "进程内开关变化后 memo 未失效"


def test_invalidation_on_admission_write(monkeypatch, tmp_path):
    """进程内准入写入（熔断拉黑）最迟 1s 时窗后必须反映。

    set_blocked 改的是 admission 文件（engine_admission 自己的读缓存会同步
    失效），route 侧 memo 靠 1s 时窗兜底——睡过时窗后断言生效。
    ARGO_ADMISSION_TTL_S=0（memo 关闭）则立即生效，作为无侵入回滚开关验证。

    准入存储整体重定向到本用例专属目录：临时目录里没有先行用例的拉黑记录，
    且拉黑后「删文件即复原」——此前用 set_blocked(False, reason='test cleanup')
    收尾会在真实会话目录留下 blocked=false 却残留 reason 的记录，击穿
    test_engine_admission 的准入不变式（未拉黑不得残留 reason）。
    """
    import time as _t

    import engine_admission  # 函数级 import：拿 sys.modules 当前实例（见文件头）

    monkeypatch.setattr(engine_admission, "DEFAULT_ADMISSION_DIR", tmp_path / "adm")

    first = get_engines(routable_only=True)
    engine = next(iter(first))
    assert engine

    engine_admission.set_blocked(engine, True, reason="test")
    try:
        # 轮询而非固定 sleep：对 daemon 线程中途重刷 memo（滑动过期）免疫；
        # 生产语义不变——重刷永远基于当下输入，陈旧度仍有界。
        deadline = _t.monotonic() + 5.0
        later = first
        while _t.monotonic() < deadline:
            later = get_engines(routable_only=True)
            if engine not in later:
                break
            _t.sleep(0.1)
        assert engine not in later, (
            f"准入拉黑超过时窗后仍被路由选中；磁盘记录："
            f"{engine_admission.load_admission(engine)}")
    finally:
        p = engine_admission.admission_path(engine)
        p.unlink(missing_ok=True)
        assert engine_admission.load_admission_fresh(engine) is None
        engine_admission._admission_read_cache.pop(str(p), None)
        config._routable_memo = None  # 拉黑期间写入的 memo 不得泄给后续用例
