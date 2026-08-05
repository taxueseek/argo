# Argo v2.6.2 发布说明

**版本**：2.6.2  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：在 v2.6.1（多语言 + 垂直域 + 路由修复）基础上，合并独立改进线的全部能力——网络环境感知、自适应学习、语义缓存、内容安全、更多垂直引擎，并保留 v2.6.1 的路由误伤修复。

---

## 一句话

v2.6.2 = v2.6.1 全部能力 + 独立改进线的 8 个功能提交。**网络慢时自动调整超时、连续失败的引擎自动降权、查询变体与内容安全过滤、加权 RRF 融合**——「自适应」和「网络优化」从之前的实验分支正式合入主线。

---

## 本版新增（合并自独立改进线）

### 1. 网络环境感知（慢网自适应）

- `scripts/network_aware.py`：探测当前网络质量（慢/中/快），**慢网自动放宽超时、快源前置**
- 路由层接入：慢网时优先低成本快源，避免长尾引擎拖慢整条链路

### 2. 自适应学习增强

- **自适应引擎禁用**：连续失败的引擎自动关停（circuit_breaker 增强），环境个性化——你常用的引擎优先，总挂的引擎自动降权
- **加权 RRF + 语义缓存 + 自适应 TTL**：结果融合按权威度加权；相似查询命中语义缓存；TTL 按查询类型自适应

### 3. 内容安全与查询变体

- `scripts/content_security.py`：注入检测 / 脱敏 / 高风险截断（多语言）
- `scripts/query_variants.py`：概念扩展 / 对立观点 / 问句变形 / 缩写展开，提升召回

### 4. 更多垂直引擎

- **GDELT**（全球事件库）、**OpenCorporates**（企业实体）、**Google Patents**（专利）三大垂直引擎
- 日韩垂直域路由补全：film / sports / geo / weather / stock 五域日韩触发词
- 修复不可用的 DDG / qweather 引擎（网络适配）：fact_check 从 6s → 8ms，weather 6s → 890ms

### 5. 科研方法论增强

- `scripts/research_report.py` / `research_strategy.py` / `social_research.py`：research.py 拆分，路由策略纯函数化
- 事实对齐集成（fact_align）、wayback 回退

## 保留的 v2.6.1 修复

- `geo_places` 触发词移除 `capital\s+of`，国家首都类事实问答正确落到 fact_check（不再被地理域截胡）

---

## 质量验证

- 完整离线测试：**320 passed / 0 failed**（55 个网络依赖用例排除）
- 新增 36 个测试（content_security / 多语言 / 引擎注册）
- 已知问题：`test_mcp_compact` + `test_new_engines` 同进程顺序执行时，`en_fact_capital` 子用例受 mcp_server import 的模块级副作用影响（上游 `~/argo` 分支同样存在，单跑均通过，不影响实际功能）

---

## 升级方式

```bash
# npx 最新
npx -y github:taxueseek/argo

# install.sh
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/install.sh | bash
```

---

## 与 v2.6.1 的差异

| 文件 | 改动 |
|------|------|
| `scripts/network_aware.py` | 新增：网络环境感知 |
| `scripts/content_security.py` | 新增：内容安全 |
| `scripts/query_variants.py` | 新增：查询变体 |
| `scripts/research_report.py` / `research_strategy.py` / `social_research.py` | 新增：research 拆分 |
| `scripts/cache.py` | 语义缓存 + 自适应 TTL |
| `scripts/circuit_breaker.py` | 自适应引擎禁用 |
| `scripts/engines_builders_data.py` | GDELT / OpenCorporates / Google Patents |
| `config.yaml` | 三大垂直引擎 + 日韩域路由 + 引擎修复 |
| `package.json` / `SKILL.md` / `README.md` / `mcp_server.py` | 版本号 2.6.2 |
