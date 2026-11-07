"""settings_page（argo settings）测试：掩码纪律与 HTML 逃逸是安全红线。

settings 页会把密钥状态渲染进 HTML——任何一条测试失败都意味着密钥明文
可能出现在落盘文件里，这是本模块唯一不可退让的契约。
"""
import os
import sys
import types
from pathlib import Path

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import settings_page  # noqa: E402


# ── 掩码 ────────────────────────────────────────────────────────────────────

def test_mask_never_returns_plaintext():
    long_key = "sk-abcdef1234567890"
    m = settings_page._mask(long_key)
    assert long_key not in m
    assert m.endswith("…7890）")


def test_mask_short_and_missing():
    assert settings_page._mask("abc") == "已配置"
    assert settings_page._mask("") == "缺失"
    assert settings_page._mask(None) == "缺失"


def test_mask_whitespace_only_is_missing():
    assert settings_page._mask("   ") == "缺失"


# ── collect（stub 注入，不读真实注册表/网络）────────────────────────────────

def _install_stubs(monkeypatch, envfile, engines_spec):
    """把 engines / engine_env 换成受控 stub，collect 从此离线且确定。

    引擎名单从 engines_spec 键派生（另加一个无密钥要求的 eng_no_env），
    名单与 spec 永远一致，测试间不会互相错位。get_env 复刻真实优先级：
    进程 env 优先、envfile 兜底，按候选链顺序取第一个非空。
    """
    names = list(engines_spec.keys()) + ["eng_no_env"]
    fake_engines = types.ModuleType("engines")
    fake_engines.available_engines = lambda routable_only=False: (
        names if not routable_only else list(engines_spec.keys()))
    fake_engines.get_engine_spec = lambda name: engines_spec.get(name)
    fake_env = types.ModuleType("engine_env")
    fake_env._envfile_load = lambda: envfile
    fake_env.required_env_for = lambda name, spec=None: (
        (spec or {}).get("required_env") or [])
    fake_env.missing_env_for = lambda name, spec=None: [
        v for v in ((spec or {}).get("required_env") or [])
        if not os.environ.get(v) and not envfile.get(v)]

    def fake_get_env(names, default=""):
        """复刻真实 get_env：进程 env 优先，envfile 兜底，首个非空生效。"""
        if isinstance(names, str):
            names = [names]
        for n in names:
            v = os.environ.get(n)
            if v and v.strip():
                return v
        for n in names:
            v = envfile.get(n)
            if v and v.strip():
                return v
        return default

    fake_env.get_env = fake_get_env
    fake_env.PLACEHOLDER_ALIASES = {}
    monkeypatch.setitem(sys.modules, "engines", fake_engines)
    monkeypatch.setitem(sys.modules, "engine_env", fake_env)


def test_collect_key_rows_masked(monkeypatch):
    secret = "sk-live-9988776655443322"
    _install_stubs(
        monkeypatch,
        envfile={"ARGO_A_KEY": secret},
        engines_spec={"eng_a": {"required_env": ["ARGO_A_KEY"]}},
    )
    data = settings_page.collect()
    assert data["engine_counts"] == {"total": 2, "routable": 1}
    rows = {r["engine"]: r for r in data["key_rows"]}
    assert "eng_a" in rows and "eng_no_env" not in rows  # 无 required_env 不上表
    statuses = [v["status"] for v in rows["eng_a"]["envs"]]
    assert statuses == ["已配置（…3322）"]
    assert secret not in str(data)


def test_collect_missing_key_reports_missing(monkeypatch):
    _install_stubs(
        monkeypatch,
        envfile={},
        engines_spec={"eng_a": {"required_env": ["ARGO_MISSING_KEY"]}},
    )
    data = settings_page.collect()
    assert data["key_rows"][0]["envs"][0]["status"] == "缺失"


def test_process_env_wins_over_envfile(monkeypatch):
    _install_stubs(
        monkeypatch,
        envfile={"ARGO_A_KEY": "from-envfile-123456789"},
        engines_spec={"eng_a": {"required_env": ["ARGO_A_KEY"]}},
    )
    monkeypatch.setenv("ARGO_A_KEY", "from-process-123456789")
    data = settings_page.collect()
    assert "…6789）" in data["key_rows"][0]["envs"][0]["status"]
    assert "from-envfile" not in str(data)
    monkeypatch.delenv("ARGO_A_KEY")


