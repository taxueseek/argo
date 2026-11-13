<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo: unified search and evidence verification for AI agents across hundreds of sources and 18 languages">
</p>

<p align="center">
  <strong>English</strong> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.es.md">Español</a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#why-argo">Why Argo</a> ·
  <a href="#query-shaped-routing">Routing Proof</a> ·
  <a href="#how-it-works">How It Works</a> ·
  <a href="#ai-search--sub-skills">AI Search &amp; Sub-skills</a> ·
  <a href="#dsh-plugin">DSH Plugin</a> ·
  <a href="#docs--changelog">Docs</a>
</p>

<p align="center">
  <a href="https://github.com/taxueseek/argo/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/taxueseek/argo?style=social"></a>
  <a href="https://www.npmjs.com/package/argo-search"><img alt="npm downloads" src="https://img.shields.io/npm/dm/argo-search?color=cb3837&label=npm%20downloads"></a>
  <a href="https://skills.sh/taxueseek/argo"><img alt="skills.sh" src="https://img.shields.io/badge/skills.sh-argo-black"></a>
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.14.1-informational">
  <img alt="engines" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Ftaxueseek%2Fargo%2Fmain%2Fassets%2Freadme%2Fcounts.json">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **Part of the taxueseek skill series** (search &amp; deep research): [plainspoken-writing](https://github.com/taxueseek/plainspoken-writing) 75★ (AI-tone removal) · [session-digger](https://github.com/taxueseek/session-digger) 34★ (cross-environment session mining) · [taxue-invest](https://github.com/taxueseek/taxue-invest) 30★ (investment analysis) · [taxueskills](https://github.com/taxueseek/taxueskills) 25★ (decision system) · [taxue-creative-style](https://github.com/taxueseek/taxue-creative-style) 19★ (creative illustration styles) · [ime-lexicon](https://github.com/taxueseek/ime-lexicon) 15★ (IME lexicon toolkit).
