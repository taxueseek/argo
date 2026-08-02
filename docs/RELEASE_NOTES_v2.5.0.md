# Argo v2.5.0 发布说明

**版本**：2.5.0  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：给 Agent 用的统一搜索与证据核验——不只返回链接，而是尽量给出能核验、能吸收的材料。

---

## 一句话

本版在 v2.4.1「搜得快、回得省、研得深」之上，把**装得上、用得稳、路由更准**补齐：介绍页重写，安装脚本 + npx 双路径，查询改写与路由解耦，引擎与 MCP 模块拆分，配置单一真源。

---

## 用大白话看本版

1. **更好装**：一条 `install.sh`，或 `npx -y argo-search` 挂 MCP；Skill 用符号链接挂入口，不复制多份代码。  
2. **搜得更对路**：口语可以改写再检索，但**路由仍看原问题**，减少「苹果股价却进购物站」这类误吸。  
3. **重复问更快**：路由热路径可缓存；熔断 + 负缓存，挂掉的源不会一直拖后腿。  
4. **Agent 更省上下文**：MCP 紧凑回包（延续并强化 v2.4.1），snippet 可控。  
5. **代码更好维护**：engines / MCP 大文件拆分；`config.yaml` 为引擎真源，backends 可派生。

---

## 为什么值得升级

| 你可能遇到的问题 | v2.5.0 怎么处理 |
|------------------|-----------------|
| 介绍页还是早期版本，不知道怎么装 | README 重写：安装脚本 / npx / 克隆 / Skill 链接 |
| 只有 clone，没有一键或 npm 入口 | `scripts/install.sh` + `npx -y argo-search` |
| 查询改写把域路由带偏 | 改写只改检索串，路由永远用原始 query |
| 大文件难改、MCP 与引擎缠在一起 | engines_base / builders 拆分；mcp_tools / mcp_payload |
| 本机路径、Key 写进仓库 | 社交引擎改为仓库相对路径；`installs.local.yaml` gitignore |

---

## 本版增量（按主题）

### 1. 安装与介绍

- 新增 **`scripts/install.sh`**：克隆/更新到 `~/.local/share/argo`（可改）、装 PyYAML、可选 `--link` 挂 Skill。  
- **npx**：`package.json` 提供 `argo-search` → `bin/argo.js`（自动找 `python3` / `ARGO_PYTHON`）。  
- **README / SKILL** 对齐约 **88 源、16 MCP** 与当前能力。  
- Skill 入口推荐 **`link_source.py`**（符号链接，禁止 rsync 多副本）。

### 2. 路由、改写与性能

- 查询改写（v2.5 能力）与域匹配解耦。  
- 路由热路径缓存（重复路由接近亚毫秒级）。  
- 保留熔断、负缓存、柔性命中、`engine_outcomes`（含 v2.4.x 基础）。

### 3. 架构与真源

- **engines** 拆为 `engines.py` 门面 + `engines_base` + builders。  
- **MCP** 工具 schema / 紧凑序列化可拆分维护（与 v2.4.1 紧凑回包一致方向）。  
- **`config.yaml` 单一真源**；`sync_backends.py` 派生注册表。  
- 与 v2.4.1 合并：plan / archive / 深度研究选题、引擎生命周期等能力仍在。

### 4. 脱敏与发布卫生

- 配置中引擎命令改为仓库内相对路径（如 `scripts/social_engines/...`）。  
- 文档示例使用 `/path/to/argo`、`~/.local/share/argo` 等占位，不提交本机绝对路径与真实 Key。

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

客户端示例：

```json
{
  "mcpServers": {
    "argo": {
      "command": "npx",
      "args": ["-y", "argo-search"]
    }
  }
}
```

### 方式 C：源码 / 本包

```bash
# 解压本 Release 的 argo-2.5.0.tar.gz 或 zip 后：
cd argo-2.5.0
pip install pyyaml
python3 scripts/search.py "Python asyncio" --json
python3 scripts/mcp_server.py
```

---

## 验证

```bash
python3 --version   # 3.10+
python3 -c "import yaml; print('PyYAML OK')"
python3 scripts/search.py --list-engines
python3 -m pytest tests/test_unit.py tests/test_mcp_compact.py -q
```

---

## 升级注意

- 从 **v2.4.1** 升级：能力兼容；建议重新拉代码或解压本包，再按 README 检查 MCP 路径。  
- 若本机曾用 rsync 复制多份 argo，请改回**一份真源 + `link_source.py`**。  
- API Key 仍全部可选；不配则走免费 / 本地引擎。

---

## 资源

| 资源 | 说明 |
|------|------|
| 源码包 | `argo-2.5.0.tar.gz` / `argo-2.5.0.zip`（本 Release Assets） |
| 说明文档 | 仓库 [README](https://github.com/taxueseek/argo/blob/main/README.md) |
| 上版说明 | [v2.4.1](https://github.com/taxueseek/argo/blob/main/docs/RELEASE_NOTES_v2.4.1.md) |

---

## 版本线（简）

| 版本 | 要点 |
|------|------|
| **v2.5.1** | 垂直答案源 + 引擎分层预算；研究 boost；约 110 源（见 RELEASE_NOTES_v2.5.1） |
| **v2.5.0** | 安装双路径 + 介绍页；改写不污染路由；热路径缓存；模块拆分；配置真源 |
| **v2.4.1** | MCP 预热与紧凑回包；深度研究子技能；引擎生命周期；搜索体验标准化 |
| **v2.4.0** | 低分回退、熔断负缓存、柔性命中、engine_outcomes |
| **v2.2–v2.3** | 证据两阶段、中文信源、fetch 栈 |

MIT License © 2026 taxueseek
