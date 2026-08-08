# Argo v2.7.3 发布说明

**版本**：2.7.3
**定位**：在 2.7.2 基础上，新增「实时索引」数据源引擎，并把「发布时间维度 + 时间窗过滤」做成 argo 通用能力；同时收编外部索引 CLI 的工程侧形态（结构化输出、声明式接入）。

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
