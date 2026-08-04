# Argo v2.6.0 发布说明

**版本**：2.6.0  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：给 Agent 用的统一搜索与证据核验——面向**不同领域、不同语言、不同需求**，尽量给出能核验、能吸收的材料。

---

## 一句话

搜索本来就该「问啥像啥、用啥语就懂啥语」。本版两件大事：**多语言搜索从「中英优先」扩到真正多语可用**；**垂直领域继续补全**（影视、体育、地理、组织、音乐等），并加上空结果恢复时的防污染门禁——查询越用越准，少被无关包管理器、快讯源带偏。

---

## 用大白话看本版

### 1. 多语言：不只中英文

以前路由和语言参数主要围着中英转，日文、韩文、俄语等要么被当成「英文网页问题」，要么误塞进中文专用源。

现在会先认**你在用什么语言**，再决定：

- 用哪套引擎语言参数（例如 Bing 的 `setlang`、Google 的 `hl`）
- 要不要补上本地多语言源（如 `local_bing` / `local_google`）
- 中文专用源（知乎、微信搜狗、A 股快照等）**别误伤**非中文查询
- 搜不到时，可以按策略做**跨语言回退**（不是硬翻译整句，而是用通用源 + 偏好语言再试）

你用日文问推荐、用韩文问电影、用俄文问怎么写脚本，系统会尽量按「这门语言的用户」来选路，而不是一律按英文网页处理。

### 2. 垂直领域：问啥走啥专线

在金融、宏观、化学等答案源之外，本版把更多「一问就该有标准答案」的场景接上专线，例如：

| 你这样问 | 大致会怎样 |
|----------|------------|
| Inception 导演 / 肖申克的救赎 主演 | 影视域 → IMDb 等 |
| 梅西俱乐部 / 库里 球队 / Ronaldo club | 体育域 → TheSportsDB 等 |
| 埃菲尔铁塔在哪 / where is Eiffel Tower | 地理实体 → OpenStreetMap 等 |
| NASA founding year / 国务院职能 | 组织实体 → Wikidata 等 |
| 周杰伦 专辑 / Taylor Swift album | 媒体音乐 → iTunes 等 |
| 贵州茅台股价 / AAPL stock | 继续走行情专线（本版仍保留并加固） |

目标很简单：**少在泛网页标题里碰运气，多直接打到能给答案的源。**

### 3. 空结果也会「聪明地重试」，且不乱拉源

搜不到时会按成本从低到高试放宽、换引擎、跨语言。换引擎时**只允许通用网页 / 百科，或同一能力族**——不会因为兜底，突然从影视查询跳到 PyPI、npm、金十快讯这类无关垂直源。

### 4. 能力族与回归

搜索源按「能力族」归类（全网、学术、代码、行情、百科、体育…），同族可互换、测试可按族断言。配套多语言 × 场景矩阵测试与 P0/P1 回归，方便升级后自检有没有倒退。

---

## 为什么值得升级

| 你可能遇到的问题 | v2.6.0 怎么处理 |
|------------------|-----------------|
| 日文 / 韩文 / 俄语查询总像「英中网页乱搜」 | 统一语言检测 + 引擎语言参数 + 语言补充源 |
| 非中文问题却出现知乎、搜狗微信 | 语言门禁：非中文主查询避开中文专用源 |
| 问电影 / 球星 / 地标 / 机构，却只得到泛网页 | 影视 / 体育 / 地理 / 组织 / 媒体域补全 |
| 恢复空结果时结果「串味」（包、快讯混进 unrelated 查询） | recovery L3 族门禁 + 查询信号过滤 |
| 不知道多语言有没有回归 | `matrix_search_eval.py` + `regression_p0p1.py` |

---

## 本版增量（按主题）

### 1. 多语言搜索

- `lang_detect`：中 / 英 / 日 / 韩 / 拉丁变音 / 西里尔 / 泰 / 阿 / 希伯来 / 希腊 / 天城体等主语言判定；假名、谚文优先于纯汉字误判为中文（纯汉字日文人名等地名仍可能判中文，后续可继续收紧）
- 路由：`primary_lang` 驱动语言补充源与 must_keep；日韩优先本地语言友好引擎
- `lang_pref`：中英基线 + 系统 locale + 使用习惯，弱信号查询时决定引擎语言
- recovery L4：跨语言 / 基线中文反向补源（`mode=fast` 可跳过以控延迟）
- 引擎层：HTML 构建器动态覆盖 `setlang` 等参数

### 2. 垂直域补全与质量

- 域：`film_search` / `sports_search` / `geo_places` / `org_entity` / `media_search` 等（配置真源仍在 `config.yaml`）
- 引擎：IMDb、TheSportsDB、iTunes（中文区与专辑实体等）、Wikidata、OpenStreetMap 等衔接
- 别名与解析：如「库里」→ Stephen Curry；世界杯年份正则修复等
- 空结果恢复：禁止 code / 快讯等无关族污染；结果需带查询信号才可吸收

### 3. 能力族与测试

- `engine_families`：web_general / knowledge / sports / media_book / finance_* …
- 回归：`scripts/regression_p0p1.py`、`scripts/matrix_search_eval.py`（offline / live）
- 报告基线：`tests/matrix_search_report.json` 等

### 4. 相对 v2.5.1 的延续

v2.5.1 的日常 combo 预算、答案域 early-stop、研究 boost 不锁死、金融 / 宏观 / 化学专线**全部保留**，本版是在之上加「语言维度」和「更多垂直场景」。

---

## 已知边界（诚实说明）

- **纯汉字日文**（如仅「宮崎駿」、无假名）语言检测可能仍判中文。  
- **日韩垂直关键词**未完全对齐中英 pattern 时，可能落到通用 `local_bing`，而不是 IMDb / SportsDB——通用路径仍可用，专线覆盖会继续加。  
- 个别 API（体育库限流、百科 SSL）偶发失败时，依赖 recovery + 通用源兜底。

---

## 安装（任选）

### 方式 A：一键脚本

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

### 方式 B：npx 启动 MCP

```bash
# 需 Node.js 18+ 与 Python 3.10+，并 pip install pyyaml
npx -y github:taxueseek/argo
```

### 方式 C：本 Release 源码包

```bash
tar -xzf argo-2.6.0.tar.gz
cd argo-2.6.0
pip3 install pyyaml
python3 scripts/search.py "Inception movie director" --json
python3 scripts/mcp_server.py
```

---

## 验证

```bash
python3 --version   # 3.10+
python3 -c "import yaml; print('PyYAML OK')"
python3 scripts/search.py --list-engines
python3 scripts/regression_p0p1.py --offline
python3 scripts/matrix_search_eval.py --offline
python3 -m pytest tests/test_multilingual.py tests/test_unit.py -q
```

---

## 版本对照（简）

| 版本 | 侧重 |
|------|------|
| **v2.6.0** | 多语言搜索；影视/体育/地理/组织/媒体等垂直补全；recovery 防污染；矩阵回归 |
| v2.5.1 | 金融/宏观/化学答案源加厚；日常 combo 预算；研究 boost |
| v2.5.0 | 安装与介绍页；查询改写；路由热缓存；MCP 紧凑响应 |
