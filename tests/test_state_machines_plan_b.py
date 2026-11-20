"""Plan B 状态机收敛与热路径防退步回归测试。

每条用例对应一处已修复缺陷：回退旧实现时对应断言必红。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_config_cache_symlink_key_normalized(tmp_path, monkeypatch):
    """symlink 与真实路径必须命中同一个 _config_disk_cache_key（零乒乓 miss）。"""
    import config_cache

    real_cfg = tmp_path / "real_config.yaml"
    real_cfg.write_text("engines: {}\n", encoding="utf-8")
    sym_cfg = tmp_path / "sym_config.yaml"
    sym_cfg.symlink_to(real_cfg)

    st = real_cfg.stat()
    scan = (0.0, 0, 0, "ext0")

    monkeypatch.setattr(config_cache, "_config_path", lambda: real_cfg)
    k_real = config_cache._config_disk_cache_key(st, scan, "dig1")

    monkeypatch.setattr(config_cache, "_config_path", lambda: sym_cfg)
    k_sym = config_cache._config_disk_cache_key(st, scan, "dig1")

    assert k_real["config_path"] == k_sym["config_path"] == str(real_cfg.resolve())


def test_circuit_breaker_open_to_half_open_sets_probe_at_and_persists_ever_nonempty(tmp_path, monkeypatch):
    """OPEN→HALF_OPEN 必须落盘 half_open_probe_at 防双探测；首次 record_success 必须落盘 ever_nonempty。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    import circuit_breaker
    import engine_states

    cb = circuit_breaker.CircuitBreaker()
    # 1. 首次成功落盘 ever_nonempty
    cb.record_success("fresh_ok_eng")
    raw = json.loads((tmp_path / "circuit_breaker.json").read_text(encoding="utf-8"))
    assert raw["engines"]["fresh_ok_eng"]["ever_nonempty"] is True

    # 2. OPEN 过期转 HALF_OPEN：首次放行并写入 half_open_probe_at，紧随第二次被拦
    open_entry = {
        "state": engine_states.BREAKER_OPEN,
        "failures": 3,
        "opens": 1,
        "opened_at": time.time() - circuit_breaker.OPEN_SECONDS - 5,
    }
    cb._mutate_locked(lambda: cb._engines.__setitem__("flaky_eng", open_entry))

    ok1, reason1 = cb.allow("flaky_eng")
    assert ok1 is True and reason1 == "half_open_probe"
    ok2, reason2 = cb.allow("flaky_eng")
    assert ok2 is False and reason2 == "half_open_probe_busy"


def test_circuit_breaker_neg_cache_bounded(tmp_path, monkeypatch):
    """负缓存超过阈值时自动清理过期项并控制上限，防常驻进程内存泄漏。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    import circuit_breaker

    cb = circuit_breaker.CircuitBreaker()
    for i in range(1050):
        cb.set_negative(f"q{i}", "eng", ttl=-1)
    cb.set_negative("fresh", "eng", ttl=60)
    assert len(cb._neg) <= 60


def test_quota_self_write_does_not_trigger_redundant_reload_and_float_budget(tmp_path, monkeypatch):
    """quota 自写后刷新 _state_hot，不触发下一次访问冗余重读；浮点预算边界与 is_over_budget 一致。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    import quota

    qm = quota.QuotaManager()
    load_calls = [0]
    orig_load = qm._load_state

    def counted_load():
        load_calls[0] += 1
        return orig_load()

    monkeypatch.setattr(qm, "_load_state", counted_load)
    qm.record("exa")
    after_record = load_calls[0]
    qm.is_available("exa")
    assert load_calls[0] == after_record, "自写后 is_available 不应触发冗余 _load_state"

    # 浮点边界：0.1 + 0.2 == 0.30000000000000004 vs 0.3
    qm._profiles["float_eng"] = {"budget_amount": 0.3, "cost_per_call": 0.1, "period": "monthly"}
    qm._state["float_eng"] = {"used": 3, "cost_used": 0.3 - 1e-12, "calls": [], "errors": 0, "last_reset": time.time()}
    assert qm.is_hard_down("float_eng") == qm.is_over_budget("float_eng")


def test_sqlite_cache_read_throttles_accessed_at_and_read_many_copies(tmp_path):
    """SQLiteCache.get 60s 内不重复写 accessed_at；_read_many 返回浅拷贝不污染 L1。"""
    import cache

    sc = cache.SearchCache(db_path=str(tmp_path / "c.db"))
    payload = {"results": [{"title": "a", "url": "https://example.com/a"}]}
    sc.set("hello", "exa", 5, payload)
    with sc._l2._conn() as conn:
        k = conn.execute("SELECT key FROM search_cache LIMIT 1").fetchone()[0]

    # 首次 _read_many 命中并篡改返回对象，不应污染 L1
    many1 = sc._read_many([k])
    many1[k]["results"][0]["title"] = "MUTATED"
    many2 = sc._read_many([k])
    assert many2[k]["results"][0]["title"] == "a"

    # 连续 L2 get 不应在 60s 内频繁更新 accessed_at
    with sc._l2._conn() as conn:
        t0 = conn.execute("SELECT accessed_at FROM search_cache WHERE key = ?", (k,)).fetchone()[0]
    time.sleep(0.02)
    assert sc._l2.get(k) is not None
    with sc._l2._conn() as conn:
        t1 = conn.execute("SELECT accessed_at FROM search_cache WHERE key = ?", (k,)).fetchone()[0]
    assert t1 == t0


def test_adaptive_batch_is_thread_local(tmp_path, monkeypatch):
    """线程 A 开启 batch() 期间，线程 B 的 record() 必须立即提交而非悬空。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    import adaptive

    monkeypatch.setattr(adaptive, "DB_DIR", tmp_path)
    monkeypatch.setattr(adaptive, "DB_PATH", tmp_path / "adaptive.db")
    learner = adaptive.AdaptiveLearner()

    entered = threading.Event()
    done_b = threading.Event()

    def thread_a():
        with learner.batch():
            entered.set()
            done_b.wait(timeout=5)

    t = threading.Thread(target=thread_a)
    t.start()
    entered.wait(timeout=5)
    # 线程 B 在线程 A batch 期间写入，应立即 commit，新连接立即可见
    learner.record("eng_b", success=True, latency_ms=100)
    done_b.set()
    t.join(timeout=5)

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "adaptive.db"))
    cnt = conn.execute("SELECT count(*) FROM engine_perf WHERE engine = 'eng_b'").fetchone()[0]
    conn.close()
    assert cnt == 1


def test_fail_notes_thread_local_isolation():
    """engines_base._FAIL_NOTES 线程隔离：子线程写入不得串味到主线程。"""
    import engines_base as eb

    eb._FAIL_NOTES.clear()
    eb.note_failure("shared_eng", "network", "main-err")

    other_seen = []

    def worker():
        other_seen.append(eb.peek_failure_note("shared_eng"))
        eb.note_failure("shared_eng", "auth", "worker-err")
        other_seen.append(eb.pop_failure_note("shared_eng")["reason"])

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)

    assert other_seen == [None, "worker-err"]
    main_note = eb.pop_failure_note("shared_eng")
    assert main_note is not None and main_note["reason"] == "main-err"
