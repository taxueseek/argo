#!/usr/bin/env python3
"""config.local.yaml 本机覆盖层 — 契约测试。

覆盖层要同时满足三件事，缺一不可：
  1. **本机生效**：临时停用引擎、本机路径、本机调优都能改得动；
  2. **不进公开仓**：config.local.yaml 在 .gitignore 里，示例文件进仓；
  3. **失效判据认它**：改完立即生效，磁盘缓存不返回旧结论。

为什么单独立一个文件：覆盖层是「本机状态与公共配置的唯一分界线」，
它坏了的表现是「本机改不动」或「本机状态被提交」——两者都不会被别的测试
顺带发现（前者是功能静默失效，后者要等 push 之后才暴露）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import config as C  # noqa: E402


@pytest.fixture
def local_cfg(tmp_path, monkeypatch):
    """把本机覆盖层指到临时文件；同时关掉磁盘缓存，避免测试污染真实缓存槽。"""
    p = tmp_path / "config.local.yaml"
    monkeypatch.setattr(C, "LOCAL_CONFIG_PATH", p)
    monkeypatch.setenv("ARGO_CONFIG_CACHE", "0")
    return p


def _reload() -> dict:
    return C.load_config(force=True)


# ── 1. 本机生效 ──────────────────────────────────────────────────────────────

def test_absent_local_file_is_noop(local_cfg):
    """没有覆盖层时，配置与历史行为一致（覆盖层是可选的，不是必填项）。"""
    assert not local_cfg.exists()
    assert _reload()["engines"]["parallel"]["enabled"] is True


def test_local_disable_engine_deep_merges(local_cfg):
    """只写要改的键：翻一个布尔，原声明的其余键不丢。

    这是「深度合并」与外置 spec 的「浅覆盖」最关键的差别——若退化成浅覆盖，
    本机写三行就会把整个引擎声明换成三行（label/type/timeout 全没）。
    """
    local_cfg.write_text(
        "engines:\n  parallel:\n    enabled: false\n    disabled_reason: 本机欠费\n",
        encoding="utf-8")
    spec = _reload()["engines"]["parallel"]
    assert spec["enabled"] is False
    assert spec["disabled_reason"] == "本机欠费"
    assert spec["label"] == "Parallel搜索"
    assert spec["type"] == "parallel"
    assert spec["timeout"] == 15


def test_local_override_does_not_touch_siblings(local_cfg):
    """深合并的边界：只翻目标引擎，兄弟引擎原样。"""
    local_cfg.write_text("engines:\n  parallel:\n    enabled: false\n", encoding="utf-8")
    engines = _reload()["engines"]
    assert engines["parallel"]["enabled"] is False
    assert engines["parallel_free"]["enabled"] is True


def test_local_override_wins_over_config_yaml(local_cfg):
    """本机覆盖层优先级最高：能翻掉 config.yaml 里写死的开关。"""
    before = _reload()["engines"]["parallel"]["enabled"]
    local_cfg.write_text(
        f"engines:\n  parallel:\n    enabled: {str(not before).lower()}\n",
        encoding="utf-8")
    assert _reload()["engines"]["parallel"]["enabled"] is (not before)


def test_local_override_non_engine_section(local_cfg):
    """覆盖层不限于 engines 段：cache 等本机调优同样生效。"""
    local_cfg.write_text("cache:\n  ttl: 123\n", encoding="utf-8")
    cache = _reload()["cache"]
    assert cache["ttl"] == 123
    assert cache["max_size_mb"] == 200  # 兄弟键不动


def test_local_override_can_add_new_engine(local_cfg):
    """覆盖层能新增 config.yaml 里没有的引擎声明（本机专属源）。"""
    local_cfg.write_text(
        "engines:\n  my_local_probe:\n    enabled: true\n    type: http\n",
        encoding="utf-8")
    assert "my_local_probe" in _reload()["engines"]


# ── 1b. 域表按 name 合并 ────────────────────────────────────────────────────

def _domain(cfg: dict, name: str) -> dict:
    for d in cfg["domains"]:
        if d.get("name") == name:
            return d
    raise AssertionError(f"配置里没有域 {name}")


def test_local_domain_override_merges_by_name(local_cfg):
    """domains 按 name 合并：只写源序，域的其余键（patterns/priority…）不丢。

    整份替换要求本机抄全 99 个域才能改一个域的源序，实际不可用；但也不能让
    未提及的域消失——同名的原位深合并，其余原样。
    """
    before = _reload()
    target = "chinese_general"
    orig = dict(_domain(before, target))
    local_cfg.write_text(
        "domains:\n"
        f"  - name: {target}\n"
        "    engines_combo: [metaso, anysearch]\n"
        "    primary: metaso\n",
        encoding="utf-8")
    cfg = _reload()
    dom = _domain(cfg, target)
    assert dom["engines_combo"] == ["metaso", "anysearch"]
    assert dom["primary"] == "metaso"
    assert dom["patterns"] == orig["patterns"]  # 未写的键保持公共值
    assert dom["priority"] == orig["priority"]
    assert len(cfg["domains"]) == len(before["domains"])  # 未提及的域一个不少


def test_local_domain_override_leaves_sibling_domains_untouched(local_cfg):
    """只动点名的域：兄弟域的源序不被顺手改掉。"""
    before = _reload()
    local_cfg.write_text(
        "domains:\n  - name: local_chinese\n    engines_combo: [local_search]\n",
        encoding="utf-8")
    after = _reload()
    assert _domain(after, "local_chinese")["engines_combo"] == ["local_search"]
    for name in ("chinese_general", "world_news"):
        assert _domain(after, name) == _domain(before, name)


def test_local_domain_override_can_add_new_domain(local_cfg):
    """覆盖层能新增本机专属域（未知名追加，不影响既有域）。"""
    before = _reload()["domains"]
    local_cfg.write_text(
        "domains:\n"
        "  - name: my_local_domain\n"
        "    engines_combo: [anysearch]\n"
        "    primary: anysearch\n",
        encoding="utf-8")
    after = _reload()["domains"]
    assert len(after) == len(before) + 1
    assert _domain({"domains": after}, "my_local_domain")["primary"] == "anysearch"


def test_public_artifacts_read_domains_without_local(local_cfg):
    """公共派生件与门禁只看公共域表：本机改了源序，休眠台账/文档生成不变红。

    这两处若走 load_config() 默认口径，本机把某源提到主源位次就会让
    sync_backends --check 报「台账过期」、gen_engine_catalog --check 报文档过期
    ——本机状态污染公共产物，正是覆盖层要挡的事。
    """
    import dormant_ledger as DL
    import gen_engine_catalog as GC

    before = DL.dormant_engines()
    local_cfg.write_text(
        "domains:\n"
        "  - name: chinese_general\n"
        "    engines_combo: [metaso, anysearch]\n"
        "    primary: metaso\n",
        encoding="utf-8")
    assert DL.check_dormant_ledger() == []
    assert DL.dormant_engines() == before
    assert GC._count_truth()["domains"] == str(len(C.load_config(include_local=False)["domains"]))


# ── 2. 失效判据认它 ──────────────────────────────────────────────────────────

def test_local_file_change_invalidates_stamp(local_cfg, monkeypatch):
    """改完立即生效：config_stamp 必须认覆盖层（TTL 关掉，直接看判据本身）。"""
    monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0")
    local_cfg.write_text("engines:\n  parallel:\n    enabled: false\n", encoding="utf-8")
    s1 = C.config_stamp()
    time.sleep(0.01)
    local_cfg.write_text("cache:\n  ttl: 9\n", encoding="utf-8")
    assert C.config_stamp() != s1


def test_disk_cache_stores_public_config(local_cfg, monkeypatch):
    """磁盘缓存键**不含**本机覆盖层：缓存存的是公共配置，派生件才能安全复用。

    反方向也要钉：键里若含覆盖层，本机跑一次 sync 就会读到「带本机状态」的
    缓存，把本地停用写进 engine_registry.yaml（公开仓）。
    """
    import config_cache as CC
    monkeypatch.setattr(CC, "_local_config_path", lambda: local_cfg)
    st = CC._config_path().stat()
    scan = (0.0, 0, 0, "x")
    k1 = CC._config_disk_cache_key(st, scan, "d1")
    local_cfg.write_text("engines:\n  parallel:\n    enabled: false\n", encoding="utf-8")
    assert CC._config_disk_cache_key(st, scan, "d1") == k1


def test_include_local_false_skips_override(local_cfg):
    """include_local=False 是派生公共产物的入口：本机状态不进去。"""
    local_cfg.write_text("engines:\n  parallel:\n    enabled: false\n", encoding="utf-8")
    assert _reload()["engines"]["parallel"]["enabled"] is False
    assert C.load_config(force=True, include_local=False)["engines"]["parallel"]["enabled"] is True


def test_local_cache_db_path_applied_in_peek(local_cfg, monkeypatch):
    """本机覆盖层改 db_path 时 peek 也要认——否则 load_config 与 argo_paths 分裂。"""
    import config_cache as CC
    monkeypatch.setattr(CC, "_local_config_path", lambda: local_cfg)
    local_cfg.write_text("cache:\n  db_path: /tmp/argo-local-probe.db\n", encoding="utf-8")
    hit, db = CC._peek_disk_cache_db_path(CC._config_path().stat(), "d1")
    assert db == "/tmp/argo-local-probe.db"


def test_sync_style_load_excludes_local(local_cfg):
    """sync_backends 的口径：派生件不含本机覆盖层（否则本机状态进公开仓）。"""
    import sync_backends as SB
    local_cfg.write_text("engines:\n  parallel:\n    enabled: false\n", encoding="utf-8")
    assert SB.load_engines()["parallel"]["enabled"] is True


# ── 3. 降级与契约 ────────────────────────────────────────────────────────────

def test_corrupt_local_file_fails_open(local_cfg):
    """覆盖层损坏一律静默忽略，退化为「只用 config.yaml」。

    本机一个手滑的 YAML 不该升级成「所有命令都跑不了」——覆盖层是可选的
    加速器，它的损坏必须与「不存在」走同一条路径。
    """
    local_cfg.write_text(": : : [broken\n", encoding="utf-8")
    assert _reload()["engines"]["parallel"]["enabled"] is True


def test_local_file_is_gitignored():
    """契约：config.local.yaml 必须在 .gitignore 里。

    这一条是「本机状态不进公开仓」的唯一保障——覆盖层再好用，只要忘了
    gitignore，一次 `git add -A` 就把本机欠费/本机路径推给所有用户。
    """
    gi = (SKILL_DIR / ".gitignore").read_text(encoding="utf-8")
    assert "config.local.yaml" in gi


def test_example_file_committed():
    """契约：示例文件进仓（它本身不含本机状态，是覆盖层的说明书）。"""
    example = SKILL_DIR / "config.local.yaml.example"
    assert example.is_file()
    text = example.read_text(encoding="utf-8")
    assert "config.local.yaml" in text


def test_optional_local_dep_engines_use_runtime_gating_not_static_disable():
    """契约：依赖可选 CLI 或自建 URL 的引擎在公开配置中应由运行时条件门控，不静态写死停用。"""
    cfg = C.load_config(force=True, include_local=False)
    engines = cfg["engines"]

    digup = engines["digup"]
    assert digup.get("enabled") is True
    assert digup.get("explicit_only") is True
    assert any(req.get("bin") == "digup" for req in (digup.get("requires") or []))

    from engine_env import required_env_for
    for eng_id, expected_var in (("fourget", "ARGO_FOURGET_URL"), ("searxng", "ARGO_SEARXNG_URL")):
        spec = engines[eng_id]
        assert spec.get("enabled") is True, eng_id
        assert spec.get("explicit_only") is True, eng_id
        reqs = required_env_for(eng_id, spec)
        assert any(expected_var in cands for cands in reqs), (eng_id, reqs)


def test_public_demo_key_engine_is_explicit_only():
    """契约：使用公共 demo key 的引擎不得进自动路由，避免共享限额被路由消耗。

    europeana 用官方公共 wskey（api2demo），实测 HTTP 200 可用，但响应里
    requestNumber 提示这是共享额度；自动路由选中会耗尽公共额度并静默空转
    （401 或空结果），故与 digup、fourget、searxng 一致改为显式专用。
    """
    cfg = C.load_config(force=True, include_local=False)
    spec = cfg["engines"]["europeana"]
    assert spec.get("enabled") is True
    assert spec.get("explicit_only") is True
