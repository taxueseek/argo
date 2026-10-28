<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo: search and deep research for agents — 284 search sources, routing by domain and language, absorbable evidence output">
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
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.10.0-informational">
  <img alt="engines" src="https://img.shields.io/badge/engines-284-orange">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **Part of the taxueseek DeepSeek Harness plugin lineup** ([dsh-files](https://github.com/taxueseek/dsh-files) 39★). Same-author skill series: [session-digger](https://github.com/taxueseek/session-digger) 29★ · [taxue-invest](https://github.com/taxueseek/taxue-invest) 21★ · [taxueskills](https://github.com/taxueseek/taxueskills) 23★ · [taxue-creative-style](https://github.com/taxueseek/taxue-creative-style) 12★

# Argo: search and deep research for agents

## What it is

**Argo is a search and deep-research tool that lives inside your agent, and can also pull information from the relevant web pages. It connects to 284 search sources across 99 business domains, and also covers basic local file search. Among open-source search projects, its search capability is exceptionally strong.**

API keys are optional and improve quality (keyed sources are usually better; unkeyed ones are skipped automatically), with free engines and local engines as an automatic fallback. It covers 18 languages including Chinese, English, Japanese and Korean, and supports vertical scenarios such as finance, academia, film, sports, code and macro data.

## Sub-skills

Argo's capabilities are organized as four sub-skills, each a standalone pipeline:

| Sub-skill | Capability |
|-----------|------------|
| **local-search** | Local file search + local zero-cost aggregation engines (32 local engines). Search code symbols, filenames, document text and file structure. Can merge into web search results: flip one switch and local hits join the same search |
| **local-seek** | Local semantic search (BM25 + vector hybrid) for notes, memories and knowledge bases. Three layers: locate the file, read the context, then read the passage closely |
| **ego-search** | Logged-in professional search. Content behind logins on Zhihu, Xiaohongshu, WeChat public accounts and similar, searched with a real browser and a logged-in session. Off by default; turn it on when needed |
| **js-run** | Run web JS without a browser. Run environment-probe and pure-compute page scripts without opening a browser; median single-run context is about 1ms |

Together the four cover the full chain from local machine to open web to behind login walls. Most cases need only local-search plus web search; turn on ego-search and js-run for special cases.

## DSH plugin

Argo is also a DeepSeek Harness plugin. One line installs it:

```bash
dsh plugin --profile web add argo-search                  # npm release
dsh plugin --profile web add "github:taxueseek/argo"      # GitHub main
```

Restart `dsh web` and it takes effect. Search and fetch become native first-class tools: no MCP connection, no standing context overhead. There is also one `wide_research` parallel researcher and 19 MCP tools you can enable on demand. See integration forms below.

### Three invocation channels, one capability set

The same argo retrieval core (same engines, same compression, same guards) opens three channels inside DSH. They degrade automatically by host capability and back each other up:

| Channel | Default | What it does |
|---------|---------|--------------|
| **Native first-class tools** | On | `argo_search` / `argo_fetch` and friends register via `ctx.tools.register` and run as CLI single-shots (`--call`), reusing the exact engine and guard stack MCP uses — **no MCP connection, zero standing token overhead** |
| **Native web seam** | On | Registers `argo` as both a search and a fetch provider, so the host's built-in `web_search` routes to argo's engine chain and `web_fetch` to argo's fetch chain (HTTP first, auto-escalating to an anti-detection browser on Cloudflare / JS-rendered pages) |
| **MCP form** | On demand | The full 19-tool `mcp__argo__*` surface. Tool definitions are injected into context every round and survive compaction (measured ≈3.7K tokens/round for 19 tools), and the hot path is already covered by native tools and the web seam — so it is not mounted by default. Uncomment the `mcp-argo` line in the profile patch when you need the long-tail forensics tools |

There is also one `wide_research`: it plans complementary tracks (`depends_on` staggers them, parallel by default) → bounded concurrent subagent evidence gathering → a source ledger (only http(s) URLs are recorded) → a synthesis report. Output carries `quality_gate_results` (`conclusion_cap`: low/medium/high); when `passed=false` the wording is downgraded and the ledger sources are verified before any conclusion. The worker evidence-tool allowlist writes both MCP and native tool names (allow semantics), so it works whether or not MCP is mounted: with MCP it uses the full surface, without it falls back to `argo_search` / `argo_fetch` and the host's `web_search` / `web_fetch`.

For an everyday question use `argo_search`; for single-agent forensics use `argo_research`; for multi-perspective comparison with a citation ledger use `wide_research`. Workers can only call argo evidence tools by default and cannot call `argo_research` again (hard guard — user-level overrides do not work).

### Built-in settings page: six sections, effective on save

The plugin ships a browser-side settings page with two entries: the settings popup (`Cmd+,`) left-hand menu item "Argo", or Settings > Plugins > Argo (a summary card for the same panel — click to open).

<p align="center">
  <img src="assets/readme/settings-tools.webp" width="100%" alt="Argo settings, Tools: 18 native tools to toggle — argo_search / argo_fetch on in the hot path, 16 long-tail tools on demand, with per-call timeout and returned-text cap">
</p>

<p align="center">
  <img src="assets/readme/settings-engines.webp" width="100%" alt="Argo settings, Engines: argo's 28 behaviour switches moved into the panel — cache / TF-IDF rerank / numeric cross-check / MinHash dedup / SERP guard and dedup threshold; unconfigured rows show the default and changes apply immediately">
</p>

<p align="center">
  <img src="assets/readme/settings-keys.webp" width="100%" alt="Argo settings, Keys: DeepSeek official search shows configured · credential store and shares one key with the host's web_search; common engine keys (Tencent Yuanbao / Tavily) can be set, updated or cleared, plaintext never echoed">
</p>

| Section | What it controls |
|---------|------------------|
| **Access** | `web_search` / `web_fetch` seam toggles; engine entry command and args (npx or local `mcp_server.py`); search/fetch timeouts, body cap, idle connection reclamation, concurrent connections |
| **Tools** | Toggles for the 18 native first-class tools (only `argo_search` / `argo_fetch` on by default); per-call timeout and returned-text cap |
| **Engines** | argo's own behaviour switches (formerly env vars, curated allowlist of 28: cache / TF-IDF rerank / numeric cross-check / MinHash dedup / SERP guard, Jina / mobile UA / Parallel fetch layer, semantic evidence layer, hedge grace and tail width, engine enable list, proxy, local search roots and read allowlist, warmup / usage log / connection pool / assorted TTLs). Switch-type changes apply immediately; number / text / list types save as a group; unconfigured rows show argo's default and are marked "default". Authorisation switches (recompute, intranet URLs) deliberately stay out of the panel |
| **Keys** | Set, update and clear common engine keys; shows "configured / unconfigured + source layer (credential store / startup env / .env) + read-only state", never echoing plaintext; each row has a Forward checkbox, equivalent to adding the reference name to `keyRefs` |
| **Research** | worker / track / sources-per-track / token caps and report output directory |
| **Advanced** | worker tool allow / deny lists, credential forward list |

Saving takes effect immediately (all volatile config): the native tool set is registered and unregistered per the toggles, timeouts / concurrency / research caps are read on the next call; engine switches are written as `argoEnv` (one `KEY=VALUE` per line) into the argo subprocess env and apply to the next new connection (pooled old connections are reclaimed by `searchIdleMs`, default 60s). Structural fields (`toolName`, `provider`, `searchProviderId`, `fetchProviderId`) are not in the settings page — they still go through the profile patch and need a restart.

**Key security**: keys are written to the DSH credential store (the same place as the Models page) — never into plugin config, never logged, never echoed. Credential resolution priority is **stored file layer > startup env > project .env > home .env**: saving in the panel (or the Models page) overrides older startup-env values, and the argo subprocess picks up the new key on the next new connection; Clear only deletes the credential-store value, so keys supplied by env vars or `.env` reappear. The settings page is only a write entry for the credential domain — reads are still performed by the host on every call.

**DeepSeek official key, supplied natively**: argo's `deepseek_search` engine (the official `api.deepseek.com/v1/tools/search`, whose results carry authority levels / rerank scores / publish times) **shares one key with DSH's official search backend**. The plugin obtains it through DSH's native credential seam, so no manual configuration is needed: resolution is `ctx.get('credentials').resolve('DEEPSEEK_API_KEY')`, resolved per call — after rotating the key on the web Models page the next new connection uses it, with no plugin restart. The plugin never reads key files, never writes to disk, never logs; the key value reaches the argo subprocess env in memory only, and no output or error message contains it. On older hosts without a credentials service it silently skips: that engine is disabled for lack of a key, and the other sources are unaffected.

**The web seam pin is required reading** (a missing pin breaks `web_fetch`): the host `dsh-web` provider selection rule is "configured id wins; if none is configured and exactly one is available, pick it; **if more than one is available, fail with `WEB_PROVIDER_AMBIGUOUS`**". The web profile ships an official http fetch provider by default (keyless, always available), and this bundle registers another — so without a pin there are two available and **`web_fetch` breaks entirely** (same for `web_search` if the profile has another search provider). Any profile with the web seam enabled must therefore specify explicitly on the web service line:

```yaml
- id: web
  config:
    searchProvider: argo    # required when the search seam is enabled (it is by default)
    fetchProvider: argo     # required when the fetch seam is enabled (it is by default)
```

`searchProviderEnabled: false` / `fetchProviderEnabled: false` turn the corresponding seam off entirely (the provider is still registered but not selectable, and the other is unaffected). On the engine side, anonymously usable engines such as Firecrawl work **keyless out of the box** (free-tier quota), engines with a configured key automatically upgrade to authenticated requests, and a missing key does not block routing.

> Full configuration options, the Compatibility version table and uninstall steps are in [packages/dsh-plugin/README.md](packages/dsh-plugin/README.md).

## Core strengths

**284 sources, 241 keyless and ready out of the box.** Runs without any API key; free and local engines fall back automatically. Keyed sources are usually better, and unkeyed ones are skipped automatically. One install covers every scenario.

**Local and web search in one.** This is a capability only Argo has. Local file search is built on the same tools developers use daily, rg and mdfind: code symbols, filenames, document text and file structure. Flip one switch and local hits join the same web search results, labelled with the local file as the source. When you remember writing something six months ago but forget where, this beats digging through folders.

**Two-layer cache, ~10ms for hot queries.** For content whose freshness matters less (encyclopedia entries, historical data), the first query populates the cache and the same query then hits it directly instead of going through the API again. Saves money and time.

**Multilingual and vertical.** Tested across 18 languages; Chinese, English and Japanese reach local first-party sources. Papers go to academic databases, film to title databases, code to Q&A communities, market quotes to financial sources. Only broad questions go to generic search.

## Install and run

Zero config works. Three ways to install, pick your habit:

**Want your AI assistant to search on its own**: install the skill package into its skills directory. Then when you say "look this up" or "verify this", it goes and searches.

**Want to try it casually**: one npx command, no install needed.

**Want it always on**: attach it over MCP, 19 retrieval tools ready. There is also a one-command injector that configures Claude Code, Cursor and Windsurf automatically; Codex, OpenCode and Cline are on the list too. Your config is backed up before writing, so you can always roll back.

```bash
# Option 1: install script (recommended for long-term local use)
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash

# Option 2: npm / npx (supported, currently 2.10.0; released per tag, version pinned)
npm install -g argo-search       # install the CLI globally
npx -y argo-search               # or run it without installing
npx -y github:taxueseek/argo     # use this to follow the latest commit on main

# Option 3: git clone (development / editing source)
git clone https://github.com/taxueseek/argo.git
```

Try one query right after install:

```bash
argo search "python asyncio error handling"
```

What you get is questions from StackOverflow's official API, ranked by community score and marked with accepted status, not a pile of SEO farms.

To see how many sources are available on your machine right now:

```bash
argo search --list-engines
```

## Integration forms

Argo supports four integration forms; pick by scenario:

| Form | Best for | Notes |
|------|----------|-------|
| **MCP Server** | Always-on agent attachment | 19 retrieval tools ready; one command injects Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline, config backed up first |
| **DSH plugin** | DeepSeek Harness users | One line `dsh plugin add`; search and fetch become native first-class tools, no MCP connection, no standing context overhead; ships a six-section settings page effective on save |
| **Skill files** | Claude Code / Grok Build / Kimi, etc. | Drop the skill package into the agent's skills directory; say "look this up" or "verify this" and it searches |
| **npx on demand** | Quick trial | `npx -y argo-search` (npm release) or `npx -y github:taxueseek/argo` (main), no install, good for temporarily attaching MCP to any client |

All four forms run the same Python code underneath; CLI and MCP tools map one to one, so no form lacks capability.

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
4. **The free/open ecosystem is enough.** Government, academic, standards and security open APIs plus keyless engines cover most domains (241 keyless sources).
5. **Quality is measurable.** Ranking golden floors, fusion-ablation gates and negative routing controls — "is this version better" is now a number, not a vibe.

> All five are already shipped across the v2.9.x line: 284 sources, 99 domains, 241 keyless out of the box. Details in [docs/为什么选择argo.md](docs/为什么选择argo.md).

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

Pick any path. **npm install is supported, current version 2.10.0**, published from this repo like GitHub. The npm route uses `npm install -g argo-search` or `npx -y argo-search`, is released per tag, with a pinned version and a smaller download. The GitHub route uses `install.sh` / `install.ps1` or `npx github:taxueseek/argo` and follows main. From now on every tag push makes CI publish both npm packages together with the GitHub Release; the older v1.0.1 on the registry is a historical version, do not pin to it.

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
# A: 19 mcp__argo__* tools (main-package bundle, same as full MCP)
dsh plugin --profile web add argo-search                  # npm release
dsh plugin --profile web add "github:taxueseek/argo"      # GitHub main

# B: search tools + wide_research parallel research orchestration (subpackage)
dsh plugin --profile web add @taxueseek/argo-dsh          # npm release
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
| Query operators | `site:` / `-site:` / `-term` / `-"phrase"` / `"phrase"` / `lang:` / `after:` / `before:` stripped from routing and retrieval (details in references/usage.md) | write directly in the query |
| Trust signals | `authority_level` discrete grade (0-4, >=3 highly authoritative) + `summary` agent-grade digest (backfilled after verification) | result fields, visible via `--fields agent` |
| Social / sentiment | Weibo / Xiaohongshu / Bilibili / Reddit / X … | `argo_social_search` |
| DSH native tools | once installed as a DSH plugin, `argo_search` / `argo_fetch` and friends become first-class host tools running as CLI single-shots with zero standing token overhead | call directly after `dsh plugin add` |
| DSH web seam | the host's built-in `web_search` / `web_fetch` route to argo's engine and fetch chains (HTTP first, auto-escalating to a browser on Cloudflare / JS-rendered pages) | registered by default by the plugin; pin `searchProvider` / `fetchProvider` in the profile |
| DSH settings page | six sections — Access / Tools / Engines / Keys / Research / Advanced — effective on save; keys go to the host credential store and are never echoed | settings popup `Cmd+,` → left menu "Argo" |

### Budget modes

| Mode | Best for | Behavior |
|------|----------|----------|
| `fast` | simple Q, need speed | free engines first; skip paid re-rank |
| `auto` | daily default | cost-aware quality/spend tradeoff |
| `deep` | research, surveys | quality first; more engines allowed |
| `budget` | tight quota | quota control; degrade when exhausted |

### Rough capability set (v2.10.0)

What Argo does, in one sentence: it sends each question to the right sources by domain and language, ranks what comes back by credibility, and hands the agent material it can sort and verify. Search, fetch, evidence checking and local file search share one pipeline, the 19 MCP tools map one-to-one onto CLI commands, so nothing is missing whichever form you install. A few standalone commands round it out: argo tweet packs an X post, argo auth handles login state, argo job aggregates job listings. The full catalog, with cost, key and status per source, sits in docs/ENGINE_CATALOG.md: **284 sources (241 usable with no key), 99 domains**.

Installed as a DSH plugin, these capabilities become native host tools: the hot-path search and fetch are on by default, the long-tail forensics tools toggle on in the settings page, and argo's own 28 behaviour switches (cache, rerank, dedup, guard, proxy, TTLs and the rest) moved out of environment variables and into the panel. Keys go into the host credential store, are never echoed, and every change takes effect on save.

The last few releases each fixed one class of problems.

v2.10.0 widens the capability surface. Sources grow from 272 to 284: five Metaso tiers (scholar, library, image, video, podcast), Sciverse records, NCPSSD Chinese social science, plus three DSH plugin catalogs and two skill indexes — the skill sources carry install counts for the first time. Video works end to end, from query to fetch: a new video domain with --video, duration filters, Bilibili subtitles on fetch and YouTube timedtext. The crawl chain gained frontier quality control and polite pacing — per-host rate limits, 429 backoff — and watch now separates noise from substantive change. General source selection uses measured ordering, English general queries get a conditional DeepSeek hedge, and response bodies go through one decoder with JSON-LD. The installer takes the venv path, so PEP 668 environments no longer end up without their dependencies.

v2.9.9 is a security-and-delivery-quality release. The content-security engine used to score web content but not stop it: a high-risk injection page went straight into the pipeline. Now anything over the risk threshold is quarantined, the cleaned text replaces the original, and it never reaches the evidence score. The network egress gains two layers of defense: redirects are validated hop by hop, and the resolved address is pinned to the connection, closing the DNS-rebinding window. On the dispatch side, results from slow engines abandoned by early-stop are no longer thrown away: they are collected in the background, delivered as usual if they finish within the grace window, and their real performance finally reaches the learner. A six-family scan of real-world scenarios tightened five classes of mis-routing: A-share financial queries, corporate due-diligence / invoicing / person-to-company lookups, civil-law practice, drug leaflets and paper-writing how-tos all land where they should. New this round: `argo engine add` wires a new engine up in one step, and the DSH settings page gains a route-observation card with tool recommendation combos.

v2.9.8 is a routing-precision and feedback-loop release. The three problems in issue #18 — "latest" being routed into realtime news, people_daily returning its default feed when nothing matches, and local_bing matching only "2026" — were all reproduced and fixed. Replaying real queries along the same lines tightened 16 more classes of false triggers: "snow" inside brand names, "Volcano Engine" inside product names, UI-troubleshooting "icons", "code" inside business/finance field names, and "exchange rate" inside code/library queries. A SERP coverage gate and CJK cross-validation now stop "it returned something" from counting as "it is relevant". Thanks to @wpp666 for the reproduction and diagnosis.

v2.9.7 fixed three long-standing ones. When an AI inside a DSH host asked to read a URL, the host used its own fetcher, so short-link redirects and JS challenge pages often failed outright while argo's fetch chain sat unused; this release wires the host's reads through argo, and those pages go from failing to succeeding, with static pages unchanged. The routing candidate pool used to hold 18 explicit-only engines, and if one was ever auto-selected the only paid engine would quietly bill you; the pool is clean now, and using them requires saying so. Every engine success used to take a file lock and rewrite breaker state on disk; healthy engines now cost zero file I/O, and multi-engine accounting commits in one transaction. The license also moved from MIT to Apache-2.0, which sits easier with commercial users.

After that the DSH plugin gained another layer: the settings page grew from five sections to six, the new "Engines" section brings argo's behaviour switches into the panel (they used to be env-var only), the keys page now shows whether each key comes from the credential store, the startup environment or `.env`, and tool toggles, timeouts, concurrency and research caps all apply on save. Cancellation now reaches the connection-establishment stage too — when the AI stops mid-flight, an argo subprocess that is still being spawned is killed immediately instead of waiting out its timeout.

v2.9.6 handled query operators and the transport layer. A query like site:github.com foo used to have its operator treated as a plain word, or parsed but never applied; a contract layer now strips operators before routing and caching, applies site: as a real filter, and folds it into the cache key, so two searches don't bleed into each other. On transport, every POST used to re-pay the TLS handshake, 320 to 561 ms of it; with connections pooled, a resident MCP process saves that on every query. This release also added the official DeepSeek search engine, billed per call and kept out of the everyday combos.

v2.9.5 added FX rates and tightened dedup. Asking for the USD/CNY rate used to spin through the macro domain and land on an encyclopedia entry, 1.4 s with no answer; a dedicated fx domain now answers in 675 ms with the live rate and daily news. Dedup gained site-suffix normalization and a fuzzy second pass, so official docs with separators in the title stop getting pushed to the back.

v2.9.4 fixed the explicit-engine path. Pinning --engine X used to get a blanket cache TTL; with domain determination in place, the TTL follows the domain, and a missing fallback engine no longer triggers an empty re-run of the same engine.

Per-version detail lives in "Recent updates"; release notes for each version are in docs/.
---

## Engines & routing

Config currently has **284** sources (**241** usable with no key) and **99** domains (see `config.yaml` and `--list-engines`).

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
| byted / bocha / metaso / octen / yuanbao | Chinese web / AI search | API / low cost |
| hkex / shfe / cfets / defillama / gleif | HKEX announcements / SHFE / RMB central parity / DeFi / legal entity IDs | Free |
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

## Recent updates

Only the three most recent versions. Full history in the [changelog](#changelog); per-release detail on [Releases](https://github.com/taxueseek/argo/releases).

### v2.10.0: 284 sources + video search and fetch + crawl frontier quality control

- **272 → 284 sources**: five Metaso tiers (scholar/library/image/video/podcast, all explicit-only), Sciverse records (filter by year/journal/author/subject), NCPSSD Chinese social science (CLI bridge), plus three DSH plugin catalogs and two skill indexes with install counts
- **A video lane end to end**: new `video` domain and `--video` to force routing, `--min-duration`/`--max-duration` filters; fetching pulls Bilibili descriptions and subtitles, YouTube oEmbed/timedtext, optional `ARGO_VIDEO_ASR_CMD` fallback
- **Crawl frontier quality control**: URL canonicalization and asset filtering, body-hash dedupe, per-host pacing with 429 exponential backoff, robots Crawl-delay; `argo watch` block-level diff separates noise from substantive change. Measured: 10 pages in 12.1s, 2000 characters of body text (was 500)
- **Source selection and cost**: general pool now uses measured ordering with a coverage floor and language gate (issue #23); the DeepSeek hedge stays second for English general queries (`ARGO_PAID_HEDGE_DEFER` moves it down); authority levels are backfilled and authority early-stop is in
- **Installable and releasable**: the installer takes the venv path with `.venv` auto-discovery; new `release_check.py` release-consistency precheck and the MCP Registry manifest `server.json`

### v2.9.9: security in depth + slow-source results no longer dropped + five mis-routing classes closed

- **High-risk page text can no longer reach the context**: the content-security engine upgrades from "score only" to "quarantine + clean replacement"; text with `risk_score ≥ 0.5` is replaced by its cleaned version and excluded from the evidence score. On by default; `ARGO_CONTENT_RISK_GATE=0` for an emergency override
- **Network egress defense in depth**: redirects validated hop by hop (`ARGO_MAX_REDIRECTS`, default 5) + resolve-and-pin, closing the DNS-rebinding window
- **Results from slow engines abandoned by early-stop are no longer dropped**: abandoned engines are collected in the background; whatever finishes within `ARGO_TAIL_COMPLETE_S` (default 3s, 0=off) is delivered as usual and recorded as a real score, with results merged back into the cache
- **Five mis-routing classes closed**: A-share financial queries to `stock_query`, corporate due-diligence / invoicing / person-to-company to `company_search`, civil-law practice to `flk_law`, drug leaflets no longer stolen by the "usage" token, paper-writing how-tos no longer routed to academic search
- **`argo engine add`**: one-step engine onboarding; the manual spec/env/validate/sync/combo/allowlist chain becomes a single command
- **Bucketed scenario learning + ZCode plugin package**: routing can now tell "strong in domain A, weak in domain B"; skill+CLI direct channel and a zero-token panel shipped to zcode-plugins

### v2.9.8: routing precision audit + Tencent Yuanbao search + three more keyless sources + a structured failure list

- **issue #18/#19 all fixed**: `latest` mis-routed into realtime news, people_daily default feed, local_bing matching only "2026", Chinese cross-validation always 0, nightly crash on empty logs; 16 more isomorphic false-trigger classes tightened along the way
- **Yuanbao search**: a first-hand, official-voice Chinese source, sitting 2nd in the Chinese-general domain as an authority hedge. Key: `ARGO_YUANBAO_API_KEY`
- **Three more sources usable with no key**: `tavily_free`, `keenable_free` and the `exa` anonymous fallback
- **Failures you can act on**: each failure names its source and class and says whether to retry, switch sources or configure a key, alongside a `meta.uncertainty` confidence level
- **Local config overlay `config.local.yaml`**: machine-local tuning is no longer written into the public config
- **A new "Engines" section on the DSH settings page**: argo's 28 behaviour switches move out of environment variables and into the panel

---

## Changelog

| Version | Notes |
|---------|-------|
| **v2.10.0** | **284 sources + video search and fetch + crawl frontier quality control**: five Metaso tiers, Sciverse, NCPSSD and five DSH/skill index sources; a `video` domain with `--video`/`--min-duration`/`--max-duration`, Bilibili subtitles and YouTube timedtext with optional ASR; crawl frontier quality control and noisy-diff `argo watch`; measured general source selection with the DeepSeek English hedge and authority early-stop; venv-based installer. See [release notes](docs/RELEASE_NOTES_v2.10.0.md). |
| **v2.9.9** | **Security in depth + slow-source results no longer dropped + five mis-routing classes closed**: high-risk text quarantined with cleaned replacement (excluded from the evidence score), hop-by-hop redirect validation and DNS-rebinding pinning; slow engines abandoned by early-stop are collected in the background (`ARGO_TAIL_COMPLETE_S`) and their real scores recorded; a six-family real-scenario scan closes academic/finance/corporate/legal/pharma mis-routing; plus `argo engine add` one-step onboarding, bucketed scenario learning, and the ZCode plugin package. See [release notes](docs/RELEASE_NOTES_v2.9.9.md). |
| **v2.9.8** | **Routing precision audit + issue #18/#19 fixes + test-state isolation**: replayed real queries and tightened 16 classes of false triggers (brand/product names, field names, ASCII substrings); added the SERP coverage gate and CJK cross-validation; fixed the empty-log nightly ledger, people_daily default feed, and cached-result guarding; isolated adaptive/breaker state per test. Thanks to @wpp666 for the reproduction and diagnosis. See [release notes](docs/RELEASE_NOTES_v2.9.8.md). |
| **v2.9.7** | **Architecture consolidation + DSH web_fetch seam + License to Apache-2.0**: MIT → Apache License 2.0. Host web_fetch now flows through argo's fetch chain (HTTP first, browser upgrade on JS challenges), and explicit-only engines leave the routing pool so auto mode cannot bill them by accident. |
| **v2.9.6** | **DeepSeek Search engine + query operator contract + transport connection pooling + routing snapshot gate**: New `deepseek_search` engine, on by default (445-510ms; skipped without a key). Query operators (`site:`, `-term`, `lang:`, `after:`, `before:`) share one contract with real post-filtering; POST pooling saves 320-561ms per request. |
| **v2.9.5** | **FX-rate dedicated domain + two-layer dedup + ranking false-kill fix + per-checkout daemon isolation**: New `fx_query` domain answers rate and parity questions in 675ms. Dedup gains site-suffix normalization plus an optional fuzzy check (`ARGO_DEDUP_FUZZY`), and a separator false-kill is fixed. |
| **v2.9.4** | **Explicit engine domain attribution + recovery chain circuit-breaker + gate sync**: `--engine X` supplements domain matching, so cached results are no longer blanket-general, and single-engine runs skip the empty first level. `config.yaml` domain rules gained unless-terms handling. |
| **v2.9.3** | **Full X post packaging + browser login-state lane + local-chain quality/speed fixes**: New `argo tweet` packs a full post into a `--out` bundle; `argo auth` adds browser login-state sessions excluded from the shared cache. English dispatch falls to 2.2-2.5s, image fingerprints 158ms→3.9ms. |
| **v2.9.2** | **DSH 0.2.1-alpha.1 compatibility verified + routing/guard fixes + js-run sub-skill**: The DSH plugin is verified on DSH 0.2.1-alpha.1, and `argo mcp inject` adds WorkBuddy as a seventh host. Import cost drops 50%, and js-run (a new sub-skill) runs web JS with no browser, P50 ≈1ms. |
| **v2.9.1** | **Content-quality optimization + content-farm resistance + latest-version adaptation**: Content-quality scoring better identifies and filters low-quality bulk-generated content, and local search gained a self-install-tree guard so argo's own source stops polluting results. |
| **v2.9.0** | **Source optimization + plugin/CLI parity + minor languages**: Sources 237→253 (keyless 198→219), adding 14 minor-language news sources and the CORE full-text source; Baidu and Sogou are revived. Import −79%, rerank 33ms→1.5ms, new `argo stats`. See [release notes](docs/RELEASE_NOTES_v2.9.0.md) |
| **v2.8.9** | **Output slimming + richer, faster retrieval + 5 international & fact-check sources**: Default output 15.5KB→5.2KB (−66%, `--envelope` restores the full envelope); slow-network dispatch −36%. Five keyless sources added, taking sources to 237 and keyless to 198. See [release notes](docs/RELEASE_NOTES_v2.8.9.md) |
| **v2.8.8** | **Real-user fixes (#12 key-alias silence, #13 proxy-less fetch) + across-the-board speedups + sources 218 → 235**: One egress path for all traffic (`ARGO_PROXY`). Cold start 2.1s→0.55s; new `argo answer` and `argo watch` (page-change watching), web_general fallback 41%→20%. See [release notes](docs/RELEASE_NOTES_v2.8.8.md) |
| **v2.8.7** | **218 sources / 89 domains + three fetch direct-out channels + routing trigger discipline + macro zero-result fix**: 218 sources across 89 domains after 50 new ones (statutes, standards, security, academia, news, legal). Fetch probes llms.txt and `.md` variants; `--engine` takes comma lists again. See [release notes](docs/RELEASE_NOTES_v2.8.7.md) |
| **v2.8.6** | **Hedged racing + Zhihu three-source split + declarative language dispatch + reachability gate + academic search protocol**: First-engine grace-window racing lets fast engines pay one call. Zhihu global search and personal data are wired in, dead sources surface, geo tail latency drops 56%. See [release notes](docs/RELEASE_NOTES_v2.8.6.md) |
| **v2.8.5** | **Native DSH plugin tools + MCP off by default + Windows compat + quota self-healing + fetch deadline**: `argo_search` and `argo_fetch` are now first-class tools; MCP is on demand only. Windows support (`install.ps1`), quota self-healing, and `ARGO_FETCH_DEADLINE_S` for the fetch deadline. See [release notes](docs/RELEASE_NOTES_v2.8.5.md) |
| **v2.8.4** | **Local data fusion + multi-client MCP inject + structured search + Keenable**: Research L1 can draw on first-hand local data (`file_inputs`, `recompute`), and `argo mcp inject` wires clients from `mcp/clients.yaml`. Query normalization and `--include-local` added. See [release notes](docs/RELEASE_NOTES_v2.8.4.md) |
| **v2.8.3** | **Multilingual routing fix + in-process anysearch + weighted RRF**: ja/ko queries return the target language; DE/FR/ES/IT via anysearch; weakest-link downweight (paper 2508.01405). See [release notes](docs/RELEASE_NOTES_v2.8.3.md) |
| **v2.8.2** | **Windows + unified evidence semantics**: npm `os` limit removed; UTF-8 path against GBK crashes; main-package `dsh.bundle`; `wide_research` quality gate. See [release notes](docs/RELEASE_NOTES_v2.8.2.md) |
| **v2.8.0** | **Evidence loop + jobs v3 + dual weather**: `fetch_required` / `--verify`; `argo job`; wttr.in + Open-Meteo; Parallel / You.com. See [release notes](docs/RELEASE_NOTES_v2.8.0.md) |
| **v2.7.3** | Engine-layer HTTP client; TF-IDF activates 25 verticals; 70-domain TTL; bilingual verticals. See [release notes](docs/RELEASE_NOTES_v2.7.3.md) |
| **v2.7.2** | Logged-in professional search (ego-search, off by default); JA/KO no longer mix Chinese engines. See [release notes](docs/RELEASE_NOTES_v2.7.2.md) |
| **v2.7.1** | SSRF hardening + routing health-state fix. See [release notes](docs/RELEASE_NOTES_v2.7.1.md) |
| **v2.7.0** | **Vertical structured modal cards**: Built-in `bocha` / `bocha_ai` engines; the `modal_card` domain recognizes real-time cards in one place: train tickets, fuel prices, calendar, horoscope, vehicles, hospital registration. See [release notes](docs/RELEASE_NOTES_v2.7.0.md) |
| **v2.6.2** | Merged improvement lines: network-environment awareness, weighted RRF with semantic cache, adaptive engine disabling, content safety with query variants, vertical engines and ja-ko routing. See [release notes](docs/RELEASE_NOTES_v2.6.2.md) |
| **v2.6.1** | v2.6.0 fix release: routing collateral-damage fix (`capital of` no longer grabs fact_check); version sync. See [release notes](docs/RELEASE_NOTES_v2.6.1.md) |
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

Apache License 2.0 © 2026 [taxueseek](https://github.com/taxueseek)

<p align="center">
  <a href="https://github.com/oil-oil/beautify-github-readme"><img src="assets/readme/made-with-beautify.svg" width="300" alt="README made with beautify-github-readme"></a>
</p>

---

> Good search is not about seeing more—it is about concluding with confidence, and knowing when you still should not.