>
> **DeepSeek Harness (DSH) plugin ecosystem**: [dsh-files](https://github.com/taxueseek/dsh-files) 43★ (folder upload + document parsing) · [plugin-fast-adapt](https://github.com/taxueseek/plugin-fast-adapt) 15★ (plugin upgrade adapter) · [taxue-dsh-artisan](https://github.com/taxueseek/taxue-dsh-artisan) 3★ (visual creation) · [dsh-plugin-guard](https://github.com/taxueseek/dsh-plugin-guard) 2★ (pre-install audit).

**Argo** is an open-source metasearch, web-fetch, and evidence-verification engine that lives inside your AI agent. It routes queries across **hundreds of sources** in **18 languages** (web, academic, finance, code, local files, and leading **AI search engines**), falls back to **keyless &amp; local engines** when API keys are absent, and verifies facts from full page text instead of trusting search snippets.

---

## Quick Start

Zero-config works out of the box (API keys are optional). Pick your client:

- **Claude Code (MCP)**:
  ```bash
  claude mcp add argo -- npx -y argo-search
  ```
- **Agent Skill (`skills.sh` — Claude Code, Cursor, Codex, Antigravity)**:
  ```bash
  npx skills add taxueseek/argo
  # Or install all scenario skills (argo, argo-deep-research, argo-local-search):
  npx skills add taxueseek/argo --full-depth --all
  ```
- **Cursor / Windsurf / LibreChat (`mcpServers` JSON)**:
  ```json
  {
    "mcpServers": {
      "argo": { "command": "npx", "args": ["-y", "argo-search"] }
    }
  }
  ```
- **Local CLI (recommended for long-term local use)**:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
  # or via npm: npm install -g argo-search
  ```
- **DeepSeek Harness (DSH) Plugin**:
  ```bash
  dsh plugin --profile web add argo-search
  ```

<details>
<summary>Verify installation, requirements &amp; optional API keys</summary>

```bash
argo search "python asyncio error handling" --verify 2 --json --fields agent
argo search --list-engines
```

- Requires **Python 3.9+** (`pip install pyyaml`) and **Node.js 18+** (for `npx`).
- Optional keys (`DEEPSEEK_API_KEY`, `ARGO_YUANBAO_API_KEY`, `ARGO_TAVILY_API_KEY`, `EXA_API_KEY`, `ARGO_METASO_API_KEY`, `ARGO_BOCHA_API_KEY`) improve quality when present and are skipped automatically when missing. See [docs/MCP_SETUP.md](docs/MCP_SETUP.md) and [docs/ENGINE_CATALOG.md](docs/ENGINE_CATALOG.md).

</details>

---

## Why Argo

<p align="center">
  <img src="assets/readme/why-better.svg" width="100%" alt="Why Argo outperforms built-in search, single AI search APIs, and generic metasearch">
</p>

| Capability | Built-in Web Search | Commercial Search APIs (Tavily / Exa / Serper) | Argo |
| :--- | :--- | :--- | :--- |
| **Pricing &amp; Keys** | Tied to host quota | Paid per call; API key required | **Free &amp; keyless fallback** (optional keys boost quality) |
| **AI Search Aggregation** | Single host backend | Single vendor index | **Unifies keyless (`tavily_free`, `felo`, `parallel_free`) + BYOK (DeepSeek Search, Tencent Yuanbao/Hunyuan, Metaso, Bocha, Tavily, Exa)** |
| **Source Routing** | Single generic index | Generic web index | **Domain + 18-language routing** across academic, finance, code, news &amp; local files |
| **Fact Verification** | Snippet only | Snippet or raw chunks | **Built-in `--verify` &amp; `--numverify`** (fetches source &amp; scores credibility) |
| **Context Efficiency** | Verbose HTML/JSON | Raw chunks | **Agent-compact output** (`--fields agent` ~4.3 KB) |

---

## Query-Shaped Routing

<p align="center">
  <img src="assets/readme/proof-routes.svg" width="100%" alt="Argo query-shaped routing across finance, academic, code, and general domains">
</p>

Argo classifies each query by **domain and language** before dispatching, hitting first-party sources directly and triggering **early-stop** once consensus is reached:

- **Academic** → `arxiv`, `semantic_scholar`, `openalex`, `crossref`, `pubmed` (`--academic-deep`)
- **Finance &amp; Macro** → `sec_edgar`, `yahoo_finance`, `sina_quote`, `eastmoney`
- **Code &amp; Tech** → `stackoverflow`, `github`, `octen`, `context7`
- **AI Search** → `tavily_free`, `felo`, `parallel_free`, `anysearch`, `deepseek_search`, `yuanbao`, `metaso`, `bocha`, `exa`, `tavily`, `you`
- **Deep Research** → `python3 scripts/research.py "topic" --depth deep --json` outputs a structured **dossier** (sources, coverage gaps, quality gates)

---

## How It Works

<p align="center">
  <img src="assets/readme/workflow.svg" width="100%" alt="Argo retrieval, RRF fusion, fetch degradation chain, and evidence scoring pipeline">
</p>

### Evidence Scoring (Selection × Absorption)

Search engines optimize for clicks; agents need **verifiable facts**. Argo scores every candidate along two orthogonal axes:
- **Selection**: domain authority, consensus across engines (`RRF`), freshness, and SERP-shell rejection (blocks `baidu/s` and `sogou/link` redirect stubs from posing as primary sources).
- **Absorption**: actual readable density after fetching the full page (`--verify N`) and numerical cross-checking (`--numverify N` for prices, financials, and dates).

### Budget Modes (`--mode`)

| Mode | Behavior | Best for |
| :--- | :--- | :--- |
| **`fast`** | Free + local `local_*` engines only, zero API spend | High-frequency agent lookups |
| **`auto`** | Cost-aware default: free first, escalates to keyed sources when needed | Daily coding &amp; general research |
| **`deep`** | Broad multi-source fan-out, disables early-stop, pairs with `--verify` | High-stakes finance, legal, academic dossiers |
| **`budget`** | Strict daily/monthly quota caps with local fallback | Unattended batch &amp; cron workflows |

---

## AI Search &amp; Sub-skills

### Sub-skills (4 Built-in Pipelines)

| Sub-skill | Capability |
| :--- | :--- |
| **local-search** | Local file search + 32 zero-cost local aggregation engines (`--local-first`) |
| **local-seek** | Hybrid BM25 + vector semantic search for local notes, markdown, and knowledge bases |
| **ego-search** | Real-browser logged-in search for Zhihu, Xiaohongshu, WeChat Official Accounts (off by default) |
| **js-run** | Sub-millisecond headless JS execution for environment probes and pure-compute pages |

### 19 MCP Tools

`argo_search`, `argo_local_search`, `argo_local_read`, `argo_recompute`, `argo_research`, `argo_evidence`, `argo_clarify`, `argo_fetch`, `argo_crawl`, `argo_screenshot`, `argo_pdf`, `argo_social_search`, `argo_article`, `argo_job`, `argo_extract`, `argo_preflight`, `argo_answer`, `argo_watch`, `argo_cite`.

---

## DSH Plugin

Argo ships a first-class **DeepSeek Harness (DSH)** plugin (`@taxueseek/argo-dsh`) with three invocation channels from one capability set:
1. **19 `mcp__argo__*` tools** (main-package bundle, identical to full MCP).
2. **Search + `wide_research` parallel orchestration** (subpackage).
3. **Host `web_search` / `web_fetch` seam replacement** (requires explicit provider pin in the web profile; see [packages/dsh-plugin/README.md](packages/dsh-plugin/README.md)).

The built-in settings UI exposes **18 native first-class tools**, **28 curated engine switches**, and shares DSH's native `DEEPSEEK_API_KEY` credential store with zero plaintext echoing:

<p align="center">
  <img src="assets/readme/settings-tools.webp" width="32%" alt="DSH Tools settings tab">
  <img src="assets/readme/settings-engines.webp" width="32%" alt="DSH Engines settings tab">
  <img src="assets/readme/settings-keys.webp" width="32%" alt="DSH Keys settings tab">
</p>

---

## Docs &amp; Changelog

- [docs/ENGINE_CATALOG.md](docs/ENGINE_CATALOG.md) · [docs/MCP_SETUP.md](docs/MCP_SETUP.md) · [docs/ARGO_INTRO.md](docs/ARGO_INTRO.md) · [docs/为什么选择argo.md](docs/为什么选择argo.md) · [docs/CHANGELOG.md](docs/CHANGELOG.md)

| Version | Highlights |
| :--- | :--- |
| **v2.14.1** | Architecture slimming, MCP 3+1 orthogonal tools + backward-compat aliases, 304 sources, fail-closed `secret_guard.py` redaction |
| **v2.14.0** | Parser layer consolidation, 300 sources, badcase ledger loop |
| **v2.13.0** | Documentation consolidation, 22-state-machine SSOT, dispatch tail grace |

<details>
<summary>v2.14.1 Release Summary &amp; Capability Snapshot</summary>

### Capabilities (v2.14.1)

v2.14.1 slims the source architecture, converges MCP tools to 3+1 orthogonal core interfaces with backward-compatible aliases, expands keyless sources (Hugging Face Datasets, Maven Central, NuGet, Open-Meteo), and adds fail-closed `secret_guard.py` output redaction.

### v2.14.1: Architecture Slimming + MCP 3+1 Tools + Security Redaction

See [docs/RELEASE_NOTES_v2.14.1.md](docs/RELEASE_NOTES_v2.14.1.md) and [docs/CHANGELOG.md](docs/CHANGELOG.md).

</details>

## Contributing &amp; License

Issues and PRs are welcome (see [docs/ADDING_NEW_ENGINE.md](docs/ADDING_NEW_ENGINE.md) to add a source). Licensed under [Apache-2.0](LICENSE).
