# Argo v2.6.1 发布说明

**版本**：2.6.1  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：v2.6.0 的路由修复版——多语言、垂直域补全等 v2.6.0 全部能力原样保留，仅修正一处路由误伤。

---

## 一句话

v2.6.0 已经做到「问啥像啥、用啥语就懂啥语」；v2.6.1 把一处会抢答的路由误伤修掉：**「X 的首都是什么」这类事实问答不再被地理域截胡**，回到通用事实查询的正确答案源。

---

## 本版修复

### 1. 路由误伤修复：`capital of` 不再抢 fact_check

**问题**：`geo_places`（地理/地点域）的英文触发词含 `capital\s+of`，导致「what is the capital of France」「法国的首都是什么」这类**国家首都事实问答**被路由到地理域（`local_openstreetmap` 等），而不是通用事实查询（`fact_check`：duckduckgo / wikipedia / wolframalpha）。

- 国家首都本质是**事实问答**，`fact_check` 域的 `what is|who is|where is` 触发词本就能接住
- `geo_places` 保留 `where is|located in|latitude|longitude` 等**地点定位**触发词，地理查询不受影响

**影响**：英文事实问答类查询现在会正确落到 `duckduckgo / wikipedia / wolframalpha` 组合，而非 `local_openstreetmap` 单源。

---

## 能力基线

v2.6.1 与 v2.6.0 在能力上完全一致：

- **多语言搜索**：统一语言检测 + 引擎语言参数 + 语言补充源 + 跨语言回退 + 中英基线偏好
- **垂直域补全**：影视（imdb）/ 体育（thesportsdb）/ 地理（OSM）/ 组织（wikidata）/ 媒体（itunes）与金融宏观化学等
- **能力族体系**：`engine_families.py` MECE 能力族，同族可互换、按族去重与回填
- **知乎全网搜索**：`zhihu_global` 真全网 + site 语法
- **恢复防污染**：recovery 空结果恢复时防无关垂直域污染
- **约 120+ 搜索源**、16 个 MCP 工具、矩阵回归

## 质量验证

- 完整离线测试套件通过（284 passed / 0 failed，55 个网络依赖用例排除）
- 多语言、垂直域、路由、证据、MCP 回归全部通过

---

## 升级方式

任选一种：

```bash
# npx 最新
npx -y github:taxueseek/argo

# install.sh
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/install.sh | bash

# Release 源码包
# 下载 argo-2.6.1.tar.gz 后：
tar -xzf argo-2.6.1.tar.gz
cd argo-2.6.1
python3 scripts/mcp_server.py
```

---

## 与 v2.6.0 的差异

| 文件 | 改动 |
|------|------|
| `config.yaml` | `geo_places` 触发词移除 `capital\s+of`（避免抢 fact_check） |
| `package.json` / `SKILL.md` / `README.md` | 版本号与文档同步 2.6.1 |

无任何功能回退；v2.6.0 的既有测试全部保留并通过。
