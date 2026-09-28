#!/usr/bin/env python3
"""test_http_attribution.py — P1 观测盲区统一处理检查。

守的缺陷：手写引擎构建器里曾有近百处直连 `urllib.request.urlopen`。HTTP 失败
只留一行 warning 就变成空结果，既不写归因寄存器，也不出现在
`--list-engines --detail`——博查 AI Search 端点 `403 套餐额度不足` 就是这样长期
显示 ready 的。本次把它们统一处理到 `engines_base.http_open`。

这些检查从真实入口取事实（AST 扫真实源码、调用真实函数、走真实熔断持久化），
不做「用实现验证实现」式的同义反复：

  1. builder 文件里不得再出现直连 urlopen（否则归因又成盲区）。
  2. `http_open` 必须是一个**等价替换**：成功时字节流与响应对象原样透传，
     非 UTF-8（gbk）站点仍能手工解码。
  3. 失败必须归因且**原样抛出**，调用方既有的 except 分支语义不受影响；
     HTTPError 的错误体在读走归因后必须仍能 `e.read()`（博查依赖它）。
  4. 归因必须能从失败现场一路走到 `--list-engines --detail` 的可观测面
     （寄存器 → 熔断持久化 → engine_status.runtime.failure）。
  5. 熔断记录的是 `kind` 粗标签，不得把它当响应文本再归类（只会得到 unknown，
     这正是修复前 `runtime.failure` 恒为 unknown:empty 的根因）。
"""

import ast
import io
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 已统一处理的 builder 文件：这些文件里的 HTTP 必须全走 http_open
BUILDER_FILES = [
    "engines_builders_data.py",
    "engines_builders_cn.py",
    "engines_builders_tech.py",
    "engines_builders_data_macro.py",
    "engines_builders_intl.py",
    "engines_builders_search.py",
]


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── 1. 源码检查：builder 不得直连 urlopen ────────────────────────────────────

class TestNoDirectUrlopenInBuilders:
    def test_builders_have_zero_direct_urlopen(self):
        offenders = {}
        for name in BUILDER_FILES:
            src = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
            tree = ast.parse(src)
            hits = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "urlopen"
                        and isinstance(f.value, ast.Attribute)
                        and f.value.attr == "request"):
                    hits.append(node.lineno)
            if hits:
                offenders[name] = hits
        assert not offenders, (
            "builder 内仍有直连 urllib.request.urlopen，HTTP 失败将不进归因寄存器："
            f"{offenders}。请改用 engines_base.http_open(req, timeout=..., engine=...)。"
        )

    def test_builders_import_http_open(self):
        """改用了 http_open 的文件必须真的 import 了它（否则是 NameError 潜伏）。"""
        for name in BUILDER_FILES:
            src = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
            if "http_open(" not in src:
                continue
            tree = ast.parse(src)
            imported = any(
                isinstance(n, ast.ImportFrom) and n.module == "engines_base"
                and any(a.name == "http_open" for a in n.names)
                for n in ast.walk(tree)
            )
            assert imported, f"{name} 用了 http_open 但没从 engines_base 导入"


# 出口调度的唯一实现：只有它可以直接调 urlopen（open_url 的本体）
_EGRESS_SOURCE = {"net_proxy.py"}


