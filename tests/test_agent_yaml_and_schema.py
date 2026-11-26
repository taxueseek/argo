"""tests/test_agent_yaml_and_schema.py — 方案一：YAML-Compact 输出与 --schema 自描述契约测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import agent_output  # noqa: E402
import search_cli  # noqa: E402
import search_output  # noqa: E402


def _sample_payload(*, with_local: bool = False) -> dict:
    web = [
        {
            "title": "Next.js Middleware: App Router Guide",
            "url": "https://nextjs.org/docs/app/building-your-application/routing/middleware",
            "snippet": "Middleware allows you to run code before a request is completed: redirect, rewrite, or add headers.",
            "source": "firecrawl",
            "score": 0.91,
            "published_at": "2026-10-12",
            "fetch_suggested": True,
        },
        {
            "title": "true",
            "url": "https://example.com/a?x=1&y=2#frag",
            "snippet": "Line 1: key: value\nLine 2: # comment",
            "source": "uapi",
            "score": 0.72,
        },
    ]
    local = (
        [{"title": "notes.md", "url": "file:///tmp/notes.md", "snippet": "local hit", "source": "local_files"}]
        if with_local
        else []
    )
    return {
        "query": "nextjs middleware",
        "engine": "auto",
        "engines_used": ["firecrawl", "uapi"],
        "domain": "english_tech",
        "count": len(web) + len(local),
        "mode": "auto",
        "depth": "fast",
        "status": "completed",
        "fetch_required": False,
        "errors": [],
        "failures": [],
        "timing": {
            "elapsed_ms": 420,
            "stages_ms": 420.5,
            "stages": [{"stage": "dispatch", "ms": 410.0, "pct": 97.5}],
            "dispatch": {"wall_ms": 410, "engines_run": 2},
        },
        "meta": {"uncertainty": {"level": "low"}},
        "content_trust": "untrusted",
        "local_results": local,
        "web_results": web,
        "facets": {"web_count": len(web), "local_count": len(local), "total_count": len(web) + len(local)},
        "results": local + web,
    }


def test_yaml_compact_roundtrip_and_dedup():
    raw = _sample_payload(with_local=False)
    agent_json = search_output._strip_for_agent(raw)
    compacted = agent_output.compact_for_yaml(agent_json)

    # 无本地命中时，重复的 web_results / 空 local_results / 空 errors / 冗余 facets 被安全省略
    assert "web_results" not in compacted
    assert "local_results" not in compacted
    assert "errors" not in compacted
    assert "facets" not in compacted
    assert compacted["timing"] == {"elapsed_ms": 420, "engines_run": 2}

    yaml_text = agent_output.dumps_yaml_compact(compacted)
    parsed = yaml.safe_load(yaml_text)
    assert isinstance(parsed, dict)
    assert parsed["query"] == "nextjs middleware"
    assert parsed["engines_used"] == ["firecrawl", "uapi"]
    assert len(parsed["results"]) == 2
    assert parsed["results"][1]["title"] == "true"
    assert "Line 2: # comment" in parsed["results"][1]["snippet"]

    # 字节数显著小于原始 --agent JSON
    json_bytes = len(json.dumps(agent_json, ensure_ascii=False).encode("utf-8"))
    yaml_bytes = len(yaml_text.encode("utf-8"))
    assert yaml_bytes < json_bytes * 0.65


def test_yaml_compact_preserves_facets_when_local_exists():
    raw = _sample_payload(with_local=True)
    agent_json = search_output._strip_for_agent(raw)
    compacted = agent_output.compact_for_yaml(agent_json)

    assert len(compacted["local_results"]) == 1
    assert len(compacted["web_results"]) == 2
    assert compacted["facets"]["local_count"] == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["search.py", "q", "--yaml", "--json"],
        ["search.py", "q", "--yaml", "--envelope"],
        ["search.py", "q", "--yaml", "--fields", "full"],
        ["search.py", "q", "--agent", "--yaml"],
        ["search.py", "q", "--yaml", "--format", "json"],
    ],
)
def test_yaml_mutex_fail_fast(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        search_cli.main()
    assert exc.value.code == 2


def test_schema_offline_json_and_yaml(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["search.py", "--schema"])
    search_cli.main()
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["command"] == "argo search <query>"
    assert "--yaml" in out_json["output_modes"]

    monkeypatch.setattr(sys, "argv", ["search.py", "--schema", "--yaml"])
    search_cli.main()
    out_yaml = yaml.safe_load(capsys.readouterr().out)
    assert out_yaml["command"] == "argo search <query>"
    assert "--yaml" in out_yaml["output_modes"]
