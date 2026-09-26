# Argo 阿尔戈 — 统一搜索与证据核验

> 一个开源的「搜索 + 证据核验」工具，给 AI Agent 和终端使用。把 218 个搜索源、多语言路由、深度研究与证据判定整合在一个命令行 / MCP 里，让「搜」和「核」变成一件事。

---

## 安装方式

> **唯一真实源是 GitHub。** 请用下面任一方式，勿用 `npm install argo-search`（npm registry 上那份是**非官方陈旧版 v1.0.1**，不随本项目更新）。

### 方式一：一键脚本（推荐本机长期用）

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

### 方式二：npx（不加装，即用即走）

```bash
npx -y github:taxueseek/argo
```

### 方式三：给 Agent 挂 MCP

Claude / Codex / Kimi 等支持 MCP 的客户端，直接挂：

```json
{
  "mcpServers": {
    "argo": { "command": "npx", "args": ["-y", "github:taxueseek/argo"] }
  }
}
```

挂上后获得 19 个工具（与 CLI 能力面对齐）：`argo_search`、`argo_local_search`、`argo_local_read`、`argo_recompute`、`argo_research`、`argo_evidence`、`argo_clarify`、`argo_crawl`、`argo_fetch`、`argo_screenshot`、`argo_pdf`、`argo_social_search`、`argo_article`（公众号文章全文）、`argo_job`（招聘多平台聚合）、`argo_extract`（结构化提取）、`argo_preflight`（URL 批量预检）、`argo_answer`（直答）、`argo_watch`（网页变化监控）、`argo_cite`（DOI 引用条目）。

### 依赖

- **必需**：Python 3.9+、`pip install pyyaml`
  - 下限由语法决定，与 `bin/argo` 的 `MIN_PYTHON` / `scripts/install.sh` 保持一致（改一处就要改另外两处）。唯一越界过的 `job.py` 用了 3.12+ 的 PEP 701 f-string 写法，已按 3.9 改写，此后由跨解释器解析检查兜住。
- **可选**：`curl_cffi`（TLS 指纹，反爬站抓取更稳）、Chrome（截图）
- 只有 npx 入口才需 Node.js 18+

---

## 使用示例

### 1. 搜索

```bash
argo search "Rust async runtime tokio 性能"
argo search "python asyncio error handling"  # 编程问答域 → StackOverflow 官方 API
argo search "미국 연준 금리 인하 전망"      # 多语言，返回韩语结果
```

### 2. 证据核验（高后果问题）

金融 / 医疗 / 法律 / 事实核查类查询，搜索结果自带证据门控：高后果问题标 `fetch_required`，每条结果标 `fetch_suggested`、`has_fetched_evidence`。

```bash
# 对 top-3 核验正文，回填「核实后证据分」
argo search "某公司财报" --verify 3
```

### 3. 深度研究（多子查询取证）

复杂问题用 `argo research`，产出 **dossier**（来源 / 覆盖 / 缺口 / 可判定检查），判断稿交给 Agent 写（事实 / 推断 / 建议 / 未知 分开）。

```bash
argo research "半导体行业 2026 供需格局"
argo research "iPhone 16 用户口碑" --mode social-sentiment
```

### 4. 抓取与结构化

```bash
argo fetch "https://example.com" --focus "关键词"   # 自动降级抓取
argo pdf "https://example.com/paper.pdf" --pages "1-5"
```

### 5. 单代理里直接用

```python
from scripts.search import search          # 或走 MCP 工具
results = search("查一下")
```

---

## 为什么是现在（2026 检索趋势）

- **Agent 检索进入证据管线时代**：交付物从链接清单变成可排序、可复核、不撑爆上下文的结构化证据
- **上下文经济**：token 是第一成本，Agent 档（约 3.7KB/次）与字节预算检查让检索不烧窗口
- **AI 友好内容标准铺开**：llms.txt 与 `.md` 直出在头部文档站普及，为 Agent 备料的网站越来越多
- **免费开放生态成熟**：政府/学术/标准/安全机构的开放 API + 免 key 引擎覆盖大多数领域
- **质量可度量**：排序金标（MRR/nDCG）、融合增益消融、路由负向控制——检索质量有数字可查

Argo v2.8.8 把这五条全部落在代码里：220 源 / 89 域 / 185 免配置。更详细的场景对照与重点源清单见 [为什么选择 Argo](为什么选择argo.md)。

---

## 能力边界（诚实说明）

**能力强项**

- **多语言**：中 / 英 / 日 / 韩 / 欧语（德法西意）/ 西里尔等路由，多数查询返回对应目标语言（ja/ko 已大幅修复）
- **218 搜索源、89 业务域**（184 个免配置开箱）+ TF-IDF 语义路由 + RRF 融合（weighted，弱源自动降权）
- **三条拿正文直出通道**：站点根 llms.txt 与页面 `.md` 探测（命中跳过反爬链）+ r.jina.ai 阅读器级保底
- **垂直官方 API 新成员**：StackExchange（180+ 问答站）、DOI 元数据（CSL JSON）、endoflife、NVD、法律法规库
- **垂直域**：金融行情、宏观数据、化学、学术、本地、社交、模态卡（火车票/油价/万年历等）
- **证据完整链路**：`fetch_required` / `--verify` / 证据分回填，适合高后果问题
- **兼容开源生态**：可直接挂 MCP / DSH 插件（`wide_research` 并行研究调度）

**已知限制**

- 部分高质量源（`exa`、`bocha` 等）需 API Key；无 Key 自动走免费引擎 + 本地引擎，质量略有折损
- 日 / 韩等技术类专业查询的分源质量仍有提升空间（宏观/日常已明显改善，但非完美）
- 网络不稳定时（SSL 波动）个别源会暂时失败，会熔断降级自动切备选（证据分能提示是否可下结论）
- **安装请用 GitHub 来源**；npm registry 上的 `argo-search` 是陈旧的 v1.0.1，功能残缺
- 判断稿、结论始终由 Agent 负责；`argo` 负责「取证 + 给可判定检查」，不替你下结论

---

**License**：MIT · 仓库：https://github.com/taxueseek/argo