class TestNoDirectUrlopenRepoWide:
    """全仓检查：任何脚本不得绕开代理感知的出口（issue #13 同类统一处理，2026-09-15）。

    issue #13 的修复只覆盖了 `http_open` 所在的引擎路径，于是 fetch_v3（正是
    该 issue 报的文件）、job、health_check、pdf_extract、readability_extract、
    train、wx、search 里的 13 处 `urlopen` 仍然直接调用——在「必须经代理才能出网」
    的环境里那些出口一律连不上。上面那条 builder 检查的作用域写死在
    BUILDER_FILES 上，所以看不见它们。

    这项检查把范围放宽到全部 scripts/**/*.py（含子目录）：唯一允许直调
    urlopen 的是 net_proxy.py 自己（open_url 的本体）。新增出口请走
    net_proxy.open_url 或 engines_base.http_open。

    作用域必须是递归的：第一版写的是 `glob("*.py")`（只看 scripts/ 顶层），
    于是 scripts/social_engines/*（7 个）与 scripts/redskill/*（1 个）里同样
    直接调用的 urlopen 一个都没被拦住——28 项检查全绿而缺陷在场。子目录正是
    社交/技能这类「按域分家」的引擎自然落脚处，漏了子目录等于漏了新增出口
    的默认位置。记录相对路径，同名文件不再互相覆盖。
    """

    def test_no_module_bypasses_egress_dispatch(self):
        offenders = {}
        assert (SCRIPTS_DIR / "net_proxy.py").is_file(), "net_proxy.py 不见了，门禁前提失效"
        for path in sorted(SCRIPTS_DIR.rglob("*.py")):
            if path.name in _EGRESS_SOURCE or "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            hits = [
                node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "urlopen"
            ]
            if hits:
                offenders[str(path.relative_to(SCRIPTS_DIR))] = hits
        assert not offenders, (
            "这些文件绕开了出口调度（不认 config.yaml 的 network.proxy，"
            "在必须走代理的环境里会连不上）："
            f"{offenders}。请改用 net_proxy.open_url(req, timeout=...)。"
        )

    def test_gate_has_teeth(self):
        """故意造错验证：往一个原本干净的脚本里塞一处 urlopen，检查必须报红。"""
        target = SCRIPTS_DIR / "wx.py"
        src = target.read_text(encoding="utf-8")
        patched = src.replace(
            "from net_proxy import open_url",
            "from net_proxy import open_url as _ou") + (
            "\n\ndef _mutant_egress(req):\n"
            "    import urllib.request\n"
            "    return urllib.request.urlopen(req, timeout=1)\n")
        assert patched != src, "造错样本没生效，测试本身失效"
        target.write_text(patched, encoding="utf-8")
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"))
            found = [
                n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "urlopen"
            ]
            assert found, "造错之后检查没抓住——等于没检查"
        finally:
            target.write_text(src, encoding="utf-8")


# ── 2. http_open 是等价替换：成功路径字节保真 ───────────────────────────────

class TestHttpOpenSuccessPath:
    def test_gbk_bytes_survive_roundtrip(self):
        """非 UTF-8 站点（gbk）必须仍能由调用方手工解码——不能被替成 utf-8。"""
        from engines_base import http_open
        body = "中国 上海 天气".encode("gbk")
        with patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            with http_open("https://x.example/q", timeout=5, engine="demo") as resp:
                got = resp.read()
        assert got == body
        assert got.decode("gbk") == "中国 上海 天气"

    def test_str_url_is_wrapped_into_request(self):
        from engines_base import http_open
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url if hasattr(req, "full_url") else req
            return _FakeResp(b"ok")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with http_open("https://x.example/q", timeout=5) as resp:
                resp.read()
        assert captured["url"] == "https://x.example/q"

    def test_response_object_is_yielded_unchanged(self):
        """成功路径必须交出真实响应对象（调用方会读 resp.headers）。"""
        from engines_base import http_open

        class R(_FakeResp):
            headers = {"Content-Encoding": "gzip"}

        with patch("urllib.request.urlopen", return_value=R(b"z")):
            with http_open("https://x.example/q", timeout=5) as resp:
                assert resp.headers.get("Content-Encoding") == "gzip"


# ── 3. 失败必须先归因再原样抛出 ─────────────────────────────────────────────

