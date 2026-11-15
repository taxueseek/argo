# 接入新搜索引擎（标准化流程）

目标：在现有 Argo 上**自由增减、组合**搜索 API，走统一配置与准入，而不是每次改一堆散落文件。

## 引擎三档

| 档 | 何时用 | 你要交付什么 | 改 Python？ |
|----|--------|--------------|-------------|
| **L1 声明式 HTTP** | 标准 REST JSON 搜索 | `engines/specs/<id>.yaml` + 环境变量 | 否 |
| **L2 增强声明式** | 特殊 header/body/output_map | 同上，补全 `output_map` / `required_env` | 通常否 |
| **CLI 桥接** | 本机已装 CLI 输出 YAML/JSON（结构化） | `engines/specs/<id>.yaml`，`type: cli` + `cmd`/`search_args`/`output_format`/`filter_args` | 否 |
| **MCP 连接器** | 任意 MCP server（stdio 子进程或 Streamable HTTP）暴露的搜索工具 | `engines/specs/<id>.yaml`，`type: mcp` + `transport`/`tool`/`arguments`/`output_map` | 否 |
| **L3 插件** | 多端点、HTML、会话、非标协议 | `engines/plugins/<id>.py` + spec `type` | 是 |

模板：

- L1/L2：`engines/_template_http.yaml`
- CLI 桥接：`engines/_template_cli.yaml`（参考实现 `engines/specs/realtime_index.yaml`）
- L3：`engines/plugins/_template_plugin.py`

CLI 桥接关键字段：

- `cmd` / `search_args`：命令与参数模板，支持 `{query}` / `{n}` 占位符；裸命令（PATH 中）与绝对路径均可。
- `output_format: yaml`：走通用 YAML 解析，支持 `results`/`items`/`data` 或顶层 list，字段别名 `title|name`、`url|link`、`snippet|description|content`，并保留 `published_at` 发布时间维度。
- `filter_args`：条件参数表。查询带 `--since`/`--until`（如 `7d` / `2026-08-01`）时自动追加对应 CLI 参数；未携带则不追加。缓存按时间窗隔离，不会串结果。

MCP 连接器关键字段（适配器 `scripts/engines_mcp.py`，样板 `engines/specs/grepapp_mcp.yaml`）：

- `transport: http | stdio`：http 走 Streamable HTTP（JSON-RPC，SSE 帧自动解析，`Mcp-Session-Id` 自动回传）；stdio 走子进程换行帧（`command` 必须是列表，禁 shell，env 走 `build_child_env` 白名单）。
- `tool`：单一 allowlist，只可调用声明的那一个工具；`arguments` 参数模板支持 `{query}` / `{n}` 占位符并做 int/bool 类型保真。
- 内容层二选一：`output_map.mode: json`（默认）——`structuredContent` 或 JSON 文本走与 http 引擎同一套字段映射；`mode: text`——纯文本工具用 `text_split` 分块正则 + `item_title_regex` / `item_url_regex` / `item_summary_regex` 逐块抽字段。
- `isError` 的结果不产出条目（fail-visible）；json 模式收到非 JSON 文本同样返回空并告警，不编造条目。
- 接入新连接器建议 spec 带 `synthesized: true` 标记，人工确认后摘除。

## 可选结果字段（图源 / 全文源必看）

结果 dict 只要求 `title` / `url`，其余字段原样透传到融合、精排与 JSON 输出
（无字段白名单，`rrf_merge` 与 `local_five_dim_rerank` 都不会裁字段）。三个
**可选**字段有下游消费者，写对了才有价值：

| 字段 | 语义 | 谁在用 |
|------|------|--------|
| `image_url` | 可直接打开的图片地址 | 结果展示；判断「这是图源」的唯一依据 |
| `image_license` | 该图的授权/权利状态 | 合规展示（如 `CC0`、`公有领域（NASA）`） |
| `full_text_url` | **可确定性取到正文**的端点 | `evidence_loop.gate_results` 的取数建议优先用它，见下 |
| `upstream_authority` | 上游原生权威档 0-4（只透传不赋权） | `search_rank`/`search_output` 合成 `authority_level`；`evidence.compute_credibility` 挂到 `credibility.authority.upstream_level` 供取证引用 |

> **字段落点**：这些字段进的是 `results`（以及归档用的 `candidates` 视图），
> **不进** `sources`。`build_sources` 是「底部相关链接」形态的**稳定 5 字段投影**
> （`ref/title/url/engine/score/snippet`），有意裁剪，别往里加字段。消费方要拿
> 图片/正文端点请读 `results`。

### `image_url` 的三条硬约束

1. **不对就不写**：上游没给图（或 Met 这类对非公版件不给链接）时**不要**填空串或详情页 URL——宁缺勿假，下游据此判断「有无图」。
2. **路径必须指向字符串**：声明式 `output_map` 里写 `item_image: links.0.href`。NASA 的 `links.0` 是 `{href, rel, render, …}`，写成 `links.0` 会被 `_coerce_field` 当 dict 丢成空串。
3. **图床可能有额外要求**：AIC 的 IIIF 要求 `AIC-User-Agent` 头才放行（否则 403），复用该 URL 抓图时要带上。

