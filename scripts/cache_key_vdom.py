"""cache_key_vdom — 缓存键里的「引擎级垂直域」维度。

**为什么单独成文件**：`cache.py` 已到模块体积门禁上限（`tests/test_module_size_gate.py`
登记 1375 行），而这层语义又必须与缓存键的其余维度待在一起。

**为什么它必须在键里**：`--domain` / `--sub_domain` 改变的是**发给引擎的请求**，
同 query 在不同垂直域下结果集不同。此前两个开关被 argparse 接下、写进 SKILL.md
参数表，却没有任何一层读取——请求照发、结果照回，`domain` 从未到达引擎。
接上之后若不进键，「不限域」的结果会被当成「限定金融域」的答案发回去：
一个静默失效的开关会变成一个静默给错答案的开关。

**两种 domain 同名不同义，刻意不合并**：

- `domain`：**路由域**（general / stock / weather…），决定选哪些源；
- `engine_domain` / `engine_sub_domain`：**引擎入参**，改变单个引擎的请求体。

前者早就在键里；这里补的是后者。`short` 短键只用于拼 `raw`，不进 DB 列。
"""
from __future__ import annotations

from typing import Any, Iterable

# kwargs 名 → 键内短名。顺序固定，保证键的构造是确定性的。
_VDOM_TAGS: tuple[tuple[str, str], ...] = (
    ("engine_domain", "ed"),
    ("engine_sub_domain", "esd"),
)


def cache_key_vdom(vdom: dict[str, Any] | None) -> list[tuple[str, str]]:
    """把垂直域 kwargs 摊成 `[(短名, 值)]`，供缓存键拼装使用。

    未知键一律忽略（而不是透传）：`_key` 的 `**vdom` 是开放签名，
    拼键只认登记过的维度，避免调用方拼错名字时静默丢维度——那正是本模块
    要消灭的那类 bug。空值 / 非字符串 / None 都被滤掉，保证「没给」
    与「给了空串」在键上等价，不会凭空分裂出两份缓存。
    """
    if not vdom:
        return []
    out: list[tuple[str, str]] = []
    for name, tag in _VDOM_TAGS:
        val = vdom.get(name)
        if isinstance(val, str) and val:
            out.append((tag, val))
    return out


def has_vdom(vdom: dict[str, Any] | None) -> bool:
    """是否指定了任意垂直域维度（供需要早退的调用方省一次拼键）。"""
    return bool(cache_key_vdom(vdom))


def vdom_of(iterable: Iterable[tuple[str, str]]) -> dict[str, str]:
    """`cache_key_vdom` 的逆（按登记顺序还原成长名 → 值）。"""
    rev = {tag: name for name, tag in _VDOM_TAGS}
    return {rev[tag]: val for tag, val in iterable if tag in rev}