class TestHttpOpenFailurePath:
    def test_http_error_records_attribution_and_propagates(self):
        from engines_base import http_open, pop_failure_note
        pop_failure_note("attr_src")  # 清干净
        err = urllib.error.HTTPError(
            "https://x.example/q", 403, "Forbidden", {},
            io.BytesIO(b'{"message":"You do not have enough money or package quota"}'))
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(urllib.error.HTTPError):
                with http_open("https://x.example/q", timeout=5, engine="attr_src") as _:
                    pass
        note = pop_failure_note("attr_src")
        assert note is not None, "HTTP 失败没有写归因寄存器"
        # 403 + 额度文案 → 限流语义（不是 auth，重登录无用）
        assert note["category"] == "rate_limited"
        assert "quota" in note["detail"]

    def test_http_error_body_still_readable_by_caller(self):
        """归因读走错误体后必须回挂，调用方 e.read() 仍可用（_bocha_http_error 依赖）。"""
        from engines_base import http_open
        body = b'{"message":"You do not have enough money or package quota"}'
        err = urllib.error.HTTPError("https://x.example/q", 403, "F", {},
                                     io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(urllib.error.HTTPError) as ei:
                with http_open("https://x.example/q", timeout=5, engine="bocha_like") as _:
                    pass
        assert ei.value.read() == body, "错误体被归因读走后没有还回调用方"

    def test_network_error_records_and_propagates(self):
        from engines_base import http_open, pop_failure_note
        pop_failure_note("net_src")
        with patch("urllib.request.urlopen", side_effect=OSError("connection reset")):
            with pytest.raises(OSError):
                with http_open("https://x.example/q", timeout=5, engine="net_src") as _:
                    pass
        note = pop_failure_note("net_src")
        assert note and note["category"] == "network"

    def test_empty_engine_is_inert(self):
        """未标引擎名时不写寄存器（测试直调 builder 保持无副作用）。"""
        from engines_base import http_open, pop_failure_note
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                with http_open("https://x.example/q", timeout=5) as _:
                    pass
        assert pop_failure_note("") is None


# ── 4. 归因能走到可观测面 ───────────────────────────────────────────────────

class TestAttributionReachesObservability:
    def test_note_becomes_classify_shaped_contract(self):
        """from_note 输出必须与 classify 同构（category/reason/evidence/action）。"""
        from engine_failure import from_note
        note = {"category": "rate_limited", "reason": "http-403+quota-sign",
                "detail": "quota exhausted", "ts": 1.0}
        out = from_note(note, "eng_x")
        assert set(("category", "reason", "evidence", "action", "confidence")) <= set(out)
        assert out["category"] == "rate_limited"
        assert out["evidence"] == "quota exhausted"
        assert out["engine_id"] == "eng_x"

    def test_breaker_persists_attribution(self):
        from circuit_breaker import get_breaker
        from engine_failure import from_note
        b = get_breaker()
        b.reenable("persist_src")
        attr = from_note({"category": "blocked", "reason": "anti-bot-page",
                          "detail": "just a moment"}, "persist_src")
        b.record_failure("persist_src", kind="blocked", attribution=attr)
        st = b.status("persist_src")
        assert st["last_attribution"]["category"] == "blocked"
        assert "just a moment" in st["last_attribution"]["evidence"]

    def test_runtime_status_prefers_persisted_attribution(self):
        """engine_status 必须优先用失败现场记录，而不是把 kind 文本再归类。"""
        import engine_status
        st = {"state": "closed", "failures": 1, "cooldown_remain": 0,
              "last_kind": "rate-limited",
              "last_attribution": {"category": "rate_limited",
                                   "reason": "http-403+quota-sign",
                                   "evidence": "quota exhausted",
                                   "action": "x", "confidence": "high"}}
        fake = type("B", (), {"status": lambda self, e: st})()
        with patch.dict(sys.modules, {"circuit_breaker": type(
                "M", (), {"get_breaker": staticmethod(lambda: fake)})()}):
            runtime = engine_status._runtime_status("eng_y", {}, {})
        assert runtime["failure"]["category"] == "rate_limited"
        assert runtime["failure"]["evidence"] == "quota exhausted"

    def test_kind_label_alone_classifies_as_unknown_not_misattributed(self):
        """没有持久化归因时的回落路径：kind 是粗标签，不得被当成「限流」等结论。

        修复前 runtime.failure 恒为 unknown（把 'empty'/'blocked' 当响应文本），
        这条锁住「回落也只能是 unknown，不许硬猜」——错的归因比没有归因更坏。
        """
        from engine_failure import classify
        assert classify(output="empty")["category"] == "unknown"
        assert classify(output="blocked")["category"] == "unknown"

    def test_detail_console_shows_failure_column(self):
        """控制台 detail 表必须有一列展示失败原因（此前只在 --json 里）。"""
        from engine_status import format_engines_table
        rows = [{
            "engine_id": "eng_z", "status": "rate-limited", "cost_tier": "free",
            "type": "http", "routable": False, "env_ready": True,
            "missing_env": [], "runtime": {"failure": {
                "category": "rate_limited", "evidence": "quota exhausted"}},
        }]
        out = format_engines_table(rows)
        assert "FAILURE" in out.splitlines()[0]
        assert "rate_limited" in out
        assert "quota exhausted" in out


# ── 5. 配额耗尽（P1 的原始动机场景）必须在可观测面体现 ──────────────────────

class TestQuotaExhaustedVisibility:
    """博查 AI Search 的 403 套餐额度不足长期显示 ready，是 P1 的起因。

    此前 quota-exhausted 分支为了让配额状态机接管而 `pass`，归因被 pop 后销毁，
    可观测面反而没有任何改善。这组检查锁住「配额耗尽也要可见且不可路由」。
    """

    def test_engine_detail_marks_quota_exhausted_not_ready(self):
        import engine_status
        marks = {"bocha_ai": {"reason": 'HTTP 403: {"message":"no quota"}',
                              "until": 9999999999.0}}
        # 自备密钥（2026-09-29）：bocha_ai 无密钥时状态机先判 missing_key，
        # 配额分支根本走不到——此前依赖开发者 env 文件里的真密钥
        with patch.dict(os.environ, {"ARGO_BOCHA_API_KEY": "test-key"}), \
             patch.object(engine_status, "_quota_exhausted_marks", return_value=marks):
            row = engine_status.engine_detail("bocha_ai")
        assert row["status"] == "quota_exhausted", \
            f"配额耗尽仍显示 {row['status']}（应为 quota_exhausted）"
        assert row["routable"] is False, "配额耗尽的引擎不该被判为可路由"
        assert row["quota_exhausted"] is True
        assert "no quota" in row["quota_exhausted_reason"]

    def test_breaker_record_note_does_not_count_as_failure(self):
        """归因记录不得影响熔断计数/状态——配额问题不是引擎故障。"""
        from circuit_breaker import get_breaker
        b = get_breaker()
        b.reenable("note_src")
        b.record_note("note_src", {"category": "rate_limited",
                                   "reason": "http-403+quota-sign",
                                   "evidence": "quota", "action": "x",
                                   "confidence": "high"})
        st = b.status("note_src")
        assert st["failures"] == 0, "record_note 不应累计 failures"
        assert st["state"] == "closed", "record_note 不应改变熔断状态"
        assert st["last_attribution"]["category"] == "rate_limited"

    def test_quota_reason_beats_stale_breaker_attribution(self):
        """配额原因是状态机确认过的，应优先于熔断里可能陈旧的归因。"""
        from engine_status import format_engines_table
        rows = [{
            "engine_id": "bocha_ai", "status": "quota_exhausted",
            "cost_tier": "low", "type": "bocha_ai", "routable": False,
            "env_ready": True, "missing_env": [],
            "quota_exhausted": True,
            "quota_exhausted_reason": "HTTP 403: no money or package quota",
            "runtime": {"failure": {"category": "unknown", "evidence": "empty"}},
        }]
        out = format_engines_table(rows)
        assert "quota_exhausted" in out and "no money" in out
        assert "unknown: empty" not in out

    def test_remote_exhausted_marks_snapshot_shape(self):
        """配额状态机快照的字段契约（engine_status 依赖 reason/until）。"""
        from quota import get_quota_manager
        marks = get_quota_manager().remote_exhausted_marks()
        assert isinstance(marks, dict)
        for eng, m in marks.items():
            assert isinstance(eng, str)
            assert set(("reason", "until")) <= set(m)


# ── 6. 排查配套修掉的低级 bug（各自单独锁一条）─────────────────────────────

class TestSurveyFixes:
    """审查轮发现的独立缺陷，逐条回归。"""

    def test_external_spec_cmd_is_absolute(self):
        """外置 spec 的相对 cmd 必须解析成绝对路径。

        引擎以子进程执行且不设 cwd：相对路径在非仓库根目录下直接失败
        （实测 rc=2），且校验结果随 CWD 漂移（同一配置 165 vs 163 个引擎）。
        """
        from config import load_config, get_engines
        eng = get_engines(load_config())
        cli = [(k, v) for k, v in eng.items() if v.get("type") == "cli"]
        assert cli
        for name, spec in cli:
            cmd = spec.get("cmd") or []
            if not cmd or cmd[0] in ("npx", "node") or str(cmd[-1]).startswith("--"):
                continue
            last = str(cmd[-1])
            assert last.startswith("/") or not ("/" in last), \
                f"{name} 的 cmd 仍是相对路径: {cmd}"

    def test_engine_set_is_cwd_independent(self):
        """同一份配置在不同 CWD 下必须解析出同一批引擎。

        此前 _validate_engine_paths 用 Path(cmd).exists() 做 CWD 相对判定，
        在 /tmp 下会把 train/weather 静默停用，一致性检查随之红/绿漂移。
        """
        import json
        import subprocess
        script = (
            "import sys,json; sys.path.insert(0,'" + str(SCRIPTS_DIR) + "');"
            "from config import load_config,get_engines;"
            "print(json.dumps(sorted(get_engines(load_config()).keys())))"
        )
        outs = []
        for cwd in (str(SKILL_DIR), "/tmp"):
            r = subprocess.run([sys.executable, "-c", script], cwd=cwd,
                               capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stderr[:300]
            outs.append(json.loads(r.stdout))
        assert outs[0] == outs[1], (
            f"引擎集合随 CWD 变化：{len(outs[0])} vs {len(outs[1])}；"
            f"差异={sorted(set(outs[0]) ^ set(outs[1]))}")

    def test_unknown_charset_does_not_drop_body(self):
        """未知 charset 不得把 200 响应降级成 status=0。

        `decode("x-unknown-8bit")` 抛 LookupError，而 LookupError 不是 OSError，
        会被 get() 的保底吞成「连接失败」并归因为 network。
        """
        from http_client import _charset_of, _decode_body
        assert _charset_of("text/html; charset=x-unknown-8bit") == "utf-8"
        assert _charset_of("text/html; charset=gb2312;") == "gb2312"
        assert _charset_of("text/html") == "utf-8"
        assert _decode_body("中文".encode("utf-8"),
                            "text/html; charset=x-unknown-8bit") == "中文"

    def test_note_redacts_credentials(self):
        """归因细节会持久化并展示，写入前必须脱敏（唯一写入点做）。"""
        from engines_base import note_failure, pop_failure_note
        pop_failure_note("leak_src")
        note_failure("leak_src", "auth", "http-401",
                     "Incorrect API key: sk-live-abcdef1234567890 "
                     "and ?token=deadbeefsecret and Bearer TOPSECRETTOKEN")
        detail = pop_failure_note("leak_src")["detail"]
        assert "abcdef1234567890" not in detail
        assert "deadbeefsecret" not in detail
        assert "TOPSECRETTOKEN" not in detail

    def test_from_note_confidence_not_inflated(self):
        """unknown 的归因不得被标成 high 置信。"""
        from engine_failure import from_note
        low = from_note({"category": "unknown", "reason": "insufficient-signal",
                         "detail": "empty"}, "e")
        high = from_note({"category": "rate_limited",
                          "reason": "http-403+quota-sign", "detail": "q"}, "e")
        assert low["confidence"] == "low"
        assert high["confidence"] == "high"

    def test_http_open_attributes_non_oserror_exceptions(self):
        """http.client.HTTPException（BadStatusLine 等）也要归因，不能穿透。"""
        import http.client
        from engines_base import http_open, pop_failure_note
        pop_failure_note("bad_line")
        exc = http.client.BadStatusLine("NOT-HTTP")
        assert not isinstance(exc, OSError)
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(http.client.BadStatusLine):
                with http_open("https://x.example/q", timeout=5,
                               engine="bad_line") as _:
                    pass
        assert pop_failure_note("bad_line")["category"] == "network"

    def test_quota_ratio_uses_profile_limit(self):
        """配额惩罚必须真的会触发：limit 的来源是 quota_profiles.json。

        state 文件里的 limit 恒为 0（写入方不维护它），此前 _quota_ratio 只读
        state → 恒返回 1.0，配额感知惩罚是死代码。现在应以 profile 的 limit
        为准，剩余比例降到 0.2/0.5 阈值以下时真的触发惩罚。
        """
        import tfidf_router as t
        profiles = {"bounded": {"limit": 1000}}
        state = {"bounded": {"used": 950, "limit": 0}}
        assert t._quota_ratio("bounded", state, profiles) == pytest.approx(0.05)
        # 无限配额源不受影响
        assert t._quota_ratio("unbounded", {"unbounded": {"used": 5}}, profiles) == 1.0

    def test_quota_penalty_changes_score(self):
        """端到端：配额见底的引擎在 route(quota_aware=True) 下必须被降权。

        构造一个两引擎路由，只让其中一个配额见底，比较 quota_aware 开/关时
        该引擎的分数——惩罚必须真实改变排序依据，而不只是「代码里有这一支」。
        """
        import tfidf_router as t
        router = t.SemanticRouter()
        # 手工装一个最小语料：两个引擎文档相同 → 基础相似度相同
        router.engine_names = ["bounded", "healthy"]
        router.boost_keywords = {"bounded": {}, "healthy": {}}
        router.boost_combos = {"bounded": {}, "healthy": {}}
        router.vectorizer.fit(["天气 查询", "天气 查询"])
        for i, name in enumerate(router.engine_names):
            router.engine_vectors[name] = router.vectorizer.transform("天气 查询")
        router._loaded = True
        # 热重载契约（_ensure_loaded 以 mtime 判变化）：手工语料装好后必须把
        # mtime 对齐当前文件，否则 route() 入口的 _ensure_loaded 判「文件已变」
        # 触发整体重载，手工语料被真实语料冲掉 → KeyError: 'bounded'
        # （2026-09-28 实锤；手工注入语料类测试的通用前置）。
        router._profiles_mtime = t.DOMAIN_PROFILES_PATH.stat().st_mtime

        profiles = {"bounded": {"limit": 1000, "cost_tier": "free"},
                    "healthy": {"limit": 1000, "cost_tier": "free"}}
        state = {"bounded": {"used": 990, "limit": 0},
                 "healthy": {"used": 10, "limit": 0}}
        orig = t._load_cost_profiles
        t._load_cost_profiles = lambda: profiles
        try:
            with patch.object(t, "_load_quota_state", lambda: state):
                aware = {n: s for n, s, _ in router.route("天气", top_k=2,
                                                          quota_aware=True)}
                neutral = {n: s for n, s, _ in router.route("天气", top_k=2,
                                                            quota_aware=False)}
        finally:
            t._load_cost_profiles = orig
        # 基础分相同，配额惩罚必须把 bounded 压到 healthy 之下
        assert neutral["bounded"] == pytest.approx(neutral["healthy"]), \
            "对照组：无配额感知时两引擎基础分应相同"
        assert aware["bounded"] < aware["healthy"], \
            f"配额见底未被降权: {aware}"
        assert aware["healthy"] == pytest.approx(neutral["healthy"]), \
            "配额健康的引擎不应被惩罚"
