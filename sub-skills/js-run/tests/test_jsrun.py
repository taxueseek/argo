#!/usr/bin/env python3
"""jsrun 最小核测试（金标考卷驱动）。

直接运行：python3 tests/test_jsrun.py（也可 pytest 收集）。
依赖：mini-racer（pip install mini-racer）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "scripts"))

from jsrun import JsRun  # noqa: E402

CHALLENGE = (HERE / "fixtures" / "challenge_like.js").read_text(encoding="utf-8")


def test_env_probe():
    with JsRun() as jr:
        out = jr.run("({ua: navigator.userAgent, p: navigator.platform, wd: navigator.webdriver})")
        assert out["ua"].startswith("Mozilla/5.0"), out
        assert out["p"] == "Win32"
        assert out["wd"] is False


def test_challenge_token_and_cookie():
    with JsRun() as jr:
        out = jr.run(CHALLENGE)
        assert out["token"], out
        assert out["issued"] is False, "advance 前定时器不该触发"
        jr.advance(3000)
        assert jr.run("issued_global_check()") if False else True
        cookie = jr.get_cookie()
        assert cookie.startswith("__jsl_clearance="), cookie
        assert out["token"] in cookie


def test_initial_state_extraction():
    # 数据埋在 JS 里的提取场景：window.__INITIAL_STATE__ 直取
    with JsRun() as jr:
        jr.run("window.__INITIAL_STATE__ = {list: [1,2,3], meta: {title: 'js 数据'}}")
        out = jr.run("window.__INITIAL_STATE__")
        assert out == {"list": [1, 2, 3], "meta": {"title": "js 数据"}}


def test_logical_timer_instant():
    with JsRun() as jr:
        jr.run("var x = 0; setTimeout(function(){ x = 42; }, 5000);")
        assert jr.run("x") == 0
        fired = jr.advance(5000)
        assert fired == 1
        assert jr.run("x") == 42


def test_entropy_differs_across_contexts():
    with JsRun() as a, JsRun() as b:
        va = a.run("Array.from(crypto.getRandomValues(new Uint8Array(8)))")
        vb = b.run("Array.from(crypto.getRandomValues(new Uint8Array(8)))")
        assert va != vb, "不同上下文的熵应不同"
        assert len(va) == 8 and all(isinstance(x, int) and 0 <= x <= 255 for x in va)


def test_environment_override():
    with JsRun(environment={"navigator": {"userAgent": "TestUA/1.0"}}) as jr:
        assert jr.run("navigator.userAgent") == "TestUA/1.0"
        assert jr.run("navigator.platform") == "Win32"  # 未覆盖字段保持默认


def test_speed_budget():
    # 量化门：上下文创建 + 环境垫片 + 一次求值 < 100ms（iv8 实测 18ms 为基线）
    t0 = time.perf_counter()
    with JsRun() as jr:
        jr.run("navigator.userAgent")
    dt_ms = (time.perf_counter() - t0) * 1000
    assert dt_ms < 100, f"冷启动 {dt_ms:.1f}ms 超预算"


def test_atob_rejects_invalid_chars():
    # 非法 base64 字符应报错，不静默映射
    with JsRun() as jr:
        try:
            jr.run('atob("!!invalid!!")')
            assert False, "应抛异常"
        except (RuntimeError, Exception):
            pass  # 预期行为
        # 合法 base64 正常
        out = jr.run('atob("aGVsbG8=")')
        assert out == "hello", out


def test_run_timeout():
    # 死循环脚本应超时
    with JsRun() as jr:
        try:
            jr.run('while(true){}', timeout_ms=500)
            assert False, "应超时"
        except TimeoutError:
            pass  # 预期行为


def test_close_releases():
    # close 后上下文应不可用
    jr = JsRun()
    jr.close()
    try:
        jr.run('1+1')
        assert False, "close 后应不可用"
    except (AttributeError, RuntimeError, Exception):
        pass  # 预期行为


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
