---
name: argo-deep-research
description: Deep research dossier, multi-source fact checking, and evidence verification for AI agents using Argo (research, evidence, --verify, --numverify). (深度研究取证包与多源事实核验)
version: 2.14.1
---

# Argo Deep Research & Fact Verification

Use this skill when a question requires multi-source evidence gathering, academic literature synthesis, or strict fact/number verification rather than a single web search.

## Core Workflow

1. **Generate a research dossier** (sources, coverage, gaps, quality gates):
   ```bash
   python3 scripts/research.py "<topic>" --depth deep --json
   ```
   Or pass structured work packages:
   ```bash
   python3 scripts/research.py "<topic>" --work-packages '<json-or-path>' --verify 3 --json
   ```
2. **Verify high-stakes facts & numbers** directly from full source pages:
   ```bash
   argo search "<query>" --verify 3 --numverify 2 --json --fields agent
   ```
3. **Academic deep dive** (arXiv, Semantic Scholar, OpenAlex, Crossref, PubMed; disables early-stop):
   ```bash
   argo search "<topic>" --academic-deep --json --fields agent
   ```
4. **Score credibility independently**:
   ```bash
   echo '{"results": [...]}' | python3 scripts/evidence.py "<query>" --stdin --json --high-stakes
   ```

## Discipline

- Never treat SERP redirect URLs (`baidu/s`, `sogou/link`) or social posts as primary factual truth.
- When `fetch_required=true` or `quality_gate_results.passed=false`, downgrade claims and cite exact source URLs.
- See `references/research-protocol.md` for full dossier and work-package schemas.
