# Argo v2.7.3 发布说明

**版本**：2.7.3
**定位**：在 2.7.2 基础上，新增「实时索引」数据源引擎，并把「发布时间维度 + 时间窗过滤」做成 argo 通用能力；同时收编外部索引 CLI 的工程侧形态（结构化输出、声明式接入）。

> **后续更新（2026-08-10）**：本版本号下追加发布「引擎激活 + 体验修复」批次——引擎层 HttpClient 接入、TF-IDF 强语义注入激活 25 个垂直引擎、70 域 TTL 全覆盖、垂直源中英双语覆盖、快讯触发词修复、百科条目页兜底等，详见文末「本轮追加」。

---

## 本轮追加（2026-08-10：引擎激活 + 体验修复）

### A1. 引擎层 HttpClient 接入：UA 敏感引擎不再超时空转

**以前**：引擎请求走裸 urllib，UA 固定。arxiv 这类对 UA 敏感的源间歇性 SSL 失败，实测 5s 超时后 0 条结果。

**现在**：HTTP/HTML 引擎 GET 请求统一走 `http_client` 层——UA 轮换 + Cookie 积累 + 429/503 Retry-After 尊重 + 指数退避重试 + 重定向跟随（修复了 `follow_redirects` 无效死参数）。实测 arxiv 从「5s 超时空返回」变为「2s 内 10 条有效结果」；devto 等间歇慢源稳定在 1.3-1.6s。

**回退开关**：`ARGO_ENGINE_HTTP_CLIENT=0` 整体回退 urllib（灰度/诊断用）。

### A2. TF-IDF 强语义注入：25 个垂直引擎真正用起来了

**以前**：marginalia / wiby / searchmysite / open_meteo / usda / gov_policy / cnii / ndl / qiita 等 25 个垂直引擎有 TF-IDF 文档但正则域命中后永远选不中（注入只对 catch-all 域生效），只能 `--engine` 强制指定。

**现在**：TF-IDF 高分推荐（≥0.6）放宽到所有域，注入位置在 primary 扶正之后（串行 early-stop 下真正执行）。实测「独立博客 长尾」→ marginalia（3.7s 4 条）、「营养成分 热量」→ usda、「国务院 政策」→ gov_policy、「日本 学术论文」→ cnii、「天气 气温 预报」→ open_meteo。通用引擎（byted/bocha/octen 等）黑名单跳过，域主源保留备位。

### A3. env 占位缺失过滤：github 引擎从 401 恢复

**以前**：未配置 `{GITHUB_TOKEN}` 时把字面量 `Authorization: token {GITHUB_TOKEN}` 发给 GitHub → 401，github 引擎永久不可用。

**现在**：env 缺失替换为空 + 过滤空/认证前缀残留头（`token ` / `Bearer ` 后无凭据）。实测 github 无 token 时匿名 API 0.55s 返回 3 条结果；bocha/byted/tavily/zhihu 等 10 个用 env key 的引擎同样受益。

### A4. 70 域 TTL 全覆盖：实时数据不再缓存 1 小时

**以前**：48/70 个域没有 TTL 映射，全部落到默认 3600s。「今日金价」卡片缓存 1 小时后过期 55 分钟，财联社电报 / 同花顺热点同样。

**现在**：全部域按时效归类——modal_card（油价/金价/车票）/ 快讯 / 天气 900s，行情/宏观 300s，学术稳定型 7200s，百科/法律/地理 86400s。

### A5. 垂直源中英双语覆盖：worldbank / eurostat 英文查询命中

**以前**：国家映射和指标关键词纯中文。实测「China GDP」「US inflation」「Japan population」全部 0 条。

**现在**：国家映射补英文别名 + 缩写（含 us/uk 等短别名词边界匹配防误伤），指标词补英文（population / unemployment / inflation / gdp growth 等）。实测 China GDP 19.5 万亿美元、Japan population 1.23 亿人、US inflation +2.95% 全部命中。

### A6. 快讯类引擎触发词放行 + 百科条目页兜底

**以前**：「快讯」「美股」查询被当关键词过滤，快讯标题里没有这两个字 → 全量榜单被滤空返回 0 条；萌娘百科等搜索直接跳转条目页时列表选择器不命中 → 恒空。