def test_legacy_unprefixed_key_counts_as_configured(monkeypatch, tmp_path):
    """#41 同类回归：envfile 只存裸名（BOCHA_API_KEY）而 required_env 声明
    ARGO_ 前缀名时，runtime 候选链判定已配置，设置页必须同口径——
    本测试用真 engine_env（ARGO_ENV_FILE 指向临时文件），只 stub 引擎名单。"""
    env_file = tmp_path / "env"
    # 假值刻意短于 16 位：privacy-guard 会拦「KEY=16 位以上字面量」形态，
    # 测试桩不该长得像真密钥
    env_file.write_text("BOCHA_API_KEY=legacy-key-1234\n",
                        encoding="utf-8")
    monkeypatch.setenv("ARGO_ENV_FILE", str(env_file))
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    monkeypatch.delenv("ARGO_BOCHA_API_KEY", raising=False)
    fake_engines = types.ModuleType("engines")
    fake_engines.available_engines = lambda routable_only=False: ["bocha"]
    fake_engines.get_engine_spec = lambda name: (
        {"required_env": ["ARGO_BOCHA_API_KEY"]} if name == "bocha" else None)
    monkeypatch.setitem(sys.modules, "engines", fake_engines)

    data = settings_page.collect()
    assert data["key_rows"], "bocha 必须上密钥表"
    status = data["key_rows"][0]["envs"][0]["status"]
    assert status.startswith("已配置"), f"裸名密钥被误报缺失: {status}"
    assert "1234" in status and "legacy-key-1234" not in status


# ── render / main ───────────────────────────────────────────────────────────

def test_render_escapes_hostile_values(monkeypatch):
    _install_stubs(
        monkeypatch,
        envfile={"ARGO_X": "<script>alert(1)</script>12345678"},
        engines_spec={"<script>eng</script>": {"required_env": ["ARGO_X"]}},
    )
    data = settings_page.collect()
    html_text = settings_page.render_html(data)
    # 恶意密钥值先经掩码（只露尾 4 位）再逃逸：原始值不得出现
    assert "<script>alert(1)</script>" not in html_text
    assert "…5678）" in html_text
    # 引擎名不走掩码、逐字进表：必须以逃逸形态出现
    assert "&lt;script&gt;eng&lt;/script&gt;" in html_text


def test_main_writes_page_and_prints_uri(monkeypatch, tmp_path, capsys):
    out = tmp_path / "settings.html"
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "--out", str(out), "--no-open"])
    rc = settings_page.main()
    assert rc == 0
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "Argo 设置" in body
    assert "复制 MCP JSON" in body
    printed = capsys.readouterr().out
    assert out.resolve().as_uri() in printed  # issue #44：末行给 agent 可转述的地址


# ── 方案 A：set / unset / list ──────────────────────────────────────────────

@pytest.fixture()
def env_target(monkeypatch, tmp_path):
    """ARGO_ENV_FILE 指向临时文件，写回与读取全部离线。"""
    target = tmp_path / "env"
    monkeypatch.setenv("ARGO_ENV_FILE", str(target))
    return target


def test_set_reads_value_from_piped_stdin(monkeypatch, tmp_path, capsys,
                                          env_target):
    import io
    secret = "piped-value-123456"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "set", "ARGO_A_KEY"])
    rc = settings_page.main()
    assert rc == 0
    body = env_target.read_text(encoding="utf-8")
    assert "ARGO_A_KEY=" in body and secret in body
    out = capsys.readouterr().out
    assert secret not in out            # 明文不回显
    assert "…3456）" in out             # 只有掩码
    assert "热读" in out


def test_set_rejects_empty_value(monkeypatch, tmp_path, capsys, env_target):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "set", "ARGO_A_KEY"])
    rc = settings_page.main()
    assert rc == 1
    assert "失败" in capsys.readouterr().err


