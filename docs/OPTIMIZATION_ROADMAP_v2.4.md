# Argo v2.4 系统优化路线图

> 基于 v2.2/v2.3 归档（`cbd8076`）、核心路径审计、竞品对照、本机实测。  
> 方法：重新定义问题 → 第一性原理 → MECE → 量化验收 → 驱动开发与测试。

---

## 0. 归档状态

| 项 | 值 |
|----|-----|
| 仓库 | `~/.claude/skills/argo` → `github.com/taxueseek/argo` |
| 归档 commit | `cbd8076` `feat(argo): v2.2/v2.3 证据两阶段 + fetch 栈 + 引擎注册消黑盒` |
| 单测基线 | `pytest tests/test_unit.py tests/test_evidence_v22.py` → **54 passed** |
| 未 push | 本地 main 领先 origin，需人工确认后再 `git push` |

---

## 1. 重新定义问题

| 层级 | 表述 |
|------|------|
| 表象 | 统一搜索要更快、更准、缓存更好用 |
| 第一性 | Agent 在**有限上下文 + 有限时间/配额**内，**吸收可核验事实**并降低幻觉 |
| 可操作定义 | 输出不是「链接清单」，而是「**可被采用的证据候选 + 可信度分解 + 可观测引擎状态**」 |

### 1.1 旧 KPI（废止）

- 召回条数、启用引擎数、单次 JSON 是否「看起来丰富」

### 1.2 新 KPI（MECE × 可测）

| KPI | 方向 | 测法 |
|-----|------|------|
| `serp_rate@top8` | ↓ | top8 中 SERP/跳转壳占比 |
| `useless_snippet_rate@top8` | ↓ | snippet 过短 / 纯导航话术占比 |
| `evidence_number_rate@top8` | ↑ | snippet 含可核验数字的比例 |
| `fetch_evidence_rate@top3` | ↑ | 对 top3 做 fetch 后仍含证据块的比例 |
| `absorbable_domain_count` | ↑ | 非 SERP、selection≥阈值 的独立域名数 |
| `p50/p95_latency_cold` / `_cached` | ↓ | 冷/热延迟分位 |
| `cache_hit_rate_repeat` | ↑ | 同查询 10 次内重复命中率 |
| `wasted_engine_ms` | ↓ | 空结果/超时引擎累计等待 |
| `cost_per_absorbable_fact` | ↓ | 付费调用成本 / 可吸收事实数 |
| `route_precision@domain` | ↑ | 路由域/主引擎与人工标注一致率 |

---

## 2. MECE 能力地图（现状一览）

```
Query
  ├─ Clarify     意图可执行？          [独立，未喂给 route]
  ├─ Route       选哪些引擎？          [TF-IDF + 域规则；零分塌缩 bug]
  ├─ Retrieve    调引擎拿候选          [并行≤3；无 circuit breaker]
  ├─ Fuse        RRF / 去重 / rerank   [rerank 阻塞；共识丢弃]
  ├─ Score       Selection×Absorption  [snippet 级；SERP 覆盖不全]
  ├─ Cache       L1+L2 复用            [key 缺 depth；无负缓存；无 per-engine]
  ├─ Fetch       正文可吸收？          [能力强；结果不缓存]
  └─ Research    多步分解              [子查询缓存不共享；social 参数风险]
```

四块证据职责（与 v2.2 一致，互不重叠）：

| 模块 | 负责 | 不负责 |
|------|------|--------|
| Selection | 能否进候选 | 正文写得好不好 |
| Absorption | 证据块密度 | 单独代表「真」 |
| Freshness | 时间可决策性 | 替代权威 |
| Consensus | 多源是否同向 | 覆盖单源细节 |

---

## 3. 本机实测（2026-07-23）

### 3.1 路由塌缩（P0）

| 查询 | 实际主引擎 | 问题 |
|------|-----------|------|
| `pytest fixtures` | **eastmoney** | TF-IDF 全 0 时回落到引擎表首项；金融引擎处理开发文档 |
| `Python asyncio` | local_stackoverflow | 可接受，但 domain=None |
| `贵州茅台股价` | eastmoney | 正确 |
| `transformer attention paper` | arxiv | 正确 |

**根因**：`semantic_route` 在全引擎 score=0 时仍返回「第一名」；`route_query` 把零分当有效语义路由。

### 3.2 延迟与缓存

| 场景 | 观测 |
|------|------|
| microbench 路由 | TF-IDF ~1.8ms，端到端 route ~9ms（达标） |
| 冷搜 `pytest fixtures` | ~2–6.7s；eastmoney 空结果仍进 combo |
| L1 二次命中 | ~0–14ms wall；`cached=true` |
| 改 `max_results` 3→5 | **缓存未命中**，再冷跑 ~2.4s |
| L2 条目 | 仅 ~10–11 条 → 跨会话复用极低 |
| eastmoney 空结果 | `[]`，约数百 ms 浪费（串/并时均抬升尾延迟） |

### 3.3 吸收信号

