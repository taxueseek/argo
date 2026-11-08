# Argo 操作参考（按需读取）

> 本页承接 SKILL.md 已下沉的低频**操作型**细节：工具清单、MCP/DSH 接入、配额、子技能、本地打通、工程纪律。
> 只在需要「配置 MCP / 接入 DSH 插件 / 用子技能 / 改引擎」时读，日常搜索/抓取/深度研究不必读。

## MCP 工具清单（19 个）

`tools/list` 默认只注入 `argo_search` / `argo_fetch` / `argo_local_search`（约 3.6KB/轮）。全量设 `ARGO_MCP_TOOLS=all`（或逗号名单）。`execute_tool` / `--call` 不受此限。URL 在 `results[].url`，默认不生成 `sources`。

| 工具 | 用途 |
|------|------|
| `argo_search` | 统一网络搜索（多引擎融合、时间/域名过滤） |
| `argo_local_search` | 本机文件/记录搜索（非联网） |
| `argo_local_read` | 白名单本地文本预览（`ARGO_LOCAL_READ_DIRS`；没配置就不放行） |
| `argo_recompute` | 可复算执行器（受限子进程重算数值；没明确授权就不执行） |
| `argo_research` | 深度研究取证（含 social-sentiment） |
| `argo_evidence` | 来源可信度评估（Selection×Absorption） |
| `argo_clarify` | 意图消歧 |
| `argo_crawl` | 站点级爬取（sitemap/BFS） |
| `argo_fetch` | 智能抓取（mode=extract 结构化提取） |
| `argo_extract` | 网页结构化提取（表格 / Meta / JSON-LD） |
| `argo_screenshot` | 网页截图 |
| `argo_pdf` | PDF 结构化提取 |
| `argo_social_search` | 社交平台搜索（mode=sentiment 舆情聚合） |
| `argo_article` | 微信公众号文章全文 |
| `argo_job` | 招聘岗位多平台聚合 |
| `argo_preflight` | 批量 URL 预检（可用 / 登录墙 / 已失效） |
| `argo_answer` | 直答（带引用的合成答案） |
| `argo_watch` | 网页变化监控（add / check / list / remove） |
| `argo_cite` | DOI → 引用条目（GB/T 7714 / APA / BibTeX） |

## MCP 服务

```bash
python3 scripts/mcp_server.py [--test]
```

多客户端 MCP 一键接入（自研，注入/诊断/还原/配置查看；客户端描述来源 `mcp/clients.yaml`）：

```bash
argo mcp status                 # 诊断各客户端（已安装/已配置）与统一配置真源状态
argo mcp config                 # 查看跨客户端共享的配置真源与凭证脱敏摘要
argo mcp config --path          # 仅输出配置文件绝对路径（方便编辑器快速打开）
argo mcp inject --all           # 注入所有已安装客户端（原子写 + 备份）
argo mcp inject --cursor        # 注入指定客户端（支持逗号分隔）
argo mcp undo --all             # 还原（精确移除 entry 或从备份回滚）
argo mcp inject --all --dry-run # 只预览不写
```

跨客户端单一真源：所有注入客户端统一共用 `~/.config/argo/env`（`ARGO_ENV_FILE` 可覆盖），无需在各个客户端重复配置环境变量；底层支持文件指纹感知，配置修改免重启即时热生效。安全可逆：写入前备份到 `~/.argo/mcp-backup/`（带时间戳），atomic_write（同目录 temp+rename），含密钥配置 0600 权限；TOML 走行级 append section 不破坏手写注释。支持 Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline / WorkBuddy 等。

细节见 `docs/MCP_SETUP.md`。

## DeepSeek Harness 插件接入

一键安装（原生 `argo_search` / `argo_fetch` 工具 + `web_search` seam + `wide_research` 调度；MCP 完整工具面默认不挂、按需在 profile patch 中开启——搜索/抓取高频路径走 CLI 单发同引擎同守卫，零常驻 token 开销）：