**现在**：纯触发词（快讯/美股/资讯等白名单）放行全量榜单，具体主题（美联储/AI）才过滤且拉取量放大 3×；HTML 引擎容器未命中时兜底提取 `<title>` + 正文首段 + canonical URL 返回单条。实测「快讯」3 条、「融资」1 条过滤命中、moegirl 初音未来 / 东方Project 各 1 条。

### A7. 其余修复

- 熔断 empty 语义：查询无结果不再累计 opens，易空引擎不会被误判为持续故障而静默禁用
- 国际引擎中文查询 URL 编码修复（18 处专用 builder，cnii/ndl/qq_music 等中文查询全部恢复）
- open_meteo geocode 加 language=zh：中文地名「北京」从恒空恢复为 2 条
- europeana 禁用（官方 demo key api2demo 已失效 401，路由不再选中空转）
- 查询改写去重（「Python async Python 编程语言」残留修复）、research 顶层 query 保持原查询
- 慢源超时收紧（fast/auto 非 deep 6s）、wave-2 并行累计足够即提前终止、自适应 TTL 键匹配修复
- 抓取链升级（crawl/extract 走 fetch_v3 降级链）、fetch_page_v3 raw 模式绕过缓存取完整 HTML
- 版本号统一 2.7.3、单一真源文档修正（engines/specs/ 外置目录说明）

---

## 这次更新有什么

### 1. 新增实时索引引擎：realtime_index

**以前**：搜「刚发布的内容」要靠通用网页引擎碰运气，结果里也没有发布时间这个维度，判断新旧只能点开链接自己看。

**现在**：新增 `realtime_index` 引擎，免 Key 直连，输出结构化结果，每条自带 `published_at` 发布时间，适合「最近几天有什么新东西」这类时效性查询。

**怎么用**：默认不进日常组合，需要时显式指定引擎：

```bash
# 用实时索引引擎搜索
python3 scripts/search.py "rust async" --engine realtime_index

# 只看最近 7 天
python3 scripts/search.py "rust async" --engine realtime_index --since 7d

# 指定绝对日期区间
python3 scripts/search.py "rust async" --engine realtime_index --since 2026-08-01 --until 2026-08-05
```

### 4. 抓取合规第一波：尊重限速停止信号

**以前**：目标网站返回 429（限速）/ 503（过载）时，抓取链会把它当成普通失败，继续「换着花样重试」——换 UA、换 TLS 指纹、上浏览器，直到把对方惹毛或自己超时。这是最典型的爬虫行为。

**现在**：429/503 是服务器明确的「请停止」信号，与请求方式无关，继续升级只会放大负载。三处整改：

- **按服务器指示等待**：响应带 `Retry-After` 头且等待时间在可接受范围（≤10s）时，等它说的时间再试一次；无头或要等太久，直接放弃不再重试。
- **不再升级重链**：收到 429/503 后，抓取链立即终止，不再换 TLS 指纹、不再走 Wayback、不再启动浏览器。
- **TLS 层不再徒劳轮换**：指纹轮换只针对与指纹相关的拒绝（403 等）；429/503 与指纹无关，轮换纯属徒劳。

### 5. robots.txt 尊重层（合规抓取门禁）

**以前**：不管目标网站是否在 robots.txt 里声明「某些路径别抓」，一律照抓。

**现在**：抓取前先查目标域的 robots.txt，明确禁止的路径直接返回「robots.txt 禁止抓取」，不发起请求。三个边界：

- **拿不到就放行**：robots.txt 不存在（404）、暂时不可用、或网络异常时，不拦正常抓取——避免因一次抓取失败让整站瘫痪。
- **通配规则匹配**：用 `User-agent: *` 通用规则判定（本技能 UA 轮换，匹配通配规则最保守合规），抓 robots.txt 时自报身份，不用随机 UA。
- **可关闭**：`ARGO_RESPECT_ROBOTS=0` 关闭本层，默认开启。

### 2. 时间窗过滤成为通用能力

**以前**：想限定「发布时间范围」，没有任何统一入口，每个数据源各写各的。

**现在**：`--since` / `--until` 成为 argo 通用参数（接受 `7d` 相对值或 `2026-08-01` 绝对日期），CLI 与 MCP `argo_search` 都能传；只有声明支持时间窗的引擎才会收到这两个参数，其余引擎不受影响。

缓存也按时间窗隔离：同一句话用不同时间窗搜，不会互相串结果——不会出现「我限定到 8 月 5 号之前，却拿到 8 月 7 号的条目」这类错误。

### 3. 引擎接入更省事：CLI 桥接零 Python

