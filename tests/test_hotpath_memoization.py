#!/usr/bin/env python3
"""test_hotpath_memoization.py — 热路径重复劳动的消除检查（2026-09-15）。

## 守的是什么

一次真实搜索的 cProfile 显示两处**同一份计算被反复做**，都是纯函数、
都只差一层记忆化：

1. `cache._signature`：`query_similarity(q1, q2)` 要和缓存里多条候选比对，
   旧写法每次都把两边的 n-gram 集合全量重哈希——一次搜索实测 28,552 次
   `_hash_token` 调用，绝大多数是同一批 token 的重复置换。签名化 + 记忆化
   后同一查询全进程只算一次。**等价性必须逐位保持**（这是缓存命中判据）。
2. `config.config_stamp()`：取值要 stat 全部 63 个外置声明文件，而
   `engines.get_registry()` 每访问一次注册表就调它一次——一次搜索触发 28 次
   = 1764 次 stat。按 TTL 折叠（默认 1 s，可用 `ARGO_CONFIG_STAMP_TTL_S`
   关闭/调节），热加载语义不要求亚秒级感知。

本文件锁：等价性、记忆化生效、TTL 可关闭、非法配置容错。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cache  # noqa: E402
import config  # noqa: E402
import config_cache  # noqa: E402
import route  # noqa: E402


def _reference_similarity(q1: str, q2: str) -> float:
    """改动前的实现，作为等价性基准（原样复刻，勿按新实现改写）。"""
    a = cache._ngrams(q1)
    b = cache._ngrams(q2)
    if not a or not b:
        return 0.0
    hits = 0
    for seed in range(cache._MINHASH_PERM):
        if (min(cache._hash_token(t, seed) for t in a)
                == min(cache._hash_token(t, seed) for t in b)):
            hits += 1
    return hits / cache._MINHASH_PERM


_QUERIES = [
    "苹果 2025 营收", "苹果 2025 年营收", "苹果2025营收",
    "2026 中国新能源汽车出口数据", "2026 中国新能源汽车 出口 数据",
    "特斯拉 财报", "", " ", "a", "中", "苹果 2025 营收 ",
    "Bernanke 2005 savings glut", "savings glut Bernanke 2005",
]


class TestQuerySimilarityEquivalence:
    """签名化不得改变任何一对输入的相似度值——它是缓存软命中的判据。"""

    @pytest.mark.parametrize("q1", _QUERIES)
    def test_matches_reference_implementation(self, q1):
        for q2 in _QUERIES:
            new = cache.query_similarity(q1, q2)
            old = _reference_similarity(q1, q2)
            assert abs(new - old) < 1e-12, (
                f"签名化改变了相似度：{q1!r} vs {q2!r} → {new} ≠ {old}")

    def test_near_duplicate_still_high(self):
        """近重复查询仍要判为高相似（语义缓存软命中的业务前提）。"""
        assert cache.query_similarity("苹果 2025 营收", "苹果 2025 年营收") > 0.7

    def test_empty_side_is_zero(self):
        assert cache.query_similarity("", "苹果 2025 营收") == 0.0
        assert cache.query_similarity("苹果", "") == 0.0


class TestSignatureIsMemoized:
    def test_repeated_comparison_hits_cache(self):
        cache._signature.cache_clear()
        for _ in range(50):
            cache.query_similarity("2026 中国新能源汽车出口数据",
                                   "2026 中国新能源汽车 出口 数据")
        info = cache._signature.cache_info()
        # 两条查询各算一次，其余全命中——这正是旧实现被重复消耗的部分
        assert info.misses == 2, f"签名重算了（misses={info.misses}）"
        assert info.hits >= 98, f"命中数不足（hits={info.hits}）"

    def test_signature_is_pure(self):
        cache._signature.cache_clear()
        assert cache._signature("苹果 2025 营收") == cache._signature("苹果 2025 营收")

    def test_signature_length_is_perm_count(self):
        assert len(cache._signature("苹果 2025 营收")) == cache._MINHASH_PERM


class TestExternalEnginesMtimeTtl:
    """外置声明指纹的 TTL 记忆化：命中不扫盘、过期重扫、TTL=0 可关。"""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(config, "_ext_scan_cache", None)
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "1.0")
        yield
        config._ext_scan_cache = None

    def _count_scans(self, monkeypatch):
        n = [0]
        real = config._external_engines_scan_uncached

        def counting():
            n[0] += 1
            return real()

        monkeypatch.setattr(config, "_external_engines_scan_uncached", counting)
        return n

    def test_repeated_calls_scan_once(self, monkeypatch):
        n = self._count_scans(monkeypatch)
        vals = [config._external_engines_mtime() for _ in range(20)]
        assert n[0] == 1, f"TTL 内扫盘 {n[0]} 次（应 1 次）"
        assert len(set(vals)) == 1, "TTL 内返回值不一致"

    def test_expiry_rescans(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0.05")
        n = self._count_scans(monkeypatch)
        config._external_engines_mtime()
        config._external_engines_mtime()
        assert n[0] == 1, "TTL 内应复用"
        time.sleep(0.08)
        config._external_engines_mtime()
        assert n[0] == 2, "过期后应重新扫盘"

    def test_ttl_zero_disables_memoization(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0")
        n = self._count_scans(monkeypatch)
        for _ in range(4):
            config._external_engines_mtime()
        assert n[0] == 4, "TTL=0 应关闭记忆化"

    def test_scan_fingerprint_sees_deletion_and_backdated_add(self, monkeypatch, tmp_path):
        """指纹必须覆盖「集合变化」，只有 max_mtime 时对删除与 cp -p 是盲的。

        这是写入文件配置缓存的黑洞：删掉一个不是最新的声明、或拷进来一个时间戳
        被保留成旧值的声明，max_mtime 都不变 → 缓存继续命中 → 「引擎删了还在 /
        加了不生效」。
        """
        d = tmp_path / "engines"
        d.mkdir()
        (d / "a.yaml").write_text("a: 1\n", encoding="utf-8")
        (d / "b.yaml").write_text("b: 1\n", encoding="utf-8")
        monkeypatch.setattr(config, "ENGINES_DIR", d)
        before = config._external_engines_scan_uncached()
        assert before[1] == 2, "文件数没被统计"

        (d / "a.yaml").unlink()
        after_del = config._external_engines_scan_uncached()
        assert after_del != before, "删除声明后指纹未变——缓存不会失效"
        # 模拟 cp -p：新增文件的 mtime 比现有的旧
        (d / "c.yaml").write_text("c: 1\n", encoding="utf-8")
        os.utime(d / "c.yaml", (before[0] - 1000, before[0] - 1000))
        after_add = config._external_engines_scan_uncached()
        assert after_add != after_del, "回填旧 mtime 的新增声明未被指纹察觉"
        assert after_add[0] <= after_del[0], "本用例前提：新文件不比旧的更新"

    def test_force_refresh_backfills_memo(self, monkeypatch):
        """force=True 必须重扫，并把真实值写回记忆——否则紧接着的调用又白扫一遍。

        此前 force 只绕过 TTL、不写回，且不碰 _stamp_cache：重读之后 1 秒内
        get_registry() 仍按旧 stamp 判断要不要重建，重读与快照脱节。
        """
        monkeypatch.setattr(config, "_stamp_cache", None)
        monkeypatch.setattr(config, "_config_cache", None)
        n = self._count_scans(monkeypatch)
        config.load_config()
        before = n[0]
        config.load_config(force=True)
        assert n[0] == before + 1, "force 必须重扫一次外置声明（不得吃 TTL 记忆）"
        config.load_config()
        assert n[0] == before + 1, "force 后未回填记忆——下一次非 force 调用又白扫一遍"
        assert config._stamp_cache is not None, (
            "force 未刷新 config_stamp 记忆：1 秒内 registry 会按旧 stamp 判断")


class TestConfigStampTtl:
    """config_stamp 按 TTL 折叠重复扫盘；语义与可关闭性都要站得住。"""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(config, "_stamp_cache", None)
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "1.0")
        yield
        config._stamp_cache = None

    def _count_scans(self, monkeypatch):
        n = [0]
        real = config._external_engines_mtime

        def counting():
            n[0] += 1
            return real()

        monkeypatch.setattr(config, "_external_engines_mtime", counting)
        return n

    def test_repeated_calls_scan_once(self, monkeypatch):
        n = self._count_scans(monkeypatch)
        vals = [config.config_stamp() for _ in range(28)]
        assert n[0] == 1, f"TTL 内扫盘 {n[0]} 次（应 1 次）——get_registry 每次访问都会调它"
        assert len(set(vals)) == 1, "TTL 内返回值不一致"

    def test_ttl_zero_disables_memoization(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0")
        n = self._count_scans(monkeypatch)
        for _ in range(5):
            config.config_stamp()
        assert n[0] == 5, "TTL=0 应关闭记忆化（热加载即时感知的逃生门）"

    def test_expiry_rescans(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0.05")
        n = self._count_scans(monkeypatch)
        config.config_stamp()
        config.config_stamp()
        assert n[0] == 1, "TTL 内应复用"
        time.sleep(0.08)
        config.config_stamp()
        assert n[0] == 2, "过期后应重新取值"

    @pytest.mark.parametrize("bad", ["abc", "", "None"])
    def test_invalid_ttl_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", bad)
        assert config._stamp_ttl() == 1.0

    def test_stamp_value_is_still_mtime_max(self, monkeypatch):
        """折叠的是调用次数，不是取值——TTL 后取到的必须还是真实 mtime 最大值。"""
        config._stamp_cache = None
        got = config.config_stamp()
        expected = 0.0
        try:
            expected = config.CONFIG_PATH.stat().st_mtime
        except OSError:
            pass
        assert got >= expected


def _use_config(monkeypatch, path):
    """把「配置文件的身份」同时告诉两层。

    磁盘缓存拆出 config_cache 后，路径有两个读取处：config 的加载器
    （`config.CONFIG_PATH`）与缓存层（`config_cache._config_path()`）。测试要把
    配置指向临时文件，就得两处都打——只打一处，另一半继续读真实 config.yaml
    （2026-09-21 实测：只打 config.CONFIG_PATH 时缓存层静默读真实文件）。
    """
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config_cache, "_config_path", lambda: path)


class TestConfigDiskCache:
    """写入文件配置缓存：省掉**每个新进程**的整条解析合并链（实测 57 ms → 11 ms）。

    每条命令都是一次新进程，进程内记忆化救不了跨进程的重复，只有写入文件缓存能省。
    这里锁四件事：暖进程不再解析、配置变了必失效、force 不吃缓存、损坏缓存可
    自愈——外加「缓存结果与完整解析结果逐字段一致」这条等价性底线。
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.delenv("ARGO_CONFIG_CACHE", raising=False)
        self._reset_memo()
        yield
        self._reset_memo()

    def _reset_memo(self):
        config._config_cache = None
        config._config_mtime = 0.0
        config._parsed_yaml_cache = None
        config._disk_cache_memo = None
        config_cache._content_digest_memo = None
        # _ext_scan_cache 也要清：测试会 monkeypatch ENGINES_DIR/CONFIG_PATH，留着
        # 上一个夹具的外置声明指纹会让「本该不匹配」的缓存键恰好匹配，于是测试
        # 读到真实仓库的配置——缓存类用例最常见的假绿/假红来源。
        config._ext_scan_cache = None

    def _count_parses(self, monkeypatch):
        """计「真解析」次数：记忆化命中时入口仍会被调一次，只有真解析才走
        _load_yaml。"""
        n = [0]
        real = config._load_yaml

        def counting(text):
            n[0] += 1
            return real(text)

        monkeypatch.setattr(config, "_load_yaml", counting)
        return n

    def test_warm_process_skips_full_parse(self, monkeypatch):
        # 先走一次完整链，把缓存写出来
        config.load_config()
        assert config._config_disk_cache_path().exists(), "完整解析后应写出落盘缓存"
        self._reset_memo()          # 模拟「又一个新进程」
        n = self._count_parses(monkeypatch)
        cfg = config.load_config()
        assert n[0] == 0, f"暖进程仍解析了 {n[0]} 次 config.yaml——落盘缓存没生效"
        assert cfg.get("engines"), "缓存读回后配置不得为空"

    def test_cached_result_matches_full_parse(self, monkeypatch):
        """等价性底线：缓存读回的配置必须与整条解析链的结果逐字段一致。"""
        config.load_config()        # 走缓存（或首次写入）
        cached = config.load_config()
        self._reset_memo()
        monkeypatch.setenv("ARGO_CONFIG_CACHE", "0")   # 关缓存 → 强制完整解析
        full = config.load_config()
        assert (json.dumps(cached, sort_keys=True, ensure_ascii=False)
                == json.dumps(full, sort_keys=True, ensure_ascii=False)), \
            "落盘缓存与完整解析结果不一致——缓存改变了配置语义"

    def test_config_change_invalidates(self, monkeypatch, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("cache:\n  db_path: /tmp/a/x.db\nengines: {}\n",
                     encoding="utf-8")
        _use_config(monkeypatch, p)
        monkeypatch.setattr(config, "ENGINES_DIR", tmp_path / "no-engines")
        first = config.load_config()
        assert "/tmp/a/x.db" in json.dumps(first)
        p.write_text("cache:\n  db_path: /tmp/bb/longer.db\nengines: {}\n",
                     encoding="utf-8")
        second = config.load_config()
        assert "/tmp/bb/longer.db" in json.dumps(second), \
            "配置改写后仍返回旧值——缓存键没覆盖 mtime/size"

    def test_force_bypasses_disk_cache(self, monkeypatch):
        config.load_config()
        self._reset_memo()
        n = self._count_parses(monkeypatch)
        config.load_config(force=True)
        assert n[0] == 1, "force 必须走完整解析链，不得命中落盘缓存"

    def test_env_switch_disables_cache(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_CACHE", "0")
        n = self._count_parses(monkeypatch)
        config.load_config()
        config.load_config()
        assert n[0] == 1, "ARGO_CONFIG_CACHE=0 时应回到进程内记忆化的老口径"

    def test_corrupt_cache_recovers(self, monkeypatch):
        config.load_config()
        path = config._config_disk_cache_path()
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self._reset_memo()
        cfg = config.load_config()
        assert cfg.get("engines"), "缓存损坏后应自愈（退回解析链），不得把配置读空"

    def test_non_string_keys_are_not_cached(self):
        """YAML 允许非字符串键，JSON 会静默写坏——这类配置整份不缓存。"""
        assert config._json_round_trip_safe({"a": {"b": [1, 2]}})
        assert not config._json_round_trip_safe({"a": {1: "x"}})
        assert not config._json_round_trip_safe({"a": [{"深层": {2: "y"}}]})

    def test_windows_semantics_do_not_weaken_invalidation(self, monkeypatch, tmp_path):
        """内容摘要必须是唯一强判据：stat 字段全被还原也要能失效。

        这是 Windows 短板的根因回归：`touch -r`/`cp -p` 能还原 mtime，而 Windows
        上 st_ctime 是**创建时间**（改写内容后根本不变），于是「用 ctime 兜住还原
        mtime」这条设计在 Windows 上等于没有——同一份代码两个平台失效保真度不同，
        表现是「改了配置不生效」。这里用**同一个 stat 对象 + 两个摘要**直接锁死
        「键只认内容」，与平台无关。
        """
        st = config.CONFIG_PATH.stat()
        scan = (0.0, 0, 0, "ext-digest")
        k_old = config._config_disk_cache_key(st, scan, "digest-old")
        k_new = config._config_disk_cache_key(st, scan, "digest-new")
        assert k_old != k_new, "键对内容不敏感——stat 被还原时会静默沿用旧配置"
        assert "config_digest" in k_old, "键里应有内容摘要字段"
        assert not {"config_ctime_ns", "config_mtime_ns"} & set(k_old), \
            "键不该再依赖 ctime/mtime（ctime 在 Windows 是创建时间，跨平台语义不一致）"

    def test_content_digest_detects_same_size_rewrite(self, monkeypatch, tmp_path):
        """等长改写（编辑器/脚本都可能）必须改变摘要——不靠 size 或 mtime。"""
        p = tmp_path / "c.yaml"
        p.write_text("a: 1111\n", encoding="utf-8")
        # 打桩打在**读取处**：摘要读的是 config_cache._config_path()（config 只是
        # 同源转出）。打 config.CONFIG_PATH 依赖「两个模块对象是同一个」，
        # 而 sys.modules 被别的测试 pop/重导入后会静默失效。
        _use_config(monkeypatch, p)
        config_cache._content_digest_memo = None
        first = config._config_content_digest(p.stat())
        p.write_text("a: 2222\n", encoding="utf-8")      # 同上长度
        config_cache._content_digest_memo = None
        second = config._config_content_digest(p.stat())
        assert first and second and first != second, "等长改写没被摘要识别"

    def test_missing_digest_disables_cache(self, monkeypatch):
        """摘要取不到时不写也不读缓存：宁可每次解析，也不用来源不明的摘要命中。"""
        # 打桩打在**读取处**（摘要与缓存 IO 都住在 config_cache）
        monkeypatch.setattr(config_cache, "_config_content_digest", lambda st: None)
        assert config._load_config_disk_cache(
            config.CONFIG_PATH.stat(), (0.0, 0, 0, "x"), None) is None
        config._save_config_disk_cache(
            config.CONFIG_PATH.stat(), (0.0, 0, 0, "x"), None, {"engines": {}})
        # 不写缓存：解析链照样工作，只是每次全量
        assert config.load_config().get("engines")

    def test_disk_path_is_bootstrap_root(self, monkeypatch, tmp_path):
        """缓存必须落在**不依赖配置解析**就能算出的位置，且按 config 路径分槽。

        放进状态目录（state_root 要靠 config.yaml 的 db_path 才能算出）会形成
        「为了找缓存先解析配置、而缓存的意义正是省掉这次解析」的自锁；不分槽则
        同机多份 argo 共用引导根时互相顶掉缓存（每次调用都退化成全量解析）。
        """
        monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
        p = config._config_disk_cache_path()
        assert p.parent == tmp_path, "落盘缓存没放在引导根"
        assert p.name.startswith("config-cache-") and p.suffix == ".json"

    def test_peek_ignores_stale_cache_after_config_change(self, monkeypatch, tmp_path):
        """db_path 是状态目录的源头：缓存里的值必须与 config.yaml 同版本。

        回归审计发现：peek 只比 config_path 就返回，改过 cache.db_path 之后
        peek 拿到旧路径、load_config 拿到新路径，长驻进程整个生命周期都把状态
        文件写到旧目录。
        """
        p = tmp_path / "c.yaml"
        p.write_text("cache:\n  db_path: /tmp/one/x.db\nengines: {}\n", encoding="utf-8")
        _use_config(monkeypatch, p)
        monkeypatch.setattr(config, "ENGINES_DIR", tmp_path / "no-engines")
        assert config.peek_cache_db_path() == "/tmp/one/x.db"
        config.load_config()
        self._reset_memo()
        p.write_text("cache:\n  db_path: /tmp/two/longer-name.db\nengines: {}\n",
                     encoding="utf-8")
        assert config.peek_cache_db_path() == "/tmp/two/longer-name.db", \
            "peek 返回了上一个版本的 db_path——状态目录会错位"

    def test_deleted_external_spec_invalidates_cache(self, monkeypatch, tmp_path):
        """删掉一个不是最新的外置声明，缓存必须失效（max_mtime 对此是盲的）。"""
        eng = tmp_path / "engines"
        eng.mkdir()
        (eng / "alpha.yaml").write_text(
            "engine_id: alpha\nlabel: Alpha\ntype: http\nurl: https://a.invalid\n",
            encoding="utf-8")
        (eng / "beta.yaml").write_text(
            "engine_id: beta\nlabel: Beta\ntype: http\nurl: https://b.invalid\n",
            encoding="utf-8")
        monkeypatch.setattr(config, "ENGINES_DIR", eng)
        cfg1 = config.load_config()
        assert "alpha" in cfg1["engines"] and "beta" in cfg1["engines"]
        self._reset_memo()
        # 删掉一个「不是最新」的声明：max_mtime 不变，只有文件数变了
        (eng / "alpha.yaml").unlink()
        os.utime(eng / "beta.yaml", None)   # 保证 beta 仍是最新的那个
        cfg2 = config.load_config()
        assert "alpha" not in cfg2["engines"], \
            "删除声明后仍从缓存读出已删除的引擎（指纹漏了文件数/字节）"
        assert "beta" in cfg2["engines"]


class TestEngineNamesCacheInvalidation:
    """引擎显示名表按 config_stamp 热重建——长驻进程里新引擎不再要重启才可见。"""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(route, "_ENGINE_NAMES_CACHE", None)
        monkeypatch.setattr(route, "_ENGINE_NAMES_STAMP", None)
        yield
        route._ENGINE_NAMES_CACHE = None
        route._ENGINE_NAMES_STAMP = None

    def test_rebuilds_when_stamp_changes(self, monkeypatch):
        calls = [0]
        real = route._build_engine_names

        def counting():
            calls[0] += 1
            return real()

        monkeypatch.setattr(route, "_build_engine_names", counting)
        stamps = iter([100.0, 100.0, 200.0])
        monkeypatch.setattr(route, "config_stamp", lambda: next(stamps, 200.0))
        route._engine_names_map()
        route._engine_names_map()
        assert calls[0] == 1, "stamp 未变时应复用（热路径不得每次重建）"
        route._engine_names_map()
        assert calls[0] == 2, "配置变了（stamp 变化）必须重建，否则新引擎要重启才可见"

    def test_broken_stamp_does_not_thrash(self, monkeypatch):
        """取 stamp 失败时退化为旧行为（建一次不失效），不得每次重建。"""
        calls = [0]
        real = route._build_engine_names

        def counting():
            calls[0] += 1
            return real()

        def boom():
            raise RuntimeError("config 不可用")

        monkeypatch.setattr(route, "_build_engine_names", counting)
        monkeypatch.setattr(route, "config_stamp", boom)
        route._engine_names_map()
        route._engine_names_map()
        assert calls[0] == 1, "stamp 取不到时不应每次调用都重建显示名表"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