`docs.pytest.org` 官方文档：`selection=0.75`，`absorption≈0.32`（导航型 snippet），`credibility_fast≈0.54`。  
说明：**Selection 已过门槛，Absorption 被 snippet 形态压低**——高后果路径必须 fetch，而非只看搜索 JSON。

---

## 4. 竞品机制吸收（只吸收机制，不抄代码）

| 机制 | 来源 | 映射 Argo 模块 | 预期 |
|------|------|----------------|------|
| Circuit breaker 60s | Hound | route + engines | 砍死引擎等待 30–50% |
| Never answer from snippets alone | Hound | evidence / research 门控 | 降假阳性 |
| engines_consensus | Hound | rrf_merge 保留来源 | 排序+可信度加权 |
| Cache-first + ultra-fast | Wigolo | search 入口 | 重复查询 <10ms |
| engine_outcomes 8 态 | last30days | search 输出 schema | Agent 不误判空结果 |
| 查询陷阱预检 | last30days | clarify | 挡无效烧配额 |
| 子代理分解（仅 deep research） | Exa | research | 覆盖深度，不进热路径 |

### 明确不吸收

- 超长 SKILL 输出合同（last30days 回归史）
- 热路径 LLM 证据打分
- 全量搜索结果当场 fetch
- 复杂 LTR 替代 RRF
- 简单查询也走子代理

---

## 5. Top 优化项（ROI 排序）

### P0 — 本周可交付（约 6–8h）

| # | 项 | 改动点 | 验收 |
|---|-----|--------|------|
| 1 | **TF-IDF 零分回退** | `tfidf_router` / `route_query`：max(score)=0 时走 general 免费引擎，禁止 eastmoney 等垂直引擎 | `pytest fixtures` 主引擎 ∈ {anysearch, duckduckgo, local_bing…} |
| 2 | **cache key + depth** | `cache._key` / `get`/`set` 传 `depth` | fast/deep 不互污 |
| 3 | **负缓存 + 熔断** | 空结果/超时写 30–60s 负缓存；连续失败 skip | `wasted_engine_ms` ↓；二次调用 <1ms 跳过 |
| 4 | **engine_outcomes** | 替代纯 `errors[]` 字符串 | 区分 no-results / timeout / rate-limit |
| 5 | **SERP 覆盖补全** | bing/google/weixin.sogou list | `serp_rate@top8` 回归不劣化 |
| 6 | **social_sentiment 参数** | `research.py` `engine=` 非 `engines=` | social 模式不崩 |

### P1 — 两周（约 12–16h）

| # | 项 | 说明 |
|---|-----|------|
| 7 | **per-engine 结果缓存** | combo 缓存之外缓存单引擎结果，组合复用 |
| 8 | **max_results 柔性命中** | 缓存 n≥请求 n 时可截断返回，避免 3/5 分裂 |
| 9 | **rrf 保留 consensus_engines** | 共识≥2 → selection 加权 |
| 10 | **fetch URL 缓存** | 复用 SearchCache 形态，TTL 按 content age |
| 11 | **source_type 保底分修复** | eastmoney.com 经 eastmoney 引擎 authority≥0.85 |
| 12 | **rerank 可观测 + 可选跳过** | mode=fast 默认跳过 Bocha；结果标 `reranker` |
| 13 | **空结果不写长 TTL 正缓存** | 避免把「失败」固化成「无结果」 |

### P2 — 架构级（择机）

| # | 项 |
|---|-----|
| 14 | research 子查询共享父缓存 / 流式首包 |
| 15 | golden set 100 条 + A/B harness（serp/number/latency） |
| 16 | fetch_required 门控（高后果 research 强制 top-k fetch） |
| 17 | find_similar（缓存内近邻，非热路径） |

---

## 6. 开发闭环（测试驱动）

```
假设（可测 KPI）
  → 改最小模块（单文件优先）
  → 单测 + golden 回归
  → 本机 A/B：同查询 cold/warm × mode × depth
  → 写回 SKILL「Agent 纪律」与本路线图验收表
```

### 6.1 最小回归命令

```bash
cd ~/.claude/skills/argo
python3 -m pytest tests/test_unit.py tests/test_evidence_v22.py -q

# 路由塌缩守卫（应进 general，不进 eastmoney）
python3 -c "import sys;sys.path.insert(0,'scripts');from route import route_query;d=route_query('pytest fixtures');assert d['engine']!='eastmoney', d"

# 缓存隔离
python3 scripts/search.py "cache contract" --depth fast --mode auto --json >/tmp/a.json
python3 scripts/search.py "cache contract" --depth deep --mode auto --json >/tmp/b.json
# 期望：key 不同；或 deep 不复用 fast 的 shallow 结果

# 重复命中
python3 scripts/search.py "React hooks" --mode fast --json | jq '{cached,elapsed_ms}'
python3 scripts/search.py "React hooks" --mode fast --json | jq '{cached,elapsed_ms,cache_level}'
```

### 6.2 Golden 集种子（先 20 条，再扩 100）

