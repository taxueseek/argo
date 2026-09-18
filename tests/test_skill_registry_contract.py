#!/usr/bin/env python3
"""test_skill_registry_contract.py — 技能目录源的三条契约。

背景（2026-09-18）：对照 MCP 官方 Skills over MCP 扩展（SEP-2640）审查本仓的
技能检索面，发现三个可吸纳的点落在这里——

  1. **可验证出处**：技能目录返回的是市场页（skillUrl/canonicalUrl），要再开一次
     浏览器才知道搜到的是什么。上游仓目录（SkillsMP 的 githubUrl）与安装引用
     （ClawHub 的 install.reference）才是能核对、能安装的地址，与市场页分开存。
  2. **更新时间**：两个源的 updatedAt 单位不同（SkillsMP 秒 / ClawHub 毫秒），
     差 1000 倍。原样映射等于交出一串数字，按量级归一才对得上。
  3. **局限声明**：市场页不是安装包。这条规则原先只写在 AGENTS.md 里，靠人记；
     现在随结果一起到达调用方。

另有一条防漂移门禁：技能目录源的清单（SKILL_REGISTRY_ENGINES）必须与
config.yaml / engines/specs 里声明 `coverage: skill` 的引擎**完全一致**。
常量是性能选择（输出路径不解析 130KB 的 config.yaml），一致性靠本文件断言，
不靠人记得同步。
"""

import glob
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
ARGO_DIR = SCRIPT_DIR.parent
SCRIPTS_DIR = ARGO_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_spec(name: str) -> dict:
    from yaml_load import load
    return load(str(ARGO_DIR / "engines" / "specs" / f"{name}.yaml")) or {}


def _declared_skill_engines() -> set[str]:
    """config.yaml + engines/specs/*.yaml 里声明 coverage 含 skill 的引擎。"""
    from yaml_load import load
    found: set[str] = set()
    cfg = load(str(ARGO_DIR / "config.yaml")) or {}
    for name, spec in (cfg.get("engines") or {}).items():
        if isinstance(spec, dict) and "skill" in (spec.get("coverage") or []):
            found.add(name)
    for path in glob.glob(str(ARGO_DIR / "engines" / "specs" / "*.yaml")):
        spec = load(path) or {}
        if "skill" in (spec.get("coverage") or []):
            found.add(spec.get("engine_id") or Path(path).stem)
    return found


class TestSkillRegistryListMatchesSpecs:
    def test_constant_matches_declared_coverage(self):
        """防漂移：常量与 spec 声明必须一致，新增技能目录源时本用例会红。"""
        from candidate_envelope import SKILL_REGISTRY_ENGINES
        declared = _declared_skill_engines()
        assert declared == set(SKILL_REGISTRY_ENGINES), (
            f"spec 声明 {sorted(declared)}，常量 {sorted(SKILL_REGISTRY_ENGINES)}——"
            "新增/删除技能目录源时两边要一起改"
        )

    def test_constant_is_not_empty(self):
        from candidate_envelope import SKILL_REGISTRY_ENGINES
        assert len(SKILL_REGISTRY_ENGINES) >= 3


class TestEpochNormalization:
    def test_seconds_and_millis_land_on_the_same_day(self):
        """同一个时刻的两种单位必须归一成同一天，差 1000 倍不能被放过。"""
        from engines_base import _normalize_epoch
        seconds = 1786285295            # SkillsMP 口径
        millis = seconds * 1000         # ClawHub 口径
        assert _normalize_epoch(seconds) == _normalize_epoch(millis)

    @pytest.mark.parametrize("value,expected", [
        (1786285295, "2026-08-09"),
        (1789594554485, "2026-09-16"),
    ])
    def test_known_timestamps(self, value, expected):
        from engines_base import _normalize_epoch
        assert _normalize_epoch(value) == expected

    @pytest.mark.parametrize("value", ["2026-07-26", "2026-07-26T10:00:00Z"])
    def test_non_epoch_is_passed_through(self, value):
        """别的源已经在给 ISO 日期，不能被二次解释。"""
        from engines_base import _normalize_epoch
        assert _normalize_epoch(value) == value

    @pytest.mark.parametrize("value", ["", "abc", "12345", "0"])
    def test_out_of_range_is_not_guessed(self, value):
        """范围外不猜——猜错会把可疑值变成看起来正常的值，比留着更糟。"""
        from engines_base import _normalize_epoch
        assert _normalize_epoch(value) == value

    def test_negative_never_raises(self):
        from engines_base import _normalize_epoch
        assert _normalize_epoch(-5) == "-5"