```bash
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

`wide_research` 与 `argo_research` 共用同一套证据语义：规划互补轨道（可带 `depends_on` 依赖分阶段，默认并行）→ 取证 → 来源账本（仅 http(s) URL 入账）→ 综合报告，输出自带 `quality_gate_results`（`passed` / `conclusion_cap`：failures→low、warnings→medium、干净→high）。`passed=false` 或 `conclusion_cap=low` 时禁止把报告结论当事实表述，先 `argo_fetch` / `--verify` 核验账本来源再下判断。取证引擎按 `references/research-protocol.md`「广泛研究」表按宿主能力选一条：宿主挂了本插件走有界 LLM worker（工具白名单硬保护，不允许调用 `argo_research`，防研究套研究）；没有该工具的宿主走 `research.py --work-packages` 零子代理管道。不要在两种形态间混用 worker 纪律。

### DeepSeek 官方 key：DSH 凭证服务原生供给

argo 的 `deepseek_search` 引擎（默认开启，按次计费 5 元/千次成功请求）与 DSH 官方搜索后端共用同一把官方 key。DSH 插件侧经原生凭证 seam 取用，无需手工配置：

- 解析路径：`ctx.get('credentials').resolve('DEEPSEEK_API_KEY')`，逐次解析——web Models 页轮换 key 后下一跳新连接即生效（池内连接最长存活 `searchIdleMs`，默认 60s）
- 层级：启动环境变量 > 调用目录 `.env` > `$DSH_HOME/.env` > 凭证服务管理的 `.credentials.yaml`
- 插件配置 `keyRefs`（默认 `['DEEPSEEK_API_KEY']`）控制转发哪些宿主凭证引用；空数组关闭；entry 须为 POSIX 标识符，apply 期 loud fail
- 安全：key 值只经内存进 argo 子进程 env，不读 key 文件、不落盘、不打日志
- 无 credentials 服务的旧宿主静默跳过，argo 侧该引擎按缺 key 停用
- 取用方式：`argo_search` 的 `engine` 参数显式指定 `deepseek_search`（新闻时事类查询优先，结果带 authority 权威分级 / rerank_score / 发布时间）；自动路由不含它，日常查询不烧额度

## 配额与引擎

```bash
python3 scripts/quota.py stats              # 配额状态
python3 scripts/search.py --list-engines    # 全量引擎清单（来源 config.yaml）
python3 scripts/search.py --list-engines --routable-only
```

TinyFish 搜索引擎（`tinyfish` / `tinyfish_news` / `tinyfish_paper`）与抓取渲染层（`_tinyfish_fetch`）**原生直连** `api.search/fetch.tinyfish.ai`，认证用 `X-API-Key`（官方标准）。配置：

```bash
export TINYFISH_API_KEY="sk-tinyfish-..."   # 去 agent.tinyfish.ai/api-keys 申请
```

未配置 `TINYFISH_API_KEY` 时，search 引擎不进路由（`env_ready=false`），抓取渲染层自动回退浏览器，不崩、不改变既有抓取行为。

## 子技能

| 子技能 | 位置 | 入口 |
|--------|------|------|
| local-search（本地零成本聚合） | `sub-skills/local-search/` | `python3 scripts/search.py "查询" --local-first` |
| local-seek（本机文件搜索） | `sub-skills/local-seek/` | `python3 sub-skills/local-seek/scripts/seek.py "查询" --path ~/notes --count`（MCP: `argo_local_search`） |
| local-image（本机图片多模态） | `scripts/local_image.py` | `argo local-image search "查询"`（需 `ARGO_LOCAL_IMAGE=1`） |
| ego-search（登录态专业搜索） | `sub-skills/ego-search/` | `python3 sub-skills/ego-search/scripts/ego_search.py search "AI 搜索" --runtime auto` |
| js-run（V8 算车道） | `sub-skills/js-run/` | 无独立入口：fetch 降级链第一级 E 自动触发（`fetch_jsrun.py`），开关 `ARGO_FETCH_JSRUN`（默认开，=0 关闭） |

细节见 `references/usage.md`（子技能章节）。

## 本地打通（三通道）

- **搜索体验**：`python3 scripts/search.py "查询" --include-local` —— 联网结果尾部并入本机文件命中（file:// 带行号，source=local_files，不参与融合评分）
- **本地分析**：MCP `argo_local_read`（白名单预览，`ARGO_LOCAL_READ_DIRS=~/data,~/notes` 配置；worker 侧在 wide_research 默认工具白名单）；数据计算走工作包 `recompute`（不授权就不执行）
- **插件 wide_research 接入**：`file_inputs`（本地一手数据，登记来源记录 sha256/路径，内容不入账）+ `recompute`（可复算契约，调度器侧受限执行，产出 `recomputed_values`）+ `include_local`（worker 搜索并入本机命中）；检查 `recompute_skipped` / `recompute_conflict` 保持一致核心，本地一手计入一手命中（防 no_source 假阴性）
- **成果复用**：`python3 scripts/research.py --search-archive "主题词" [--archive-since 日期]` —— 检索历史研究/搜索归档（`数据/argo-search-archive/runs/`），按主题词 + 时间窗列出历史 run 与来源统计

## 改完之后怎么验证（都不联网）

| 想确认的事 | 跑什么 | 要多久 |
|---|---|---|
| 路由还选得对 | `python3 scripts/matrix_search_eval.py --offline` | 秒级 |
| 排序质量有没有退步 | `python3 -m pytest tests/test_ranking_golden.py -q` | 秒级 |
| 结果条数与输出体积有没有退步 | `python3 scripts/replay_eval.py --check` | 秒级 |
| 这次改动到底比上次好还是差 | 先 `--save-baseline 旧.json`，改完再 `--compare 旧.json` | 秒级 |
| 全面回归 | `python3 -m pytest tests/ -q` | 约 5 分钟（4441 条，本机实测 4:45） |

## CI 流水线（与本地门禁同源）

线上跑的和本地跑的是同一批东西，不在 CI 里另写一套判断：

| 作业 | 触发 | 跑什么 | 实测（本机） |
|---|---|---|---|
| `gates` | push/PR | `bash scripts/pre-commit` 七道门（与本地完全一致） | 12s |
| `unit` | push/PR | 全量用例按历史耗时切 4 片（`tests/.test_durations`） | 4 片并行 71s（单片 54–70s） |
| `smoke` | push/PR | `node --check`、`npm pack --dry-run`、CLI/MCP 入口、状态目录可写 | 2s（runner 上含 npm 约 15s） |
| `extras` | push main / 手动 | 装全 `requirements-optional.txt` 九件后再跑一遍（与 unit 同样切 4 片，共用 venv 缓存） | 缓存命中后四片各 ≤3min |
| `nightly:replay` | 每日 | `replay_eval --check`（离线，带下限） | 0s |
| `nightly:drift` | 每日 | 重录 `tests/golden/drift_queries.txt` 并比基线报告 | 8s |
| `nightly:ledger` | 每日 | `argo stats` 的时延/结果率落曲线 | <1s |

`extras` 不进 PR 的原因写在 workflow 注释里：它 9 分钟，一进 PR 就把「2 分钟反馈」
变成「9 分钟反馈」，而可选依赖的漂移是周级别的。

两个踩过的环境坑（第一次真实跑 CI 时踩到，已写进用例）：

- **系统依赖 `ripgrep`**：local-seek 的 5 条用例依赖它，runner 默认没有，
  缺失会退回 grep 并红成「grep 执行失败」。`unit`/`extras` 作业显式装。
- **假域名的 DNS**：`docs.example.com` 这类地址在开发机可能被 ISP 劫持成公网 IP，
  在 CI 上是 NXDOMAIN，SSRF 防护 fail-closed 后整条 fetch 链返回 `blocked`。
  用假域名的用例声明 `allow_fake_hosts` fixture（`tests/conftest.py`），
  只放行可达性，SSRF 自身仍由 `test_tls_spoof.py` 覆盖。

本地复现某一片（与 CI 同参数；先 `pip install -r requirements-dev.txt`）：

```bash
python3 -m pytest --splits 4 --group 2 --durations-path tests/.test_durations --timeout=120
```

`requirements-dev.txt` 里除 pytest 插件外还有三件**测试自身要用**的可选依赖
（`curl_cffi` / `bs4` / `lxml`）：一批用例用 mock 替换 `curl_cffi.requests`，
补丁目标必须真实存在；HTML 引擎解析链缺 bs4 会让 keep 数归零、红成「解析坏了」。
缺这些件的环境里，相关用例会 skip 并写明原因（`tests/_deps.py`），而不是报 import 错误。

分片文件靠历史耗时切，用例增删后重新生成一次（约 5 分钟）：
`python3 -m pytest tests -q --store-durations --durations-path tests/.test_durations`

漂移基线（`tests/golden/drift_baseline*.json`）只在**确认变好**时重录，
不要因为某天红了就覆盖——那等于把回归擦掉：

```bash
python3 scripts/replay_eval.py --record --out tests/golden/drift_baseline.json \
  --queries "$(cat tests/golden/drift_queries.txt)"
