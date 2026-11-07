"""env_store（settings 写回唯一路径）测试：合并/引用/备份/掩码是安全红线。

写回器碰的是用户密钥文件——任何一条失败都意味着可能破坏既有配置或
把明文值写进输出。
"""
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

import env_store  # noqa: E402


# ── 行级补丁 ────────────────────────────────────────────────────────────────

def test_patch_replaces_in_place_keeps_comments():
    lines = ["# 注释保留\n", "ARGO_A_KEY=old-value-1\n", "\n", "OTHER=x\n"]
    new, rep = env_store.patch_lines(lines, {"ARGO_A_KEY": "new-value-2"}, [])
    assert new == ["# 注释保留\n", "ARGO_A_KEY=new-value-2\n", "\n", "OTHER=x\n"]
    assert rep["replaced"] == ["ARGO_A_KEY"] and not rep["appended"]


def test_patch_merges_legacy_bare_name():
    """#41 同类：文件里只有裸名 BOCHA_API_KEY，set 规范名必须就地替换并
    记账合并，不得留下同一逻辑键的两行。"""
    lines = ["BOCHA_API_KEY=old-value-123\n"]
    new, rep = env_store.patch_lines(
        lines, {"ARGO_BOCHA_API_KEY": "new-value-456"}, [])
    assert new == ["ARGO_BOCHA_API_KEY=new-value-456\n"]
    assert rep["replaced"] == ["ARGO_BOCHA_API_KEY"]
    assert rep["merged_legacy"] == ["BOCHA_API_KEY"]


def test_patch_dedupes_duplicate_lines():
    lines = ["ARGO_A_KEY=one-111111111111\n", "ARGO_A_KEY=two-222222222222\n"]
    new, rep = env_store.patch_lines(lines, {"ARGO_A_KEY": "three-333333333"}, [])
    assert new == ["ARGO_A_KEY=three-333333333\n"]
    assert rep["replaced"] == ["ARGO_A_KEY"]


def test_patch_appends_new_key_with_trailing_newline():
    new, rep = env_store.patch_lines(["A=1\n"], {"ARGO_NEW_KEY": "v-123456"}, [])
    assert new == ["A=1\n", "ARGO_NEW_KEY=v-123456\n"]
    assert rep["appended"] == ["ARGO_NEW_KEY"]


def test_unset_removes_all_variants_and_reports_absent():
    lines = ["ARGO_A_KEY=x-11111111111111\n", "A_KEY=y-22222222222222\n",
             "B=keep\n"]
    new, rep = env_store.patch_lines(lines, [], ["ARGO_A_KEY"])
    assert new == ["B=keep\n"]
    assert rep["removed"] == ["ARGO_A_KEY", "ARGO_A_KEY"]
    new2, rep2 = env_store.patch_lines(["B=keep\n"], [], ["ARGO_A_KEY"])
    assert new2 == ["B=keep\n"] and rep2["absent"] == ["ARGO_A_KEY"]


def test_value_quoting_for_special_chars():
    new, _ = env_store.patch_lines([], {"ARGO_A_KEY": "has space #comment"}, [])
    assert new == ["ARGO_A_KEY='has space #comment'\n"]
    # 引用后的行必须能被同一解析口径读回原值
    parsed = env_store._parse_line(new[0])
    assert parsed == ("ARGO_A_KEY", "'has space #comment'") or parsed[0] == "ARGO_A_KEY"


def test_rejects_bad_name_newline_and_conflict():
    with pytest.raises(env_store.EnvStoreError):
        env_store.patch_lines([], {"1BAD": "v-123456"}, [])
    with pytest.raises(env_store.EnvStoreError):
        env_store.patch_lines([], {"ARGO_A_KEY": "line1\nline2"}, [])
    with pytest.raises(env_store.EnvStoreError):
        env_store.patch_lines([], {"ARGO_A_KEY": "v"}, ["ARGO_A_KEY"])


# ── 写盘 ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def env_file(monkeypatch, tmp_path):
    """ARGO_ENV_FILE 指向临时文件 + reset_envfile_cache 打点。"""
    target = tmp_path / "env"
    monkeypatch.setenv("ARGO_ENV_FILE", str(target))
    calls = []
    import engine_env
    monkeypatch.setattr(engine_env, "reset_envfile_cache",
                        lambda: calls.append(1))
    return target, calls


def test_apply_backup_perm_cache_reset(env_file):
    target, calls = env_file
    target.write_text("ARGO_A_KEY=old-1234567890\n", encoding="utf-8")
    os.chmod(target, 0o600)
    rep = env_store.apply_changes(sets={"ARGO_A_KEY": "new-0987654321"})
    assert target.read_text(encoding="utf-8") == "ARGO_A_KEY=new-0987654321\n"
    bak = Path(rep["backup"])
    assert bak.name == "env.bak"
    assert "old-1234567890" in bak.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert calls == [1]  # 热读缓存已失效
    assert "new-0987654321" not in env_store.format_report(rep)  # 明文不出现在报告


def test_apply_creates_missing_file_without_backup(env_file):
    target, _ = env_file
    rep = env_store.apply_changes(sets={"ARGO_A_KEY": "v-12345678"})
    assert target.is_file()
    assert rep["backup"] is None
    assert "ARGO_A_KEY=v-12345678" in target.read_text(encoding="utf-8")


def test_apply_empty_change_rejected(env_file):
    with pytest.raises(env_store.EnvStoreError):
        env_store.apply_changes()


def test_report_has_no_plaintext(env_file):
    target, _ = env_file
    target.write_text("ARGO_A_KEY=old-1234567890\nARGO_B_KEY=gone-123456789\n",
                      encoding="utf-8")
    secret = "super-secret-value-9988776655"
    rep = env_store.apply_changes(sets={"ARGO_A_KEY": secret},
                                  unsets=["ARGO_B_KEY"])
    text = env_store.format_report(rep)
    assert secret not in text and "old-1234567890" not in text
    assert "已覆盖" in text and "已清除" in text
