/* @taxueseek/argo-dsh — 浏览器端设置页与工作台入口（无需构建步骤）
 * 由 DSH ModuleLoader 以 lazy-CJS 加载，注册 settings.section / plugins.item，
 * 并在浏览器环境按需挂载 /argo/workbench-client.js 搜索分析工作台。 */
window.__ModuleLoader__.load({
  id: '@taxueseek/argo-dsh',
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    const react = require('react');
    const h = react.createElement;
    const useState = react.useState;
    const useEffect = react.useEffect;
    const useRef = react.useRef;

    const NS = 'wide-research';
    const SECTION_ID = 'argo';

    // 双语字典：[zh, en] 单源表，避免两份拷贝各自漂移
    const I18N = {
      title: ['Argo 搜索配置', 'Argo Search Settings'],
      intro: ['接入方式、原生工具、API 密钥与研究编排。保存后即时生效；工具名与 provider 属结构性字段，仍需在 profile patch 中修改。', 'Access mode, native tools, API keys and research orchestration. Saves apply immediately; tool name and provider remain structural profile settings.'],
      loading: ['配置加载中…', 'Loading configuration…'],
      unavailable: ['本插件的配置服务不可用（未挂载或只读）。', 'Configuration service unavailable (not mounted or read-only).'],
      readonly: ['当前配置只读。', 'Configuration is read-only.'],
      saved: ['已保存', 'Saved'],
      refused: ['宿主拒绝写入（可能只读或字段不可热更新）', 'Host refused the write (read-only or not live-editable)'],
      save: ['保存', 'Save'],
      saving: ['保存中…', 'Saving…'],
      on: ['开', 'on'],
      off: ['关', 'off'],
      showMore: ['显示更多', 'Show more'],
      showLess: ['收起', 'Show less'],
      tabAccess: ['接入', 'Access'],
      tabTools: ['工具', 'Tools'],
      tabKeys: ['密钥', 'Keys'],
      tabResearch: ['研究', 'Research'],
      tabAdvanced: ['高级', 'Advanced'],
      tabDashboard: ['看板', 'Dashboard'],
      tabEngines: ['引擎', 'Engines'],
      dashboardTitle: ['运行监控与健康态看板', 'Runtime Monitoring & Health Dashboard'],
      routeHealthTitle: ['路由观测', 'Routing telemetry'],
      routeHealthHint: ['最近的路由决策与引擎状态（宿主提供时显示）。', 'Recent routing decisions and engine status (when provided by the host).'],
      routeHealthFallback: ['在终端运行 `argo stats` 或 `argo engine list` 查看路由健康、引擎成功率与配额消耗。', 'Run `argo stats` or `argo engine list` in a terminal to inspect routing health, engine success rates and quota usage.'],
      dashboardHint: ['实时监控常驻 MCP 连接池、上游搜索源拓扑与额度状态，提供一键预设与快速调优。', 'Real-time overview of the resident MCP connection pool, upstream search engine matrix, and quick workflow presets.'],
      poolTitle: ['常驻 MCP 连接池监控', 'Resident MCP Pool Monitoring'],
      poolStatusHealthy: ['按需连接 · 连接数不常驻', 'On demand · no resident connections'],
      presetTitle: ['工作流快速预设', 'Workflow Presets'],
      presetHint: ['一键对齐常用工作场景的并发与引擎策略，免去逐项手动调优。', 'One-click presets tailored for common research and search workflows.'],
      presetFast: ['极速日常', 'Fast & Free'],
      presetFastDesc: ['免密免费源优先，关闭 TF-IDF 重排与数值交叉比对，超低延迟', 'Keyless & free sources, disable rerank & crosscheck for lowest latency'],
      presetStandard: ['平衡生产（推荐）', 'Balanced (Recommended)'],
      presetStandardDesc: ['开启 TF-IDF 重排、数值交叉核验、主流付费源，标准超时', 'TF-IDF rerank, numeric crosscheck, mainstream paid sources, standard timeouts'],
      presetDeep: ['深度调研', 'Deep Research'],
      presetDeepDesc: ['激活更多取证工具、对冲自适应宽限、语义证据层、更大并发', 'Full forensic tools, adaptive hedge grace, semantic evidence layer, max concurrency'],
      presetApplied: ['已应用预设：', 'Preset applied:'],
      matrixTitle: ['核心引擎拓扑与覆盖全景', 'Core Engine Topology & Coverage'],
      matrixHint: ['当前可用引擎的接入层级与角色分工。带免密标识的引擎无需密钥即可直接使用。', 'Access tiers and roles of available engines. Keyless engines work without an API key.'],
      matrixConfigured: ['密钥已就绪', 'Key Ready'],
      matrixKeyless: ['免密层可用', 'Keyless Ready'],
      matrixPending: ['待配密钥', 'Key Required'],
      searchFilterPlaceholder: ['输入关键词快速筛选...', 'Filter by keyword or engine...'],
      summary: ['Argo · web_search {search} · web_fetch {fetch} · 原生工具 {tools} 个 · 研究 {workers}/{max}', 'Argo · web_search {search} · web_fetch {fetch} · {tools} native tools · research {workers}/{max}'],
      accessTitle: ['接入方式', 'Access'],
      searchSeam: ['宿主 web_search 走 argo', 'Route host web_search to argo'],
      searchSeamHint: ['关闭后 web_search 回到宿主默认搜索后端（web 行 searchProvider）。', 'When off, web_search falls back to the host provider configured on the web row.'],
      fetchSeam: ['宿主 web_fetch 走 argo', 'Route host web_fetch to argo'],
      fetchSeamHint: ['开启前必须在 web 配置行加 fetchProvider: argo；否则两个 provider 同时可用会报 WEB_PROVIDER_AMBIGUOUS。', 'Requires fetchProvider: argo on the web row; otherwise two available providers trigger WEB_PROVIDER_AMBIGUOUS.'],
      entryTitle: ['引擎入口', 'Engine entry'],
      entryHint: ['npx 模式填 npx，参数 -y / github:taxueseek/argo；本机模式填 python3，参数填 mcp_server.py 的绝对路径。', 'npx mode: command npx, args -y / github:taxueseek/argo. Local mode: python3 plus the absolute mcp_server.py path.'],
      command: ['命令', 'Command'],
      args: ['参数（一行一个）', 'Args (one per line)'],
      searchTimeout: ['搜索超时 (ms)', 'Search timeout (ms)'],
      fetchTimeout: ['抓取超时 (ms)', 'Fetch timeout (ms)'],
      fetchChars: ['抓取正文上限（字符）', 'Fetch body cap (chars)'],
      idle: ['连接空闲回收 (ms)', 'Idle recycle (ms)'],
      concurrency: ['并发连接数', 'Concurrent connections'],
      toolsTitle: ['原生工具', 'Native tools'],
      toolsHint: ['勾选即注册为原生一等工具（CLI 单发，不依赖 MCP）。长尾工具只在需要时开，避免每轮工具面膨胀。', 'Checked tools register as native first-class tools (single-shot CLI, no MCP). Enable long-tail tools only when needed.'],
      toolPresetTitle: ['推荐组合', 'Recommended sets'],
      toolPresetHint: ['按场景一键套用（会覆盖当前勾选）。工具分区见下方。', 'Apply a scenario preset in one click (replaces current selection). See the grouped tools below.'],
      coreTools: ['高频路径', 'Hot path'],
      longTools: ['长尾取证', 'Long tail'],
      nativeTimeout: ['单次调用超时 (ms)', 'Per-call timeout (ms)'],
      nativeChars: ['返回文本上限（字符）', 'Returned text cap (chars)'],
      keysTitle: ['API 密钥', 'API keys'],
      keysHint: ['密钥写入 DSH 凭证库（与 Models 页同一处），不会写进插件配置、不回显明文。只有勾选「转发」的引用才会作为环境变量传给 argo 子进程。「清除」只删凭证库里的值，环境变量或 .env 提供的 key 仍会生效。', 'Keys are stored in the DSH credential store (same place as the Models page), never in plugin config and never echoed. Only refs marked Forward reach the argo child process. Clear removes the stored value only; a key supplied by the environment or .env still applies.'],
      commonKeys: ['常用引擎', 'Common engines'],
      moreKeys: ['更多引擎', 'More engines'],
      configured: ['已配置', 'Configured'],
      notConfigured: ['未配置', 'Not configured'],
      forward: ['转发', 'Forward'],
      keyPlaceholderSet: ['输入新值（留空保留现有密钥）', 'Enter a new value (leave blank to keep the current key)'],
      keyPlaceholderEmpty: ['粘贴 API Key', 'Paste API key'],
      saveKey: ['保存', 'Save'],
      clearKey: ['清除', 'Clear'],
      keySaved: ['密钥已保存；下一次新建 argo 连接生效（池内旧连接最长 60s 回收）。', 'Key saved; effective on the next new argo connection (old pooled connections recycle within ~60s).'],
      keyCleared: ['密钥已清除。', 'Key cleared.'],
      keyUnavailable: ['凭证服务不可用。', 'Credential service unavailable.'],
      researchTitle: ['研究编排', 'Research'],
      workers: ['默认 worker 数', 'Default workers'],
      maxWorkers: ['worker 上限', 'Worker cap'],
      tracks: ['轨道上限', 'Track cap'],
      sources: ['每轨信源上限', 'Sources per track'],
      workerTokens: ['worker token 上限', 'Worker token cap'],
      synthTokens: ['综合 token 上限', 'Synthesis token cap'],
      reportDir: ['报告落盘目录', 'Report directory'],
      advancedTitle: ['高级', 'Advanced'],
      allow: ['worker 工具白名单（一行一个）', 'Worker tool allowlist (one per line)'],
      allowHint: ['留空表示使用内置默认（MCP 与原生工具双写）。wide_research 自身与 argo_research 会被硬排除。', 'Empty uses the built-in default (MCP + native tools). wide_research itself and argo_research are always excluded.'],
      deny: ['worker 工具拒绝名单（一行一个）', 'Worker tool denylist (one per line)'],
      keyRefs: ['凭证转发名单（一行一个）', 'Credential forwarding refs (one per line)'],
      keyRefsHint: ['这些引用名会经 DSH credentials 解析后作为环境变量传给 argo 子进程；没有对应凭证的项会被跳过。', 'These refs resolve through DSH credentials and reach the argo child process as environment variables. Missing refs are skipped.'],
      structural: ['结构性字段', 'Structural fields'],
      structuralHint: ['toolName / provider / searchProviderId / fetchProviderId 只能改 profile patch；watched patch 由官方 watcher 同 PID 重组，改后核对功能即可，不默认重启。', 'toolName / provider / searchProviderId / fetchProviderId are profile-patch only; the watched patch is reconciled by the official watcher on the same PID. Verify the behavior, no default restart.'],
      mcpHint: ['MCP 全量工具面（mcp__argo__*）是 profile 里的独立 mcp-argo 行，不在本页开关；需要时在 patch 中取消注释后按 live activation 分支验证。', 'The full MCP tool face (mcp__argo__*) is a separate mcp-argo entry in the profile patch; enable it there and verify through the live activation branch.'],
      noKey: ['（未配置）', '(not configured)'],
      enginesTitle: ['argo 引擎开关', 'argo engine switches'],
      enginesHint: ['这些开关原先是 argo 的环境变量，只能改 ~/.config/argo/env 或启动环境；现在写进插件配置，随 argo 子进程生效。改动对下一次新建连接生效（池内旧连接按「连接空闲回收」时长退出，默认 60s）。密钥不在这里配，请用「密钥」页；本页也不收授权类开关（可复算执行、内网 URL）。', 'These switches used to be argo environment variables, editable only in ~/.config/argo/env or the launch environment. They now live in plugin config and reach the argo child process. Changes apply to the next new connection (pooled connections retire after the idle recycle window, 60s by default). Keys do not belong here; use the Keys tab. Authorization switches (recompute execution, private URLs) are deliberately not exposed.'],
      groupCache: ['缓存与排序', 'Cache & ranking'],
      groupFetch: ['抓取层', 'Fetch layer'],
      groupSemantic: ['语义与自适应', 'Semantics & adaptivity'],
      groupEngines: ['引擎启停', 'Engine on-off'],
      groupNet: ['网络出口', 'Network'],
      groupLocal: ['本机检索', 'Local search'],
      groupMisc: ['其他', 'Misc'],
      groupPaid: ['额度与预算', 'Quotas & budgets'],
      groupPaidHint: ['免费与付费源统一管理：金额计费的源设月度预算，按次数限额的源设日/月次数上限；0=不限制。额度或预算用尽后该源自动跳过并回落免费源。改动对下一次新建连接生效。', 'Free and paid sources share one manager: money-metered sources take a monthly budget, count-capped sources take daily/monthly call caps; 0 = unlimited. When a budget or cap is exhausted, that source is skipped and free fallbacks take over. Changes apply to the next new connection.'],
      envDefault: ['默认', 'default'],
      envRange: ['需在 {min}–{max} 之间', 'must be between {min} and {max}'],
      keySourceFile: ['凭证库', 'credential store'],
      keySourceEnv: ['启动环境', 'launch environment'],
      keySourceProjectEnv: ['项目 .env', 'project .env'],
      keySourceUserEnv: ['家目录 .env', 'home .env'],
      keySourceOther: ['其他层', 'other layer'],
      keyReadonly: ['只读', 'read-only'],
      keyOverrideHint: ['保存后将覆盖当前层', 'saving overrides the current layer'],
      deepseekShared: ['与宿主 web_search 官方后端共用同一把 key，改动同时影响两边', 'Shared with the host web_search backend; changing it affects both'],
    };

    // 工具分区（场景化）：核心 / 取证 / 本地 / 长尾。
    // 旧版只有 core/long 两组，16 个长尾工具平铺，新用户不知道该开哪些；
    // 按使用场景分组后，配合下方「推荐组合」卡可以一键套用。
    const TOOLS = [
      { id: 'argo_search', group: 'core', zh: '统一搜索', en: 'Unified search', sceneZh: '日常一问的首选', sceneEn: 'Default for everyday queries' },
      { id: 'argo_fetch', group: 'core', zh: '智能抓取', en: 'Smart fetch', sceneZh: '读某个具体页面', sceneEn: 'Read a specific page' },
      { id: 'argo_evidence', group: 'forensics', zh: '来源可信度评估', en: 'Source credibility', sceneZh: '高后果问题的证据分级', sceneEn: 'Grade evidence for high-stakes answers' },
      { id: 'argo_clarify', group: 'forensics', zh: '意图消歧', en: 'Intent clarification', sceneZh: '查询有歧义时先消歧', sceneEn: 'Disambiguate an ambiguous query' },
      { id: 'argo_crawl', group: 'forensics', zh: '站点级爬取', en: 'Site crawl', sceneZh: '整站/多页采集', sceneEn: 'Site-level or multi-page collection' },
      { id: 'argo_extract', group: 'forensics', zh: '结构化提取', en: 'Structured extract', sceneZh: '从页面抽表格/字段', sceneEn: 'Extract tables/fields from a page' },
      { id: 'argo_pdf', group: 'forensics', zh: 'PDF 提取', en: 'PDF extract', sceneZh: '读 PDF 正文', sceneEn: 'Read PDF text' },
      { id: 'argo_screenshot', group: 'forensics', zh: '网页截图', en: 'Screenshot', sceneZh: '取证留档/看渲染结果', sceneEn: 'Archive or inspect a rendered page' },
      { id: 'argo_article', group: 'forensics', zh: '公众号全文', en: 'WeChat article', sceneZh: '读公众号文章正文', sceneEn: 'Read a WeChat article' },
      { id: 'argo_local_search', group: 'local', zh: '本机文件搜索', en: 'Local file search', sceneZh: '搜本机笔记/代码', sceneEn: 'Search local notes/code' },
      { id: 'argo_local_read', group: 'local', zh: '本机文件读取', en: 'Local file read', sceneZh: '读本机文件', sceneEn: 'Read a local file' },
      { id: 'argo_recompute', group: 'local', zh: '可复算执行器', en: 'Recompute', sceneZh: '本地可复算任务（需授权）', sceneEn: 'Local recompute (needs authorization)' },
      { id: 'argo_social_search', group: 'longtail', zh: '社交平台搜索', en: 'Social search', sceneZh: '舆情/UGC 采集', sceneEn: 'Sentiment / UGC collection' },
      { id: 'argo_job', group: 'longtail', zh: '招聘聚合', en: 'Job aggregation', sceneZh: '招聘/岗位聚合', sceneEn: 'Job aggregation' },
      { id: 'argo_preflight', group: 'longtail', zh: '批量 URL 预检', en: 'URL preflight', sceneZh: '批量检查 URL 可达性', sceneEn: 'Batch-check URL reachability' },
      { id: 'argo_answer', group: 'longtail', zh: '带引用直答', en: 'Cited answer', sceneZh: '在给定语料里直答', sceneEn: 'Answer from a given corpus' },
      { id: 'argo_watch', group: 'longtail', zh: '网页变化监控', en: 'Page watch', sceneZh: '盯页面变化', sceneEn: 'Watch a page for changes' },
      { id: 'argo_cite', group: 'longtail', zh: 'DOI 引用生成', en: 'DOI citation', sceneZh: '生成引用条目', sceneEn: 'Generate a citation' },
    ];

    // 分区展示名 + 推荐组合（场景化，让用户一键套用而不是逐个勾选）
    const TOOL_GROUPS = [
      { id: 'core', zh: '高频路径', en: 'Hot path' },
      { id: 'forensics', zh: '取证与核验', en: 'Forensics' },
      { id: 'local', zh: '本机', en: 'Local' },
      { id: 'longtail', zh: '长尾', en: 'Long tail' },
    ];
    const TOOL_PRESETS = [
      { id: 'daily', zh: '日常', en: 'Daily', tools: ['argo_search', 'argo_fetch'], descZh: '一问一搜，最快', descEn: 'Fastest for simple queries' },
      { id: 'research', zh: '研究', en: 'Research', tools: ['argo_search', 'argo_fetch', 'argo_evidence', 'argo_clarify'], descZh: '加证据分级与消歧', descEn: 'Add evidence grading + disambiguation' },
      { id: 'forensics', zh: '取证', en: 'Forensics', tools: ['argo_search', 'argo_fetch', 'argo_crawl', 'argo_pdf', 'argo_screenshot', 'argo_extract'], descZh: '整站/多页/PDF 留档', descEn: 'Site/multi-page/PDF archiving' },
    ];

    // <generated:keys> 由 scripts/sync_backends.py 从引擎声明生成，勿手改
    const KEYS = [
      { ref: "DEEPSEEK_API_KEY", common: true, label: "DeepSeek Search", en: "DeepSeek Search", engines: ["deepseek_search"] },
      { ref: "ARGO_YUANBAO_API_KEY", common: true, label: "腾讯元宝搜索", en: "Tencent Yuanbao", engines: ["yuanbao"] },
      { ref: "TAVILY_API_KEY", common: true, label: "Tavily", en: "Tavily", engines: ["tavily"] },
      { ref: "EXA_API_KEY", common: true, label: "Exa语义搜索", en: "Exa Semantic Search", engines: ["exa"] },
      { ref: "BOCHA_API_KEY", common: true, label: "博查搜索", en: "Bocha Search", engines: ["bocha", "bocha_ai"] },
      { ref: "BRAVE_API_KEY", common: true, label: "Brave", en: "Brave Search", engines: ["brave"] },
      { ref: "TINYFISH_API_KEY", common: true, label: "TinyFish 实时搜索", en: "TinyFish Live Search", engines: ["tinyfish", "tinyfish_news", "tinyfish_paper"] },
      { ref: "ANYSEARCH_API_KEY", common: false, label: "AnySearch", en: "AnySearch", engines: ["anysearch"] },
      { ref: "ARGO_PIXIV_REFRESH_TOKEN", common: false, label: "Pixiv 公开作品", en: "Pixiv App OAuth", engines: ["pixiv"] },
      { ref: "EASTMONEY_APIKEY", common: false, label: "东财妙想", en: "Eastmoney Miaoxiang", engines: ["em_miaoxiang"] },
      { ref: "FELO_API_KEY", common: false, label: "Felo AI搜索", en: "Felo AI Search", engines: ["felo"] },
      { ref: "FIRECRAWL_API_KEY", common: false, label: "Firecrawl", en: "Firecrawl", engines: ["firecrawl"] },
      { ref: "FOURGET_URL", common: false, label: "4get 极简元搜索", en: "4get", engines: ["fourget"] },
      { ref: "GEMINI_API_KEY", common: false, label: "语义证据·Gemini 决策", en: "Semantic Evidence · Gemini Decision", engines: [] },
      { ref: "GITHUB_TOKEN", common: false, label: "GitHub", en: "GitHub", engines: ["github"] },
      { ref: "KEENABLE_API_KEY", common: false, label: "Keenable", en: "Keenable", engines: ["keenable"] },
      { ref: "METASO_API_KEY", common: false, label: "秘塔", en: "Metaso", engines: ["metaso", "metaso_doc", "metaso_image", "metaso_podcast", "metaso_scholar", "metaso_video"] },
      { ref: "OCTEN_API_KEY", common: false, label: "Octen", en: "Octen", engines: ["octen"] },
      { ref: "OPENCORPORATES_API_TOKEN", common: false, label: "OpenCorporates 公司注册", en: "OpenCorporates", engines: ["opencorporates"] },
      { ref: "PARALLEL_API_KEY", common: false, label: "Parallel搜索", en: "Parallel Search", engines: ["parallel"] },
      { ref: "QWEATHER_KEY", common: false, label: "和风天气", en: "QWeather", engines: ["qweather"] },
      { ref: "SAUCENAO_API_KEY", common: false, label: "SauceNAO 反搜图", en: "SauceNAO Reverse Image", engines: ["saucenao"] },
      { ref: "SCIVERSE_API_TOKEN", common: false, label: "Sciverse（题录）", en: "Sciverse", engines: ["sciverse"] },
      { ref: "SEARXNG_URL", common: false, label: "SearXNG", en: "SearXNG", engines: ["searxng"] },
      { ref: "SELTZ_API_KEY", common: false, label: "Seltz搜索", en: "Seltz Search", engines: ["seltz"] },
      { ref: "SERPAPI_API_KEY", common: false, label: "Google Lens 反搜图", en: "Google Lens (SerpAPI)", engines: ["google_lens"] },
      { ref: "TYPESAFE_API_KEY", common: false, label: "语义证据·JEV（TypeSafe）", en: "Semantic Evidence · JEV (TypeSafe)", engines: [] },
      { ref: "UNPAYWALL_EMAIL", common: false, label: "Unpaywall 开放获取定位", en: "Unpaywall Open Access", engines: ["unpaywall"] },
      { ref: "WEB_SEARCH_API_KEY", common: false, label: "字节搜索", en: "ByteDance Search", engines: ["byted"] },
      { ref: "WEREAD_API_KEY", common: false, label: "微信读书", en: "WeRead", engines: ["weread"] },
      { ref: "WOLFRAM_APPID", common: false, label: "WolframAlpha", en: "WolframAlpha", engines: ["wolframalpha"] },
      { ref: "YDC_API_KEY", common: false, label: "You.com搜索", en: "You.com Search", engines: ["you"] },
      { ref: "ZHIHU_ACCESS_SECRET", common: false, label: "知乎", en: "Zhihu", engines: ["zhihu", "zhihu_global", "zhihu_hot", "zhihu_kb", "zhihu_questions", "zhihu_user"] },
    ];
    // </generated:keys>

    const CORE_ENGINES = [
      { id: 'deepseek', nameZh: 'DeepSeek 搜索', nameEn: 'DeepSeek Search', roleZh: '通用搜索 / 官方对齐', roleEn: 'General / Official Seam', ref: 'DEEPSEEK_API_KEY', keyless: false },
      { id: 'yuanbao', nameZh: '腾讯元宝', nameEn: 'Tencent Yuanbao', roleZh: '中文对冲主力', roleEn: 'Chinese General Primary', ref: 'ARGO_YUANBAO_API_KEY', keyless: false },
      { id: 'tavily', nameZh: 'Tavily Search', nameEn: 'Tavily Search', roleZh: 'AI 知识增强检索', roleEn: 'AI Knowledge Retrieval', ref: 'TAVILY_API_KEY', keyless: false },
      { id: 'exa', nameZh: 'Exa Neural', nameEn: 'Exa Neural', roleZh: '神经语义精准搜索', roleEn: 'Neural Semantic Search', ref: 'EXA_API_KEY', keyless: false },
      { id: 'brave', nameZh: 'Brave Search', nameEn: 'Brave Search', roleZh: '独立全球 Web 索引', roleEn: 'Independent Global Index', ref: 'BRAVE_API_KEY', keyless: true },
      { id: 'bocha', nameZh: '博查 Bocha', nameEn: 'Bocha', roleZh: '垂直多源聚合', roleEn: 'Vertical Multi-source', ref: 'BOCHA_API_KEY', keyless: false },
      { id: 'jina', nameZh: 'Jina Reader', nameEn: 'Jina Reader', roleZh: '正文高保真提取', roleEn: 'Article Text Extraction', ref: '', keyless: true },
      { id: 'tinyfish', nameZh: 'TinyFish', nameEn: 'TinyFish', roleZh: '无头渲染抓取兜底', roleEn: 'Headless Render Fallback', ref: 'TINYFISH_API_KEY', keyless: true },
      { id: 'zhihu', nameZh: '知乎 Zhihu', nameEn: 'Zhihu', roleZh: '中文专业问答舆情', roleEn: 'Professional Q&A / Social', ref: 'ZHIHU_ACCESS_SECRET', keyless: true },
    ];

    // argo 引擎开关（argoEnv 的 KEY=VALUE 行）。与 specs.js 的 ARGO_ENV_SPECS
    // 同一份名单，由 test/client.test.js 的漂移门禁核对；kind 决定控件：
    // bool=开关、num=数字、str=文本、list=名单。def 仅作「未配置时显示什么」，
    // 缺省即不写该行。开关文案写正向行为或明确的关闭动作，不再用 invert 反转
    // （历史 bug 见 index.js 注释）。
    const ENGINE_TOGGLES = [
      { name: 'ARGO_NO_CACHE', kind: 'bool', group: 'cache', def: false, zh: '测试模式（关闭结果缓存）', en: 'Test mode (disable result cache)', hintZh: '日常保持关闭：结果缓存 1 小时。改进 Skill/插件、需要看实时结果时再打开', hintEn: 'Off by default: results are cached for 1h. Enable only when developing the skill/plugin' },
      { name: 'ARGO_TFIDF_RERANK', kind: 'bool', group: 'cache', def: true, zh: 'TF-IDF 重排', en: 'TF-IDF rerank', hintZh: '多引擎结果融合前的相关性重排，默认开', hintEn: 'Relevance rerank before fusion, on by default' },
      { name: 'ARGO_NUMBER_CROSSCHECK', kind: 'bool', group: 'cache', def: true, zh: '数值交叉校验', en: 'Number cross-check', hintZh: '对查询里的数值主张做跨源核对，默认开', hintEn: 'Cross-check numeric claims across sources, on by default' },
      { name: 'ARGO_MINHASH_DEDUPE', kind: 'bool', group: 'cache', def: true, zh: 'MinHash 去重', en: 'MinHash dedupe', hintZh: '近重复结果合并，默认开', hintEn: 'Merge near-duplicate results, on by default' },
      { name: 'ARGO_MINHASH_THRESHOLD', kind: 'num', group: 'cache', def: 0.85, min: 0.5, max: 1, step: 0.05, zh: '去重阈值', en: 'Dedupe threshold', hintZh: '越高越严（越多判为不同），默认 0.85', hintEn: 'Higher = stricter, default 0.85' },
      { name: 'ARGO_SERP_GUARD', kind: 'bool', group: 'cache', def: true, zh: 'SERP 守卫', en: 'SERP guard', hintZh: '拦截壳页/风控页混入结果，默认开；误杀时先关它复现', hintEn: 'Keep shell/blocked pages out of results, on by default' },
      { name: 'ARGO_FETCH_JINA', kind: 'bool', group: 'fetch', def: true, zh: 'Jina 阅读器抓取', en: 'Jina reader fetch', hintZh: 'r.jina.ai 正文提取，默认开', hintEn: 'r.jina.ai text extraction, on by default' },
      { name: 'ARGO_FETCH_MOBILE', kind: 'bool', group: 'fetch', def: true, zh: '移动端 UA 抓取', en: 'Mobile UA fetch', hintZh: '按 host 记忆移动端身份，默认开', hintEn: 'Per-host mobile identity, on by default' },
      { name: 'ARGO_FETCH_PARALLEL', kind: 'bool', group: 'fetch', def: true, zh: 'Parallel 免费渲染抓取', en: 'Parallel free render fetch', hintZh: 'keyless 云端渲染兜底，默认开', hintEn: 'Keyless cloud render fallback, on by default' },
      { name: 'ARGO_FETCH_DEADLINE_S', kind: 'num', group: 'fetch', def: 60, min: 5, max: 300, step: 5, zh: '抓取总截止（秒）', en: 'Fetch deadline (s)', hintZh: '单次抓取全链路墙钟上限，默认 60', hintEn: 'Wall-clock cap per fetch, default 60' },
      { name: 'ARGO_SEMANTIC_EVIDENCE', kind: 'bool', group: 'semantic', def: false, zh: '语义证据层', en: 'Semantic evidence', hintZh: '显式核验链路的零样本判定，默认关；开启后多一次外呼', hintEn: 'Zero-shot support check on the verify path, off by default' },
      { name: 'ARGO_ADAPTIVE_GRACE', kind: 'bool', group: 'semantic', def: true, zh: '对冲宽限自适应', en: 'Adaptive hedge grace', hintZh: '按引擎历史 p75 收窄对冲等待窗，默认开', hintEn: 'Narrow hedge window by engine p75, on by default' },
      { name: 'ARGO_ADAPTIVE_TAIL', kind: 'bool', group: 'semantic', def: false, zh: '分层尾宽', en: 'Layered straggler tail', hintZh: '尾宽按未回来引擎分层取值，默认关', hintEn: 'Per-engine layered tail width, off by default' },
      { name: 'ARGO_STRAGGLER_GRACE_S', kind: 'num', group: 'semantic', def: 1.5, min: 0, max: 30, step: 0.5, zh: '掉队宽限（秒）', en: 'Straggler grace (s)', hintZh: '已有引擎交付后还等多久，默认 1.5；0 关闭', hintEn: 'Wait after first delivery, default 1.5; 0 disables' },
      { name: 'ARGO_SERIAL_STAGGER_S', kind: 'num', group: 'semantic', def: 0.8, min: 0, max: 30, step: 0.5, zh: '串行起步间隔（秒）', en: 'Serial stagger (s)', hintZh: '首引擎超时前补发备选源的提前量，默认 0.8；0 最激进', hintEn: 'Lead before firing a backup source, default 0.8; 0 = fire together' },
      { name: 'ARGO_ENABLE_ENGINES', kind: 'list', group: 'engines', zh: '额外启用引擎', en: 'Extra enabled engines', hintZh: '逗号分隔；叠加在 config.yaml 启用集之上', hintEn: 'Comma-separated; adds to the config.yaml enabled set' },
      { name: 'ARGO_DISABLE_ENGINES', kind: 'list', group: 'engines', zh: '停用引擎', en: 'Disabled engines', hintZh: '逗号分隔；优先级高于启用集，调试单引擎时用', hintEn: 'Comma-separated; wins over the enabled set' },
      { name: 'ARGO_PROXY', kind: 'str', group: 'net', zh: 'argo 全局代理', en: 'argo global proxy', hintZh: 'http:// 代理 URL，或 direct 强制直连；留空走环境变量', hintEn: 'http:// proxy URL, or direct; empty falls back to env' },
      { name: 'ARGO_GDELT_PROXY', kind: 'str', group: 'net', zh: 'GDELT 代理', en: 'GDELT proxy', hintZh: 'GDELT 按出口 IP 限流，经本地代理更稳；留空自动直连', hintEn: 'GDELT rate-limits by egress IP; empty = direct' },
      { name: 'ARGO_LOCAL_SEEK_ROOTS', kind: 'list', group: 'local', zh: '本机检索根目录', en: 'Local seek roots', hintZh: '路径分隔符分隔；seek.py 的候选安装根，默认用打包位置', hintEn: 'Path-separated roots for seek.py; default uses the bundled copy' },
      { name: 'ARGO_LOCAL_SEEK_PATH', kind: 'str', group: 'local', zh: 'seek.py 绝对路径', en: 'seek.py path', hintZh: '显式指定 seek.py，优先于根目录候选', hintEn: 'Explicit seek.py, wins over root candidates' },
      { name: 'ARGO_LOCAL_READ_DIRS', kind: 'list', group: 'local', zh: '本机读取白名单', en: 'Local read allowlist', hintZh: '逗号分隔；不配置则 local_read 一律不放行', hintEn: 'Comma-separated; empty means local_read allows nothing' },
      { name: 'ARGO_TEXT_LEXICON', kind: 'str', group: 'local', zh: '词表 JSON 路径', en: 'Lexicon JSON path', hintZh: '文本处理词表覆盖，默认用打包词表', hintEn: 'Text lexicon override; default uses the bundled one' },
      { name: 'ARGO_PAID_SEARCH', kind: 'str', group: 'paid', def: 'auto', zh: '付费源策略', en: 'Paid source policy', hintZh: 'auto=优势域自动使用付费源；off=只用免费源。额度见下方', hintEn: 'auto = use paid sources in proven domains; off = free sources only. See quotas below' },
      { name: 'ARGO_BUDGET_DEEPSEEK_SEARCH', kind: 'num', group: 'paid', def: 5, min: 0, max: 1000, step: 1, zh: 'DeepSeek 月度预算（元）', en: 'DeepSeek monthly budget (CNY)', hintZh: '0=关闭；¥5≈1000次成功调用（¥0.005/次）', hintEn: '0 = off; CNY 5 ≈ 1000 successful calls (CNY 0.005 each)' },
      { name: 'ARGO_BUDGET_EXA', kind: 'num', group: 'paid', def: 10, min: 0, max: 10000, step: 1, zh: 'Exa 每月预算（美元）', en: 'Exa monthly budget (USD)', hintZh: '默认 $10/月（约 1000 次）；0=关闭自动使用。按成功调用计费', hintEn: 'Default $10/month (~1000 calls); 0 = off. Billed per successful call' },
      { name: 'ARGO_QUOTA_ANYSEARCH_LIMIT', kind: 'num', group: 'paid', def: 1000, min: 0, max: 1000000, step: 1, zh: 'AnySearch 每日次数上限', en: 'AnySearch daily call cap', hintZh: '默认 1000/日；0=不限制。改动对下一次新建连接生效', hintEn: 'Default 1000/day; 0 = unlimited. Applies to new connections' },
      { name: 'ARGO_QUOTA_ZHIHU_LIMIT', kind: 'num', group: 'paid', def: 5000, min: 0, max: 1000000, step: 1, zh: '知乎 每日次数上限', en: 'Zhihu daily call cap', hintZh: '默认 5000/日；0=不限制。改动对下一次新建连接生效', hintEn: 'Default 5000/day; 0 = unlimited. Applies to new connections' },
      { name: 'ARGO_QUOTA_YUANBAO_LIMIT', kind: 'num', group: 'paid', def: 2000, min: 0, max: 1000000, step: 1, zh: '元宝 每月次数上限', en: 'Yuanbao monthly call cap', hintZh: '默认 2000/月；0=不限制。改动对下一次新建连接生效', hintEn: 'Default 2000/month; 0 = unlimited. Applies to new connections' },
      { name: 'ARGO_QUOTA_TAVILY_LIMIT', kind: 'num', group: 'paid', def: 1000, min: 0, max: 1000000, step: 1, zh: 'Tavily 每月次数上限', en: 'Tavily monthly call cap', hintZh: '默认 1000/月；0=不限制。改动对下一次新建连接生效', hintEn: 'Default 1000/month; 0 = unlimited. Applies to new connections' },
      { name: 'ARGO_NO_PREWARM', kind: 'bool', group: 'misc', def: false, zh: '关闭连接预热', en: 'Disable prewarm', hintZh: 'MCP 握手后预热引擎注册表，默认开；开启本项即关闭预热', hintEn: 'Prewarm the engine registry after handshake, on by default; enable this to turn it off' },
      { name: 'ARGO_USAGE_LOG', kind: 'bool', group: 'misc', def: true, zh: '使用日志', en: 'Usage log', hintZh: 'argo 侧 JSONL 使用账本，默认开', hintEn: 'argo JSONL usage ledger, on by default' },
      { name: 'ARGO_ENGINE_HTTP_CLIENT', kind: 'bool', group: 'misc', def: true, zh: 'HttpClient 连接池', en: 'HttpClient pool', hintZh: '引擎 HTTP 走连接池，默认开；=0 回退 urllib（诊断用）', hintEn: 'Pooled engine HTTP, on by default; =0 falls back to urllib' },
      { name: 'ARGO_ADMISSION_TTL_S', kind: 'num', group: 'misc', def: 1, min: 0, max: 60, step: 0.5, zh: '准入记忆 TTL（秒）', en: 'Admission memo TTL (s)', hintZh: '引擎准入状态记忆时长，默认 1；0 每次现查', hintEn: 'Admission memo window, default 1; 0 = always fresh' },
      { name: 'ARGO_CONFIG_STAMP_TTL_S', kind: 'num', group: 'misc', def: 1, min: 0, max: 60, step: 0.5, zh: '配置戳记忆 TTL（秒）', en: 'Config stamp memo TTL (s)', hintZh: '外部引擎扫描记忆时长，默认 1；0 每次现扫', hintEn: 'External engine scan memo window, default 1; 0 = always rescan' },
    ];
    const ENV_NAMES = new Set(ENGINE_TOGGLES.map((spec) => spec.name));

    const DEFAULTS = {
      searchCommand: 'npx', searchArgs: ['-y', 'github:taxueseek/argo'],
      searchTimeoutMs: 30000, fetchTimeoutMs: 25000, fetchMaxChars: 50000,
      searchIdleMs: 60000, searchConcurrency: 3, nativeTimeoutMs: 60000, nativeMaxChars: 24000,
      defaultWorkers: 6, maxWorkers: 9, maxTracks: 9, maxSourcesPerTrack: 5,
      workerMaxTokens: 5000, synthesisMaxTokens: 7000, reportDir: '',
    };

    const FORM_FIELDS = {
      access: [
        { key: 'command', prop: 'searchCommand', type: 'text', label: 'command', layout: 'full' },
        { key: 'argsText', prop: 'searchArgs', type: 'lines', label: 'args', fallbackNonEmpty: true, layout: 'full' },
        { key: 'searchTimeoutMs', prop: 'searchTimeoutMs', type: 'num', min: 1000, label: 'searchTimeout' },
        { key: 'fetchTimeoutMs', prop: 'fetchTimeoutMs', type: 'num', min: 1000, label: 'fetchTimeout' },
        // argo_fetch 工具 schema 上限 50000：与 clamp 对齐防显示失真
        { key: 'fetchMaxChars', prop: 'fetchMaxChars', type: 'num', min: 500, max: 50_000, step: 500, label: 'fetchChars' },
        { key: 'searchIdleMs', prop: 'searchIdleMs', type: 'num', min: 0, step: 1000, label: 'idle' },
        { key: 'searchConcurrency', prop: 'searchConcurrency', type: 'num', min: 1, label: 'concurrency' },
      ],
      tools: [
        { key: 'nativeTimeoutMs', prop: 'nativeTimeoutMs', type: 'num', min: 1000, label: 'nativeTimeout' },
        { key: 'nativeMaxChars', prop: 'nativeMaxChars', type: 'num', min: 500, step: 500, label: 'nativeChars' },
      ],
      research: [
        { key: 'defaultWorkers', prop: 'defaultWorkers', type: 'num', min: 1, label: 'workers' },
        { key: 'maxWorkers', prop: 'maxWorkers', type: 'num', min: 1, minDraft: 'defaultWorkers', label: 'maxWorkers' },
        { key: 'maxTracks', prop: 'maxTracks', type: 'num', min: 2, label: 'tracks' },
        { key: 'maxSourcesPerTrack', prop: 'maxSourcesPerTrack', type: 'num', min: 1, label: 'sources' },
        { key: 'workerMaxTokens', prop: 'workerMaxTokens', type: 'num', min: 512, step: 512, label: 'workerTokens' },
        { key: 'synthesisMaxTokens', prop: 'synthesisMaxTokens', type: 'num', min: 512, step: 512, label: 'synthTokens' },
        { key: 'reportDir', prop: 'reportDir', type: 'text', allowEmpty: true, label: 'reportDir', layout: 'full' },
      ],
      advanced: [
        { key: 'allowText', prop: 'childToolAllow', type: 'lines', label: 'allow', hint: 'allowHint', layout: 'full' },
        { key: 'denyText', prop: 'childToolDeny', type: 'lines', label: 'deny', layout: 'full' },
        { key: 'keyRefsText', prop: 'keyRefs', type: 'lines', label: 'keyRefs', hint: 'keyRefsHint', layout: 'full' },
      ],
    };

    const PRESETS = [
      {
        id: 'fast', titleKey: 'presetFast', descKey: 'presetFastDesc', badge: '15s 超时', badgeColor: 'blue', active: false,
        env: { ARGO_TFIDF_RERANK: '0', ARGO_NUMBER_CROSSCHECK: '0', ARGO_PAID_SEARCH: 'off', ARGO_ADAPTIVE_GRACE: '1', ARGO_SERIAL_STAGGER_S: '0.5' },
        fields: { searchTimeoutMs: 15000, fetchTimeoutMs: 15000 },
      },
      {
        id: 'standard', titleKey: 'presetStandard', descKey: 'presetStandardDesc', badge: '标准生产', badgeColor: 'green', active: true,
        env: { ARGO_TFIDF_RERANK: '1', ARGO_NUMBER_CROSSCHECK: '1', ARGO_MINHASH_DEDUPE: '1', ARGO_PAID_SEARCH: 'auto', ARGO_ADAPTIVE_GRACE: '1', ARGO_SERIAL_STAGGER_S: '0.8' },
        fields: { searchTimeoutMs: 30000, fetchTimeoutMs: 25000 },
      },
      {
        id: 'deep', titleKey: 'presetDeep', descKey: 'presetDeepDesc', badge: '深度核验', badgeColor: 'blue', active: false,
        env: { ARGO_TFIDF_RERANK: '1', ARGO_NUMBER_CROSSCHECK: '1', ARGO_MINHASH_DEDUPE: '1', ARGO_SEMANTIC_EVIDENCE: '1', ARGO_ADAPTIVE_GRACE: '1', ARGO_PAID_SEARCH: 'auto' },
        fields: { searchTimeoutMs: 45000, fetchTimeoutMs: 35000, nativeTimeoutMs: 60000 },
      },
    ];

    const col = (gap) => ({ display: 'flex', flexDirection: 'column', gap });
    const rowBetween = (gap = 8) => ({ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap });
    const S = {
      wrap: { ...col(14), maxWidth: 800, color: 'var(--dsw-alias-label-primary, #1f2328)' },
      title: { margin: 0, fontSize: 18, fontWeight: 600, lineHeight: '26px' },
      hint: { margin: 0, fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-label-tertiary, #7b818b)' },
      notice: { margin: 0, fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-state-business-primary, #3b6ef6)' },
      error: { margin: 0, fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-state-error-primary, #d1242f)' },
      tabs: { display: 'flex', gap: 2, flexWrap: 'wrap', borderBottom: '.5px solid var(--dsw-alias-border-l2, #e5e7eb)' },
      tab: (active) => ({
        padding: '7px 12px 9px', border: 0, background: 'transparent', cursor: 'pointer',
        font: 'inherit', fontSize: 13, lineHeight: '20px', marginBottom: -1,
        color: active ? 'var(--dsw-alias-label-primary, #1f2328)' : 'var(--dsw-alias-label-tertiary, #7b818b)',
        borderBottom: active ? '2px solid var(--dsw-alias-state-business-primary, #3b6ef6)' : '2px solid transparent',
      }),
      card: { ...col(10), padding: '14px 16px', borderRadius: 12, border: '.5px solid var(--dsw-alias-border-l3, #e5e7eb)', background: 'var(--dsw-alias-settings-card-fill, #fff)' },
      cardTitle: { margin: 0, fontSize: 14, fontWeight: 600, lineHeight: '20px' },
      row: rowBetween(12),
      field: { ...col(4), minWidth: 0 },
      label: { fontSize: 13, fontWeight: 500, lineHeight: '18px' },
      sub: { fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-label-tertiary, #7b818b)' },
      input: { width: '100%', boxSizing: 'border-box', font: 'inherit', fontSize: 13, lineHeight: '20px', padding: '7px 9px', borderRadius: 8, border: '.5px solid var(--dsw-alias-border-l4, #d0d7de)', background: 'var(--dsw-alias-bg-layer-1, #fff)', color: 'inherit' },
      mono: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12 },
      textarea: { minHeight: 64, resize: 'vertical' },
      grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 },
      toolGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: 6 },
      check: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' },
      btn: { padding: '6px 12px', borderRadius: 8, border: '.5px solid var(--dsw-alias-border-l4, #d0d7de)', background: 'var(--dsw-alias-bg-layer-1, #fff)', color: 'inherit', font: 'inherit', fontSize: 12, lineHeight: '18px', cursor: 'pointer' },
      btnPrimary: { padding: '6px 14px', borderRadius: 8, border: '.5px solid var(--dsw-alias-state-business-primary, #3b6ef6)', background: 'var(--dsw-alias-state-business-primary, #3b6ef6)', color: '#fff', font: 'inherit', fontSize: 12, lineHeight: '18px', cursor: 'pointer' },
      status: { margin: 0, fontSize: 12, minHeight: 18, lineHeight: '18px' },
      keyRow: { display: 'grid', gridTemplateColumns: 'minmax(150px, 1fr) minmax(0, 1.5fr)', gap: 10, alignItems: 'start', padding: '10px 0', borderTop: '.5px solid var(--dsw-alias-border-l2, #eee)' },
      keyHead: { ...col(2), minWidth: 0 },
      code: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 10, color: 'var(--dsw-alias-label-tertiary, #7b818b)' },
      dot: (on) => ({ display: 'inline-block', width: 6, height: 6, borderRadius: 999, background: on ? '#2da44e' : 'var(--dsw-alias-border-l4, #d0d7de)', marginRight: 5 }),
      actions: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
      divider: { height: '.5px', background: 'var(--dsw-alias-border-l2, #eee)', margin: '2px 0' },
      metricGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 },
      metricCard: { ...col(6), padding: '12px 14px', borderRadius: 10, border: '.5px solid var(--dsw-alias-border-l3, #e5e7eb)', background: 'var(--dsw-alias-bg-layer-2, #f8fafc)' },
      metricLabel: { fontSize: 11, fontWeight: 500, color: 'var(--dsw-alias-label-tertiary, #7b818b)', textTransform: 'uppercase', letterSpacing: '0.04em' },
      metricValue: { fontSize: 18, fontWeight: 600, lineHeight: '22px', color: 'var(--dsw-alias-label-primary, #1f2328)' },
      metricSub: { fontSize: 11, lineHeight: '16px', color: 'var(--dsw-alias-label-secondary, #4b5563)' },
      presetGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 },
      presetCard: (active) => ({
        ...col(6), padding: '12px 14px', borderRadius: 10, cursor: 'pointer', textAlign: 'left',
        border: active ? '1.5px solid var(--dsw-alias-state-business-primary, #3b6ef6)' : '.5px solid var(--dsw-alias-border-l3, #e5e7eb)',
        background: active ? 'var(--dsw-alias-bg-layer-1, #fff)' : 'var(--dsw-alias-settings-card-fill, #fff)',
        boxShadow: active ? '0 1px 3px rgba(59, 110, 246, 0.12)' : 'none',
      }),
      matrixGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8 },
      matrixCard: { ...col(4), padding: '10px 12px', borderRadius: 8, border: '.5px solid var(--dsw-alias-border-l3, #e5e7eb)', background: 'var(--dsw-alias-bg-layer-1, #fff)' },
      badge: (c) => ({
        display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px', borderRadius: 999, fontSize: 10, fontWeight: 500, lineHeight: '14px',
        background: c === 'green' ? 'rgba(46, 160, 67, 0.12)' : (c === 'blue' ? 'rgba(59, 110, 246, 0.12)' : 'rgba(100, 116, 139, 0.12)'),
        color: c === 'green' ? '#2da44e' : (c === 'blue' ? '#3b6ef6' : '#64748b'),
        border: '.5px solid ' + (c === 'green' ? '#2da44e' : (c === 'blue' ? '#3b6ef6' : '#64748b')),
      }),
      filterInput: { width: '100%', boxSizing: 'border-box', font: 'inherit', fontSize: 12, lineHeight: '18px', padding: '6px 10px', borderRadius: 8, border: '.5px solid var(--dsw-alias-border-l4, #d0d7de)', background: 'var(--dsw-alias-bg-layer-1, #fff)', color: 'inherit', marginBottom: 8 },
    };

    const asString = (v, fb) => (typeof v === 'string' ? v : (fb ?? ''));
    const asArray = (v) => (Array.isArray(v) ? v.filter((item) => typeof item === 'string') : []);
    const lines = (text) => String(text ?? '').split('\n').map((l) => l.trim()).filter(Boolean);
    function asNumber(v, fb) {
      const n = typeof v === 'number' ? v : Number.parseInt(String(v ?? ''), 10);
      return Number.isFinite(n) ? n : fb;
    }

    function toDraft(value) {
      const out = {};
      for (const specs of Object.values(FORM_FIELDS)) {
        for (const f of specs) {
          if (f.type === 'lines') out[f.key] = asArray(value[f.prop]).join('\n');
          else if (f.type === 'num') out[f.key] = String(asNumber(value[f.prop], DEFAULTS[f.prop]));
          else out[f.key] = asString(value[f.prop], DEFAULTS[f.prop]);
        }
      }
      return out;
    }

    const Card = (p) => h('section', { style: S.card }, [
      p.title ? h('h3', { key: 't', style: S.cardTitle }, p.title) : null,
      p.hint ? h('p', { key: 'h', style: S.hint }, p.hint) : null,
      p.children,
    ]);
    const Field = (p) => h('label', { style: S.field }, [
      h('span', { key: 'l', style: S.label }, p.label),
      p.children,
      p.hint ? h('span', { key: 'h', style: S.sub }, p.hint) : null,
    ]);
    const Switch = (p) => h('div', { style: S.row }, [
      h('div', { key: 'txt', style: { minWidth: 0 } }, [
        h('div', { key: 'l', style: S.label }, p.label),
        p.hint ? h('div', { key: 'h', style: S.sub }, p.hint) : null,
      ]),
      h('input', {
        key: 'i', type: 'checkbox', checked: p.checked === true, disabled: p.disabled === true,
        onChange: (e) => p.onChange(e.target.checked), 'aria-label': p.label,
      }),
    ]);
    const NumberField = (p) => h(Field, { label: p.label, hint: p.hint }, h('input', {
      type: 'number', style: S.input, value: p.value, min: p.min, step: p.step ?? 1,
      disabled: p.disabled === true, onChange: (e) => p.onChange(e.target.value),
    }));
    const PrimaryButton = (p) => h('button', {
      type: 'button', style: p.primary ? S.btnPrimary : S.btn, disabled: p.disabled === true, onClick: p.onClick,
    }, p.children);

    function ArgoPanel(props) {
      const state = props.useArgoForm((snapshot) => snapshot);
      const value = state && state.value ? state.value : {};
      const ready = state && state.status === 'ready';
      const writable = ready && state.writable !== false;
      const disabled = !writable;

      const locale = props.locale;
      const initialLang = locale && locale.getSnapshot && locale.getSnapshot().active === 'en' ? 'en' : 'zh';
      const [lang, setLang] = useState(initialLang);
      const [tab, setTab] = useState('dashboard'); // hooks[1]：测试按此索引注入页签
      const [draft, setDraft] = useState(() => toDraft(value));
      const [busy, setBusy] = useState('');
      const [message, setMessage] = useState('');
      const [error, setError] = useState(false);
      const [credentials, setCredentials] = useState({});
      const [keyDraft, setKeyDraft] = useState({});
      const [keyBusy, setKeyBusy] = useState('');
      const [showAllKeys, setShowAllKeys] = useState(false);
      // argoEnv 引擎开关：bool 类即时写；num/str/list 类走草稿 + 分组保存
      const [envDraft, setEnvDraft] = useState({});
      const [envBusy, setEnvBusy] = useState('');
      const [engineFilter, setEngineFilter] = useState('');
      const [keyFilter, setKeyFilter] = useState('');
      const [routeHealth, setRouteHealth] = useState(null);

      const dirty = useRef(new Set());
      const credSeq = useRef(0);
      // 读-改-写共享字段（keyRefs / nativeTools / argoEnv）读实时快照，防连点丢更新
      const liveValue = () => {
        const snap = typeof props.useArgoForm?.getSnapshot === 'function' ? props.useArgoForm.getSnapshot() : undefined;
        return snap && snap.value ? snap.value : value;
      };

      const langIdx = lang === 'en' ? 1 : 0;
      const t = (key) => (I18N[key] ? (I18N[key][langIdx] ?? I18N[key][0]) : key);

      useEffect(() => {
        const off = locale && locale.subscribe
          ? locale.subscribe(() => setLang(locale.getSnapshot && locale.getSnapshot().active === 'en' ? 'en' : 'zh'))
          : undefined;
        return typeof off === 'function' ? off : undefined;
      }, [locale]);

      useEffect(() => {
        setDraft((previous) => {
          const next = toDraft(value);
          for (const key of dirty.current) {
            if (Object.prototype.hasOwnProperty.call(previous, key)) next[key] = previous[key];
          }
          return next;
        });
      }, [state]);

      const refreshCredentials = async () => {
        if (!props.credentials || typeof props.credentials.describe !== 'function') return;
        const seq = ++credSeq.current;
        try {
          const res = await props.credentials.describe(KEYS.map((entry) => entry.ref));
          if (seq !== credSeq.current) return;
          if (res && res.ok) setCredentials(res.value || {});
        } catch { /* 凭证服务暂不可用 */ }
      };

      useEffect(() => {
        if (props.view === 'summary') return undefined;
        void refreshCredentials();
        const offs = [];
        if (props.remote && typeof props.remote.$on === 'function') {
          const offCreds = props.remote.$on('credentials/reference-updated', () => { void refreshCredentials(); });
          const offRoute = props.remote.$on('route-health/updated', (data) => { setRouteHealth(data); });
          if (typeof offCreds === 'function') offs.push(offCreds);
          if (typeof offRoute === 'function') offs.push(offRoute);
        }
        return () => { for (const off of offs) off(); };
      }, [state]);

      const edit = (key, text) => { dirty.current.add(key); setDraft((p) => ({ ...p, [key]: text })); };
      const report = (ok, text) => { setError(!ok); setMessage(text); };

      const runSave = async (key, fn) => {
        if (disabled) return false;
        setBusy(key);
        try { return await fn(); }
        catch (cause) { report(false, cause && cause.message ? cause.message : String(cause)); return false; }
        finally { setBusy(''); }
      };

      const write = (ops) => runSave('save', async () => {
        const ok = await props.mutate(ops);
        report(ok, ok ? t('saved') : t('refused'));
        return ok;
      });

      const setField = (field, next) => runSave(field, async () => {
        const ok = await props.set(field, next);
        report(ok, ok ? t('saved') : t('refused'));
        return ok;
      });

      const saveFormGroup = async (groupKey) => {
        const specs = FORM_FIELDS[groupKey];
        const parsed = {};
        const ops = specs.map((f) => {
          let val;
          if (f.type === 'lines') {
            const arr = lines(draft[f.key]);
            val = f.fallbackNonEmpty && !arr.length ? DEFAULTS[f.prop] : arr;
          } else if (f.type === 'num') {
            const minVal = f.minDraft ? Math.max(f.min, parsed[f.minDraft]) : f.min;
            val = Math.max(minVal, asNumber(draft[f.key], DEFAULTS[f.prop]));
            if (f.max !== undefined) val = Math.min(f.max, val);
          } else {
            const trimmed = draft[f.key].trim();
            val = f.allowEmpty ? trimmed : (trimmed || DEFAULTS[f.prop]);
          }
          parsed[f.key] = val;
          return { op: 'set', path: [f.prop], value: val };
        });
        const ok = await write(ops);
        if (ok) for (const f of specs) dirty.current.delete(f.key);
      };

      const toggleTool = (id) => {
        const current = new Set(asArray(liveValue().nativeTools));
        if (current.has(id)) current.delete(id);
        else current.add(id);
        const ordered = TOOLS.map((tool) => tool.id).filter((tid) => current.has(tid));
        for (const extra of current) if (!ordered.includes(extra)) ordered.push(extra);
        void setField('nativeTools', ordered);
      };

      const applyToolPreset = async (tools) => {
        const ordered = TOOLS.map((tool) => tool.id).filter((tid) => tools.includes(tid));
        for (const extra of tools) if (!ordered.includes(extra)) ordered.push(extra);
        await props.set('nativeTools', ordered);
      };

      const mutateKey = async (ref, action) => {
        setKeyBusy(ref);
        try { await action(); await refreshCredentials(); }
        catch (cause) { report(false, cause && cause.message ? cause.message : String(cause)); }
        finally { setKeyBusy(''); }
      };

      const saveKey = (ref) => {
        const key = asString(keyDraft[ref]).trim();
        if (!key || !props.credentials) return;
        return mutateKey(ref, async () => {
          const res = await props.credentials.set(ref, key);
          if (!res || !res.ok) throw new Error(res && res.error ? res.error.message : t('keyUnavailable'));
          setKeyDraft((p) => ({ ...p, [ref]: '' }));
          const refs = asArray(liveValue().keyRefs);
          if (!refs.includes(ref)) await props.set('keyRefs', [...refs, ref]);
          report(true, t('keySaved'));
        });
      };

      const clearKey = (ref) => {
        if (!props.credentials) return;
        return mutateKey(ref, async () => {
          const res = await props.credentials.unset(ref);
          if (!res || !res.ok) throw new Error(res && res.error ? res.error.message : t('keyUnavailable'));
          report(true, t('keyCleared'));
        });
      };

      const setForward = (ref, on) => {
        const refs = new Set(asArray(liveValue().keyRefs));
        if (on) refs.add(ref);
        else refs.delete(ref);
        void setField('keyRefs', [...refs]);
      };

      // argoEnv 单一真源：写入时过滤白名单外脏键，避免 apply 期 loud fail 卡死
      const envMap = () => {
        const map = {};
        for (const entry of asArray(liveValue().argoEnv)) {
          const eq = entry.indexOf('=');
          if (eq > 0) map[entry.slice(0, eq)] = entry.slice(eq + 1);
        }
        return map;
      };
      const ENV_FALSY = ['', '0', 'false', 'no', 'off', 'n', 'disable', 'disabled', 'none'];
      const envBoolState = (spec, map) => (
        map[spec.name] === undefined
          ? spec.def === true
          : !ENV_FALSY.includes(String(map[spec.name]).trim().toLowerCase())
      );
      const writeEnvLines = async (updates) => {
        const map = envMap();
        for (const name of Object.keys(updates)) {
          const val = updates[name];
          if (val === null || val === undefined || val === '') delete map[name];
          else map[name] = val;
        }
        return props.set('argoEnv', Object.keys(map).filter((n) => ENV_NAMES.has(n)).map((n) => `${n}=${map[n]}`));
      };
      const toggleEnv = (spec, on) => {
        if (disabled) return;
        void writeEnvLines({ [spec.name]: on ? '1' : '0' })
          .then((ok) => report(ok, ok ? t('saved') : t('refused')))
          .catch((cause) => report(false, cause && cause.message ? cause.message : String(cause)));
      };
      const saveEnvGroup = async (group) => {
        if (disabled) return;
        const updates = {};
        for (const spec of ENGINE_TOGGLES) {
          if (spec.group !== group || spec.kind === 'bool' || envDraft[spec.name] === undefined) continue;
          const text = String(envDraft[spec.name]).trim();
          if (text === '') { updates[spec.name] = null; continue; }
          if (spec.kind === 'num') {
            const n = Number(text);
            if (!Number.isFinite(n) || n < spec.min || n > spec.max) {
              report(false, `${lang === 'en' ? spec.en : spec.zh}: ${t('envRange').replace('{min}', String(spec.min)).replace('{max}', String(spec.max))}`);
              return;
            }
          }
          updates[spec.name] = text;
        }
        if (Object.keys(updates).length === 0) return;
        setEnvBusy(group);
        try {
          const ok = await writeEnvLines(updates);
          report(ok, ok ? t('saved') : t('refused'));
          if (ok) setEnvDraft({});
        } catch (cause) { report(false, cause && cause.message ? cause.message : String(cause)); }
        finally { setEnvBusy(''); }
      };

      const applyPreset = (preset) => runSave('preset', async () => {
        const map = { ...envMap(), ...preset.env };
        const ops = Object.entries(preset.fields).map(([path, val]) => ({ op: 'set', path: [path], value: val }));
        const envLines = Object.keys(map).filter((n) => ENV_NAMES.has(n)).map((n) => `${n}=${map[n]}`);
        ops.push({ op: 'set', path: ['argoEnv'], value: envLines });
        const ok = await props.mutate(ops);
        report(ok, ok ? `${t('presetApplied')} ${t(preset.titleKey)}` : t('refused'));
        return ok;
      });

      if (props.view === 'summary') {
        return t('summary')
          .replace('{search}', value.searchProviderEnabled !== false ? t('on') : t('off'))
          .replace('{fetch}', value.fetchProviderEnabled !== false ? t('on') : t('off'))
          .replace('{tools}', String(asArray(value.nativeTools).length))
          .replace('{workers}', String(asNumber(value.defaultWorkers, DEFAULTS.defaultWorkers)))
          .replace('{max}', String(asNumber(value.maxWorkers, DEFAULTS.maxWorkers)));
      }
      if (!ready) {
        return h('div', { style: S.wrap }, [
          h('h2', { key: 't', style: S.title }, t('title')),
          h('p', { key: 'p', style: S.hint }, state && state.status === 'unavailable' ? t('unavailable') : t('loading')),
        ]);
      }

      const renderField = (f) => {
        if (f.type === 'num') {
          return h(NumberField, {
            key: f.key, label: t(f.label), min: f.min, step: f.step,
            value: draft[f.key], disabled, onChange: (next) => edit(f.key, next),
          });
        }
        const isLines = f.type === 'lines';
        return h(Field, { key: f.key, label: t(f.label), hint: f.hint ? t(f.hint) : undefined }, h(isLines ? 'textarea' : 'input', {
          type: isLines ? undefined : 'text',
          style: isLines ? { ...S.input, ...S.textarea, ...S.mono } : { ...S.input, ...S.mono },
          value: draft[f.key], disabled, spellCheck: false,
          onChange: (e) => edit(f.key, e.target.value),
        }));
      };
      const renderGroup = (groupKey) => {
        const specs = FORM_FIELDS[groupKey];
        const full = specs.filter((f) => f.layout === 'full').map(renderField);
        const grid = specs.filter((f) => f.layout !== 'full').map(renderField);
        return [
          ...full,
          grid.length ? h('div', { key: 'g', style: S.grid }, grid) : null,
        ];
      };
      const saveBtn = (groupKey) => h('div', { key: 'save', style: S.actions }, [
        h(PrimaryButton, {
          primary: true, disabled: disabled || busy === 'save',
          onClick: () => { void saveFormGroup(groupKey); },
        }, busy === 'save' ? t('saving') : t('save')),
      ]);

      const keyRow = (entry) => {
        const info = credentials[entry.ref];
        const configured = info && info.configured === true;
        const rowWritable = !info || info.writable !== false;
        const sourceLabel = configured && typeof info.source === 'string'
          ? t('keySource' + info.source.split('-').map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(''))
          : '';
        const statusParts = [configured ? t('configured') : t('notConfigured')];
        if (sourceLabel && sourceLabel !== 'keySource' + info.source) statusParts.push(sourceLabel);
        if (configured && !rowWritable) statusParts.push(t('keyReadonly'));
        if (configured && rowWritable && info.source !== undefined && info.source !== 'file') statusParts.push(t('keyOverrideHint'));
        return h('div', { key: entry.ref, style: S.keyRow }, [
          h('div', { key: 'head', style: S.keyHead }, [
            h('span', { key: 'n', style: S.label }, [h('span', { key: 'dot', style: S.dot(configured) }), lang === 'en' ? (entry.en || entry.label) : entry.label]),
            h('span', { key: 'ref', style: S.code }, entry.ref),
            h('span', { key: 's', style: S.sub }, statusParts.join(' · ')),
            entry.ref === 'DEEPSEEK_API_KEY' ? h('span', { key: 'shared', style: S.sub }, t('deepseekShared')) : null,
          ]),
          h('div', { key: 'body', style: col(6) }, [
            h('input', {
              key: 'i', type: 'password', autoComplete: 'off', spellCheck: false, style: S.input,
              value: asString(keyDraft[entry.ref]), placeholder: t(configured ? 'keyPlaceholderSet' : 'keyPlaceholderEmpty'),
              disabled: disabled || !props.credentials || !rowWritable,
              onChange: (e) => setKeyDraft((p) => ({ ...p, [entry.ref]: e.target.value })),
            }),
            h('div', { key: 'a', style: S.actions }, [
              h('label', { key: 'f', style: S.check }, [
                h('input', { key: 'i', type: 'checkbox', checked: asArray(value.keyRefs).includes(entry.ref), disabled, onChange: (e) => setForward(entry.ref, e.target.checked) }),
                h('span', { key: 't' }, t('forward')),
              ]),
              h('span', { key: 'sp', style: { flex: 1 } }),
              h('button', { key: 's', type: 'button', style: S.btn, disabled: disabled || !rowWritable || keyBusy === entry.ref || asString(keyDraft[entry.ref]).trim() === '', onClick: () => { void saveKey(entry.ref); } }, t(keyBusy === entry.ref ? 'saving' : 'saveKey')),
              configured ? h('button', { key: 'c', type: 'button', style: S.btn, disabled: disabled || !rowWritable || keyBusy === entry.ref, onClick: () => { void clearKey(entry.ref); } }, t('clearKey')) : null,
            ]),
          ]),
        ]);
      };

      const panel = [];
      if (tab === 'dashboard') {
        const configuredKeys = Object.values(credentials).filter((c) => c && c.configured).length;
        const searchOn = value.searchProviderEnabled !== false;
        const fetchOn = value.fetchProviderEnabled !== false;
        const poolConc = asNumber(value.searchConcurrency, DEFAULTS.searchConcurrency);
        const metrics = [
          { k: 'pool', label: t('poolTitle'), val: `${poolConc} 并发`, dot: poolConc > 0, sub: `${t('poolStatusHealthy')} · 空闲回收 ${asNumber(value.searchIdleMs, DEFAULTS.searchIdleMs) / 1000}s` },
          { k: 'seam', label: t('accessTitle'), val: searchOn && fetchOn ? '双路接管' : (searchOn || fetchOn ? '单路接管' : '未接管'), sub: `Search: ${searchOn ? 'ON' : 'OFF'} · Fetch: ${fetchOn ? 'ON' : 'OFF'}` },
          { k: 'tools', label: t('toolsTitle'), val: `${asArray(value.nativeTools).length} / ${TOOLS.length}`, sub: '已就绪原生 MCP 工具' },
          { k: 'keys', label: t('keysTitle'), val: `${configuredKeys} 配置 · ${CORE_ENGINES.filter((e) => e.keyless).length} 免密`, sub: '覆盖全局多源引擎' },
        ];
        panel.push(h('div', { key: 'metrics', style: S.metricGrid }, metrics.map((m) => h('div', { key: m.k, style: S.metricCard }, [
          h('span', { key: 'l', style: S.metricLabel }, m.label),
          m.dot !== undefined
            ? h('div', { key: 'v', style: { display: 'flex', alignItems: 'center', gap: 6 } }, [h('span', { key: 'd', style: S.dot(m.dot) }), h('span', { key: 'val', style: S.metricValue }, m.val)])
            : h('span', { key: 'val', style: S.metricValue }, m.val),
          h('span', { key: 's', style: S.metricSub }, m.sub),
        ]))));
        const rhSummary = routeHealth && routeHealth.summary;
        const topLangs = rhSummary && rhSummary.langCounts
          ? Object.entries(rhSummary.langCounts).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, n]) => `${k}:${n}`).join(' · ')
          : '';
        const topDomains = rhSummary && Array.isArray(rhSummary.topDomains)
          ? rhSummary.topDomains.slice(0, 3).map((d) => `${d.name}:${d.count}`).join(' · ')
          : '';
        panel.push(h(Card, { key: 'route', title: t('routeHealthTitle'), hint: t('routeHealthHint') }, [
          routeHealth && Array.isArray(routeHealth.samples) && routeHealth.samples.length > 0
            ? h('div', { key: 'rows', style: col(6) }, [
                rhSummary ? h('div', { key: 'sum', style: S.sub }, `${rhSummary.total} 条 · 兜底 ${Math.round((rhSummary.fallbackRate || 0) * 100)}%${topLangs ? ' · ' + topLangs : ''}`) : null,
                topDomains ? h('div', { key: 'doms', style: S.sub }, `高频域：${topDomains}`) : null,
                ...routeHealth.samples.slice(-5).reverse().map((r, i) => {
                  const langTag = r.effective_lang || r.primary_lang || '-';
                  const intentTag = Array.isArray(r.intents) && r.intents.length > 0 ? ` · ${r.intents.join(',')}` : '';
                  const confTag = r.confidence !== '' && r.confidence !== undefined ? ` (${r.confidence})` : '';
                  return h('div', { key: 'r' + i, style: S.keyRow }, [
                    h('span', { key: 'q', style: S.label }, `${r.domain || 'fallback'} · ${langTag}${intentTag}`),
                    h('span', { key: 'd', style: S.sub }, `${r.engine || '-'}${confTag}`),
                  ]);
                }),
              ])
            : h('p', { key: 'hint', style: S.hint }, t('routeHealthFallback')),
        ]));
        panel.push(h(Card, { key: 'presets', title: t('presetTitle'), hint: t('presetHint') }, [
          h('div', { key: 'grid', style: S.presetGrid }, PRESETS.map((p) => h('div', { key: p.id, style: S.presetCard(p.active), onClick: () => { void applyPreset(p); } }, [
            h('div', { key: 't', style: rowBetween() }, [h('span', { key: 'title', style: S.label }, t(p.titleKey)), h('span', { key: 'badge', style: S.badge(p.badgeColor) }, p.badge)]),
            h('span', { key: 'd', style: S.sub }, t(p.descKey)),
          ]))),
        ]));
        panel.push(h(Card, { key: 'matrix', title: t('matrixTitle'), hint: t('matrixHint') }, [
          h('div', { key: 'grid', style: S.matrixGrid }, CORE_ENGINES.map((engine) => {
            const hasKey = engine.ref && credentials[engine.ref] && credentials[engine.ref].configured;
            return h('div', { key: engine.id, style: S.matrixCard }, [
              h('div', { key: 'h', style: rowBetween(6) }, [
                h('span', { key: 'n', style: S.label }, lang === 'en' ? engine.nameEn : engine.nameZh),
                h('span', { key: 'b', style: S.badge(hasKey ? 'green' : (engine.keyless ? 'blue' : 'gray')) }, t(hasKey ? 'matrixConfigured' : (engine.keyless ? 'matrixKeyless' : 'matrixPending'))),
              ]),
              h('span', { key: 'r', style: S.sub }, lang === 'en' ? engine.roleEn : engine.roleZh),
            ]);
          })),
        ]));
      }

      if (tab === 'access') {
        panel.push(h(Card, { key: 'seam', title: t('accessTitle') }, [
          h(Switch, { key: 's', checked: value.searchProviderEnabled !== false, disabled, label: t('searchSeam'), hint: t('searchSeamHint'), onChange: (n) => { void setField('searchProviderEnabled', n); } }),
          h(Switch, { key: 'f', checked: value.fetchProviderEnabled !== false, disabled, label: t('fetchSeam'), hint: t('fetchSeamHint'), onChange: (n) => { void setField('fetchProviderEnabled', n); } }),
        ]));
        panel.push(h(Card, { key: 'entry', title: t('entryTitle'), hint: t('entryHint') }, [...renderGroup('access'), saveBtn('access')]));
      }

      if (tab === 'tools') {
        panel.push(h(Card, { key: 'presets', title: t('toolPresetTitle'), hint: t('toolPresetHint') }, [
          h('div', { key: 'grid', style: S.presetGrid }, TOOL_PRESETS.map((pr) =>
            h('button', { key: pr.id, type: 'button', disabled, style: S.presetCard(false), onClick: () => { void applyToolPreset(pr.tools); } }, [
              h('div', { key: 't', style: rowBetween() }, [h('span', { key: 'title', style: S.label }, lang === 'en' ? pr.en : pr.zh), h('span', { key: 'badge', style: S.badge('blue') }, `${pr.tools.length} 工具`)]),
              h('span', { key: 'd', style: S.sub }, lang === 'en' ? pr.descEn : pr.descZh),
            ]))),
        ]));
        panel.push(h(Card, { key: 'tools', title: t('toolsTitle'), hint: t('toolsHint') }, [
          ...TOOL_GROUPS.map((grp, gi) => [
            gi > 0 ? h('div', { key: 'div' + gi, style: S.divider }) : null,
            h('div', { key: grp.id, style: col(4) }, [
              h('span', { key: 'l', style: S.label }, lang === 'en' ? grp.en : grp.zh),
              h('div', { key: 'g', style: S.toolGrid }, TOOLS.filter((tl) => tl.group === grp.id).map((tl) => h('label', { key: tl.id, style: S.check }, [
                h('input', { key: 'i', type: 'checkbox', checked: asArray(value.nativeTools).includes(tl.id), disabled, onChange: () => toggleTool(tl.id) }),
                h('span', { key: 't' }, [h('span', { key: 'n', style: S.mono }, tl.id), h('span', { key: 'd', style: S.sub }, '  ' + (lang === 'en' ? tl.en : tl.zh))]),
              ]))),
            ]),
          ]),
          ...renderGroup('tools'),
          saveBtn('tools'),
        ]));
      }

      if (tab === 'engines') {
        const map = envMap();
        // 固定分组顺序防白名单增删导致面板乱序（test/client.test.js 校验每个开关都有控件）
        const groups = ['cache', 'fetch', 'semantic', 'engines', 'net', 'local', 'paid', 'misc'];
        const groupTitle = (g) => t('group' + g.charAt(0).toUpperCase() + g.slice(1));
        panel.push(h('p', { key: 'hint', style: S.hint }, t('enginesHint')));
        panel.push(h('input', { key: 'filter', type: 'text', style: S.filterInput, placeholder: t('searchFilterPlaceholder'), value: engineFilter, onChange: (e) => setEngineFilter(e.target.value) }));
        const query = engineFilter.trim().toLowerCase();
        for (const group of groups) {
          let specs = ENGINE_TOGGLES.filter((s) => s.group === group);
          if (query && !group.toLowerCase().includes(query) && !groupTitle(group).toLowerCase().includes(query)) {
            specs = specs.filter((s) => [s.name, s.zh, s.en, s.hintZh, s.hintEn].some((v) => (v || '').toLowerCase().includes(query)));
          }
          if (!specs.length) continue;
          const bools = specs.filter((s) => s.kind === 'bool');
          const fields = specs.filter((s) => s.kind !== 'bool');
          const children = group === 'paid' ? [h('p', { key: 'groupHint', style: S.hint }, t('groupPaidHint'))] : [];
          for (const spec of bools) {
            const suffix = map[spec.name] !== undefined ? '' : (lang === 'en' ? ` (${t('envDefault')})` : `（${t('envDefault')}）`);
            children.push(h(Switch, { key: spec.name, checked: envBoolState(spec, map), disabled, label: (lang === 'en' ? spec.en : spec.zh) + suffix, hint: lang === 'en' ? spec.hintEn : spec.hintZh, onChange: (next) => toggleEnv(spec, next) }));
          }
          if (fields.length) {
            children.push(h('div', { key: 'fields', style: S.grid }, fields.map((spec) => {
              const cur = envDraft[spec.name] !== undefined ? envDraft[spec.name] : (map[spec.name] !== undefined ? map[spec.name] : (spec.kind === 'num' ? String(spec.def) : ''));
              const isNum = spec.kind === 'num';
              return h(Field, { key: spec.name, label: lang === 'en' ? spec.en : spec.zh, hint: lang === 'en' ? spec.hintEn : spec.hintZh }, h('input', {
                key: 'i', type: isNum ? 'number' : 'text', style: isNum ? S.input : { ...S.input, ...S.mono },
                value: cur, min: isNum ? spec.min : undefined, max: isNum ? spec.max : undefined, step: isNum ? (spec.step ?? 1) : undefined,
                disabled, spellCheck: isNum ? undefined : false, onChange: (e) => setEnvDraft((p) => ({ ...p, [spec.name]: e.target.value })),
              }));
            })));
            children.push(h('div', { key: 'save', style: S.actions }, [h(PrimaryButton, { key: 'b', primary: true, disabled: disabled || envBusy === group, onClick: () => { void saveEnvGroup(group); } }, t(envBusy === group ? 'saving' : 'save'))]));
          }
          panel.push(h(Card, { key: group, title: groupTitle(group) }, children));
        }
      }

      if (tab === 'keys') {
        const q = keyFilter.trim().toLowerCase();
        const list = q
          ? KEYS.filter((e) => [e.ref, e.label, e.en, ...e.engines].some((v) => (v || '').toLowerCase().includes(q)))
          : (showAllKeys ? KEYS : KEYS.filter((e) => e.common));
        panel.push(h(Card, { key: 'keys', title: t('keysTitle'), hint: t('keysHint') }, [
          h('input', { key: 'filter', type: 'text', style: S.filterInput, placeholder: t('searchFilterPlaceholder'), value: keyFilter, onChange: (e) => setKeyFilter(e.target.value) }),
          h('span', { key: 'l', style: S.label }, t(q ? 'keysTitle' : 'commonKeys')),
          ...list.map(keyRow),
          q ? null : h('div', { key: 'a', style: S.actions }, [
            h('button', { key: 'more', type: 'button', style: S.btn, onClick: () => setShowAllKeys((p) => !p) }, t(showAllKeys ? 'showLess' : 'showMore')),
            props.credentials ? null : h('span', { key: 'u', style: S.sub }, t('keyUnavailable')),
          ]),
        ]));
      }

      if (tab === 'research') panel.push(h(Card, { key: 'research', title: t('researchTitle') }, [...renderGroup('research'), saveBtn('research')]));
      if (tab === 'advanced') panel.push(h(Card, { key: 'adv', title: t('advancedTitle') }, [
        ...renderGroup('advanced'), saveBtn('advanced'), h('div', { key: 'div', style: S.divider }),
        h('div', { key: 'struct', style: col(4) }, [h('span', { key: 'l', style: S.label }, t('structural')), h('span', { key: 'h', style: S.sub }, t('structuralHint')), h('span', { key: 'm', style: S.sub }, t('mcpHint'))]),
      ]));

      const tabs = [['dashboard', 'tabDashboard'], ['access', 'tabAccess'], ['tools', 'tabTools'], ['engines', 'tabEngines'], ['keys', 'tabKeys'], ['research', 'tabResearch'], ['advanced', 'tabAdvanced']];
      return h('div', { style: S.wrap }, [
        h('div', { key: 'head', style: col(4) }, [
          h('h2', { key: 't', style: S.title }, t('title')),
          h('p', { key: 'i', style: S.hint }, t('intro')),
        ]),
        h('div', { key: 'tabs', style: S.tabs }, tabs.map(([id, lk]) => h('button', {
          key: id, type: 'button', style: S.tab(tab === id), onClick: () => setTab(id),
        }, t(lk)))),
        message ? h('p', { style: error ? S.error : S.notice, role: 'status' }, message) : h('p', { style: S.status }, disabled ? t('readonly') : ''),
        ...panel,
      ]);
    }

    const inject = ['slots', 'locale', 'configForms', 'remote', 'remote.credentials'];

    function apply(ctx) {
      const form = ctx.configForms.get(NS);
      const locale = ctx.locale ?? (typeof ctx.get === 'function' ? ctx.get('locale') : undefined);
      const credentials = ctx.remote ? ctx.remote.credentials : undefined;
      const label = () => 'Argo 搜索';

      const injected = () => ({
        hooks: { argoForm: form },
        set: (field, next) => form.set(field, next),
        mutate: (ops) => form.mutate(ops),
        credentials,
        remote: ctx.remote,
        locale,
      });

      ctx.effect(() => ctx.configForms.whileServed([NS], () => {
        const offs = [
          ctx.slots.inject('settings.section', () => ctx.slots.register({
            name: 'settings.section',
            id: SECTION_ID,
            order: 26,
            label,
            inject: injected,
          }, ArgoPanel)),
          ctx.slots.inject('plugins.item', () => ctx.slots.register({
            name: 'plugins.item',
            id: SECTION_ID,
            order: 50,
            label,
            inject: injected,
          }, ArgoPanel)),
        ];
        return () => {
          for (const off of offs) if (typeof off === 'function') off();
        };
      }), 'argo-dsh: settings page');

      if (typeof window !== 'undefined' && typeof document !== 'undefined' && ctx.slots) {
        ctx.effect(() => {
          let disposed = false;
          let unmount = null;
          import('/argo/workbench-client.js').then((mod) => {
            if (!disposed && typeof mod?.mountArgoWorkbench === 'function') unmount = mod.mountArgoWorkbench(ctx, react);
          }).catch(() => {});
          return () => { disposed = true; if (typeof unmount === 'function') unmount(); };
        }, 'argo-dsh: workbench');
      }
    }

    exports.apply = apply;
    exports.inject = inject;
    exports.ArgoPanel = ArgoPanel;
    // 静态数据导出供漂移门禁核对：TOOLS 必须与 native-tools.mjs（mcp_tools.py
    // 生成产物）同名同集合，ENGINE_TOGGLES 必须与 specs.js 的 ARGO_ENV_SPECS
    // 同名同集合——两边任何一边增删而另一边没跟上，测试即红。
    exports.TOOLS = TOOLS;
    exports.TOOL_GROUPS = TOOL_GROUPS;
    exports.ENGINE_TOGGLES = ENGINE_TOGGLES;
    exports.KEYS = KEYS;

    return module.exports;
  }
});
