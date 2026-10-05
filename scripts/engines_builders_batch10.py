#!/usr/bin/env python3
"""批次十构建器：zhihu_global 补强。

为什么不在 engines_builders_cn.py 里加（任务原指定位置）：该文件被
test_module_size_gate.py 祖父清单冻结在 2319 行（当前恰好 2319，只能减不能增），
且 tests/ 门禁只放行新建测试文件——门禁自身文档写明「想加功能，正解是拆模块，
不是把数字往上调」，故按 batch9 命名先例落新文件（新增文件天然合规）。

zhihu_global 说明：cn 里的旧实现与本文件同名实现并存，engines.py 注册表已
指向本文件版本（候选池取满 + 错误文案行动提示两处补强）；cn 旧版仅在
engines_builders 聚合层保留转出，属被取代代码。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from engine_env import get_env

from engines_base import (
    http_open,
    rank_score,
    safe_search,
)
from engines_builders_cn import _parse_site_filter


class _LazyLogger:
    """延迟创建 logger，避免 import logging 的重量级 import 链（traceback→dataclasses→inspect）。"""
    _logger = None

    def __getattr__(self, name: str):
        if _LazyLogger._logger is None:
            import logging
            _LazyLogger._logger = logging.getLogger("unified_search.engines")
            if not _LazyLogger._logger.handlers:
                _LazyLogger._logger.setLevel(logging.WARNING)
                _LazyLogger._logger.addHandler(logging.StreamHandler(sys.stderr))
        return getattr(_LazyLogger._logger, name)


logger = _LazyLogger()


# ── zhihu_global 补强版（取代 engines_builders_cn 同名实现）───────────────────

# 端点 Count 上限：官方 global_search 上限 20（cn 旧版 min(n, 20) 同源口径）
_ZHIHU_COUNT_MAX = 20

# 非成功响应 → 可行动提示。时钟偏差类按 Message 文案识别（错误码表未公开，
# 不猜码值）：签名带 X-Request-Timestamp，本机时钟漂移会让服务端判签名过期，
# 现象是「密钥没错却全挂」——提示必须指向校时，防止用户误判 key 失效反复换 key
_ZHIHU_CLOCK_HINT_RE = re.compile(r"timestamp|时间戳|签名|时钟|clock", re.I)
_ZHIHU_CLOCK_HINT = "本机时钟可能偏差导致签名过期：先校准系统时间再重试，不必反复换密钥"


def _zhihu_error_item(code: Any, message: Any) -> dict[str, Any]:
    """非成功响应的 error item：保留原始 code/message，时钟类追加行动提示。"""
    text = f"zhihu_global Code={code} {str(message or '')[:100]}"
    if _ZHIHU_CLOCK_HINT_RE.search(str(message or "")):
        text = f"{text}；hint: {_ZHIHU_CLOCK_HINT}"
    return {"error": text, "source": "zhihu_global"}


def _build_zhihu_global_engine(spec: dict[str, Any]) -> Any:
    """知乎开放平台全网搜索（developer.zhihu.com global_search）。

    在 cn 旧版语义之上的两处微机制补强（2026-09-26）：
      1. 候选池取满再截断：Filter host== 是服务端在候选池上后筛，只取 n 条
         会把「过滤后恰好命中」的候选一并筛掉，空结果会被模型读成「知乎没有
         相关内容」；带站点限定或时间下限（since）时取满端点 Count 上限，
         客户端再截断到 n。since 的时间 Filter 语法官方未文档化，先只承担
         「取满候选池」语义。
      2. 错误码 → 行动提示：时钟偏差类错误给「校时」提示而非裸错误。
    查询语法与 cn 版一致：site:/host: → Filter host==，普通查询 SearchDB=all。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []

        # 解析 site:/host: 站点限定语法 → Filter: host=="..."（实现单点在 cn）
        filter_expr, search_query = _parse_site_filter(query)

        filtered = bool(filter_expr or kwargs.get("since"))
        params: dict[str, Any] = {
            "Query": search_query or query,
            "Count": str(_ZHIHU_COUNT_MAX if filtered else min(int(n or 5), _ZHIHU_COUNT_MAX)),
            "SearchDB": "all",
        }
        if filter_expr:
            params["Filter"] = filter_expr
        url = "https://developer.zhihu.com/api/v1/content/global_search?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
            "User-Agent": "argo-search/2.6 (unified-search@local)",
        }
        try:
            with http_open(urllib.request.Request(url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 401/403 等必须暴露为 error item 而非静默空——调用侧把
            # 「没配置」「鉴权失败」「没结果」区分开才可行动
            return [{"error": f"zhihu_global API HTTP {e.code}", "source": "zhihu_global"}]
        except Exception as e:
            logger.warning(f"zhihu_global 失败: {e}")
            return [{"error": f"zhihu_global {type(e).__name__}: {e}", "source": "zhihu_global"}]
        if data.get("Code") not in (0, None):
            # 30001=频率限制 30002=配额限制，显式暴露供配额状态机归类
            logger.warning(f"zhihu_global 返回码异常: {data.get('Code')} {data.get('Message')}")
            return [_zhihu_error_item(data.get("Code"), data.get("Message"))]
        items = (data.get("Data") or {}).get("Items") or []
        results = []
        for _rk3, item in enumerate(items[:n]):
            if not isinstance(item, dict):
                continue
            title = (item.get("Title") or "").strip()
            # API 标题统一带「 - 知乎」尾巴：截断前剥掉（先剥再切，尾巴不占正文）
            if title.endswith(" - 知乎"):
                title = title[: -len(" - 知乎")].rstrip()
            url_ = item.get("Url") or ""
            snippet = item.get("ContentText") or ""
            # 去 <em> 高亮标签
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            if not title and not url_:
                continue
            # 结构化信号：权威等级 / 互动 / 时效
            social_meta = {
                "author": item.get("AuthorName") or "",
                "content_type": item.get("ContentType") or "",
                "vote_up": item.get("VoteUpCount") or 0,
                "comment_count": item.get("CommentCount") or 0,
                "authority_level": item.get("AuthorityLevel") or "",
                "edit_time": item.get("EditTime") or 0,
            }
            results.append({
                "title": title[:200],
                "url": url_,
                "snippet": snippet[:300],
                "source": "zhihu_global",
                "score": rank_score(0.7, _rk3),
                "authority_level": social_meta["authority_level"],
                "social_meta": social_meta,
            })
        return results
    return _engine