| 查询 | 期望 domain/引擎族 | 禁止 |
|------|-------------------|------|
| pytest fixtures | general / tech | eastmoney |
| 贵州茅台股价 | stock_query | 纯社交 |
| 2026公募基金二季报 持仓 | fund_query | SERP-only top3 |
| transformer attention paper | academic | 财经 |
| 今天有什么科技新闻 | news/realtime | evergreen 长缓存污染 |
| React vs Vue 哪个好 | general/tech + compare | 误进 stock |

### 6.3 A/B 指标脚本（建议新增 `scripts/ab_eval.py`）

输入：查询列表 + before/after JSONL  
输出：`serp_rate` / `number_rate` / `p50_latency` / `cache_hit_rate` / `wasted_engine_ms`

---

## 7. 延迟上界（设计约束）

**目标模型（冷，parallel，mode=auto）：**

```
T ≈ T_route(<10ms) + T_rewrite(<20ms)
    + max(T_engine_i | breaker open)
    + T_fuse(<5ms) + T_rerank(可选 0 或 200–500ms)
    + T_score_embed(<20ms)
```

**当前偏离：**

1. 零分路由把垂直慢引擎拖进 critical path  
2. 空结果引擎无熔断 → `max` 被抬高  
3. rerank 同步阻塞  
4. CLI subprocess 冷启动 200–500ms 叠加  
5. cache 碎片（depth/n/mode）→ 本可命中却冷跑  

**mode=fast 约束：** 仅免费引擎 ≤2，跳过 rerank，优先 L1/L2，目标 p50_cached < 20ms、p50_cold < 2.5s。

---

## 8. 缓存复用设计原则（重定义后）

| 原则 | 说明 |
|------|------|
| 正确性优先于命中率 | depth/mode 必须隔离；空结果不得长 TTL 正缓存 |
| 分层复用 | L0 负缓存/熔断 → L1 进程 → L2 磁盘 → L3 per-engine partial → L4 fetch-by-url |
| 柔性命中 | `cached_n >= requested_n` 可截断返回 |
| 可观测 | 每次响应带 `cache_level` + 进程 stats 可选导出 |
| 不做 | key 堆满所有参数导致命中率归零 |

当前 key：`query|engines|max_results|domain|mode`  
目标 key：`norm_query|engines|domain|mode|depth` + **柔性 n**；引擎层另键 `norm_query|engine|depth`。

---

## 9. 不做什么

- 热路径全量 fetch / LLM 打分  
- 用「更多引擎」粉饰吸收率  
- 把社交 UGC 当真值交叉验证主证据  
- 未标 golden 前大改 TF-IDF 语料  
- 未确认前 `git push --force`

---

## 10. 建议执行顺序（下一迭代）

1. **修路由零分塌缩** + 路由 golden 5 条守卫测试  
2. **cache depth + 空结果策略 + n 柔性**  
3. **负缓存/熔断 + engine_outcomes**  
4. **SERP/consensus 小补丁**  
5. **ab_eval + golden 20** 固化基线后再动 rerank/research  

每步合并条件：单测绿 + 对应 KPI 不劣化（或明确 trade-off 写进 commit）。

---

## 11. P0/P1 落地验收（2026-07-23）

### 实现范围

| 项 | 状态 | 关键文件 |
|----|------|----------|
| TF-IDF 零分/低分回退 + 社交误路由过滤 | ✅ | `route.py`（MIN_SCORE=0.12） |
| cache depth + 柔性命中 + 空结果短 TTL | ✅ | `cache.py` |
| per-engine / fetch URL 缓存 | ✅ | `cache.py` + `search.py` + `fetch_v3.py` |
| 熔断 + 负缓存 | ✅ | `circuit_breaker.py` |
| engine_outcomes + wasted_engine_ms | ✅ | `search.py` |
| SERP bing/google/搜狗微信 | ✅ | `evidence.py` + `source_types_cn.json` |
| social_sentiment `engine=` 修复 | ✅ | `research.py` |
| RRF consensus_engines + 共识加权 | ✅ | `search.py` |
| source_type 保底分 | ✅ | `evidence.py` |
| fast 跳过 rerank + 可观测 | ✅ | `search.py` |

### 实测（`python3 scripts/ab_eval_p0p1.py`）

| 指标 | 结果 | 预期 |
|------|------|------|
| ab_eval | **23/23 PASS** | 全绿 |
| 单测 | **64 passed** | 全绿 |
| `pytest fixtures` 路由 | anysearch / general_search | ≠ eastmoney |
| `React hooks` 路由 | anysearch（不再 bilibili） | ≠ 社交误中 |
| 金融路由 | eastmoney 保持 | 不回归 |
| 冷延迟 | ~3.5s | 可接受 |
| 热延迟 L2 | **10–12ms** | <50ms |
| 加速比 | **~300×** | ≥10× |
| soft n=2 命中 | cached=true | ✅ |
| reranker fast | `skipped_fast` | ✅ |

### 回归命令

```bash
cd ~/.claude/skills/argo
python3 -m pytest tests/test_unit.py tests/test_evidence_v22.py -q
python3 scripts/ab_eval_p0p1.py
```
