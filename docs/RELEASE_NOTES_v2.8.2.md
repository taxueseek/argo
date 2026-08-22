# Argo v2.8.2 发布说明

**版本**：2.8.2
**定位**：Windows 全平台可用 + 证据语义统一（dossier 与 wide_research 同一套门禁）。

---

## 这次更新有什么（通俗版）

### 1. Windows 彻底可用

**以前**：Windows 上 `npx -y github:taxueseek/argo` 直接报 `EBADPLATFORM` 装不上；绕过平台检查后，中文查询又会因 GBK 编码崩溃，本地搜索工具探测（`command -v` / `mdfind`）在 Windows 全失效。

**现在**：

- 移除 npm `os` 限制，Windows 直接安装
- 全链路 UTF-8 防线：Node 入口注入 `PYTHONUTF8=1`，Python 入口 `-X utf8` 重启动，6 处含中文 JSON 读取改为二进制安全解析
- 工具探测改 `shutil.which`（跨平台），无 rg 时经 Git 自带 grep 兜底
- Ctrl+C 干净退出，不再刷 KeyboardInterrupt traceback
- Chrome 自动发现：新增 `CHROME_PATH` 环境变量 + Windows Chrome/Edge 五个常见安装路径

### 2. DSH 插件两种装法

**以前**：主包没有 `dsh.bundle` 声明，`dsh plugin add github:taxueseek/argo` 只当普通依赖装，MCP 工具不激活。

**现在**：

```bash
# 装法 A：仅 10 个 MCP 搜索工具
dsh plugin --profile web add "github:taxueseek/argo"
# 装法 B：搜索工具 + wide_research 并行研究编排
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

### 3. 证据语义统一：wide_research 也能判定「能不能下结论」

**以前**：CLI/MCP 的深度研究（`argo_research`）输出自带 `quality_gate_results` 门禁，但 DSH 插件的 `wide_research` 没有，Agent 无法判断报告置信度。

**现在**：`wide_research` 输出新增 `quality_gate_results`（`passed` / `conclusion_cap`：low/medium/high），与 dossier 同一套语义；`passed=false` 时必须降级表述。同时支持 `depends_on` 依赖分阶段（有定义/基线依赖的轨道不再盲目并行），并做了 SSRF 防线（仅 http(s) URL 入证据账本）与研究递归硬保护（worker 不可调用 `argo_research`）。

### 4. 取证协议化（research 重构）

**以前**：深度研究输出像「研究报告」，机器把检索头条当结论，Agent 容易被误导。

**现在**：机器只产出 **dossier**（来源 / 覆盖 / 缺口 / 门禁 / 待核验 URL），判断稿由 Agent 按 `references/research-protocol.md` 写（事实 / 推断 / 建议 / 未知 分开）。工作包支持 `--work-packages` 按 `depends_on` 分阶段取证，不再靠扩词充问题树。

### 5. 打包与供应链加固

- npm 包补 `engines/`、`data/`（此前漏打包，垂直源与地理数据在 npm 安装下缺失）
- 安装脚本新增 `ARGO_PIN` 固定 commit（curl|bash 供应链加固）

---

## 验证

- 证据门禁 / 工作包分阶段 / 门禁谓词：python 23 项 + node 13 项全过
- MCP initialize 握手正常，serverInfo.version = 2.8.2
- `npm pack` 实测 247 个文件完整入包

## 兼容性

- 无破坏性变更，v2.8.x 配置与用法继续有效
- 默认行为不变；新能力随输出附带，不阻塞日常搜索
