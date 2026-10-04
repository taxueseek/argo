"""jsrun — 不开浏览器跑网页 JS（最小核 v0）。

定位：取数链「JS 渲染/挑战」格的原语。给 HTML 与资源，还你 JS 算完的结果。
v0 面只覆盖「环境探测 + 纯计算」型脚本（挑战通行证、混淆解算），
DOM 解析与真实网络明确不在 v0（见 references/design.md 生长路线）。

底座：mini-racer（ISC，V8 绑定）；环境垫片 shim.js 自研（原创部分）。

用法：
    from jsrun import JsRun
    jr = JsRun()
    token = jr.run("btoa(navigator.userAgent.slice(0,10))")
    jr.advance(5000)   # 逻辑时间：定时器瞬间到期
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

try:
    from py_mini_racer import MiniRacer, JSUndefined
except ImportError as _e:  # pragma: no cover
    MiniRacer = None
    _IMPORT_ERR = _e
    JSUndefined = type("_JSUndefinedMissing", (), {})

_SHIM_PATH = Path(__file__).parent / "shim.js"


def _to_py(v):
    """mini-racer 对象递归转 Python 原生容器。"""
    import collections.abc as _abc
    if v is None or v is JSUndefined or isinstance(v, (bool, int, float, str)):
        return None if v is JSUndefined else v
    if isinstance(v, _abc.Mapping):
        return {k: _to_py(val) for k, val in v.items()}
    if isinstance(v, _abc.Sequence):
        return [_to_py(x) for x in v]
    return v  # 函数等不可转换对象原样交还


class JsRun:
    """单上下文。每实例一个独立 V8 isolate；用完 close() 或随 GC。"""

    def __init__(self, environment: dict | None = None):
        if MiniRacer is None:
            raise RuntimeError(f"缺少 mini-racer：pip install mini-racer（{_IMPORT_ERR}）")
        self._ctx = MiniRacer()
        env = json.dumps(environment or {}, ensure_ascii=False)
        entropy = secrets.token_hex(16)
        self._ctx.eval(f"var __jsrun_env__ = {env}; var __jsrun_entropy__ = '{entropy}';")
        self._ctx.eval(_SHIM_PATH.read_text(encoding="utf-8"))

    def run(self, source: str) -> object:
        """执行 JS 脚本，返回完成值（递归转 Python 容器）。

        直接走 mini-racer 的脚本求值：顶层 var 声明与 window 赋值跨 run()
        持久（挑战脚本普遍依赖这个），脚本完成值即返回值（IIFE 结果能透出）。
        """
        return _to_py(self._ctx.eval(source))

    def eval_raw(self, source: str):
        """不经 JSON 包装的原始求值（拿标量用）。"""
        return self._ctx.eval(source)

    def advance(self, ms: int) -> int:
        """推进逻辑时间 ms 毫秒，返回触发的定时器回调数。"""
        return self._ctx.eval(f"__jsrun__.advance({int(ms)})")

    def set_cookie(self, value: str) -> None:
        self._ctx.eval(f'document.cookie = {json.dumps(value)};')

    def get_cookie(self) -> str:
        return self._ctx.eval("document.cookie")

    def close(self) -> None:
        self._ctx = None

    def __enter__(self) -> "JsRun":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
