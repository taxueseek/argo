# 搜索成果归档与复用（v2.4.5）

## 为什么要归档（以及何时不要）

Argo 的热路径返回 `results[]` + `candidates[]` + `sources[]` 适合当次决策，但：

1. Agent 会话结束后候选易丢，后续分析无法复现  
2. 多次同类查询没有可比对的 run 级快照  
3. 「发现」与「吸收正文」混在同一响应时，容易把 snippet 当事实  

归档层只做一件事：**把当次发现 envelope 以不可覆盖的 run 目录写入工作区**，供复用、对比与后续核验。

### 分层默认（重要）

| 入口 | 默认归档 | 原因 |
|------|----------|------|
| `search.py` 日常 | **否** | 快问多、价值密度低；存则噪声淹没研究档案 |
| `research.py` 深度 | **是** | 多跳发现成本高，默认需要可复现 |

日常搜索只有用户明确「存一下 / 后续对比」时才 `--archive`。

## 硬边界

| 允许 | 禁止 |
|------|------|
| 落盘 query / results / candidates / coverage / limitations | 自动抓正文、下载媒体 |
| 新 run 目录（永不静默覆盖） | 把归档当成「已核验事实库」 |
| 从归档再触发 fetch/extract（需明确动作） | 搜索自动升级为登录态或私有读取 |
| known-url 仍 handoff，不写入「伪搜索 run」 | 把互动量当事实正确性 |

**发现 ≠ 吸收**。吸收正文用 `argo_fetch` / `extract` / 内容打包工具；搜索归档只保留发现层。

## 默认路径

```
<workspace>/数据/argo-search-archive/
  index.jsonl
  runs/
    YYYY-MM-DD/
      <run_id>/
        run-summary.json
        envelope.json
        candidates.jsonl
        results.jsonl
        coverage.json
        INDEX.md
```

解析顺序：

1. `--archive-dir`  
2. 环境变量 `ARGO_ARCHIVE_ROOT`  
3. 含 `AGENTS.md` 的工作区 → `数据/argo-search-archive`  
4. 否则 `./数据/argo-search-archive`

## CLI

```bash
cd <argo-root>

# 日常搜索：默认不归档；需要时再开
python3 scripts/search.py "贵州茅台 估值" --archive --archive-tag invest --json

# 深度研究：默认归档
python3 scripts/research.py "…议题…" --json
python3 scripts/research.py "…议题…" --no-archive   # 关闭

# 仅归档已有 JSON（stdin）
python3 scripts/search.py "q" --json | python3 scripts/archive_run.py write --tag adhoc

# 列出近期 run
python3 scripts/archive_run.py list --limit 20
python3 scripts/archive_run.py list --tag invest --query 茅台

# 查看某一 run
python3 scripts/archive_run.py show <run_dir>
python3 scripts/archive_run.py root
```

JSON 输出时，`archive` 字段包含 `run_id` / `run_dir` / `paths` / `counts`。

## 复用与分析

1. **复现检索上下文**：读 `run-summary.json` + `envelope.json`  
2. **候选池去重**：对多份 `candidates.jsonl` 按 `canonical_url` / `candidate_id` 合并  
3. **覆盖审计**：对比 `coverage.json` 的 backend 返回数与 truncated  
4. **核验流水线**：筛选高 `credibility_fast` / 权威域名候选 → `argo_fetch` → 更新本地笔记（**不改原 run**）  
5. **口径冲突**：同一 query 的多次 run 并列，禁止未对齐口径就合并数字  

示例（只读聚合）：

```bash
# 最近 5 次 run 的 query 与候选数
python3 scripts/archive_run.py list --limit 5 --json
```

## Agent 纪律

1. 需要可复现/可对比时加 `--archive`（或用户要求「存一下/归档搜索」）  
2. 日常轻问可不归档，避免磁盘噪声  
3. 归档后交付路径，不重贴大段 JSON  
4. 引用结论前：`verification.status` 仍为 `candidate` 的必须 fetch  
5. 隐私：默认不写 Cookie/Token；敏感 query 谨慎入库  

## 与已有能力的关系

| 能力 | 层 |
|------|----|
| `plan.py` / `input_kind` | 执行前分流 |
| `candidate_envelope` | 当次响应结构 |
| `archive_run` | 跨会话持久化 |
| `evidence` / `fetch` | 吸收与可信度 |
| L1/L2 cache | 加速重复查询，**不是**分析用 run 档案 |

Cache 可淘汰；archive run **默认永久保留**（清理由用户手动）。