def test_unset_removes_from_envfile(monkeypatch, tmp_path, capsys, env_target):
    env_target.write_text("ARGO_A_KEY=keepme-123456789\nARGO_B_KEY=byeeee-12345\n",
                          encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "unset", "ARGO_B_KEY"])
    rc = settings_page.main()
    assert rc == 0
    body = env_target.read_text(encoding="utf-8")
    assert "ARGO_A_KEY=keepme-123456789" in body and "ARGO_B_KEY" not in body


def test_unset_reports_absent_key(monkeypatch, tmp_path, capsys, env_target):
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "unset", "ARGO_NOT_THERE"])
    rc = settings_page.main()
    assert rc == 0
    assert "未找到" in capsys.readouterr().out


def test_list_prints_text(monkeypatch, tmp_path, capsys, env_target):
    _install_stubs(
        monkeypatch,
        envfile={"ARGO_A_KEY": "tail-only-123456"},
        engines_spec={"eng_a": {"required_env": ["ARGO_A_KEY"]}},
    )
    monkeypatch.setattr(sys, "argv", ["settings_page.py", "list"])
    rc = settings_page.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "eng_a" in out and "ARGO_A_KEY 已配置" in out
    assert "tail-only-123456" not in out  # 明文不进终端


# ── 方案 B：--edit 表单页 + apply ───────────────────────────────────────────

def test_edit_page_form_without_plaintext(monkeypatch, tmp_path, env_target):
    _install_stubs(
        monkeypatch,
        envfile={"ARGO_A_KEY": "hidden-value-123456"},
        engines_spec={"eng_a": {"required_env": ["ARGO_A_KEY"]}},
    )
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "--edit", "--no-open",
                         "--out", str(tmp_path / "edit.html")])
    assert settings_page.main() == 0
    body = (tmp_path / "edit.html").read_text(encoding="utf-8")
    assert 'data-env="ARGO_A_KEY"' in body          # 新值输入框按变量名定位
    assert "已配置" in body                          # 现值只有掩码
    assert "hidden-value-123456" not in body        # 现值明文不进页面
    assert "argo settings apply" in body            # 写回指引
    assert "exportChanges" in body                  # 导出按钮
    assert 'data-unset="ARGO_A_KEY"' in body        # 已配置项带清除勾选


def test_apply_writes_changes_from_file(monkeypatch, tmp_path, capsys,
                                        env_target):
    env_target.write_text("ARGO_OLD_KEY=legacy-123456789\n", encoding="utf-8")
    changes = tmp_path / "changes.json"
    changes.write_text('{"sets": {"ARGO_A_KEY": "fresh-123456789", '
                       '"ARGO_OLD_KEY": "replaced-12345"}, '
                       '"unsets": ["ARGO_GONE"]}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "apply", str(changes)])
    assert settings_page.main() == 0
    body = env_target.read_text(encoding="utf-8")
    assert "ARGO_A_KEY=fresh-123456789" in body
    assert "ARGO_OLD_KEY=replaced-12345" in body
    out = capsys.readouterr().out
    assert "fresh-123456789" not in out              # 报告掩码
    assert "请删除" in out                            # 明文文件删除提醒


def test_apply_accepts_stdin_dash(monkeypatch, tmp_path, capsys, env_target):
    import io
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO('{"sets": {"ARGO_A_KEY": "vv-12345678"}}'))
    monkeypatch.setattr(sys, "argv", ["settings_page.py", "apply", "-"])
    assert settings_page.main() == 0
    assert "ARGO_A_KEY=vv-12345678" in env_target.read_text(encoding="utf-8")


def test_apply_rejects_bad_shape_and_missing_file(monkeypatch, tmp_path,
                                                  capsys, env_target):
    bad = tmp_path / "bad.json"
    bad.write_text('{"sets": "not-a-dict"}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["settings_page.py", "apply", str(bad)])
    assert settings_page.main() == 1
    assert "失败" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv",
                        ["settings_page.py", "apply",
                         str(tmp_path / "nope.json")])
    assert settings_page.main() == 1
    assert "失败" in capsys.readouterr().err