class TestSpecWiring:
    @pytest.mark.parametrize("engine", ["skillsmp", "clawhub"])
    def test_spec_declares_upstream_and_published_at(self, engine):
        om = _load_spec(engine).get("output_map") or {}
        assert om.get("item_upstream"), f"{engine} 未声明 item_upstream"
        assert om.get("item_published_at") == "updatedAt", f"{engine} 未声明 item_published_at"

    def test_skillsmp_payload_yields_verifiable_upstream(self):
        """端到端：真实 spec + 真实响应形态 → upstream 是上游仓目录。"""
        from engines_base import _parse_http_payload
        spec = _load_spec("skillsmp")
        raw = json.dumps({
            "success": True,
            "data": {"skills": [{
                "id": "x", "name": "unified-memory", "author": "affaan-m",
                "description": "跨会话记忆", "contentLanguage": "en",
                "githubUrl": "https://github.com/affaan-m/ECC/tree/main/.agents/skills/unified-memory",
                "skillUrl": "https://skillsmp.com/creators/affaan-m/ecc/agents-skills-unified-memory",
                "stars": 389030, "updatedAt": 1786285295,
            }]},
        })
        items = _parse_http_payload(raw, "json", "skillsmp", 5,
                                    spec["output_map"], spec)
        assert len(items) == 1
        assert items[0]["upstream"].startswith("https://github.com/")
        assert items[0]["published_at"] == "2026-08-09"
        # 市场页仍在 url，两者不混为一谈
        assert items[0]["url"].startswith("https://skillsmp.com/")

    def test_clawhub_nested_upstream_and_millis(self):
        """端到端：嵌套字段 install.reference + 毫秒时间戳。"""
        from engines_base import _parse_http_payload
        spec = _load_spec("clawhub")
        raw = json.dumps({"results": [{
            "displayName": "Neural Memory", "canonicalUrl": "/nhadaututtheky/neural-memory",
            "summary": "记忆插件", "downloads": 40895,
            "install": {"kind": "clawhub", "reference": "nhadaututtheky/neural-memory"},
            "updatedAt": 1789594554485,
        }]})
        items = _parse_http_payload(raw, "json", "clawhub", 5,
                                    spec["output_map"], spec)
        assert items[0]["upstream"] == "nhadaututtheky/neural-memory"
        assert items[0]["published_at"] == "2026-09-16"


class TestSkillRegistryLimitation:
    def test_declared_for_skill_engines(self):
        from candidate_envelope import SKILL_REGISTRY_LIMITATION, build_limitations
        lims = build_limitations({"engines_used": ["skillsmp"], "engine": "skillsmp"})
        assert SKILL_REGISTRY_LIMITATION in lims

    @pytest.mark.parametrize("engine", ["redskill", "clawhub"])
    def test_declared_for_every_skill_engine(self, engine):
        from candidate_envelope import SKILL_REGISTRY_LIMITATION, build_limitations
        lims = build_limitations({"engines_used": [engine], "engine": engine})
        assert SKILL_REGISTRY_LIMITATION in lims

    def test_absent_for_non_skill_engines(self):
        """负向：全网搜索不该背上「这是市场页」的提示。"""
        from candidate_envelope import SKILL_REGISTRY_LIMITATION, build_limitations
        lims = build_limitations({"engines_used": ["duckduckgo"], "engine": "duckduckgo"})
        assert SKILL_REGISTRY_LIMITATION not in lims

    def test_combo_only_engine_does_not_trigger_it(self):
        """负向：combo 里挂了但没跑出结果的技能源，不该让提示出现。

        否则「本次结果来自市场页」就成了假陈述——判据是 engines_used，不是 combo。
        """
        from candidate_envelope import SKILL_REGISTRY_LIMITATION, build_limitations
        lims = build_limitations({
            "engines_used": ["duckduckgo"],
            "engine": "duckduckgo",
            "engines_combo": ["duckduckgo", "skillsmp"],
        })
        assert SKILL_REGISTRY_LIMITATION not in lims


class TestAgentViewKeepsUpstream:
    def test_upstream_survives_the_agent_projection(self):
        """--fields agent 的字段白名单必须带上 upstream，否则默认档看不到它。"""
        import search
        assert "upstream" in search._AGENT_RESULT_FIELDS

    def test_strip_for_agent_keeps_upstream(self):
        import search
        payload = {
            "query": "q", "count": 1,
            "results": [{"title": "t", "url": "u", "upstream": "a/b",
                         "_elapsed": 1.0, "_engine": "clawhub"}],
        }
        out = search._strip_for_agent(payload)
        assert out["results"][0]["upstream"] == "a/b"
        assert "_elapsed" not in out["results"][0]
