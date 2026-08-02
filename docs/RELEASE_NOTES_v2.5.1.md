# Argo v2.5.1 发布说明

**版本**：2.5.1  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：给 Agent 用的统一搜索与证据核验——不只返回链接，而是尽量给出能核验、能吸收的材料。

---

## 一句话

本版重点不是「再堆几个网页搜索」，而是让 **各类搜索更对口、日常更快、深度研究更全**：金融行情、宏观数据、化学物种等垂直源进日常路径；长尾源留给深度研究；策略收进统一的 `engine_policy`，减少越改越慢。

---

## 用大白话看本版

1. **问股价更像查行情**：A 股走新浪 / 腾讯等快照，美股走 Finviz 专线，够一条有用结果就停，不必先翻十页新闻。  
2. **问宏观更像查数据**：CPI、GDP、汇率等走 FRED、世界银行、国统局、欧统、汇率源；中国 GDP 不会轻易被美国序列「抢答」。  
3. **问化合物更像查字典**：分子式、CAS 等优先 PubChem，少被泛网页标题淹没。  
4. **日常少开引擎，研究再放宽**：平时 combo 精简（省时间、少噪音）；深度研究 / deep 才放开档案、研报类长尾源。  
5. **深度研究「抬」垂直源，不「锁」死**：子查询用 boost 把金融 / 学术源顶到前面，失败时仍可走通用路由，避免整条空结果。

---

## 为什么值得升级

| 你可能遇到的问题 | v2.5.1 怎么处理 |
|------------------|-----------------|
| 搜「茅台股价」又慢又杂 | 答案域 + early-stop + 短 combo，冷路径常见几百毫秒级 |
| 美股和 A 股搅在一起 | `us_stock` 与 `stock_query` 分流 |
| 宏观问句国家错位 | 国别词分流（如 worldbank 前置） |
| 引擎越加越多，日常反而变慢 | `engine_policy`：日常预算截断，研究全量 |
| 深度研究指定单一引擎，源挂了就空白 | boost 进 auto，不硬锁 |
| 不知道改完会不会又倒退 | `scripts/regression_p0p1.py` 离线 + 联网回归 |

---

## 本版增量（按主题）

### 1. 垂直搜索能力（金融 · 日常 · 数据）

在 v2.5.0 广义垂直源基础上继续加厚 **答案型** 源与域规则，例如：

| 方向 | 代表能力 | 典型问法 |
|------|----------|----------|
| A 股 / 港股行情 | 新浪行情、腾讯行情、东财资金流 | 「贵州茅台股价」「主力资金」 |
| 美股 | Finviz（日常）；Seeking Alpha 等偏研究 | 「AAPL 盘前」 |
| 宏观与汇率 | FRED、世界银行、国统局、欧统、实时汇率 | 「US CPI」「中国 GDP」 |
| 化学 / 药学 | PubChem | 「阿司匹林 分子式」 |
| 生物物种 | GBIF | 学名 / 物种检索 |
| 互联网标准 | RFC Editor | 「RFC 9110」 |
| 图书与公告等 | 微信读书、豆瓣、巨潮等（按域启用） | 书名 / 公告类查询 |

路由仍以 `config.yaml` 的 domain 规则 + TF-IDF 为准；**配置真源不变**。

### 2. 引擎分层与 combo 预算（性能护栏）

新增 `scripts/engine_policy.py`，`route_query` / `super_search` 贯通 `depth`、`context`、`engines_boost`：

| 场景 | 行为（简） |
|------|------------|
| 日常 `depth=fast` / `mode=fast|budget` | combo 最多约 2 个引擎；`research_only` 长尾默认不进 |
| 日常 `auto` + `balanced` | 最多约 3 个 |
| `context=research` 或 `depth/mode=deep` | 不截断；可含研究专用源 |
| 地理类查询 | 预算后仍尽量保留 OpenStreetMap（must_keep） |

目标：**防止「源越加越多 → 日常越来越慢」** 再次发生。

### 3. 深度研究：vertical_engines + boost

- 选题 profile（如 finance / academic / ai）增加 **`vertical_engines`**。  
- `collect_sources`：**始终 auto**，用 `engines_boost` 抬垂直源，**不再**轮转锁死单引擎。  
- MCP `argo_search` 默认 `envelope=False`，少造 candidates 开销；研究紧凑包可带 vertical 元数据。

### 4. 质量与回归

- 答案域 `early_stop_min_results` 等与此前 P0 修复一并纳入本线。  
- 回归：`python3 scripts/regression_p0p1.py --offline`（无网）或 `--all`（含联网）。  
- 单测对齐「日常 budget 下 stock combo 不一定含 eastmoney；deep 路径仍含」等语义。

---

## 安装（任选）

### 方式 A：一键脚本

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

### 方式 B：npx 启动 MCP

```bash
# 需 Node.js 18+ 与 Python 3.10+，并 pip install pyyaml
npx -y argo-search
# 若 npm 尚未同步最新，可用：
npx -y github:taxueseek/argo
```

### 方式 C：源码 / 本包

```bash
# 解压本 Release 的 argo-2.5.1.tar.gz 或 zip 后：
cd argo-2.5.1
pip install pyyaml
python3 scripts/search.py "贵州茅台股价" --json
python3 scripts/mcp_server.py
```

---

## 验证

```bash
python3 --version   # 3.10+
python3 -c "import yaml; print('PyYAML OK')"
python3 scripts/search.py --list-engines
python3 scripts/regression_p0p1.py --offline
python3 -m pytest tests/test_unit.py tests/test_mcp_compact.py -q
```

可选冷路径体感（需网络）：

```bash
python3 scripts/search.py "贵州茅台股价" --no-cache --json
python3 scripts/search.py "阿司匹林 分子式" --no-cache --json
```

---

## 升级注意

- 从 **v2.5.0** 升级：能力兼容；日常路由 combo **可能更短**（有意为之）。若脚本曾断言「eastmoney 必在日常 combo」，请改为检查主源或 `depth=deep`。  
- 从 **v2.4.x** 升级：建议整包替换后再挂 MCP / Skill 符号链接。  
- API Key 仍全部可选；不配则走免费 / 本地引擎（如天气源无 key 会回退通用引擎）。

---

## 资源

| 资源 | 说明 |
|------|------|
| 源码包 | `argo-2.5.1.tar.gz` / `argo-2.5.1.zip`（本 Release Assets） |
| 说明文档 | 仓库 [README](https://github.com/taxueseek/argo/blob/main/README.md) |
| 上版说明 | [v2.5.0](https://github.com/taxueseek/argo/blob/main/docs/RELEASE_NOTES_v2.5.0.md) |
| 回归 | `scripts/regression_p0p1.py` |

---

## 版本线（简）

| 版本 | 要点 |
|------|------|
| **v2.5.1** | 垂直答案源加厚；引擎分层 + combo 预算；研究 boost；约 110 源 |
| **v2.5.0** | 安装双路径 + 介绍页；改写不污染路由；热路径缓存；模块拆分 |
| **v2.4.1** | MCP 预热与紧凑回包；深度研究子技能；引擎生命周期 |
| **v2.4.0** | 低分回退、熔断负缓存、柔性命中、engine_outcomes |

MIT License © 2026 taxueseek
