# Argo v2.7.0 发布说明

**版本**：2.7.0  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：在 v2.6.2（网络自适应 + 自适应学习 + 内容安全 + 垂直扩展）基础上，新增**垂直结构化模态卡**能力——内建博查双引擎（web 搜索 + AI 模态卡），把「实时结构化卡片」查询（火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机 / 汽车 / 挂号）统一识别并直连路由，同时修复既有 web 引擎的解析缺陷。

---

## 一句话

v2.7.0 = v2.6.2 全部能力 + **内建 `bocha` / `bocha_ai` 原生引擎**。`modal_card` 域让「今天金价多少」「明天杭州到上海的高铁票」「今年属相运势」这类问题返回**结构化卡片值**（而非网页摘要列表）；`bocha` web 引擎解析缺陷修复，中文网页召回恢复真实结果。

---

## 本版新增

### 1. 垂直结构化模态卡（modal_card 域 + bocha_ai 引擎）

- **新引擎 `bocha_ai`**（`scripts/engines_builders_cn.py` 专用 builder）：调用 AI 搜索接口，按 `content_type` 分支解析——`webpage` 消息转标准结果、`image` / 空消息跳过、其余解析为**结构化模态卡**（`card_type` + 原始 `card_data` + `_flatten_card` 扁平化摘要，score=1.0）
- **`_BOCHA_CARD_NAMES`**：几十种模态卡中文名映射（天气 / 百科 / 医疗 / 万年历 / 火车 / 星座 / 贵金属 / 汇率 / 油价 / 手机 / 股票 / 汽车等）
- **新路由域 `modal_card`**：在 `weather_query` 之后、`jin10_flash` 之前，patterns 覆盖 8 类结构化语义（火车票 / 油价 / 贵金属 / 黄历万年历 / 星座运势 / 手机参数 / 汽车报价 / 医疗挂号），`primary=bocha_ai`，`engines_combo=[bocha_ai, bocha]`
- **不误抢验证**：北京天气→weather_query、今日A股收盘→stock_query、美元兑人民币→macro_data、咳嗽症状→medical 均不受影响
- **新能力族 `structured_card`**（`engine_families.py`，label「垂直结构化模态卡」）
- **RRF 兼容**：模态卡无 URL 时由标题 / `card_type` 兜底参与去重（`_canonical_url` 或 `__title__` 键）

### 2. bocha web 引擎解析修复

- `config.yaml` 中 `bocha` 引擎 `type: http → bocha`，改用**专用 builder**（`_build_bocha_engine`）解析 `data.webPages.value` 双层嵌套——通用解析器只探测顶层 keys + 一层嵌套 value，此前导致真实调用解析 0 条、靠 recovery 兜底掩盖
- **freshness 动态化**：`_bocha_freshness` 按查询语义映射——周级（本周 / 近一周 / recent 等）→ `oneWeek`、日级（今日 / 实时 / breaking 等）→ `oneDay`、其余 → `noLimit`；周级判断在缓存敏感性检测之前执行，避免「本周政策」类查询被误判为日级
- **key 读取**：`_bocha_key` 按 `ARGO_BOCHA_API_KEY → BOCHA_API_KEY` 顺序读取（`engine_env.py` 已注册 `bocha` / `bocha_ai` 别名）

### 3. 配置与派生一致性

- `config.yaml` 新增 `bocha_ai` 声明（`type: bocha_ai`，url `ai-search`，timeout 12，coverage 含 vertical）
- `scripts/engines_builders.py` / `engines.py` 注册 `_BUILDERS` 的 `bocha` / `bocha_ai` 键
- `sync_backends.py` 派生引擎数保持 126，`--check` 一致性通过

---

## 质量验证

- 完整离线测试：**363 passed / 12 skipped / 19 subtests passed**
- 新增 `tests/test_bocha.py`（web 解析 / 模态卡 / freshness / 路由纯路径 / 缺 key / mode=fast / 403），配套 `test_fallback_extract` / MCP compact，定向套件全过
- 真实冒烟：`bocha` web 搜索返回 3+ 条真实中文结果；`bocha_ai` 受账号配额限制返回 403（引擎安全捕获为空，由 `bocha` 兜底出真实结果），解析逻辑经 mock 全量验证
- 端到端：「今天金价多少一克」正确路由 `modal_card` 域，`bocha_ai` 无配额 → `bocha` 兜底 5 条真实结果

### 解析改进闭口（本轮补完）

| 缺口 | 状态 |
|------|------|
| `_parse_generic` 二层嵌套 `data.webPages.value` + 字段别名 | 已完成 |
| `modal_card` 垂直保护（adaptive / must_keep / 不混 language·geo） | 已完成 |
| `mode=fast` 不因 cost_tier=low 裁空 bocha combo | 已完成 |
| 缺 `BOCHA_API_KEY` 仍保留声明引擎，不静默 anysearch | 已完成 |
| MCP social 默认平台 `hackernews,zhihu,bilibili` | 已完成 |
| RRF/去重 card_type 键 + `bocha_ai` 权重 1.3 | 已完成 |
| local-search `_fallback_extract` | 已完成 |

### 已知限制

- `bocha_ai` 模态卡接口（ai-search）是**独立于 web-search 的单独计费产品**，需在博查开放平台单独购买 AI 搜索套餐：未购买时返回 403 `not enough money or package quota`，引擎按空结果处理、不报错、由 `bocha` web 引擎兜底。购买套餐后无需改代码即自动生效（key 不互通额度，web-search 有配额不代表 ai-search 可用）
- `bocha` 熔断状态持久化在 `~/.cache/unified-search/circuit_breaker.json`；调试期连续空结果可能累积 `auto_disabled`，可用 `python3 -c "from circuit_breaker import CircuitBreaker; CircuitBreaker().reenable('bocha')"` 恢复

---

## 升级方式

```bash
# npx 最新
npx -y github:taxueseek/argo

# install.sh
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/install.sh | bash
```

配置 key：`export BOCHA_API_KEY="<your-key>"`（或 `ARGO_BOCHA_API_KEY`，优先级更高），建议写入 `~/.zshrc`。

---

## 与 v2.6.2 的差异

| 文件 | 改动 |
|------|------|
| `scripts/engines_builders_cn.py` | 新增 `_build_bocha_engine` / `_build_bocha_ai_engine` / `_BOCHA_CARD_NAMES` / freshness 动态化 / key 读取 |
| `scripts/engines_builders.py` / `engines.py` | 注册 `bocha` / `bocha_ai` builder 键 |
| `scripts/engine_families.py` | 新增 `structured_card` 能力族 |
| `scripts/engine_env.py` | 新增 `bocha` / `bocha_ai` key 别名 |
| `scripts/cache.py` | 周级 freshness 判断提前（避免「本周」类查询误判日级） |
| `config.yaml` | `bocha` type 改专用 builder；新增 `bocha_ai` 引擎 + `modal_card` 域 |
| `scripts/engines_base.py` | `_parse_generic` 深层列表探测 + 字段别名 |
| `scripts/route.py` | modal_card 纯路径 / cost·adaptive 保护 / 缺 key 不回落通用源 |
| `scripts/search.py` | WG-RRF 权重 + 模态卡 URL 去重键 |
| `scripts/mcp_server.py` | social 默认平台对齐 |
| `sub-skills/local-search/search_v3.py` | `_fallback_extract` |
| `tests/test_bocha.py` / `test_fallback_extract.py` | 离线用例（含 pure path / fast / 缺 key） |
| `SKILL.md` / `package.json` / `README.md` | 版本号 2.7.0 + 模态卡文档 |