python3 scripts/replay_eval.py --data tests/golden/drift_baseline.json \
  --save-baseline tests/golden/drift_baseline_report.json
```

**已知边界**：录制只保留当时真正被调到的引擎；重放是瞬时完成，早停时机与
真跑不同，会去要一个当时没调过的引擎并报「没录到」。所以 nightly 的漂移门
只比结果条数与输出体积，`not_recorded` 当信息项，不当失败。

`scripts/replay_eval.py` 的用法：它把真实搜索里每个引擎返回的内容录在
`tests/golden/pipeline_golden.json`，之后不上网也能重跑整条流程。

```bash
python3 scripts/replay_eval.py                     # 重跑，看每条的结果条数与输出体积
python3 scripts/replay_eval.py --save-baseline 旧.json   # 改动前先存一份基准
python3 scripts/replay_eval.py --compare 旧.json   # 改动后列出差异：多了什么、少了什么、位次怎么动
python3 scripts/replay_eval.py --case <id>         # 看某一条的明细
python3 scripts/replay_eval.py --record            # 只有它需要联网：重新录一批真实返回
```

**切记**：录下来的引擎返回只属于录制那一天。重跑保证的是「我们的处理逻辑没有退步」，
不是「现在网上就是这个样子」——网页结构会变、配额会变、不同地区的可达性也不同。
外部环境的变化要靠 `--record` 重新录一次，不要用重跑的结果下结论。

## 工程纪律（唯一来源）

- **代码来源** = 本仓库；**引擎声明来源** = `config.yaml`（外置 `engines/specs/*.yaml` 优先覆盖同名引擎）；注册表由 `scripts/sync_backends.py` 派生到 `backends/*`（含 `env_passthrough.txt`：`mcp_launch.sh` 的密钥透传名单，引擎声明了 `required_env` 或 `{KEY}` 占位符即自动进名单，不再手改启动器；非引擎通道的变量登记在 `sync_backends.EXTRA_PASSTHROUGH`）
- **提交门禁**：`scripts/install.sh` 把 `hooks/pre-commit` 装成调度器——原有 hook 改名 `pre-commit.user` 先跑，再直接执行仓内 `scripts/pre-commit`（不拷贝，始终是当前版本）。旧版「往既有 hook 末尾追一行」在既有 hook 以 `exit 0` 结尾时永远跑不到，重跑一次 install.sh 即迁移
- **宿主入口** 用 `scripts/link_source.py` symlink 指回来源（目标来自 `--to` / `ARGO_LINK_TARGETS` / 本机 `installs.local.yaml`）；禁止 rsync/多副本；禁止在产品代码写死主机 skill 路径
- **新增搜索源**：只改 `config.yaml`（必要时 `scripts/engines.py` 注册 builder）→ `python3 scripts/sync_backends.py && python3 scripts/sync_backends.py --check` → 回归 `python3 -m pytest tests/ -q`
