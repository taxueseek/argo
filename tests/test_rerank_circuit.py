#!/usr/bin/env python3
"""语义精排端点的熔断测试（全离线，mock 出口，无需真实 API key）。

## 为什么需要它

`rerank_results` 是一次**同步阻塞**的网络调用，压在 CPU 后处理链上。实测账户
余额不足时（403 `{"code":"403","message":"You do not have enough money"}`）每次
搜索都真发一次请求、真等一次 RTT——223 ms，占 balanced 档墙钟的 63%、占全部
后处理耗时的 89%。旧实现把 HTTPError 一律吞成 `"fallback"`，既不计数也不冷却，
于是**每次搜索都重犯同一笔开销**，而输出里只有一句 `reranker: fallback`，
看不出端点其实已经死了。

修法是复用引擎熔断器（`rerank:bocha` 键）的同一套语义：失败计数 → 冷却 →
半开探测 → 自动禁用后周期复探，状态写入文件，因此后续 CLI 单发进程也直接跳过。

## 测试隔离（重要）

**不碰全局单例**。`circuit_breaker._breaker` 是全进程共享的，同一次 pytest
会话里别的用例可以把它换掉；实测这会让「行为正确但断言拿到另一个实例」——
断言的失败与实现无关，只说明测试自己不干净。这里每个用例用**自己的**
`CircuitBreaker(state_path=tmp)` 实例，靠 `patch.object(search, "_rerank_breaker", ...)`
注入。被测的仍然是 `rerank_results` 里的真实分支。

## 覆盖

  1. 连续失败打开熔断，之后的调用**不再触碰网络**（这是省下 223 ms 的那一步）；
  2. 熔断态写入文件，跨进程可见（CLI 是一次性进程，进程内记忆没有意义）；
  3. 冷却期结束后半开探测，成功则闭合（人工充值后不该被历史失败永久拦住）；
  4. 未配置密钥时走 `skipped_no_key`，不污染熔断状态；
  5. `skipped_circuit_open` 必须落在本地五维保底的状态全集里——否则会退化成
     「既不精排也不保底」，最终顺序悄悄变成 RRF 原始序。

运行：
  python3 -m pytest tests/test_rerank_circuit.py -v
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import circuit_breaker  # noqa: E402
import search  # noqa: E402
import search_rank  # noqa: E402


@pytest.fixture()
def breaker(tmp_path):
    """本用例私有的熔断器实例，并把它接到精排路径上。

    打桩打在 search_rank（`rerank_results` 的家）：search 只是同名转出，
    改转出副本不会影响实现读到的值。
    """
    b = circuit_breaker.CircuitBreaker(state_path=str(tmp_path / "cb.json"))
    with patch.object(search_rank, "_rerank_breaker", lambda: b):
        yield b


def _docs(n: int = 12) -> list[dict]:
    return [{"title": f"t{i} 新能源汽车 出口", "snippet": "数据 " * 20,
             "url": f"https://e{i}.com/a/{i}", "score": 0.5} for i in range(n)]


def _http_403():
    return urllib.error.HTTPError(
        "https://api.bocha.cn/v1/rerank", 403, "Forbidden", {}, None)


class _OkResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def with_key():
    with patch.dict("os.environ", {"ARGO_BOCHA_API_KEY": "k"}):
        yield


def test_repeated_failure_opens_breaker_and_stops_calling_network(breaker, with_key):
    """连续失败后必须停止打网——这是省下 223 ms 的那一步。"""
    calls = []

    def _fail(req, timeout=None):
        calls.append(req)
        raise _http_403()

    with patch("net_proxy.open_url", _fail):
        statuses = [search.rerank_results("q", _docs())[1] for _ in range(5)]

    # 前两次真打网（FAILURE_THRESHOLD=2），此后被熔断拦住
    assert statuses[:2] == ["fallback", "fallback"], statuses
    assert statuses[2:] == ["skipped_circuit_open"] * 3, statuses
    assert len(calls) == 2, f"熔断后仍在打网：{len(calls)} 次"
    assert breaker.status(search_rank._RERANK_BREAKER_KEY)["state"] == "open"


def test_breaker_state_is_persisted_for_next_process(breaker, with_key):
    """熔断态必须写入文件：CLI 是一次性进程，进程内记忆没有意义。"""
    def _fail(req, timeout=None):
        raise _http_403()

    with patch("net_proxy.open_url", _fail):
        search.rerank_results("q", _docs())
        search.rerank_results("q", _docs())

    on_disk = json.loads(Path(breaker._path).read_text(encoding="utf-8"))
    entry = on_disk["engines"].get(search_rank._RERANK_BREAKER_KEY)
    assert entry, "rerank 熔断态未落盘，下个 CLI 进程会重犯同一笔开销"
    assert entry["state"] == "open"
    assert entry["last_attribution"]["category"] == "auth", entry["last_attribution"]


def test_success_closes_breaker(breaker, with_key):
    """人工充值后不该被历史失败永久拦住：可解析响应即闭合熔断。"""
    def _fail(req, timeout=None):
        raise _http_403()

    with patch("net_proxy.open_url", _fail):
        search.rerank_results("q", _docs())
        search.rerank_results("q", _docs())
    assert breaker.status(search_rank._RERANK_BREAKER_KEY)["state"] == "open"

    ok = {"data": {"results": [{"index": 0, "relevance_score": 0.9}]}}
    with patch("net_proxy.open_url", lambda req, timeout=None: _OkResp(ok)):
        # 冷却期未过时仍被拦（allow 返回 False）——这是设计行为
        assert search.rerank_results("q", _docs())[1] == "skipped_circuit_open"

    # 把 opened_at 拨回冷却期之前，走**真实的**半开探测路径（不等 60s，
    # 也不用 reenable 抄近路——那会把要验证的状态机整段跳过）。
    with breaker._lock:
        st = breaker._engines[search_rank._RERANK_BREAKER_KEY]
        st["opened_at"] = time.time() - circuit_breaker.OPEN_SECONDS - 1
        breaker._engines[search_rank._RERANK_BREAKER_KEY] = st
        breaker._save()

    with patch("net_proxy.open_url", lambda req, timeout=None: _OkResp(ok)):
        out, status = search.rerank_results("q", _docs())
        assert status == "ok", "半开探测应被放行并拿到可解析响应"
    assert breaker.status(search_rank._RERANK_BREAKER_KEY)["state"] == "closed"


def test_missing_key_does_not_touch_breaker(breaker):
    """未配置密钥是配置态，不是端点故障——不得写进熔断状态。

    直接 patch `search_rank.get_env` 而不是清 os.environ：get_env 还有「读
    ~/.config/argo/env」这一层回落，只清环境变量挡不住真实密钥文件。
    """
    def _no_key(names, default=""):
        return default

    with patch.object(search_rank, "get_env", _no_key), \
            patch("net_proxy.open_url",
                  lambda req, timeout=None: pytest.fail("无密钥时不得发起请求")):
        _, status = search.rerank_results("q", _docs())
    assert status == "skipped_no_key"
    assert breaker.status(search_rank._RERANK_BREAKER_KEY)["state"] == "closed"


def test_circuit_open_status_triggers_local_fallback():
    """熔断状态必须落在保底全集里，否则退化成「既不精排也不保底」。"""
    assert "skipped_circuit_open" in search._RERANK_DEGRADED_STATUSES
    assert "ok" not in search._RERANK_DEGRADED_STATUSES, \
        "成功状态不得进入兜底全集，否则会重复重排"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
