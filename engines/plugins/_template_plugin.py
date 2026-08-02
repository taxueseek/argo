"""L3 插件模板：畸形协议 / 多端点 / HTML 重解析时使用。

复制为 engines/plugins/myengine.py（去掉前导 _），并在 config 或
engines/specs/myengine.yaml 中设置 type: myengine。

约定：
  ENGINE_TYPE = "myengine"
  def build_engine(spec) -> Callable[[query, n, timeout, ...], list[dict]]
"""

from __future__ import annotations

import os
from typing import Any, Callable

# 与 config/spec 中 type 字段对应
ENGINE_TYPE = "myengine_template"


def build_engine(spec: dict[str, Any]) -> Callable[..., list[dict[str, Any]]]:
    timeout_default = float(spec.get("timeout", 8))
    engine_name = spec.get("_name") or ENGINE_TYPE

    def _search(
        query: str,
        n: int = 5,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        # 示例：读 Key（优先 ARGO_ 前缀）
        api_key = (
            os.environ.get("ARGO_MYENGINE_API_KEY")
            or os.environ.get("MYENGINE_API_KEY")
            or ""
        )
        if not api_key:
            return []
        # TODO: 调用真实 API，统一返回 title/url/snippet/source
        _ = timeout or timeout_default
        return [
            {
                "title": f"[template] {query}",
                "url": "https://example.com",
                "snippet": "replace this plugin with real implementation",
                "source": engine_name,
                "score": 0.1,
            }
        ][:n]

    return _search
