# Argo 详细用法（v2.8.0）

> SKILL.md 只留核心命令；本页是参数大全与输出字段说明。

## search 完整参数

```bash
python3 scripts/search.py "查询词" \
  [--engine ENGINE]           # 强制引擎（可多个）\
  [--max-results N]           # 每引擎结果数\
  [--depth fast|balanced|deep] # 搜索深度\
  [--mode fast|auto|deep|budget] # 预算模式\
  [--local-first]             # 本地零成本聚合优先\
  [--no-cache] [--explain] [--json]\
  [--timeout S] [--progress]\
  [--since 7d|2026-08-01] [--until 2026-08-01] [--sort relevance|oldest|newest]\
  [--domain DOMAIN] [--sub_domain SUB_DOMAIN]  # 垂直域限定\
  [--input-kind auto|keyword|url-seed|known-url]\
  [--plan-only] [--force-search] [--no-envelope]\
  [--archive] [--archive-dir DIR] [--archive-tag TAG] [--archive-note NOTE]\
  [--verify [TOP_K]]          # 核验 top-K 未核验结果并回填证据分
```

**时间窗**：`--since`/`--until` 支持相对值（`7d`）或绝对日期（`2026-08-01`，含当天）；下推到支持时间窗的引擎，任意引擎组合融合后按 `published_at` 兜底过滤（`time_filtered: N`）；`--sort newest` 找最新动态、`oldest` 找最早出处。`wayback_cdx` 输出标准 `published_at`（CDX 最早快照）。

**Python 解释器**：脚本用 3.10+ 语法。`bin/argo` 自动探测 `ARGO_PYTHON` → python3.14/3.13/3.12/3.11/3.10 → 兜底。强制：`ARGO_PYTHON=/opt/homebrew/bin/python3.14 argo ...`。

## research 输出字段

```bash
python3 scripts/research.py "查询" [--sub-queries N] [--depth deep] [--budget N] [--json] [--verify N] [--route-strategy local_first|cost_aware|full]
```

- `key_findings`（按子查询分组）、`citations`、`gaps`、`source_distribution`
- `coverage_map`：各子查询覆盖状态 COVERED/PARTIAL/NOT_COVERED
- `verification_records`：claim-to-source 验证记录表（主张/来源/证据强度/核验方法/可核验性）
- `blind_spots`：显式盲区（未覆盖维度+单来源维度），不把「没搜到」写成「不存在」
- 证据强度分层：primary/secondary/tertiary/unknown
- Verify 阶段：`corroboration_level`（strong/moderate/weak/minimal/insufficient）、`cross_score`（0-1）、`top_sources`、`conflicts`（低证据层级混入标记）、`unverified_count`
- `fact_alignment`（auto/deep 且结果 ≥3 时启用）：抽取结构化事实（版本号/百分比/金额/日期/法规号），`fact_conflicts` 冲突标记、`fact_corroborated` 印证标记、`stats`
- `--budget N`：子查询上限，超限标记 `budget.exhausted=true` 并输出部分最佳答案

**社交舆情**：`--mode social-sentiment --platforms xiaohongshu,reddit,twitter` → `platform_breakdown` / `engagement_totals` / `top_topics` / `cross_platform_posts`。

## evidence 输出字段

```bash
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json [--high-stakes]
```

- `credibility.final` / `selection` / `absorption`
- `authority`（含 `is_serp`）、`freshness`（忽略「YYYY年以来」历史对比年）
- `evidence_density`（has_numbers/has_comparison/…）
- `cross_validation`（可吸收域名数）
- 中文信源覆盖与降权表：`backends/source_types_cn.json`

## clarify 输出字段

```bash
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

`ambiguities`（歧义词+可能含义+置信度）、`intents`（意图分类）、`recommended_strategy`（clarify_first/deep_research/split_search/direct_search）。

## 抓取三工具细节

### argo_fetch

```bash
argo fetch "https://example.com"                      # HTTP 优先，失败自动升级浏览器
argo fetch "https://example.com/long-article" --focus "关键词"   # BM25 聚焦，省 token
argo fetch "https://cloudflare-protected.com" --use-browser     # 强制反检测浏览器
```

- **降级触发**：HTTP 失败 / 内容 < 50 字符 / 检测到 CF 挑战 / 检测到 JS shell
- **Wayback 回退**：失败或空内容自动查最新快照（`fetch_method=wayback` + `snapshot_url`/`snapshot_ts`）
- **内容安全引擎**：抓取内容先过注入检测再交给 Agent——70+ 中英日韩俄阿希泰模式（指令覆盖/角色操纵/系统提示泄露/越狱/数据外泄/身份冒充/XSS）+ 编码归一化（零宽字符/RTL/Unicode 同形字/base64/URL 编码）+ 语义意图分析 + 风险评分 + 目标脱敏。输出 `content_security.content_clean / risk_score / threat_count / threat_types / redactions / content_lang`
- 单独调用：`python3 scripts/content_security.py "文本" --json` 或 `--stdin < content.txt`

### argo_screenshot

```bash
argo screenshot "https://example.com" [--full-page] [--output /tmp/page.png]
```

### argo_pdf

```bash
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]   # 支持本地路径
```

## 内容质量信号

所有抓取结果自动附带：`content_ok`、`page_type`（article/list/forum/qa/docs/js_shell/auth_wall/paywall）、`source_type`（gov/edu/github/news/blog/forum/qa/docs-site/ecommerce）、`is_official`、`is_stale`（>365 天）、`content_age_days`、`quality_score`（0-1：长度 0.2+密度 0.2+结构 0.2+证据密度 0.3+标题 0.1）、`has_numbers/has_definition/has_comparison/has_howto`、`absorption_score`、`selection/credibility_fast`。

## 子技能细节

### local-search（本地零成本聚合）

- 33 本地引擎、29 默认启用，覆盖 web_general/chinese/academic/news/code/reference/vertical 七大类
- 注册表：`sub-skills/local-search/engine_registry.py`（唯一真源，加载 config.yaml + parse_maps.yaml）
- 健康探针：canary 查询 + 反爬检测，状态缓存 5 分钟；连续 2 次失败或单次 >8s 标记 unavailable
- 智能路由：`sub-skills/local-search/smart_router.py` 按查询特征选引擎组合
- 输出与 argo 同 schema，直接参与 RRF 融合

### local-seek（本机文件搜索）

```bash
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --count   # L1 定位
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --context # L2 上下文
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --lines  # L3 精读
```

- 路由：rg（正文）→ fd（文件名）→ mdfind（Spotlight 全盘兜底）；中文「精确优先、2-gram 扩展兜底」
- 扩展：`--structural`（裸 except/空 catch/装饰函数）、`--git-log`/`--git-blame`、`--outline`、`--domains`
- MCP：`argo_local_search` subprocess 调用 seek.py，包装为 `file://` URL + `source=local_files`

### ego-search（登录态专业搜索）

```bash
python3 sub-skills/ego-search/scripts/ego_search.py status
python3 sub-skills/ego-search/scripts/ego_search.py search "AI 搜索" --runtime auto
python3 sub-skills/ego-search/scripts/ego_search.py fetch "https://www.zhihu.com/..." --site zhihu.com
python3 sub-skills/ego-search/scripts/ego_search.py merge --public /tmp/p.json --login /tmp/l.json
```

- 双运行时：ego lite + Kimi WebBridge，`--runtime auto|ego|webbridge`
- 与常规检索隔离：`search_partition=login`、`cache_eligible=false`；`--site host` 粘性空间
- 专业模式默认关：`enable`/`disable`/`status`
