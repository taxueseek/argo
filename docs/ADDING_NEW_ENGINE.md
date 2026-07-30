# 接入新搜索引擎（标准化流程）

目标：在现有 Argo 上**自由增减、组合**搜索 API，走统一配置与准入，而不是每次改一堆散落文件。

## 引擎三档

| 档 | 何时用 | 你要交付什么 | 改 Python？ |
|----|--------|--------------|-------------|
| **L1 声明式 HTTP** | 标准 REST JSON 搜索 | `engines/specs/<id>.yaml` + 环境变量 | 否 |
| **L2 增强声明式** | 特殊 header/body/output_map | 同上，补全 `output_map` / `required_env` | 通常否 |
| **L3 插件** | 多端点、HTML、会话、非标协议 | `engines/plugins/<id>.py` + spec `type` | 是 |

模板：

- L1/L2：`engines/_template_http.yaml`
- L3：`engines/plugins/_template_plugin.py`

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
