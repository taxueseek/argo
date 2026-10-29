# Argo v2.10.1 发布说明

**版本**：2.10.1
**定位**：路由精度与边界收口、空链与双跑缺陷修复。集中收口 3 个社区反馈（Issue #29、#34、#35）：引入声明式商业意图否决宏消除医药商业误路由、接入免 Key 海外视频检索并归一化 YouTube URL、消除本机检索零命中双跑税与工具缺失静默、支持无外链 spec 与空占位符死链守卫、引擎状态自洽并清理孤儿文件、解耦夜间漂移门并杜绝基线自动滚降。

---

## 这次更新有什么（通俗版）

### 1. 声明式商业意图否决宏 `@commercial_market`（Issue #34.1）

在医药/生物等垂直域检索中，药靶点与蛋白常常同名（如 PD-1、BRCA1、HER2）。此前在自然提问「PD-1 抑制剂 2026 市场规模」或「重组胶原蛋白 医美 市场规模」时，查询会被 `protein_search` 或 `medical` 抢入生信数据库（如 UniProt），导致返回无关的蛋白质化学条目。
- 在 `scripts/route_domains.py` 中引入命名否决宏 `@commercial_market`，统一收纳市场规模、销售额、营收、集采、医保、报销、价格等商业分析词；
- 在 `config.yaml` 中为 `protein_search`、`chem_search` 和 `medical` 的双义规则统一接入 `unless: '@commercial_market'`，使商业分析类问题顺利让位给通用搜索与财经研报；
- 双向守卫：真正问蛋白质序列与分子特性的查询（如「PD-1 蛋白 结构域 序列」）仍精准进入对应生信库。

### 2. 海外/英文视频通道与 YouTube URL 归一化（Issue #34.3）

- 此前 `--video` 仅调用 Bilibili，海外/英文专业视频（如技术教程、学术演讲）无有效通道；
- 在 `video` 业务域与 `--video` 强制决策中接入无需 API Key 的 `parallel_free` 引擎；当调度层感知到视频模式时，自动为该源附加 `site:youtube.com` 定向召回 YouTube 视频；
- 在 `scripts/url_canon.py` 的追踪参数表中追加 `vl` 与 `si`，自动剥离 YouTube 语言及分享追踪后缀，彻底消除同一视频在结果集中的重复条目。

### 3. 本机检索零命中双跑税消除与告警透传（Issue #34.4）

- 修复 `scripts/local_seek.py` 中 `seek_query_payload` 的退出码处理：此前在零命中时，`seek.py` 返回文本并非完全空字符串（包含「未找到匹配」说明），导致判据误判为进程内执行异常，100% 触发子进程二次重跑（带来近 800ms 的无谓双跑税，且无法缓存零命中结果）。修复后零命中立即返回空结果并写入缓存；
- 在 Windows 等未安装 `ripgrep`/`grep` 的环境下，确定性错误不再静默丢弃，而是统一写入结构化 `warnings[]` 通道并呈现在响应中；同时 `sub-skills/local-seek/scripts/seek.py` 优先走 Python 标准库自带的 SQLite FTS5 索引。

### 4. 无 URL 槽 spec 支持与空占位符死链守卫（Issue #29）

- 针对语义检索、题录库等天然没有网页 URL 的声明式源，支持在 spec 中显式声明 `url_kind: none`（如 `engines/specs/sciverse.yaml`），由 SERP 闸门条目级入口放行，不再整批吞掉条目；
- 在 `scripts/engines_base.py` 中加强 `_format_url_template`：当模板中的字段（如 `{doi}`）为空或缺失时，不再拼接出 `https://doi.org/` 等裸前缀死链，而是返回空并清空非 HTTP 原生字段。

### 5. 引擎状态自洽与孤儿 spec 清理（Issue #29、#34.6）

- 修复 `scripts/engine_status.py` 与 `scripts/search_cli.py`：对未收录/未知的引擎名，正确返回 `status="unknown", enabled=False, routable=False, type="unknown"`，不再误报为 `ready`；
- `--list-engines --detail` 在瘦身输出和分组摘要中透传 `disabled_reason`，使未启用源（如需外部 CLI 的 `realtime_index`）的成因一目了然；
- 彻底清理仓库中历史遗留且未被引用的 `engines/specs/_cisa_kev.yaml` 与 `engines/specs/_osv.yaml` 孤儿文件。

### 6. CI 漂移门与夜间任务解耦（Issue #35）

- 在 `scripts/ci_drift_gate.py` 中将返回结果体积漂移降为纯信息项（只记录不判失败，顺应活网结果换血常态），严守条数地板与零结果结构回归门禁；
- 在 `.github/workflows/nightly.yml` 中移除了夜间绿跑后自动覆盖基线文件并直推 `main` 的步骤，基线保持为固定人工标定基线，彻底杜绝基线自动滚降（温水煮青蛙）与 bot 推送导致的分支分叉。

---

## 验证

- `bash scripts/pre-commit` 100% 通过（Python 3.14，~12s）；
- 模块体积门禁（`test_module_size_gate.py`）、文档预算（`test_context_budget.py`）、源目录校验（`test_engine_catalog.py`）全绿；
- 视频路由能力（`test_video_capability.py`）、无 URL 声明式源（`test_url_kind_none.py`）、本机检索进程内化与降级（`test_seek_inprocess.py`、`test_local_seek_broad_root.py`）、CI 漂移门（`test_ci_drift_gate.py`）单测全数通过；
- 真实多语言查询路由快照对齐（`tests/golden/routing_snapshot.json`）。

## 兼容性

- 完全向下兼容，无需修改既有配置；
- 移除了未引用的孤儿文件 `_cisa_kev.yaml` 与 `_osv.yaml`，修复未知引擎在状态输出中的虚假可用标记；
- 依赖项无新增，纯标准库支持。
