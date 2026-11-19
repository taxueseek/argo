#!/usr/bin/env python3
"""Unit tests for the declarative Helve search engine spec."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import engines_base  # noqa: E402


def _load_helve_spec() -> dict:
    spec_path = ROOT / "engines" / "specs" / "helve.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["_name"] = spec["engine_id"]
    return spec


def test_helve_spec_fields_and_env_passthrough():
    spec = _load_helve_spec()
    assert spec["engine_id"] == "helve"
    assert spec["type"] == "http"
    assert spec["url"] == "https://helve.dev/v1/search"
    assert "HELVE_API_KEY" in spec["required_env"]
    assert "HELVE_API_KEY" in spec["allowed_env"]

    passthrough = (ROOT / "backends" / "env_passthrough.txt").read_text(encoding="utf-8").splitlines()
    assert "HELVE_API_KEY" in passthrough


def test_helve_http_engine_request_and_output_mapping():
    spec = _load_helve_spec()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, **kwargs):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {
                "id": "srch_test",
                "provider": "auto",
                "results": [
                    {
                        "id": "res_1",
                        "title": "Example Result",
                        "url": "https://example.com/docs",
                        "snippets": ["First snippet.", "Extra snippet."],
                        "score_synthesized": False,
                    }
                ],
                "warnings": [],
                "usage": {"cost_usd": 0.005, "credits": 1},
                "latency_ms": 95,
            }
        ).encode("utf-8")
        resp.status = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    with patch.dict(os.environ, {"HELVE_API_KEY": "hlv_test_key"}, clear=False):
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fn = engines_base._build_http_engine(spec)
            rows = fn("climate change", n=7, timeout=10)

    assert captured["url"] == "https://helve.dev/v1/search"
    assert captured["headers"]["authorization"] == "Bearer hlv_test_key"
    assert captured["body"] == {"query": "climate change", "max_results": 7}
    assert len(rows) == 1
    assert rows[0]["title"] == "Example Result"
    assert rows[0]["url"] == "https://example.com/docs"
    assert rows[0]["snippet"] == "First snippet."
