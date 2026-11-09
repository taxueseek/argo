#!/usr/bin/env python3
"""search_types.py — 统一类型系统（轻量记录 + 零依赖 dataclass 替代）。

`slot_record` / `slot_field` / `slot_replace` 是为热路径准备的
`@dataclass` / `field` / `replace` 零依赖替代：`dataclasses` 的导入链是
`dataclasses → inspect → tokenize → dis`（实测 ~8ms），而 `SearchResult`
这类记录在每次搜索都要构造，导入成本全落在固定开销上。

兼容面：挂 `__dataclass_fields__`，标准库的 `dataclasses.replace` /
`is_dataclass` / `fields` 对实例照常工作（`_field_type` 惰性解析）。

仍不建议新代码接入本模块的 `SearchResult`（引擎输出直接透传 dict，运行时
防御在 engines_base/safe_search 里）；这里的价值在**替代实现本身**。
"""
from __future__ import annotations

import sys
from enum import Enum
from typing import Any


class _FieldMeta:
    """`__dataclass_fields__` 的兼容描述子。

    `dataclasses.replace` 只读 `.init`；`dataclasses.fields` 会按
    `_field_type is dataclasses._FIELD` 过滤，所以 `_field_type` 做成**惰性
    属性**：dataclasses 已被 import 时（也就是有人真去调 `fields()` 时）
    返回真哨兵，没 import 时返回 None。热路径因此仍然零导入——`_FIELD`
    只在 dataclasses 已经进场后才去取它。
    """

    __slots__ = ("name", "init")

    def __init__(self, name: str) -> None:
        self.name = name
        self.init = True

    @property
    def _field_type(self) -> Any:
        dc = sys.modules.get("dataclasses")
        return getattr(dc, "_FIELD", None) if dc is not None else None


class _SlotField:
    __slots__ = ("factory",)

    def __init__(self, default_factory: Any) -> None:
        self.factory = default_factory


def slot_field(*, default_factory: Any) -> Any:
    """零依赖 field(default_factory=...) 替代。"""
    return _SlotField(default_factory)


def _class_annotations(cls: Any) -> dict:
    """读类的注解，兼容 PEP 649 惰性注解。

    `cls.__dict__["__annotations__"]` 在 3.14 的惰性注解下**可能根本不存在**
    （注解由 `__annotate__` 描述子按需计算，不进类字典）——此时取到的是
    `{}`，类会静默变成「零字段」，实例化时报一个与真因毫不相干的
    TypeError。必须走 `getattr` 触发描述子。
    """
    return getattr(cls, "__annotations__", None) or {}


def slot_record(_cls: Any = None, *, frozen: bool = False) -> Any:
    """零依赖 @dataclass 替代（省去 import dataclasses→inspect→tokenize→dis ~8ms）。

    挂上 `__dataclass_fields__` 兼容描述子，标准库 `dataclasses.replace` /
    `dataclasses.is_dataclass` / `dataclasses.fields` 对实例照常工作，但热
    路径自身零导入（`_field_type` 惰性解析，只在 dataclasses 已进场时取哨兵）。
    """
    def _wrap(cls: Any) -> Any:
        ann = list(_class_annotations(cls).keys())
        defaults = {k: cls.__dict__[k] for k in ann if k in cls.__dict__}

        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            if len(args) > len(ann):
                raise TypeError(f"{cls.__name__}.__init__() takes {len(ann)} positional arguments but {len(args)} were given")
            for i, val in enumerate(args):
                object.__setattr__(self, ann[i], val)
            for k in ann[len(args):]:
                if k in kwargs:
                    val = kwargs.pop(k)
                elif k in defaults:
                    d = defaults[k]
                    val = d.factory() if isinstance(d, _SlotField) else d
                else:
                    raise TypeError(f"{cls.__name__}.__init__() missing required argument: {k!r}")
                object.__setattr__(self, k, val)
            if kwargs:
                bad = next(iter(kwargs))
                raise TypeError(f"{cls.__name__}.__init__() got an unexpected keyword argument {bad!r}")

        cls._slot_fields = tuple(ann)
        cls.__dataclass_fields__ = {k: _FieldMeta(k) for k in ann}
        cls.__init__ = __init__
        if frozen:
            def __setattr__(self: Any, name: str, value: Any) -> None:
                raise AttributeError(f"cannot assign to field {name!r}")
            cls.__setattr__ = __setattr__
        return cls

    return _wrap if _cls is None else _wrap(_cls)


def slot_replace(obj: Any, **changes: Any) -> Any:
    """零依赖 replace(obj, **changes) 替代。"""
    kw = {k: getattr(obj, k) for k in obj._slot_fields}
    kw.update(changes)
    return type(obj)(**kw)


@slot_record
class SearchResult:
    """统一的搜索结果数据类。"""
    title: str
    url: str = ""
    snippet: str = ""
    score: float = 0.5
    source: str = ""
    metadata: dict = slot_field(default_factory=dict)
    social_meta: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为字典，省略空值/默认值字段。"""
        d: dict[str, Any] = {"title": self.title}
        if self.url:
            d["url"] = self.url
        if self.snippet:
            d["snippet"] = self.snippet[:300]
        if self.score != 0.5:
            d["score"] = round(self.score, 3)
        if self.source:
            d["source"] = self.source
        if self.metadata:
            d["metadata"] = self.metadata
        if self.social_meta:
            d["social_meta"] = self.social_meta
        return d


def normalize_result(item: dict, engine: str, default_score: float = 0.5) -> SearchResult:
    """将任意引擎输出统一为 SearchResult。

    处理字段名混乱（score/relevance_score/snippet/content/summary 混用）。
    """
    snippet = (
        item.get("snippet")
        or item.get("content")
        or item.get("summary")
        or item.get("description", "")
    )
    raw_score = item.get("score", item.get("relevance_score", default_score))
    try:
        score = float(raw_score) if isinstance(raw_score, (int, float, str)) else default_score
    except (ValueError, TypeError):
        score = default_score

    return SearchResult(
        title=item.get("title", "")[:200],
        url=item.get("url", ""),
        snippet=snippet[:300] if snippet else "",
        score=score,
        source=item.get("source", engine),
        metadata=item.get("metadata", {}),
        social_meta=item.get("social_meta"),
    )


class Stage(str, Enum):
    """进度回调的阶段名（on_progress 契约）。

    从 search.py 搬来这里：执行层与加工层（search_pipeline）都要发进度事件，
    枚举放在任何一侧都会让另一侧反向依赖；search_types 是本仓共享类型的家。

    终态契约：一次运行以 DONE 收尾（缓存命中路径同样发 DONE，带
    `cached=True`）；异常路径由调用方自行捕获并回落，ERROR 保留给消费方
    上报失败用，本仓热路径不自行发出——不要在流程里「补」一个 ERROR 分支，
    那会把「回调没收到事件」和「搜索失败」混成同一件事。
    """

    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"
    ERROR = "error"
