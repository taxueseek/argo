---
name: argo
description: Argo 阿尔戈 — 统一搜索与证据核验。多语言检测与跨语言回退；约 120+ 引擎 TF-IDF 路由 + RRF；影视/体育/地理/组织/媒体/金融/宏观/化学等垂直源；垂直结构化模态卡（火车票/油价/贵金属/万年历/星座/手机/汽车/挂号）；日常 combo 预算与深度研究 boost；recovery 防污染；Selection×Absorption；MCP（含 argo_local_search）。入口：npx github:taxueseek/argo / mcp_server.py。
version: 2.8.0
triggers:
  - 搜索
  - 查一下
  - 搜一下
  - 核实
  - 查证
  - 可信度
  - search for
  - look up
  - fact check
---

# Argo v2.8.0 — 统一搜索与证据核验

> 从「帮你搜到」升级为「帮你核到」。搜索输出自带**证据闭环**：高后果问题（金融/医疗/法律/事实核查）标 `fetch_required`，每条结果标 `fetch_suggested`；`--verify` 一键核验正文并回填「核实后证据分」，核实过的链接自动记忆，二次搜索直接显示已核实。本版详见 `docs/RELEASE_NOTES_v2.8.0.md`。

## 快速上手

```bash
python3 scripts/search.py "查询词"            # 自动路由搜索
python3 scripts/search.py "查询词" --json     # JSON 输出（供 Agent 消费）
python3 scripts/search.py "查询词" --verify 3 # 核验 top-3 并回填证据分
python3 scripts/research.py "复杂问题" --json # 深度研究（分解子查询 + 综合报告）
```

## 核心命令

### search — 统一搜索

| 参数 | 说明 |
|------|------|
| `--engine <name>` | 强制引擎（anysearch/byted/bocha/exa/tavily/eastmoney/zhihu/arxiv/pypi/mdn/hackernews/v2ex…，全量见 `--list-engines`） |
| `--local-first` | 本地零成本聚合优先（local_search 33 引擎） |
| `--mode fast|auto|deep|budget` | fast 免费优先 / auto 成本感知（默认）/ deep 质量优先 / budget 配额控制 |
| `--explain` | 解释路由决策（含 TF-IDF 分数） |
| `--no-cache` / `--depth fast|balanced|deep` | 跳过缓存 / 搜索深度 |
| `--since 7d|2026-08-01` `--until` `--sort relevance|newest|oldest` | 时间窗过滤 + 时间排序 |
| `--verify [N]` | 对 top-N 未核验结果 fetch 正文，回填证据分（URL→证据分缓存，同 URL 二次搜索自动复用） |
| `--domain` `--sub_domain` | 垂直域 / 子域限定 |

### 增强三工具

```bash
# research — 深度研究（问题分解→多源采集→综合报告 + citations + gaps）
python3 scripts/research.py "查询" [--sub-queries N] [--depth deep] [--budget N] \
    [--route-strategy local_first|cost_aware|full] [--json] [--verify N]

# 社交舆情模式
python3 scripts/research.py "iPhone 16 用户评价" --mode social-sentiment --platforms xiaohongshu,reddit,twitter

# evidence — 可信度评估（Selection×Absorption）
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json [--high-stakes]

# clarify — 意图消歧
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

### 抓取三工具（`bin/argo` 入口）

```bash
argo fetch "https://example.com" [--focus "关键词"] [--use-browser]
# HTTP→反检测浏览器自动降级 + BM25 聚焦提取 + 质量信号 + Wayback 快照回退 + 内容安全引擎
argo screenshot "https://example.com" [--full-page] [--output /tmp/page.png]
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]
```

### MCP 服务

```bash
python3 scripts/mcp_server.py [--test]
```

工具名：`argo_search`、`argo_local_search`（本机文件/记录搜索，非联网）、`argo_research`（含 social-sentiment）、`argo_evidence`、`argo_clarify`、`argo_crawl`、`argo_fetch`（mode=extract 结构化提取）、`argo_screenshot`、`argo_pdf`、`argo_social_search`（mode=sentiment 舆情聚合）。

DeepSeek Harness 插件一行安装：

```bash
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

### 配额与引擎

```bash
python3 scripts/quota.py stats              # 配额状态
python3 scripts/search.py --list-engines    # 全量引擎清单（真源 config.yaml）
python3 scripts/search.py --list-engines --routable-only
```

## Agent 执行纪律

1. **高后果问题**（金融/医疗/法律/事实核查）：search → evidence（或看 `credibility_fast`）→ fetch 高分 URL → 再下结论；`fetch_required=true` 时禁止跳过核验
2. **数字**：必须标注口径（全市场/主动/持仓市值 vs 占比）；冲突时并列，禁止口径未对齐合并
3. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源
4. **社交帖**：叙事/舆情，不进事实真值
5. **分层查询**：事实类 deep 至少 2–3 条子查询（来源 / 对比数据 / 关键主体）

## 证据闭环（v2.8.0）

搜索输出已带证据门控，Agent 可编程判断「现在能不能下结论」：

- `fetch_required`：bool。命中高后果域时为 true，下结论前必须核验正文
- `evidence_loop.suggested / verified_count / pending_count`：建议核验 URL 列表 / 已核验 / 待核验
- 每条结果：`fetch_suggested`（是否建议核验）、`has_fetched_evidence`（是否已核验）、`post_fetch_absorption`（正文级吸收分，核验后回填）

```bash
python3 scripts/search.py "贵州茅台股价" --verify 3
# [verify] 核验 3 条，improved=2 unchanged=1 degraded=0 mean_delta=0.18
```

## 子技能

| 子技能 | 位置 | 入口 |
|--------|------|------|
| local-search（本地零成本聚合） | `sub-skills/local-search/` | `python3 scripts/search.py "查询" --local-first` |
| local-seek（本机文件搜索） | `sub-skills/local-seek/` | `python3 sub-skills/local-seek/scripts/seek.py "查询" --path ~/notes --count`（MCP: `argo_local_search`） |
| ego-search（登录态专业搜索） | `sub-skills/ego-search/` | `python3 sub-skills/ego-search/scripts/ego_search.py search "AI 搜索" --runtime auto` |

## 工程纪律（单一真源）

- **代码真源** = 本仓库；**引擎声明真源** = `config.yaml`（外置 `engines/specs/*.yaml` 优先覆盖同名引擎）；注册表由 `scripts/sync_backends.py` 派生到 `backends/*`
- **宿主入口** 用 `scripts/link_source.py` symlink 指回真源（目标来自 `--to` / `ARGO_LINK_TARGETS` / 本机 `installs.local.yaml`）；禁止 rsync/多副本；禁止在产品代码写死主机 skill 路径
- **新增搜索源**：只改 `config.yaml`（必要时 `scripts/engines.py` 注册 builder）→ `python3 scripts/sync_backends.py && python3 scripts/sync_backends.py --check` → 回归 `python3 -m pytest tests/ -q`

## 参考文档

| 文档 | 内容 |
|------|------|
| `references/engines.md` | 引擎全景：垂直域 / 社交 / 学术 / 本地引擎表 + 路由规则 + 引擎调用示例 |
| `references/usage.md` | 详细用法：参数大全、三大工具输出字段、子技能细节 |
| `references/architecture.md` | 架构：文件结构、证据流水线、量化公式、输出 JSON Schema、内容质量信号 |
| `docs/RELEASE_NOTES_v2.8.0.md` | 本版发布说明 |
| `docs/ADDING_NEW_ENGINE.md` | 新增引擎指南 |