声明式源可在 spec 顶层写 `image_license: "…"` 作为常量（授权不随条目变化时用，
如 NASA 公版）。

### `full_text_url` 什么时候该写

当**给人看的页面拿不到正文**、而该源另有确定性正文端点时写它。两个实测例子：

- `gutenberg`：`/ebooks/{id}` 是下载门户页（去标签首段全是 noprint 脚本），gutendex 在 `formats` 里已给纯文本 URL；
- `egov_law`：`/law/{id}` 是 JS 空壳页（去标签后只剩「e-Gov 法令検索」），`/api/1/lawdata/{id}` 才是官方全文 XML。

写它的收益是让取数建议走直连，省掉「门户页找下载链」或浏览器渲染那几级；
**如果人类页面本身就能取到正文，不要写**（那是无消费方的装饰字段）。

回归门禁：`tests/test_image_sources.py`、`tests/test_full_text_sources.py`。

## 环境变量规范

```bash
# 推荐（新）
export ARGO_TAVILY_API_KEY=...
export ARGO_EXA_API_KEY=...
export ARGO_MYENGINE_API_KEY=...

# 兼容（旧名仍可用）
export TAVILY_API_KEY=...
export EXA_API_KEY=...

# 可选：白名单 / 黑名单（逗号分隔）
export ARGO_ENABLE_ENGINES="hackernews,duckduckgo,eastmoney,tavily"
export ARGO_DISABLE_ENGINES="brave,felo"
```

策略类配置（路由权重、TTL、RRF）仍在 `config.yaml`，与密钥分离。

## 生命周期

```
注册声明 → 配置注入 → 标准化验证 → 生产准入
   YAML        env         validate        admission
```

### 1. 注册声明（L1 示例）

```bash
cp engines/_template_http.yaml engines/specs/myengine.yaml
# 编辑 engine_id / url / headers / body / output_map / required_env
```

启动时 `config.py` 自动 merge `engines/*.yaml` 与 `engines/specs/*.yaml`（`_` 前缀跳过）。

也可继续写在主 `config.yaml` 的 `engines:` 段（存量方式）。

### 2. 配置注入

```bash
export ARGO_MYENGINE_API_KEY="..."
# 可选：仅启用子集做试验
export ARGO_ENABLE_ENGINES="myengine,hackernews,duckduckgo"
```

缺 Key 的引擎：

- **自动路由**：跳过
- **强制** `--engine myengine`：仍可调用（通常返回 `[]`）

### 3. 标准化验证

```bash
cd ~/.workbuddy/skills/argo   # 或你的安装路径

# 连通性 + schema
python3 scripts/engine_validate.py --engine myengine --stage health

# 质量基准（固定 query 集）
python3 scripts/engine_validate.py --engine myengine --stage quality

# 全量 + 写入准入 + 生成文档
python3 scripts/engine_validate.py --engine myengine --stage all --admit --write-doc

# 批量：所有 free 且 env 就绪
python3 scripts/engine_validate.py --all-free --stage health --admit
```

通过 health 且 `--admit` → `~/.cache/unified-search/admission/<id>.json` 中 `blocked: false`。

### 4. 生产准入与观测

```bash
# 表格式状态
python3 scripts/search.py --list-engines --detail

# 仅可自动路由
python3 scripts/search.py --list-engines --detail --routable-only

# JSON
python3 scripts/search.py --list-engines --detail --json

# 单引擎状态
python3 scripts/engine_status.py --engine myengine --json
```

手动熔断：

```python
from engine_admission import set_blocked
set_blocked("myengine", True, reason="quota_exhausted")
```

## 组合与优化

- **减少**：`enabled: false` 或 `ARGO_DISABLE_ENGINES`
- **增加**：丢 YAML / 插件 → validate → admit
- **组合**：改 `config.yaml` 的 `domains[].engines_combo`，或 TF-IDF `domain_profiles`
- **预算**：`--mode fast|auto|deep|budget` 继续按 cost_tier 过滤

## 目录真源

| 路径 | 职责 |
|------|------|
| `config.yaml` | 主配置、域规则、cost_tiers、存量引擎 |
| `engines/specs/*.yaml` | 外置引擎声明（推荐新引擎落点） |
| `engines/plugins/*.py` | L3 自定义 builder |
| `scripts/engine_env.py` | Key 别名与 ENABLE/DISABLE |
| `scripts/engine_admission.py` | 准入状态 |
| `scripts/engine_validate.py` | 验证 CLI |
| `backends/engine_registry.yaml` | 元数据/观测目录（非运行时唯一真源） |

运行时引擎列表以 **merge 后的 config engines** 为准。

## 验收清单

- [ ] `engine_validate --stage health` 为 `pass` 或合理 `skipped`（缺 Key）
- [ ] `--list-engines --detail` 中 `routable=True`（需要进自动路由时）
- [ ] `search.py "查询" --engine <id>` 有结构化结果
- [ ] 无 Key 时自动路由不含该引擎
- [ ] `blocked=true` 后自动路由剔除
