# Argo v2.8.3 发布说明

**版本**：2.8.3
**定位**：多语言路由修复 + anysearch 进程化 + weighted RRF 融合强化。

---

## 这次更新有什么（通俗版）

### 1. anysearch 主力引擎不再「每次启动个 Python 进程」

**以前**：`anysearch` 走 `type: cli`，每次搜索都 `subprocess` 拉起一个 `python3`（解释器启动 + 模块导入约 0.2-0.3s 纯开销），且用裸 `urllib` 无 UA 轮换、无重试、无退避，高失败（限流/网络）时 error 频发。

**现在**：

- 改为进程内 builder（`_build_anysearch_engine`），直接复用 `HttpClient.post`（新增 JSON-RPC POST 方法，带 UA 轮换 / 指数退避 / Retry-After / Cookie 积累）
- 省掉每次 subprocess 启动；网络失败自动重试降错
- 修复 quota 误判：正常结果正文里出现「quota / 429 / rate limit」等词不再被误判为配额耗尽而返回空

### 2. 多语言查询终于返回「目标语言」结果

**以前**：日语/韩语查询常被中文泛内容域或中文引擎「污染」——韩文「연준 금리」返回「皇室战争卖号」、日文「政策金利」返回「frb_百度百科」；德法西意查询依赖运气。

**现在**：

- **语言门控**：日/韩查询不再误入中文泛内容域（chinese_general / weather 单音节误爆等）
- **中文引擎双层过滤**：域命中路径 + TF-IDF 路径都对 ja/ko 剔出中文内容/新闻/金融/技术引擎（octen / tencent_kline / gov_policy / bocha 等）
- **anysearch 多语言兜底**：ja/ko 宏观返回日语「2026年FRB・ECB・日銀」、韩语「연준 금리 동결…FOMC」；德法西意走 anysearch 返回法语「BCE et taux d'intérêt」、意语「Calendario 2026 BCE」
- **Bing `mkt` 市场码**：`local_bing` 追加市场码（比 `setlang` 更强制区域），辅助缓解 ja/ko 返回中文站
- **语言偏好软排序**：ja/ko 查询前置含目标语言字符的结果，不误删

### 3. 融合更看重「可靠」的引擎（weighted RRF + weakest-link）

**以前**：RRF 融合虽有静态加权（权威提权、社交降权），但一个「弱引擎」（熔断中、高错误、空结果多）的结果仍会掺杂进融合，拖垮整体精度——论文 2508.01405 的「weakest-link 现象」。

**现在**：`_engine_weight` 升级为 **静态基础权重 × 动态可靠性因子**：熔断（disabled/open/half_open）与高错误引擎自动降权（duckduckgo 熔断 0.5、健康权威源 wikipedia 1.4、社交 0.7），弱检索路径不再拖累融合。

---

## 技术细节

- `HttpClient.post()`：JSON-RPC / JSON POST，`_do_post` 独立实现（不碰 GET 热路径），gzip/br 解压、重定向跟随、SSRF 防护、curl fallback
- `_build_anysearch_engine`：POST `api.anysearch.com/mcp`，解析 Markdown 结果块，quota 沉底检测
- `route.py _JA_KO_CN_ENGINES`：ja/ko 域命中路径过滤中文引擎
- `search.py _single_reliability()`：30s TTL 缓存的断路器可靠性因子（weakest-link）
- `lang_detect.py`：`ENGINE_LANG_PARAM_VALUES` 增补 `mkt`（Bing 市场码）
- 引擎对比（多语言宏观 × 研报，32 次）：byted 最快（383-736ms 目标语言）、bocha 中文/韩语快、anysearch 稳健、local_bing 对 ja/ko 差（已用 anysearch 兜底）

## 依赖

不变（PyYAML 必需；curl_cffi / Chrome 可选）。

## 测试

核心测试全过（test_multilingual 70、test_multilingual_routing 10、route/engine/breaker 317）；全量 885+ passed（仅环境网络 SSL 波动的端到端测试可能 flaky，非代码回归）。
