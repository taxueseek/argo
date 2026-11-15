# Argo 系统台账体系规范（Ledgers Architecture）

本文档是 Argo 仓内所有持久化台账、状态账本与技术债棘轮的**统一真源索引**。

按数据生命周期与变更意图做 MECE 划分，本仓共有两类、四大台账：
1. **运行时健康状态（Runtime Health）**：根据运行时的外部反馈、请求失败或配额超限动态记账与滚动淘汰。
2. **工程质量棘轮（Debt Ratchets）**：静态门禁防线，只准减不准增，机械锁死技术债膨胀。

---

## 一、台账矩阵总表

| 台账名称 | 类型 | 真源物理路径 | 写入时机 / 触发者 | 读取者与受影响行为 | 淘汰 / 维护契约 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **坏例账本**<br>`badcase_ledger` | 运行时观测 | `<state>/badcase.db`（SQLite；state 根由 `argo_paths.state_path()` 派生，`ARGO_STATE_DIR` 可覆盖） | `ingest` 从 usage_log 的 `query.jsonl` / `recovery.jsonl`（errors 非空或 count=0 的查询）与 `circuit_breaker.json` 幂等归一写入 | `scripts/badcase_ledger.py` 归因统计、Seen N× 门禁、CI 错题本 | 滚动保存；支持 `badcase_ledger.py check` 对拍 |
| **休眠账本**<br>`dormant_ledger` | 运行时决策 | **无持久化文件**：休眠状态由 `scripts/dormant_ledger.py` 的 `dormant_engines()` 从 config.yaml 引擎组合、路由槽位与档位预算派生计算；登记真源是同文件 `DORMANT_ALLOWLIST` 常量（引擎名 → 休眠原因） | 域配置变动使引擎在所有出现域都落预算外时，派生结果随之变化；登记增删直接改 `DORMANT_ALLOWLIST` | `sync_backends` 对账（`check_dormant_ledger()`）；路由决策层剔除预算外源，避免浪费配额 | 上游恢复或接入路由槽位后，从 `DORMANT_ALLOWLIST` 撤账（如 2026-11-12 keenable） |
| **配额熔断台账**<br>`quota` | 运行时防护 | `<state>/quota.json`（`QUOTA_STATE_PATH` 可覆盖）与 `backends/quota_profiles.json` | 专有或付费源当日/当月达到配额硬顶时熔断写入 | `scripts/quota.py` 拦截：熔断源在当前周期内降级或转为免费源 | 跨周期（次日 0 点或次月 1 日）自动清零重置 |
| **技术债棘轮**<br>`ratchets` | 静态门禁 | `scripts/badcase_ledger.py`（`BROAD_SILENT_CATCH_BASELINE`，`tests/test_silent_catch_gate.py` 消费同一常量）<br>`tests/test_module_size_gate.py` (`GRANDFATHERED`) | 代码修改引入新文件或窄化异常时手动/门禁比对 | `scripts/pre-commit` 快门禁：单文件超过 1000 行或裸吞异常数超标立即阻断；`badcase_ledger.py overview` 输出同一基线 | **只准减不准增**：存量窄化后下调基线（改 `BROAD_SILENT_CATCH_BASELINE` 一处即可），祖父文件只能拆不能扩 |

---

## 二、详细契约与操作规范

### 1. 坏例账本（Badcase Ledger）
- **真源与格式**：SQLite（`<state>/badcase.db`，表 `badcases`），记录时间、查询词、引擎、错误类型（`failure_kinds`）与归因（`root_cause`）。
- **隐私保护**：对用户的实际查询词脱敏后存储，禁止明文凭据反射。
- **管理命令**：
  ```bash
  # 查看最近坏例统计与归因分布（含 unattributed 计数）
  .venv/bin/python scripts/badcase_ledger.py stats
  # 列出最近坏例（可选 --kind query_error|empty_result|recovery|breaker）
  .venv/bin/python scripts/badcase_ledger.py list --limit 20
  # 四大台账全局概览（含休眠真值与对账问题，取数失败显式 error 并退出非零）
  .venv/bin/python scripts/badcase_ledger.py overview
  ```

### 2. 休眠账本（Dormant Ledger）
- **契约规则**：休眠状态没有持久化文件——`dormant_engines()` 从 config.yaml 引擎组合、路由槽位与档位预算**派生计算**（引擎在它出现的每一个域里都落预算外才算休眠）。登记真源是 `DORMANT_ALLOWLIST` 常量：对账时允许处于休眠的引擎白名单，条目必须写明原因。
- **对账入口**：`sync_backends` 流程内调 `check_dormant_ledger()` 双向对账（新增休眠未登记、台账过期、条目缺原因）；`badcase_ledger.py overview` 的 `reconciliation_issues` 字段暴露同一结果。
- **撤账方式**：上游恢复或接入路由槽位后，直接删改 `DORMANT_ALLOWLIST` 条目（如 2026-11-12 keenable 撤账）。

### 3. 配额熔断台账（Quota Ledger）
- **真源定义**：`backends/quota_profiles.json` 声明各付费/私有源额度规则（如 Exa 月度 USD 上限、Bocha 每日调用次数）；运行时熔断状态落 `<state>/quota.json`（`QUOTA_STATE_PATH` 可覆盖）。
- **防护原则**：Fail-closed 降级，触发熔断后转入备选开源/免费源，防止超额扣费。

### 4. 技术债棘轮（Quality & Size Ratchets）
- **宽吞异常棘轮**（`tests/test_silent_catch_gate.py`）：AST 扫 scripts/ 的 `except Exception: pass/continue`（sub-skills 不在本棘轮口径），基线真源是 `scripts/badcase_ledger.py` 的 `BROAD_SILENT_CATCH_BASELINE`（测试与 `overview` 消费同一常量，任何地方不再手写该数字）。窄化存量时下调该常量一处即可。
- **千行文件祖父清单**（`test_module_size_gate.py`）：登记已有的历史大文件，上限锁定为登记行数。新文件严禁超过 1000 行。
- **存根日落到期门禁**（`test_stub_targets.py`）：退役存根必须显式声明 `__sunset_version__`，到达日落版本时门禁报错阻断，倒逼物理清退。

---

## 三、扩展与演进原则
1. **奥卡姆剃刀**：严禁随意建立新的状态存储文件；新状态应优先归入上述四大台账。
2. **生命周期分离**：缓存（可清空）与台账（结论/状态不可丢）严格物理隔离，台账禁止写入临时缓存目录。
