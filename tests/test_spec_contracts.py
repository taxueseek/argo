"""spec 契约 lint 门禁：真仓零违例 + 正向控制（复刻历史 bug 形状）。

三类历史事故的形状都钉在这里，防止 lint 被静默改弱：
  - bilibili/xhs 类：type cli + output_map 但不请求机器可读输出（C1）
  - 结构完整性：缺 cmd / 缺 engine_id（C2）
  - HN 类：url_template 兜底在 url 为空/非 http 时仍能接住（行为钉，fe7e2bf）
"""

from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lint_spec_contracts import lint_specs, load_specs  # noqa: E402


def _cli_spec(**over):
    base = {
        "engine_id": "synthetic_engine",
        "type": "cli",
        "cmd": ["some-cli"],
        "search_args": ["search", "{query}"],
    }
    base.update(over)
    return base


def test_real_specs_zero_violations():
    """真仓规格必须零违例——新增引擎撞上不变式时，门禁在这里红。"""
    violations = lint_specs(load_specs())
    assert violations == [], "\n".join(violations)


def test_c1_rich_output_map_requires_machine_readable():
    """bilibili 类正向控制：声明 output_map 却只要人读文本 → C1 违例。"""
    bad = _cli_spec(output_map={"title": "t", "social_meta": "meta"})
    v = lint_specs([("synthetic", "synthetic_engine", bad)])
    assert any(v_i.startswith("C1") for v_i in v), v


def test_c1_json_flag_and_output_format_both_satisfy():
    """--json 旗标或显式 output_format 任一即满足 C1，且纯文本 cli 不受约束。"""
    with_flag = _cli_spec(
        search_args=["search", "--json", "{query}"],
        output_map={"title": "t"},
    )
    assert lint_specs([("synthetic", "synthetic_engine", with_flag)]) == []
    with_fmt = _cli_spec(output_format="yaml", output_map={"title": "t"})
    assert lint_specs([("synthetic", "synthetic_engine", with_fmt)]) == []
    plain_text = _cli_spec()  # 无 output_map，人读输出合法
    assert lint_specs([("synthetic", "synthetic_engine", plain_text)]) == []


def test_c2_structural_invariants():
    """缺 cmd 的 cli 规格与缺 engine_id 的 spec 文件都必须被点名。"""
    no_cmd = _cli_spec(cmd=[])
    v = lint_specs([("synthetic", "synthetic_engine", no_cmd)])
    assert any(v_i.startswith("C2") and "cmd" in v_i for v_i in v)
    v2 = lint_specs([("specs/x.yaml", "", {"type": "http"})])
    assert any("engine_id" in v_i for v_i in v2)


def test_url_template_fallback_when_url_missing_or_non_http():
    """HN 类行为钉：url 缺失或非 http 时模板兜底；占位为空则不留脏 url。"""
    from engines_base import _make_field_parser

    p = _make_field_parser(
        "", {"title": "t"}, url_template="https://example.com/a/{objectID}"
    )
    out = p([{"t": "第一条", "objectID": "7"}, {"t": "非http", "url": "//cdn/x", "objectID": "9"}])
    assert [r["url"] for r in out] == ["https://example.com/a/7", "https://example.com/a/9"]
    out2 = p([{"t": "占位空", "objectID": ""}])
    assert out2 == [] or "url" not in out2[0]


def test_openalex_url_is_doi_not_internal_id():
    """openalex 的 item_url 必须映射 doi（真实可点击）而非内部 id（openalex.org/W…）。

    2026-11-20 审计：旧映射 item_url: id 把证据链接断在自家 ID 上；摘要位
    item_summary: publication_year 只剩一个数字。修复后 url=doi、摘要走
    item_summary_template（缺字段由模板折叠分隔符）。用合成 fixture 钉行为，
    不依赖上游网络。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from engines_parsers import make_field_parser

    om = next(s for _, eid, s in load_specs() if eid == "openalex")["output_map"]
    assert om["item_url"] == "doi", "item_url 必须是 doi（真实链接），不得回退到内部 id"
    assert "item_summary_template" in om, "摘要位须走模板（年份·期刊），不得只有 publication_year"

    p = make_field_parser(
        om["items"], {"title": om.get("item_title", "title"), "url": om["item_url"]},
        templates={k: om[k] for k in om if k.endswith("_template")})
    data = {"results": [
        {"title": "A", "doi": "https://doi.org/10.1/a", "publication_year": 2021,
         "primary_location": {"source": {"display_name": "arXiv"}}},
        {"title": "B", "doi": None, "publication_year": 2022, "primary_location": None},
    ]}
    rows = p(data)
    assert rows[0]["url"] == "https://doi.org/10.1/a"
    assert rows[0]["snippet"] == "2021 · arXiv"
    assert rows[1]["snippet"] == "2022", "缺期刊名时模板须折叠成分隔符只剩年份"
    assert "url" not in rows[1], "doi 为空时不得造半截链接"
