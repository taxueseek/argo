# Argo v2.7.1 发布说明

**版本**：2.7.1
**定位**：以「重新定义问题」为纲的系统性加固版。在 v2.7.0（垂直模态卡）基础上，修复三层真实缺陷：SSRF 防护缺失、路由健康状态语义漂移、深度研究 local_first 浪费；并做配置/代码结构性清理。

---

## 一句话

v2.7.1 = 安全加固（SSRF 防护）+ 路由/研究准确性修复 + 配置易用性清理。全部修改以可量化测试锁定（新增 22 个测试，全量 363 passed）。

---

## 安全性

### SSRF 防护（新）

**问题**：fetch/crawl/http_client 直接请求任意 URL 并跟随重定向，搜索结果或恶意页面链接可诱导抓取内网/云元数据地址（`http://169.254.169.254/`、`http://localhost:6379/` 等）。

**修复**：新增 `scripts/url_safety.py` 统一拦截，接入 `fetch_v3` / `fetch.py` / `http_client`（含 curl fallback）：

- scheme 白名单（仅 http/https）
- 主机名黑名单（localhost、裸单标签主机、`.local` / `.internal` / `.lan` 等）
- DNS 解析后 IP 段检查（私有 / 环回 / 链路本地 / CGNAT / 保留段，IPv4+IPv6）
- curl fallback 重定向由 Python 侧逐跳安全跟随，curl 自身不跟随
- 显式放行：`ARGO_ALLOW_PRIVATE_URLS=1`

测试：`tests/test_url_safety.py`（10 例：IP 段 / 主机名 / scheme / fetch 入口 / curl 入口）。

---

## 准确性

### 路由健康状态语义漂移（根因修复）

**问题**：`sub-skills/local-search/health_check.py` 与 `scripts/health_check.py` 顶层模块名冲突。任何一次 local_search 进程内调用（engines.py）或测试导入都会把 `sub-skills/local-search` 插入 `sys.path[0]`，劫持 `import health_check` 解析。route 的健康过滤落到 fallback 分支，改用 `health_probe.get_engine_status` 过滤**所有**引擎——而 `health.db` 中 wikipedia 等 8 个非 local 引擎因 HEAD 探测失败被标记 unavailable，导致「What is the capital of France」这类查询静默丢失 wikipedia，combo 退化为 `['byted', 'local_openstreetmap']`。

**修复**（三层）：

1. `sub-skills/local-search/health_check.py` → `local_health_check.py`，消除顶层命名冲突
2. `route.py` 健康过滤 fallback 与主路径语义对齐：只对 `local_*` 引擎做健康判定，非 local 引擎无条件保留
3. `engines.py` / `mcp_server.py` 的 `sys.path.insert(0, ...)` → `append`，sub-skills 模块不再劫持 scripts 同名模块

### health_probe 只探测 local_*（消除慢源误报）

**问题**：health_probe 每 5 分钟对 120+ 引擎做 1.5s 超时 HEAD 探测，慢源（wikipedia/arxiv/bocha 等）连失败 9 次被标 unavailable，数据无消费者（route 只查 local_*）。

**修复**：`probe_all_engines` 只探测 `local_*` 子引擎；清理 health.db 中 11 条非 local 脏记录。测试：`tests/test_health_probe_scope.py`。

### macro_data 国家分流修复

**问题**：非美国宏观查询（「中国GDP」「日本通胀」）时，worldbank 前置逻辑被后续的 primary 扶正覆盖，fred 仍居首位。fred 先跑 + early-stop 会用美国序列冒充「中国GDP」答案。

**修复**：`route.py` primary 扶正跳过「macro_data + 非美国国家词」场景。验证：中国GDP/日本通胀/欧元区失业率 → worldbank 优先；美国CPI → fred 优先。

---

## 路由效率与深度研究

### local_first 决策树浪费修复

**问题**：`research.py` 的 `_search_one` 在 `use_local_first` 分支前先无条件跑一次全量搜索，结果被本地结果覆盖；本地结果不足时又跑第二次全量。每次子查询多花一次完整搜索的延迟与成本。

**修复**：先本地聚合，结果不足 3 条才升级全量；非 local_first 只跑一次。测试：`tests/test_route_research_fixes.py::TestLocalFirstEfficiency`（3 例，断言调用序列 `['local_search']` 或 `['local_search', 'auto']`）。

---

## 配置易用性

### config.py 复制粘贴合并

`_load_external_engine_specs` 中 `engines/*.yaml` 与 `engines/specs/*.yaml` 两段几乎相同的加载逻辑合并为 `_load_dir`，新增引擎目录只需一行。

### 单一真源清理

删除根目录 `local-search/` 旧副本死代码（v2.2/v2.3 时代遗留，已无任何引用），已移入 `.trash/argo/2026-08-06_local-search/`（git 历史仍可恢复）。磁盘上唯一真源 = 本仓库。

### engines_builders_data.py 拆分

3157 行按域拆分：宏观数据（FRED / 汇率 / 世界银行 / 国家统计局 / Eurostat，约 700 行数据表密集区）移至 `engines_builders_data_macro.py`，原文件降至 2458 行。路由层 `is_foreign_macro_query` import 同步更新。

---

## 验证

```bash
python3 -m pytest tests/ -q --ignore=tests/test_full.py --ignore=tests/test_integration.py
# 363 passed, 12 skipped

python3 -c "import sys; sys.path.insert(0,'scripts'); from route import route_query; print(route_query('中国GDP')['engines_combo'])"
# ['worldbank', 'fred']
```

**遗留项**（非阻塞）：`engines_builders_data.py`（2458 行）仍可继续按域拆分；`search.py`（1686 行）编排层可进一步提取；`decompose_query` 的「和/与/及」对比拆分存在语义误伤（拆出子查询质量有限但原始查询不丢，成本低，暂不调整）。