**以前**：接一个新数据源，要么是标准 HTTP API（写 YAML 就行），要么就得写 Python 插件。

**现在**：本机已装的命令行工具，只要输出 YAML/JSON 结构化结果，就能纯声明式接入——`output_format: yaml` 走通用 YAML 解析器（自动认 `results`/`items`/`data`，字段别名 `title|name`、`url|link`、`snippet|description|content`，保留 `published_at`）；`filter_args` 声明时间窗参数映射，`--since`/`--until` 来了就自动拼进命令。模板在 `engines/_template_cli.yaml`。

顺带修了一个配置校验的坑：命令写裸名（在 PATH 里）的 CLI 引擎，以前会被误判成「文件不存在」而自动禁用，现在会用 PATH 兜底查找，裸命令和绝对路径都行。

---

### 6. 新增免 Key 天气引擎

**以前**：问天气要么靠通用网页搜索碰运气，要么接和风天气这类要申请 API Key 的服务，门槛高、Key 也未必有。

**现在**：内置免 Key 天气引擎，直接说「上海天气」就返回当前天气与未来预报（wttr.in 主用、Open-Meteo 兜底，纯标准库实现）。天气域组合为 `qweather + weather`：有和风 Key 时和风为主，没 Key 时免 Key 引擎顶上，任何情况下都有结果。

**怎么用**：无需配置，天气域自动命中；也可显式指定：

```bash
python3 scripts/search.py "上海天气" --engine weather
```

### 7. 新增免 Key 航空气象引擎

**以前**：查机场 METAR / TAF（航空气象例行报告）不知道去哪查，拿到原始报文也看不懂。

**现在**：内置免 Key 航空气象引擎，直连美国 FAA 官方公开接口，按 ICAO 机场代码（如 KLAX、ZBAA）查询实时 METAR，输出结构化字段：机场名、原始报文、报告时间。查询词不是 ICAO 代码时自动降级到通用搜索。

```bash
python3 scripts/search.py "KLAX" --engine aviation_weather
python3 scripts/search.py "ZBAA" --engine aviation_weather
```

### 8. 新增免 Key 火车票引擎

**以前**：查车次、余票要么打开购票软件，要么依赖需要申请 Key 的第三方查询服务。

**现在**：内置免 Key 火车票引擎，直连 12306 官方查询接口（两步：取会话 Cookie → 查余票），返回结构化车次信息：车次号、出发/到达站、发到时刻、历时、各席别余票（商务/一等/二等/卧铺/无座）、可购状态。默认查明天；支持「今天/明天/后天」「YYYY-MM-DD」「X月X日」日期与「高铁/动车」车次类型。

**怎么用**：火车票查询词自动路由到模态卡组合 `bocha_ai + bocha + train`，无需配置；也可显式指定：

```bash
python3 scripts/search.py "北京到上海高铁" --engine train
```

站点名有本地缓存（7 天过期，仅提升速度，不影响功能）。

### 9. 垂直结构化路径不再被预算裁剪

**以前**：模态卡域组合加入新引擎后，预算截断会把声明好的兜底引擎顶掉，结构化结果不稳定。

**现在**：修正保底语义——声明组合内的引擎互相不顶位，预算紧张时优先替换非保底成员；火车票引擎归入垂直能力族，不再与通用网页引擎混排。

---

## 验证

- 新增回归测试 `tests/test_time_window_cli.py` 18 项全过（YAML 解析、filter_args 拼参、裸命令路径校验、缓存时间窗隔离）
- 新增合规回归：`tests/test_stop_signal.py`（Retry-After 解析、等待重试/放弃、TLS 停止信号、主链门禁、robots 门禁）与 `tests/test_robots_guard.py`（Disallow 命中/放行、失败容错、缓存复用、开关）共 32 项全过
- 核心回归 91 passed；引擎与缓存相关 47 passed、8 skipped（网络依赖 live 用例）
- 全量回归 640 passed、18 skipped、19 subtests passed
- 三引擎收编回归：`tests/test_aviation_weather.py` 14 项、`tests/test_wx.py` 13 项、`tests/test_train.py` 29 项全过（含 5 项 live 在线用例）；在线冒烟实测：北京天气（wttr.in 26°C）、北京南→上海虹桥 G531、KLAX METAR 均返回真实数据
- `realtime_index` health 准入通过（latency ~0.8s），`--since 7d`、绝对日期窗口实测过滤生效，`published_at` 字段贯通
