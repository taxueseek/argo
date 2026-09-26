#!/usr/bin/env python3
"""test_consistency_gates.py — 一致性与入口检查。

这个文件守的是一类具体缺陷：**检查全绿但能力实际不可用**。它们各自都很低级，
却都真实发生过（2026-09-12 审查轮），共同点是「测试没往那里看一眼」：

  1. `argo --help` 直接 NameError 崩溃（f-string 里漏转义的花括号）——
     1500 个测试全绿，因为没有任何测试真的执行过 CLI 入口。
  2. `argo fetch` 写在 SKILL.md 里但 dispatcher 里不存在——文档承诺了
     一个不存在的子命令。
  3. 派生件（registry / quota / domain）与运行时来源脱钩：外置 spec 声明的
     引擎在派生件里缺席；`--check` 拿派生结果跟自己比，永远绿。
  4. 失败归因有两份实现（归因寄存器 + engine_failure），在 97/201 个状态码上
     给出不同答案，同一引擎的解释随界面而变。
  5. 同批次的两个功能互相打架：preflight 判推文 URL 需要登录，而同一批次
     刚上线的 syndication 通道能免登录抓它。

设计原则：这些检查全部**从真实入口取事实**（执行 CLI、读磁盘派生件、
调用真实函数），不读中间变量的自述——上面第 3 条正是「用实现验证实现」的产物。
"""

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
BIN_ARGO = SKILL_DIR / "bin" / "argo"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_bin_argo():
    """bin/argo 无 .py 后缀，按源码加载器导入。"""
    loader = importlib.machinery.SourceFileLoader("argo_cli", str(BIN_ARGO))
    spec = importlib.util.spec_from_loader("argo_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def argo_cli():
    return _load_bin_argo()


# ── 1. CLI 入口可用性 ────────────────────────────────────────────────────────

class TestCliEntrypoint:
    """入口必须真的能跑：--help 崩过一次，且当时零覆盖。"""

    def test_help_exits_zero_and_prints_usage(self):
        r = subprocess.run([sys.executable, str(BIN_ARGO), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"--help 退出码 {r.returncode}: {r.stderr[:400]}"
        assert "Usage:" in r.stdout
        assert "Traceback" not in r.stderr

    def test_no_args_is_usage_error_not_crash(self):
        r = subprocess.run([sys.executable, str(BIN_ARGO)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1
        assert "Usage:" in r.stdout
        assert "Traceback" not in r.stderr

    def test_unknown_subcommand_reports_and_prints_usage(self):
        # 旧行为：打印 usage 时抛 NameError，用户看到的是 traceback 而非提示
        r = subprocess.run([sys.executable, str(BIN_ARGO), "definitely-not-a-cmd"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1
        assert "Unknown subcommand" in r.stderr
        assert "NameError" not in r.stderr
        assert "Traceback" not in r.stderr

    def test_version_flag_reports_package_version(self):
        # --version 曾不存在（落进 Unknown subcommand）；报出版本须与
        # package.json 一致，防止入口侧再长出独立的版本常量
        pkg = json.loads((SKILL_DIR / "package.json").read_text(encoding="utf-8"))
        r = subprocess.run([sys.executable, str(BIN_ARGO), "--version"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        assert pkg["version"] in r.stdout, f"--version 输出缺 {pkg['version']}: {r.stdout!r}"

    def test_help_header_count_matches_declared(self, argo_cli):
        """--help 里的收录数必须等于声明总数（曾报启用数，还对不上文档）。"""
        from config import load_config
        declared = len([k for k, v in (load_config().get("engines") or {}).items()
                        if isinstance(v, dict)])
        usage = argo_cli._usage()
        assert f"收录 {declared} 个源" in usage, \
            f"--help 未写收录数 {declared}：{usage.splitlines()[0]!r}"

    def test_usage_has_no_unescaped_fstring_braces(self):
        """字面量花括号必须双写——这是上一版崩溃的直接成因。"""
        text = BIN_ARGO.read_text(encoding="utf-8")
        assert "{{status|inject|undo}}" in text
        call = re.search(r"def _usage\(\).*?return f\"\"\"(.*?)\"\"\"", text, re.S)
        assert call, "未找到 _usage 的 f-string"
        body = call.group(1)
        # 剥掉合法替换字段 {n} 与双写花括号后，不应再有裸花括号
        residue = re.sub(r"\{\w+\}", "", body.replace("{{", "").replace("}}", ""))
        assert "{" not in residue and "}" not in residue, \
            f"_usage 内仍有未转义花括号: {residue[:200]}"


# ── 2. CLI 子命令表 = 文档承诺 = 脚本存在 ────────────────────────────────────

class TestCliSubcommandParity:
    def test_every_advertised_subcommand_is_dispatched(self, argo_cli):
        """_usage 里列出的每个子命令都必须真能分发到脚本。"""
        advertised = set(re.findall(r"^\s{2}argo\s+([a-z][a-z_]*)\s",
                                    argo_cli._usage(), re.M))
        assert advertised, "usage 文本里没解析出子命令"
        missing = sorted(c for c in advertised if c not in _dispatch_table())
        assert not missing, f"usage 承诺了但 dispatcher 没有: {missing}"

    def test_mapped_scripts_exist(self):
        for sub, (script, _defaults) in _dispatch_table().items():
            assert (SCRIPTS_DIR / script).exists(), f"{sub} -> 缺少 {script}"

    def test_fetch_maps_to_fetch_v3(self):
        # SKILL.md 长期宣传 `argo fetch`，但 dispatcher 里没有该键（已修）
        assert _dispatch_table()["fetch"][0] == "fetch_v3.py"


def _dispatch_table() -> dict:
    """从 bin/argo 源码解析子命令 → 脚本映射（不依赖执行 main）。"""
    text = BIN_ARGO.read_text(encoding="utf-8")
    block = re.search(r"scripts = \{(.*?)\n    \}", text, re.S)
    assert block, "未找到 dispatcher 的 scripts 表"
    table = {}
    for name, script in re.findall(r'"(\w+)":\s*\("([^"]+)"', block.group(1)):
        table[name] = (script, [])
    return table


# ── 3. 文档计算方式 == 代码事实 ──────────────────────────────────────────────────

def _catalog_counts() -> tuple[str, str]:
    """搜索源文档里的「收录 N 个源 / 开箱可用 M 个」——引擎数的唯一来源。"""
    doc = (SKILL_DIR / "docs" / "ENGINE_CATALOG.md").read_text(encoding="utf-8")
    m_doc = re.search(r"收录 (\d+) 个源", doc)
    m_usable = re.search(r"开箱可用 (\d+) 个", doc)
    assert m_doc and m_usable, "搜索源文档缺口径行"
    return m_doc.group(1), m_usable.group(1)


def _domains() -> list:
    from config import get_domains, load_config
    return get_domains(load_config())


def _skill_version() -> str:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^version:\s*(\S+)", skill, re.M)
    assert m, "SKILL.md 缺 version"
    return m.group(1)


class TestDocNumbersMatchCode:
    # 全部 README 变体：此前检查只查 SKILL.md 与 README.md，其余 4 个语种
    # 版本长期落后（es/ja/ko 停在 v2.8.5 + engines-150+，en 停在 175 源），
    # 而检查全绿。语言变体是同一份对外承诺，必须同源同校。
    README_VARIANTS = ("README.md", "README.en.md", "README.es.md",
                       "README.ja.md", "README.ko.md")

    def test_engine_counts_agree_with_catalog(self):
        """全部 README 变体的引擎数必须与搜索源文档（本身有检查）一致。

        计算方式是声明计算方式：收录 N 个源、M 个免密钥开箱可用。运行时「此刻能路由
        几个」随密钥与熔断状态变，不写进文档。
        """
        doc = (SKILL_DIR / "docs" / "ENGINE_CATALOG.md").read_text(encoding="utf-8")
        m_doc = re.search(r"收录 (\d+) 个源", doc)
        m_usable = re.search(r"开箱可用 (\d+) 个", doc)
        assert m_doc and m_usable, "搜索源文档缺口径行"
        total, usable = m_doc.group(1), m_usable.group(1)
        for rel in ("SKILL.md", "README.md"):
            text = (SKILL_DIR / rel).read_text(encoding="utf-8")
            assert re.search(rf"{total} 个源", text), \
                f"{rel} 未写收录数 {total}"
            assert usable in text, f"{rel} 未写免密钥可用数 {usable}"

    def test_english_readme_numbers_match_catalog(self):
        """README.en.md 用英文计算方式（sources / usable with no key / domains），
        正则不同于中文，历史上因此漏网。两处写法都要覆盖：
        要点列表 `**N sources (M usable with no key), K domains**`
        与正文 `**N** sources (**M** usable with no key) and **K** domains`。
        """
        total, usable = _catalog_counts()
        domains = str(len(_domains()))
        text = (SKILL_DIR / "README.en.md").read_text(encoding="utf-8")
        bullet = re.search(
            rf"\*\*{total} sources \({usable} usable with no key\), {domains} domains\*\*",
            text)
        prose = re.search(
            rf"\*\*{total}\*\* sources \(\*\*{usable}\*\* usable with no key\) "
            rf"and \*\*{domains}\*\* domains", text)
        assert bullet, f"README.en.md 要点列表口径不符（应 {total}/{usable}/{domains}）"
        assert prose, f"README.en.md 正文口径不符（应 {total}/{usable}/{domains}）"

    def test_translated_readmes_numbers_match_catalog(self):
        """es/ja/ko 的正文数字必须与搜索源文档一致（与中/英一致）。

        此前检查只覆盖中文（test_engine_counts_agree_with_catalog，第 184 行
        写死 ("SKILL.md","README.md")）与英文：es/ja/ko 的 220 fuentes /
        89 dominios / 185 sin clave 三个数字全错，而同页徽章（受 test_badge_
        numbers_match_truth 管）写的是正确的——检查全部通过，漂移只能靠人工发现。
        语言变体是同一份对外承诺，必须同源同校。

        反向检查用「数字 + 量词」的形式（如 `220 fuentes`）而非单个数字：
        历史条目里的 `218 → 220 fuentes`（发布说明）是当时的真实数字，
        必须留着，不能被误伤。
        """
        total, usable = _catalog_counts()
        domains = str(len(_domains()))
        patterns = {
            "README.es.md": (f"{total} fuentes", f"{usable} sin clave",
                             f"{domains} dominios"),
            "README.ja.md": (f"{total} ソース", f"{usable} 無設定",
                             f"{domains} ドメイン"),
            "README.ko.md": (f"{total} 소스", f"{usable} 무설정",
                             f"{domains} 도메인"),
        }
        stale_forms = {
            "README.es.md": ("220 fuentes", "185 sin clave", "89 dominios"),
            "README.ja.md": ("220 ソース", "185 無設定", "89 ドメイン"),
            "README.ko.md": ("220 소스", "185 무설정", "89 도메인"),
        }
        problems = []
        for rel, pats in patterns.items():
            text = (SKILL_DIR / rel).read_text(encoding="utf-8")
            for pat in pats:
                if pat not in text:
                    problems.append(f"{rel}: 未写当前数字「{pat}」")
            for line in text.splitlines():
                # 历史条目里的数字是当时的真实值，必须留着：`168 → 218 fuentes`
                # （演变写法）与 `| **v2.8.7** | ... 89 dominios`（发布日志行）
                # 都不算「现状声称」，跳过。只拦不看上下文的现状行。
                if "→" in line or re.match(r"\s*\|?\s*\*\*v2\.\d", line):
                    continue
                for old in stale_forms[rel]:
                    if old in line:
                        problems.append(f"{rel}: 现状描述里仍是过时的数字「{old}」")
        assert not problems, "翻译版 README 数字不一致：\n  " + "\n  ".join(problems)

    def test_badge_numbers_match_truth(self):
        """README 徽章数字必须与来源一致（中英之外的四语种此前只写 150+）。"""
        from mcp_tools import TOOLS
        doc = (SKILL_DIR / "docs" / "ENGINE_CATALOG.md").read_text(encoding="utf-8")
        total = re.search(r"收录 (\d+) 个源", doc).group(1)
        version = _skill_version()
        tools = len(TOOLS)
        problems = []
        for rel in self.README_VARIANTS:
            text = (SKILL_DIR / rel).read_text(encoding="utf-8")
            if f"badge/engines-{total}-orange" not in text:
                problems.append(f"{rel}: engines 徽章应为 {total}")
            if f"badge/version-{version}-informational" not in text:
                problems.append(f"{rel}: version 徽章应为 {version}")
            if f"badge/MCP-{tools}%20tools-purple" not in text:
                problems.append(f"{rel}: MCP 徽章应为 {tools} tools")
        assert not problems, "README 徽章与真源不一致：\n  " + "\n  ".join(problems)

    def test_mcp_tool_count_matches_docs(self):
        from mcp_tools import TOOLS
        n = len(TOOLS)
        for rel in ("package.json", "cordis.patch.yml", "SKILL.md",
                    "packages/dsh-plugin/package.json"):
            text = (SKILL_DIR / rel).read_text(encoding="utf-8")
            claimed = re.findall(r"(\d+)\s*个?\s*MCP\s*工具", text)
            for c in claimed:
                assert int(c) == n, f"{rel} 写 {c} 个 MCP 工具，实际 {n}"

    def test_package_description_source_count(self):
        """package.json 的 description 是对外第一句承诺，此前写 175 个源。"""
        doc = (SKILL_DIR / "docs" / "ENGINE_CATALOG.md").read_text(encoding="utf-8")
        total = re.search(r"收录 (\d+) 个源", doc).group(1)
        usable = re.search(r"开箱可用 (\d+) 个", doc).group(1)
        desc = json.loads((SKILL_DIR / "package.json").read_text(
            encoding="utf-8"))["description"]
        assert f"{total} 个源" in desc, f"package.json description 未写 {total} 个源"
        assert usable in desc, f"package.json description 未写免密钥数 {usable}"

    def test_version_strings_agree(self):
        pkg = json.loads((SKILL_DIR / "package.json").read_text(encoding="utf-8"))
        plug = json.loads(
            (SKILL_DIR / "packages/dsh-plugin/package.json").read_text(encoding="utf-8"))
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r"^version:\s*(\S+)", skill, re.M)
        assert m, "SKILL.md 缺 version"
        assert pkg["version"] == m.group(1) == plug["version"], \
            f"版本不一致: package={pkg['version']} skill={m.group(1)} plugin={plug['version']}"
        # mcp_transport.ARGO_MCP_VERSION 自称版本来源之一，此前发布升版漏改
        # （2.8.8 发布时仍停在 2.8.6）——纳入对账，正则读取避免 import 副作用
        mt = (SKILL_DIR / "scripts/mcp_transport.py").read_text(encoding="utf-8")
        mm = re.search(r'^ARGO_MCP_VERSION\s*=\s*"(\S+)"', mt, re.M)
        assert mm, "mcp_transport.py 缺 ARGO_MCP_VERSION"
        assert mm.group(1) == pkg["version"], \
            f"版本不一致: mcp_transport={mm.group(1)} package={pkg['version']}"
        # 第五面：DSH 插件在 MCP 握手时自报的 clientInfo.version。
        # 此前是硬编码 '2.8.5'，而检查只对账上面四处，于是一路漂到 2.8.8
        # 都没人发现——向 argo server 报了不存在的客户端版本。现在插件从自己的
        # package.json 读（见 dsh/index.js 的 PLUGIN_VERSION），这里确保它
        # **不再出现硬编码版本字面量**：写法一旦回退，立刻报红。
        plugin_js = (SKILL_DIR / "packages" / "dsh-plugin" / "dsh" / "index.js"
                     ).read_text(encoding="utf-8")
        assert "PLUGIN_VERSION" in plugin_js, \
            "插件未使用 PLUGIN_VERSION（版本可能又被写死）"
        hardcoded = re.search(
            r"clientInfo\s*:\s*\{[^}]*version\s*:\s*['\"]([^'\"]+)['\"]", plugin_js)
        assert hardcoded is None, (
            "插件的 clientInfo.version 又写死成字面量"
            f"（{hardcoded.group(1) if hardcoded else ''}）——请用 PLUGIN_VERSION，"
            "否则发布升版时会静默漂移")# ── 4. 派生件与运行时真源一致 ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sb():
    """sync_backends 模块：派生件的唯一生成者，检查直接用它的校验器。"""
    import sync_backends
    return sync_backends


class TestDerivedArtifactsInSync:
    """registry / quota / domain 三份派生件必须覆盖运行时可见的全部引擎。

    守的是「人工改文档件、来源没跟上」：新增引擎只写进 registry（死文档），
    配额与领域画像漏侧，且 --check 自比永远绿。
    """

    def test_no_drift_between_disk_and_truth(self, sb):
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        registry = sb._load_yaml(sb.REGISTRY_PATH)
        issues = sb.collect_issues(engines, quota, registry, domain)
        assert not issues, "派生件与真源不一致：\n  - " + "\n  - ".join(issues)

    def test_check_mode_is_not_tautological(self, sb, tmp_path, monkeypatch):
        """--check 必须能失败：把 registry 改坏后应报错，而不是照绿。"""
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        broken = sb._load_yaml(sb.REGISTRY_PATH)
        broken["engines"] = [e for e in broken["engines"]
                             if e["name"] != sorted(engines)[0]]
        issues = sb.collect_issues(engines, quota, broken, domain)
        assert issues, "缺失引擎未被告警——校验退化成自比"

    def test_value_tamper_is_detected(self, sb):
        """名字都在、值被改也必须报——限流/配额就是靠这些值生效的。

        （第一版校验只比引擎名，把 firecrawl.qps 改成 99 直接漏报。）
        """
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        registry = sb._load_yaml(sb.REGISTRY_PATH)
        victim = "firecrawl"
        assert victim in quota, "取样引擎不存在，检查前置条件"
        quota[victim]["qps"] = 99
        issues = sb.collect_issues(engines, quota, registry, domain)
        assert any(victim in i and "qps" in i for i in issues), \
            f"值级篡改未报: {issues[:3]}"

    def test_registry_field_tamper_is_detected(self, sb):
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        registry = sb._load_yaml(sb.REGISTRY_PATH)
        for e in registry["engines"]:
            if e["name"] == "nvd":
                e["coverage"] = ["general"]
        issues = sb.collect_issues(engines, quota, registry, domain)
        assert any("nvd" in i and "coverage" in i for i in issues), \
            f"registry 字段篡改未报: {issues[:3]}"

    def test_declared_engine_metadata_reaches_quota(self, sb):
        """spec 里声明的限流/配额必须出现在派生件里（firecrawl 曾整体漏侧）。"""
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        declared_specs = 0
        for name, spec in engines.items():
            if "qps" in spec or "limit" in spec:
                declared_specs += 1
                assert name in quota, f"{name} 声明了配额但派生件缺失"
                for key in ("qps", "limit", "period"):
                    if key in spec:
                        assert quota[name][key] == spec[key], \
                            f"{name}.{key}: spec={spec[key]} quota={quota[name][key]}"
        assert declared_specs > 0, "没有任何引擎声明配额，检查取样逻辑"


# ── 5. 失败归因唯一来源 ──────────────────────────────────────────────────────

class TestFailureAttributionSingleSource:
    def test_register_and_classifier_agree_on_all_status_codes(self):
        import engine_failure as ef
        import engines_base as eb
        for code in list(range(0, 600)):
            eb._FAIL_NOTES.clear()
            eb._note_http_failure("t", code, "")
            note = eb._FAIL_NOTES.get("t")
            assert note is not None, f"HTTP {code} 未写入归因"
            expected = ef.classify(status_code=code)["category"]
            assert note["category"] == expected, \
                f"HTTP {code}: 寄存器={note['category']} classify={expected}"

    def test_transient_statuses_do_not_claim_upstream(self):
        """503/408/0 不是「上游改版」，给错方向比不给更坏。"""
        import engine_failure as ef
        assert ef.classify(status_code=503)["category"] == ef.RATE_LIMITED
        assert ef.classify(status_code=408)["category"] == ef.NETWORK
        assert ef.classify(status_code=0)["category"] == ef.NETWORK


# ── 6. 同批次功能不得互相打架 ────────────────────────────────────────────────

class TestLoginWallExemption:
    def test_single_tweet_url_is_not_needs_auth(self):
        """syndication 免登录通道能抓的 URL，预检不该说「需要登录」。"""
        from batch_probe import classify_url, NEEDS_AUTH, UNKNOWN
        assert classify_url(
            "https://x.com/elonmusk/status/1585841080431321088") == UNKNOWN
        assert classify_url(
            "https://twitter.com/a/statuses/1585841080431321088?s=20") == UNKNOWN

    def test_non_tweet_urls_still_flagged(self):
        from batch_probe import classify_url, NEEDS_AUTH
        for u in ("https://x.com/someuser",
                  "https://www.instagram.com/p/abc",
                  "https://www.facebook.com/groups/x"):
            assert classify_url(u) == NEEDS_AUTH, u


# ── 7. syndication 通道契约 ──────────────────────────────────────────────────

class TestSyndicationChannel:
    def test_token_nonempty(self):
        """端点要求 token 参数存在（省略则返回空）；值不参与校验。"""
        from engines_builders_intl import syndication_token
        for tid in ("1585841080431321088", "100000000000000"):
            tok = syndication_token(tid)
            assert isinstance(tok, str) and tok.strip()


# ── 8. 按量计费的源不得被 n 桶化放大 ─────────────────────────────────────────

class TestMeteredSourceBilling:
    """n 桶化只为「免费档」服务；计费档被放大等于直接放大账单。

    实测（2026-09-12 修前）：成本表缺 api 档分支 → fallthrough 到 1.0 →
    exa/octen/you/parallel/zhihu_global/tavily 这类按量计费的源被当成免费，
    请求 5 条被放大到 10 条。
    """

    def test_only_free_tier_gets_bucketed(self):
        from config import cost_tier_of, get_cost_tiers
        from engines import bucket_n
        tiers = get_cost_tiers()
        free = sorted(tiers.get("free") or [])
        metered = sorted((tiers.get("low") or []) + (tiers.get("api") or [])
                         + (tiers.get("paid") or []))
        assert free and metered, "取样失败：档位表为空"
        assert bucket_n(free[0], 5) >= 5          # 免费档允许向上取桶
        for name in metered[:8]:
            assert bucket_n(name, 5) == 5, \
                f"{name}（{cost_tier_of(name)} 档）被桶化放大——按量计费不得放大"

    def test_api_tier_is_not_treated_as_free(self):
        from config import cost_tier_of
        from engines import _free_engine
        checked = 0
        for name in ("exa", "octen", "tavily", "you", "parallel", "zhihu_global"):
            if cost_tier_of(name) != "api":
                continue
            checked += 1
            assert not _free_engine(name), f"{name} 是 api 档却被当成免费源"
        assert checked, "没有任何 api 档引擎可校验，检查档位声明"


class TestMcpSurfaceConsistency(unittest.TestCase):
    """CLI ↔ MCP 能力面一致性门禁（2026-09-26 起为硬不变量）。

    此前的漂移形态：CLI 先有 cite，MCP 面落后；「14」在 9 处文案/门禁各写一份。
    锁两件事：①CLI 能力命令与 argo_* 工具一一映射；②文案里的工具计数必须等
    于 len(TOOLS)（计数唯一事实在 mcp_tools.TOOLS）。
    """

    CLI_COMMAND_TO_TOOL = {
        "search": "argo_search", "research": "argo_research", "fetch": "argo_fetch",
        "crawl": "argo_crawl", "extract": "argo_extract", "article": "argo_article",
        "job": "argo_job", "evidence": "argo_evidence", "clarify": "argo_clarify",
        "preflight": "argo_preflight", "answer": "argo_answer", "watch": "argo_watch",
        "cite": "argo_cite",
    }

    def test_cli_capabilities_have_tools(self):
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        from mcp_tools import TOOLS
        names = {x["name"] for x in TOOLS}
        missing = {cmd: tool for cmd, tool in self.CLI_COMMAND_TO_TOOL.items()
                   if tool not in names}
        self.assertEqual(missing, {},
                         f"CLI 有此能力但 MCP 面缺工具，补 schema 到 mcp_tools.py：{missing}")

    def test_every_tool_has_handler(self):
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        from mcp_tools import TOOLS
        import mcp_handlers
        unknown = []
        for tool in TOOLS:
            result = mcp_handlers.execute_tool(tool["name"], {})
            blob = json.dumps(result, ensure_ascii=False)
            if "Unknown tool" in blob:
                unknown.append(tool["name"])
        self.assertEqual(unknown, [],
                         f"schema 有但 execute_tool 无分支（调到 Unknown tool）：{unknown}")

    def test_tool_count_in_copy_matches_tools(self):
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        from mcp_tools import TOOLS
        desc = json.loads((SKILL_DIR / "package.json").read_text(encoding="utf-8"))["description"]
        n = len(TOOLS)
        self.assertIn(f"{n} 个 MCP 工具", desc,
                      f"package.json description 未写 {n} 个 MCP 工具（计数唯一事实=TOOLS）")
