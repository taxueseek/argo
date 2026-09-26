<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo: unified search and evidence verification for AI agents">
</p>

<p align="center">
  <a href="README.md">中文</a> ·
  <strong>English</strong> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.es.md">Español</a>
</p>

<p align="center">
  <a href="#what-it-is">Intro</a> ·
  <a href="#why-its-stronger-than-built-in-search--ai-search--metasearch">Compare</a> ·
  <a href="#query-shaped-routing">Proof</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#capabilities">Capabilities</a> ·
  <a href="#install--config">Config</a> ·
  <a href="#recent-updates">Recent updates</a>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.9.0-informational">
  <img alt="engines" src="https://img.shields.io/badge/engines-253-orange">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **Part of the taxueseek DeepSeek Harness plugin lineup** — siblings: [dsh-files](https://github.com/taxueseek/dsh-files) (send files, read documents) · [dsh-snippets](https://github.com/taxueseek/dsh-snippets) (snippet favorites) · [dsh-healthcheck](https://github.com/taxueseek/dsh-healthcheck) (read-only checkup) · [dsh-plugin-guard](https://github.com/taxueseek/dsh-plugin-guard) (plugin security audit) · [taxue-dsh-artisan](https://github.com/taxueseek/taxue-dsh-artisan) (prompt reverse-engineering & multi-provider image generation) — see all plugins on the [profile](https://github.com/taxueseek#deepseek-harness-plugins)

## Why it's stronger than built-in search / AI search / metasearch

> In short: the first three help **people** find information. Argo helps **agents** search and verify in one pipeline. The difference is the deliverable: a summary page or link list for humans vs ranked, reviewable, context-safe evidence for agents.

<p align="center">
  <img src="assets/readme/why-better.svg" width="100%" alt="Left: three default searches made for humans; right: Argo's absorbable evidence JSON for agents">
</p>

| Dimension | Model built-in search | AI search (summaries) | Metasearch / engines | **Argo** |
|-----------|----------------------|------------------------|----------------------|----------|
| Result shape | Stitched long text | Human summary page | SERP link list | **Compact JSON: evidence candidates + credibility breakdown** |
| Vertical questions (quotes / formulas) | Generic web | Generic web then summarize | Generic web | **Direct vertical sources, answer-shaped** |
| Evidence credibility | No score | No structured score | No score | **selection · absorption · freshness · consensus** |
| Repeat queries | Hit the network every time | Hit the network every time | Page cache | **Two-layer cache (memory + SQLite); hot queries ~10ms** |
| Cost control | Uncontrollable | Expensive per call | Free but laborious | **Budget modes; free first; keys all optional** |
| Multilingual | Follows the model | Follows the model | Follows the engine | **Language detect + engine locale params + multilingual routing** |

> Mechanically, Argo treats search as an **evidence pipeline**: language detect → domain route → multi-engine recall → RRF fuse → evidence skim. The output is material an agent can rank, `fetch` to verify, and keep inside context.

---

## What changed in search by 2026

1. **From links to evidence.** Agents need structured material they can rank, verify and fit in context — Argo returns credibility-decomposed JSON, not SERP lists.
2. **Context is the first cost.** A single page scrape can burn 10k+ tokens; Argo's agent profile is ~3.7KB per call, with field sets and byte budgets pinned by gates.
3. **Sites now prepare content for AI.** llms.txt and `.md` direct-out are spreading across major doc sites — Argo's fetch chain probes them at level 0, with an r.jina.ai reader fallback.
4. **The free/open ecosystem is enough.** Government, academic, standards and security open APIs plus keyless engines cover most domains (219 keyless sources).
5. **Quality is measurable.** Ranking golden floors, fusion-ablation gates and negative routing controls — "is this version better" is now a number, not a vibe.

> v2.8.9 ships all of the above: 253 sources, 93 domains, 219 keyless. Details in [docs/为什么选择argo.md](docs/为什么选择argo.md).

---

## What it is

**Argo is multilingual search infrastructure for AI agents.**

Real-world retrieval is never “one language + one search box”: someone asks for A-share quotes, someone else asks about the World Cup, someone searches anime in Japanese, someone wants a film director from IMDb. Argo’s premise is simple—**route by domain, language, and intent** to the right sources, instead of always scraping generic web titles. Web search and local file search work together.

> Output is not a “list of links”, but **evidence candidates + credibility breakdown**. Good routing is what makes evidence stand up.

### vs. wrapping yet another search API

| Common approach | Argo |
|-----------------|------|
| Hard-wired to one engine and one key | Multi-engine auto-routing; free first, budget-aware |
| Every query is generic web search | **Vertical sources first**: markets, film, sports, macro, chemistry… answer-shaped results |
| Optimized only for Chinese/English | **Language detection + engine locale params + cross-language fallback** |
| Summarize snippets and ship it | Selection × evidence density × freshness × multi-source consensus |
| One dead engine kills the chain | Circuit breakers, negative cache, staged recovery (no vertical cross-contamination) |
| Hit the network every time | Two-layer cache (memory + SQLite); hot queries ~10ms |
| Same slow path for daily and research | **Fewer engines day-to-day; open up for deep research** |
| Long JSON blows agent context | Compact MCP responses; controllable snippets |

---

## Query-shaped routing

<p align="center">
  <img src="assets/readme/proof-routes.svg" width="100%" alt="Four real routes: finance, film, multilingual, geo">
</p>

| You ask | What tends to happen |
|---------|----------------------|
| python asyncio error handling | Coding-QA domain → StackOverflow/StackExchange official API, with score & accepted-answer markers |
| AAPL / US pre-market | US equities domain, split from A-shares |
| 肖申克的救赎 主演 / Inception director | Film domain → IMDb etc. |
| 梅西 俱乐部 / 库里 球队 | Sports domain → TheSportsDB etc. |
| 埃菲尔铁塔在哪 / where is Eiffel Tower | Geo entity → OpenStreetMap etc. |
| NASA founding year / 国务院职能 | Org entity → Wikidata etc. |
| 周杰伦 专辑 / Taylor Swift album | Media domain → iTunes etc. |
| アニメ おすすめ / 한국 영화 추천 | Detect JA/KO → language-friendly sources; avoid Chinese-only sites |
| US CPI, Japan inflation | Macro domain; country split (national authoritative sources first) |
| log4j CVSS / nodejs 22 end of life | Security → NVD official; lifecycle → endoflife.date |
| attention is all you need | Academic → arXiv/OpenAlex/CrossRef metadata with DOI links |
| 阿司匹林 分子式 | Chemistry → PubChem-style answers |
| TSMC valuation debate (deep research) | Sub-questions + parallel sources; verticals boosted |

---

## How it works

<p align="center">
  <img src="assets/readme/workflow.svg" width="100%" alt="Query → language & domain → multi-engine recall → RRF → evidence → unified JSON">
</p>

```
query
  ├─ intent clarify (optional)
  ├─ query rewrite (optional; routing still sees original intent)
  ├─ language detect + language preference
  ├─ route (domain rules + TF-IDF + budget + lang supplements + hot-path cache)
  ├─ multi-engine recall (circuit breaker / negative cache / parallel)
  ├─ staged empty-result recovery (widen → same family/general → cross-lang; anti-pollution)
  ├─ RRF fusion + optional re-rank
  ├─ evidence skim (authority · density · freshness · consensus)
  └─ unified JSON (incl. engine_outcomes / recovery)
```

### Evidence scoring (short)

```
selection  ≈ domain authority; SERP / redirect shells ranked very low
absorption ≈ density of numbers / definitions / comparisons / disclosures
freshness  ≈ publish time (ignores historical comparison years like “since 2015”)
composite  ≈ 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·engine score
```

Results include `selection`, `absorption`, `credibility_fast`, `evidence_flags`, etc. so agents can sort directly.

### Agent discipline (recommended)

1. **High-stakes questions** (positions, safety, “is this true?”): search → read fast scores → `fetch` top hits → then conclude  
2. **Numbers**: state the计算方式 (definition/scope); when sources conflict, list them—don’t force a merge  
3. **SERP / redirect pages**: never treat as primary sources  
4. **Social posts**: sentiment and narrative, not ground truth  
5. **Fact-check**: prefer a few stratified queries (source / comparison / subject)

---

## Quick start

Pick any path. **GitHub is the only install source of truth** (`npx github:taxueseek/argo` or `install.sh`); current recommendation **v2.8.9**. **Do not `npm install argo-search`** — the npm registry copy is an **unofficial stale v1.0.1** (not this repo, incomplete, not updated). This package sets `private: true` so it is not published to npm by mistake.

**Zero-config works**: without API keys, free engines + local `local_*` engines run; keyed engines are skipped when missing (and usually better when present).

### Option 1: Install script (best for long-term local use)

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

Custom home + Skill link:

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh \
  | bash -s -- --home "$HOME/.local/share/argo" --link "$HOME/.claude/skills/argo"
```

Verify:

```bash
python3 ~/.local/share/argo/scripts/search.py "python asyncio error handling" --json
python3 ~/.local/share/argo/scripts/search.py --list-engines
```

### Option 2: MCP from GitHub (fast agent attach)

Needs **Node.js 18+** and **Python 3.9+**. Once:

```bash
pip3 install pyyaml
```

```bash
npx -y github:taxueseek/argo
```

Client config (Claude Code / Cursor / Kimi, etc.):

```json
{
  "mcpServers": {
    "argo": {
      "command": "npx",
      "args": ["-y", "github:taxueseek/argo"]
    }
  }
}
```

More stable, no Node: install via Option 1, point at local Python:

```json
{
  "mcpServers": {
    "argo": {
      "command": "python3",
      "args": ["/path/to/argo/scripts/mcp_server.py"]
    }
  }
}
```

Unusual Python path: `export ARGO_PYTHON=/path/to/python3` (read by the npx entry only).

### DeepSeek Harness one-line plugin

Two install paths inside DeepSeek Harness:

```bash
# A: 14 mcp__argo__* tools (main-package bundle, same as full MCP)
dsh plugin --profile web add "github:taxueseek/argo"

# B: search tools + wide_research parallel research orchestration (subpackage)
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

Restart `dsh web` after install. See `packages/dsh-plugin/`.

### Option 3: git clone (dev / patch source)

```bash
git clone https://github.com/taxueseek/argo.git
cd argo
pip3 install pyyaml
bash scripts/install.sh --link ~/.claude/skills/argo   # optional
python3 scripts/search.py --list-engines
```

---

## Platforms

| Platform | Integration | Notes |
|----------|-------------|-------|
| **Claude Code** | MCP / Skill link | `npx` or `mcp_server.py`; `link_source.py` ok |
| **Kimi / Grok Build** | MCP Server | same |
| **Cursor / Cline / Continue** | MCP | any MCP-capable IDE plugin |
| **CLI** | `search.py` / `bin/argo` | scripts, cron, manual debug |
| **Python projects** | `from search import super_search` | library call |

### Post-install check

```bash
python3 --version          # 3.9+
python3 -c "import yaml; print('PyYAML OK')"
python3 -m pytest tests/test_unit.py -q   # optional
python3 scripts/search.py --list-engines
```

---

## Capabilities

| Capability | What it does | Entry |
|------------|--------------|-------|
| Unified search | route → recall → fuse → skim score | `search.py` / `argo_search` |
| Local file search | on-disk code/notes/memory (offline) | `argo_local_search` |
| Local text preview | whitelist-dir preview (fail-closed) | `argo_local_read` |
| Recompute | sandboxed numeric recalc (deny by default) | `argo_recompute` |
| Deep research | sub-questions, multi-source, gap hints | `research.py` / `argo_research` |
| Credibility | authority / density / freshness / cross-check | `evidence.py` / `argo_evidence` |
| Intent clarify | polysemy, brand collisions, strategy hints | `clarify.py` / `argo_clarify` |
| Page fetch | HTTP first, browser fallback when needed | `argo_fetch` (`mode=extract` for structure) |
| Screenshot / PDF | page shots, structured PDF extract | `argo_screenshot` / `argo_pdf` |
| Site crawl | list-page batch crawl | `argo_crawl` |
| Social / sentiment | Weibo / Xiaohongshu / Bilibili / Reddit / X … | `argo_social_search` |

### Budget modes

| Mode | Best for | Behavior |
|------|----------|----------|
| `fast` | simple Q, need speed | free engines first; skip paid re-rank |
| `auto` | daily default | cost-aware quality/spend tradeoff |
| `deep` | research, surveys | quality first; more engines allowed |
| `budget` | tight quota | quota control; degrade when exhausted |

### Rough capability set (v2.8.9)

- **Local data fusion (new in v2.8.4)**: research work packages take `file_inputs` (first-hand local data; sha256/lineage registered) + `recompute` (sandboxed recalc); dossier emits `local_sources`
- **One-command MCP inject (new in v2.8.4)**: `argo mcp inject` for Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline (atomic write + backup + undo; source `mcp/clients.yaml`)
- **Structured search upgrades (new in v2.8.4)**: query normalize + variants + complexity gate; social-syntax first; TF-IDF keeps looking after dropping a Chinese engine; `--include-local`
- **Keenable (new in v2.8.4)**: extra general web engine (L1 declarative HTTP, free trial, `ARGO_KEENABLE_API_KEY`)
- **253 sources (219 usable with no key), 93 domains**: general web + finance / macro / film / sports / geo / orgs / media / chemistry / academic / code (source of truth: `config.yaml` + `engines/specs/*.yaml`)
- **14 MCP tools**: search, research, evidence, clarify, fetch, screenshot, PDF, social, local files, crawl, local preview, recompute, WeChat article full text, job aggregation
- **Multilingual search**: Chinese, English, Japanese, Korean, Cyrillic, Thai, Arabic, Hebrew, Greek, Devanagari, …; routing and engine params follow language; non-Chinese queries avoid Chinese-only sources (Zhihu / Sogou WeChat / A-share snapshots, etc.)
- **Vertical recovery gates**: empty-result recovery will not “leak” pypi / npm / flash news into film or sports
- **Faster daily, fuller research**: `engine_policy` tiers—tight daily combo, open long-tail for deep / research

---

## Engines & routing

Config currently has **253** sources (**219** usable with no key) and **93** domains (see `config.yaml` and `--list-engines`).

### Direct & vertical (excerpt)

| Engine | Scenario | Cost bias |
|--------|----------|-----------|
| anysearch / duckduckgo | general / tech | free |
| sina_quote / tencent_quote / eastmoney | A-share quotes / flows | free |
| finviz / seeking_alpha | US & overseas finance | depends |
| imdb / itunes / thesportsdb | film / music / sports | mostly free |
| local_openstreetmap / wikidata / wikipedia | geo / org / encyclopedia | free |
| arxiv / semantic_scholar / openalex | academic | mostly free |
| pubchem / gbif / rfc_editor | chemistry / species / standards | free |
| github / stackoverflow / pypi / npm | code & packages | depends |
| byted / bocha / metaso / octen | Chinese web / AI search | API / low cost |
| zhihu / wechat_sogou | Chinese opinion / WeChat | API / free |
| tavily / felo / exa | international / semantic | paid or quota |
| twitter / reddit / xiaohongshu / bilibili / weibo | social UGC | free (some need login) |

### Local zero-cost layer (`local_*`)

No separate SearXNG service. Main path uses in-process HTML / RSS / JSON parsing (`local_bing`, `local_sogou`, `local_google`, `local_arxiv`, …). For **multilingual queries**, routing rewrites engine language params (e.g. Bing `setlang`) and fuses with RRF.

---

## Examples

### Finance

```bash
python3 scripts/search.py "python asyncio error handling" --explain
# typical: stock_query → quote snapshot sources
```

### Academic

```bash
python3 scripts/search.py "transformer attention mechanism paper" --json
# domain often academic; combo includes arxiv etc.
```

### Research & verify

```bash
python3 scripts/research.py "2026 mutual fund Q2 holdings structure" --depth deep --json

python3 scripts/search.py "same query" --json | \
  python3 scripts/evidence.py "same query" --stdin --json
```

### MCP tools (12)

| Tool | Purpose |
|------|---------|
| `argo_search` | unified search |
| `argo_local_search` | local files (offline) |
| `argo_local_read` | whitelist local text preview (fail-closed) |
| `argo_recompute` | sandboxed recalc (deny by default; needs auth) |
| `argo_research` | deep research (incl. social-sentiment mode) |
| `argo_evidence` | credibility scoring |
| `argo_clarify` | intent disambiguation |
| `argo_fetch` | smart fetch (`mode=extract` structured extract) |
| `argo_crawl` | site crawl |
| `argo_screenshot` | page screenshot |
| `argo_pdf` | PDF extract |
| `argo_social_search` | multi-platform social (`mode=sentiment`) |

---

## Install & config

### Requirements

| Item | Requirement |
|------|-------------|
| Python | 3.9+ (CLI + MCP core) |
| Deps | `pip install pyyaml` (only hard dependency) |
| Node.js | **only** for `npx` entry, 18+ |
| SearXNG | not required (built-in local engines) |

### API keys (all optional)

Missing keys skip that engine; free engines backstop. **Use env vars**—never commit real keys or paste them into issues.

```bash
# recommended (better quality)
export TAVILY_API_KEY="your_key"
export BOCHA_API_KEY="your_key"
export METASO_API_KEY="your_key"
export ZHIHU_ACCESS_SECRET="your_key"

# optional
export BRAVE_API_KEY="your_key"
export FELO_API_KEY="your_key"
export GITHUB_TOKEN="your_key"
export WEB_SEARCH_API_KEY="your_key"
export ANYSEARCH_API_KEY="your_key"
export OCTEN_API_KEY="your_key"
export ARGO_KEENABLE_API_KEY="your_key"   # optional; Keenable free trial
```

`config.yaml` only stores `{ENV_NAME}` placeholders—no plaintext secrets in git.

### Cache

Default SQLite path is `cache.db_path` in `config.yaml` (usually `~/.cache/unified-search/cache.db`).

| Type | Approx. TTL |
|------|-------------|
| Finance | ~5 min |
| News / realtime | ~10–15 min |
| General | ~1 hour |
| Research / evergreen | ~2–24 hours |
| Empty results | very short (avoid freezing “no hits”) |

### FAQ

**Works without API keys?**  
Yes. Many local free engines and free APIs; unkeyed path is automatic.

**Install script vs npx?**  
Script: fixed local install, config, Skill link. npx: attach MCP fast. Same Python core.

**How to check engines?**  
`python3 scripts/search.py --list-engines`, or add `--explain`.

**Multiple code copies in the repo?**  
No. Prefer one source + symlinks via `link_source.py`, not rsync clones.

---

## CLI flags

```
python3 scripts/search.py [options] query

  --engine, -e       engine, default auto
  --max-results, -n  count, default 5
  --depth, -d        fast | balanced | deep
  --mode             fast | auto | deep | budget
  --no-cache         skip cache
  --explain          print routing explanation
  --json             JSON output
  --timeout, -t      timeout seconds
  --list-engines     list engines
```

---

## Design trade-offs

1. **Agent absorption first, link count second.**  
2. **Free and local first; paid is optional lift.**  
3. **Failures are observable**: empty / timeout / breaker are labeled—no silent swallow.  
4. **Config-driven engines**; `config.yaml` is the single source of truth.  
5. **Single-source install**: link entries, don’t rsync copies.  
6. **Social is not a truth library**; good for expansion and sentiment, not sole ground truth.

---

## Good fits

- Search backend for Claude Code / Grok Build / Codex / Kimi agents  
- **Multilingual, multi-domain** Q&A: CJK + EN + finance / film / sports / academic / code  
- Scripts and pipelines that need **reproducible, cacheable** retrieval  
- Fact-check and multi-source comparison of public finance / entity data  

Not a great sole solution for: platform-native engagement ranking, or long-lived max-recall aggregators (embedded local engines replace external SearXNG as the main path).

---

## Tree (short)

```
argo/
├── README.md                # Chinese (default)
├── README.en.md             # English
├── README.ja.md             # Japanese
├── README.ko.md             # Korean
├── README.es.md             # Spanish
├── SKILL.md
├── package.json             # npx entry
├── bin/argo.js              # Node MCP launcher
├── bin/argo                 # Python CLI
├── config.yaml              # engines & domains (source of truth)
├── assets/readme/           # README visuals
├── backends/
├── mcp/                     # MCP client inject source (clients.yaml)
├── scripts/                 # search / research / mcp / install …
├── sub-skills/local-search/
├── sub-skills/ego-search/      # logged-in professional search (off by default)
├── tests/
└── docs/
```

---

### v2.8.9: output slimming + richer, faster retrieval + 5 international & fact-check sources

- **Leaner**: default output −66% (the same results are no longer written three times); repeated queries −30% (routing decision cache); slow-network waits −36% (a fallback source fires automatically once the primary stalls past 0.8s)
- **Richer**: recall +35%, fetch latency −30%; body-structure fidelity 0/6 → 6/6 (headings, lists and tables no longer flattened); Chinese content no longer systematically scored as low-quality
- **New**: sources 232 → 237 (keyless 194 → 198, all free — The Guardian, France 24, DW, plus fact-checkers FactCheck.org / Full Fact covering US & UK claims); dead source gdelt retired
- **More trustworthy**: six bookkeeping defects where the numbers contradicted each other are fixed; a six-stage funnel pinpoints where a zero-result search collapsed; an offline replay tool makes change effects verifiable the same day. See [release notes](docs/RELEASE_NOTES_v2.8.9.md)

### v2.8.8: real-user fixes + across-the-board speedups + 232 sources

- **Fixes**: engines silently returning nothing when keys were set under the documented recommended names (#12); fetch chain ignoring proxies so proxy-required sites like GitHub always failed (#13) — now `ARGO_PROXY`, per-domain config rules and standard proxy env vars all work, with `NO_PROXY` respected
- **Faster**: command cold start 2.1s → 0.55s; cross-process config cache 50–82ms → 16–18ms; every search reports per-stage timings and budget spend, so "where is it slow" needs no guessing (`--no-timing` to disable)
- **New**: sources 218 → 232 (keyless out-of-the-box 184 → 194, all key-free: CN trending boards, security advisories, open-access scholarship, skill directories, agent search), `argo answer` (cited direct answers), `argo watch` (webpage change watching), CLI/MCP tool surfaces aligned
- **More robust**: platform-conventional paths on all three OSes (new `argo paths` introspection), Python 3.9 supported, silent degradations now reported; see [release notes](docs/RELEASE_NOTES_v2.8.8.md)

## Recent updates

### v2.8.7: sources expanded to 218 + a batch of fixes

- **Source expansion**: 168 → 218 sources, 72 → 89 domains (new official sources incl. StackExchange Q&A, DOI metadata, vulnerability intel, product lifecycles), plus three direct-out fetch channels
- **A batch of fixes**: multi-engine routing, zero-result fallback, Chinese redirects, local search degradation, etc.; see [release notes](docs/RELEASE_NOTES_v2.8.7.md)

### v2.8.6: faster search + multi-source split

- Engine racing, Zhihu three-source split, language/academic dispatch widened, dead-source gate

### v2.8.5: native DSH plugin tools + MCP off by default + Windows support

- **Native plugin tools**: `argo_search` / `argo_fetch` register as first-class native tools, available by default without an MCP connection; schemas are generated from the single source of truth (`mcp_tools.py`), zero drift; all 13 tools (except `argo_research`) can be enabled via `nativeTools`
- **MCP off by default**: three plugin shapes (on-demand MCP / native tools as the default entry / web_search seam); zero standing token cost, and one profile patch opens the full 14-tool surface
- **Windows compatibility** (community PR #11): system temp paths, GBK encoding fix, runtime interpreter resolution (`python3`/`python`), symlink falls back to junction, new PowerShell installer `install.ps1`
- **Quota self-healing**: remote quota exhaustion hidden in HTTP 200 envelopes is detected; routing excludes that engine and switches to backup sources, returning automatically at the next quota period
- **Fetch global deadline**: `ARGO_FETCH_DEADLINE_S` (default 60s) caps the fallback chain; 429/503 stop signals are honored; tinyfish rendering + `.md` variant probes

### v2.8.4: local data fusion + one-command MCP attach

- **Deep research can eat your local data**: work packages can take `file_inputs` (first-hand CSV / XLSX / literature; hash registered, content not stored) + `recompute` (sandboxed recalculation; mismatches are flagged)
- **MCP attach is no longer hand-edited config**: `argo mcp inject` writes Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline (atomic write + backup + undo)
- **Simple queries stay cheap**: query normalize + complexity gate + social/platform syntax first + if a Chinese engine is dropped, keep looking at backups
- **Keenable** added as a free-trial web search source
- **Security**: recompute blocks outbound subprocesses; host paths are install-aware

> Full notes: the table below and per-version [release notes](docs/).

---

## Changelog

| Version | Notes |
|---------|-------|
| **v2.8.9** | **Output slimming + richer, faster retrieval + 5 international & fact-check sources**: default output 15.5KB→5.2KB (−66%, `--envelope` restores the full envelope); routing decision cache cuts repeated queries −30%; slow-network dispatch −36% (serial domains auto-hedge past 0.8s); five-round fetch-chain overhaul (recall +35%, fetch latency −30%, long-tail P90 −8%) + body structure restoration (headings/lists/tables fidelity 0/6→6/6) + CJK quality-score fix; new keyless sources Guardian RSS / France 24 / DW / FactCheck.org / Full Fact (232→237, keyless 194→198, domains 90→92), dead gdelt retired; six self-consistency defects fixed + six-stage funnel attribution + offline replay tool (`scripts/replay_eval.py`) + post-policy deployment gates & scenario-threshold contracts. See [release notes](docs/RELEASE_NOTES_v2.8.9.md) |
| **v2.8.8** | **Real-user fixes (#12 key-alias silence, #13 proxy-less fetch) + across-the-board speedups + sources 218 → 232**: unified egress dispatch (`ARGO_PROXY` / per-domain rules / standard proxy env, `NO_PROXY` respected); 16 key-read sites unified on the alias chain; command cold start 2.1s→0.55s, cross-process config cache 50–82ms→16–18ms, QPP flatness-gated early stop, `--list-engines --detail` slimmed 152KB→51KB; new `argo answer` (cited direct answers) and `argo watch` (webpage change watching), 14 keyless sources (CN trending & lifestyle / security advisories / open-access scholarship / skill directories / agent search), 69 engines re-classified (`web_general` fallback share 41%→20%, new security domain); platform-conventional paths (`argo paths`) + Python 3.9 + bounded concurrency making timeouts real; search.py split into modules 3351→2526 lines, relevance golden over 22 engines, static-defect & output-contract gates. See [release notes](docs/RELEASE_NOTES_v2.8.8.md) |
| **v2.8.7** | **218 sources / 89 domains + three fetch direct-out channels + routing trigger discipline + macro zero-result fix**: 50 new sources across batches 7/8/9 (statutes, standards, security intel, academia, news, entertainment, energy/transport, legal), stackexchange & doi engines; fetch chain level-0 llms.txt and `.md` variant probing plus r.jina.ai reader level; `--engine` comma-list fix, tfidf scope & route_reason, negative routing-control matrix, ranking golden & fusion-ablation gates; China macro queries promote national statistics first, zero-result recovery unlocks L3, rescue engines recorded. See [release notes](docs/RELEASE_NOTES_v2.8.7.md) |
| **v2.8.6** | **Hedged racing + Zhihu three-source split + declarative language dispatch + reachability gate + academic search protocol**: first-engine grace-window racing (fast engines pay 1 call), Zhihu global-search/personal-data onboarding with starvation fix, per-sub-query language/academic dispatch (English + 17 academic sources in research collection), engine language metadata & reachability gate (dead sources surface), geo tail −56%, academic query-construction protocol. See [release notes](docs/RELEASE_NOTES_v2.8.6.md) |
| **v2.8.5** | **Native DSH plugin tools + MCP off by default + Windows compat + quota self-healing + fetch deadline**: `argo_search`/`argo_fetch` as first-class native tools (CLI one-shot, same engine & guards as MCP, schema single-source + drift gate); three plugin shapes, MCP on-demand; Windows compatibility (temp paths / GBK / interpreter resolution / junction / `install.ps1`, PR #11); quota self-healing loop (HTTP 200 envelope detection + route exclusion + period self-heal); global fetch deadline (`ARGO_FETCH_DEADLINE_S`) + tinyfish rendering + `.md` variant probes; hot-reload env & state-dir single source. See [release notes](docs/RELEASE_NOTES_v2.8.5.md) |
| **v2.8.4** | **Local data fusion + multi-client MCP inject + structured search + Keenable**: research L1 first-hand local data (`file_inputs` + `recompute` + `local_sources`); `argo mcp inject` (declarative `mcp/clients.yaml`); query normalize / variants / complexity gate / social-syntax first / TF-IDF fix / `--include-local`; Keenable web engine (free trial); security hardenings. See [release notes](docs/RELEASE_NOTES_v2.8.4.md) |
| **v2.8.3** | **Multilingual routing fix + in-process anysearch + weighted RRF**: ja/ko queries return the target language; DE/FR/ES/IT via anysearch; weakest-link downweight (paper 2508.01405). See [release notes](docs/RELEASE_NOTES_v2.8.3.md) |
| **v2.8.2** | **Windows + unified evidence semantics**: npm `os` limit removed; UTF-8 path against GBK crashes; main-package `dsh.bundle`; `wide_research` quality gate. See [release notes](docs/RELEASE_NOTES_v2.8.2.md) |
| **v2.8.0** | **Evidence loop + jobs v3 + dual weather**: `fetch_required` / `--verify`; `argo job`; wttr.in + Open-Meteo; Parallel / You.com. See [release notes](docs/RELEASE_NOTES_v2.8.0.md) |
| **v2.7.3** | Engine-layer HttpClient; TF-IDF activates 25 verticals; 70-domain TTL; bilingual verticals. See [release notes](docs/RELEASE_NOTES_v2.7.3.md) |
| **v2.7.2** | Logged-in professional search (ego-search, off by default); JA/KO no longer mix Chinese engines. See [release notes](docs/RELEASE_NOTES_v2.7.2.md) |
| **v2.7.1** | SSRF hardening + routing health-state fix. See [release notes](docs/RELEASE_NOTES_v2.7.1.md) |
| **v2.6.0** | **Multilingual search** (detect / engine params / cross-lang fallback); film·sports·geo·org·media verticals; recovery anti-pollution; capability families + matrix regression; ~120+ sources. See [release notes](docs/RELEASE_NOTES_v2.6.0.md) |
| **v2.5.1** | Thicker finance/macro/chemistry answer sources; engine tiers + combo budget; [v2.5.1 notes](docs/RELEASE_NOTES_v2.5.1.md) |
| **v2.5.0** | Install script + npx; rewrite decoupled from routing; hot-path cache; compact MCP |
| **v2.4.0** | Low-score route fallback + social mis-route filters; cache depth / soft hits; breakers & negative cache; `engine_outcomes` |
| **v2.2–v2.3** | Two-stage evidence, Chinese source table, content_signals, fetch stack, more engines |
| **v2.1** | Social engine layer (multi-platform UGC) |
| **v1.x** | Unified name Argo; multi-engine routing + two-layer cache |

---

## Contributing

Issues and PRs welcome. When you change routing or evidence logic, please add tests:

```bash
python3 -m pytest tests/test_unit.py tests/test_multilingual.py -q
python3 scripts/regression_p0p1.py --offline
python3 scripts/matrix_search_eval.py --offline
python3 scripts/ab_eval_p0p1.py   # optional, online
```

Before commit: no real API keys, absolute machine paths, or account cookies. Local Skill paths belong in `installs.local.yaml` (gitignored).

## License

MIT License © 2026 [taxueseek](https://github.com/taxueseek)

<p align="center">
  <a href="https://github.com/oil-oil/beautify-github-readme"><img src="assets/readme/made-with-beautify.svg" width="300" alt="README made with beautify-github-readme"></a>
</p>

---

> Good search is not about seeing more—it is about concluding with confidence, and knowing when you still should not.
