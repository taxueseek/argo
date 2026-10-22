/*
 * @taxueseek/argo-dsh — 浏览器端设置页（无需构建步骤）
 *
 * 由 DSH ModuleLoader 以 lazy-CJS 方式加载：工厂函数里只 require('react')，
 * 其余能力（slots / configForms / remote.credentials / locale）全部由宿主
 * 通过 cordis 服务注入。设置值走 configForms（settings namespace =
 * 'wide-research'），密钥走 credentials 域——两者都不经过插件配置文件，
 * 密钥更不会回显明文。
 *
 * 注册两个位置：
 *   settings.section —— 设置弹窗左侧一级菜单（主入口）
 *   plugins.item     —— 设置 > 插件 页里的卡片（摘要 + 同一面板）
 */
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

    const DICT = {
      zh: {
        title: 'Argo 搜索配置',
        intro: '接入方式、原生工具、API 密钥与研究编排。保存后即时生效；工具名与 provider 属结构性字段，仍需在 profile patch 中修改。',
        loading: '配置加载中…',
        unavailable: '本插件的配置服务不可用（未挂载或只读）。',
        readonly: '当前配置只读。',
        saved: '已保存',
        refused: '宿主拒绝写入（可能只读或字段不可热更新）',
        save: '保存',
        saving: '保存中…',
        on: '开',
        off: '关',
        showMore: '显示更多',
        showLess: '收起',
        tabAccess: '接入',
        tabTools: '工具',
        tabKeys: '密钥',
        tabResearch: '研究',
        tabAdvanced: '高级',
        tabDashboard: '看板',
        dashboardTitle: '运行监控与健康态看板',
        dashboardHint: '实时监控常驻 MCP 连接池、上游搜索源拓扑与额度状态，提供一键预设与快速调优。',
        poolTitle: '常驻 MCP 连接池监控',
        poolStatusHealthy: '就绪（正常）',
        presetTitle: '工作流快速预设',
        presetHint: '一键对齐常用工作场景的并发与引擎策略，免去逐项手动调优。',
        presetFast: '极速日常',
        presetFastDesc: '免密免费源优先，关闭 TF-IDF 重排与数值交叉比对，超低延迟',
        presetStandard: '平衡生产（推荐）',
        presetStandardDesc: '开启 TF-IDF 重排、数值交叉核验、主流付费源，标准超时',
        presetDeep: '深度调研',
        presetDeepDesc: '激活更多取证工具、对冲自适应宽限、语义证据层、更大并发',
        presetApplied: '已应用预设：',
        matrixTitle: '核心引擎拓扑与覆盖全景',
        matrixHint: '当前可用引擎的接入层级与角色分工。带免密标识的引擎无需密钥即可直接使用。',
        matrixConfigured: '密钥已就绪',
        matrixKeyless: '免密层可用',
        matrixPending: '待配密钥',
        searchFilterPlaceholder: '输入关键词快速筛选...',
        summary: 'Argo · web_search {search} · web_fetch {fetch} · 原生工具 {tools} 个 · 研究 {workers}/{max}',
        accessTitle: '接入方式',
        searchSeam: '宿主 web_search 走 argo',
        searchSeamHint: '关闭后 web_search 回到宿主默认搜索后端（web 行 searchProvider）。',
        fetchSeam: '宿主 web_fetch 走 argo',
        fetchSeamHint: '开启前必须在 web 配置行加 fetchProvider: argo；否则两个 provider 同时可用会报 WEB_PROVIDER_AMBIGUOUS。',
        entryTitle: '引擎入口',
        entryHint: 'npx 模式填 npx，参数 -y / github:taxueseek/argo；本机模式填 python3，参数填 mcp_server.py 的绝对路径。',
        command: '命令',
        args: '参数（一行一个）',
        searchTimeout: '搜索超时 (ms)',
        fetchTimeout: '抓取超时 (ms)',
        fetchChars: '抓取正文上限（字符）',
        idle: '连接空闲回收 (ms)',
        concurrency: '并发连接数',
        toolsTitle: '原生工具',
        toolsHint: '勾选即注册为原生一等工具（CLI 单发，不依赖 MCP）。长尾工具只在需要时开，避免每轮工具面膨胀。',
        coreTools: '高频路径',
        longTools: '长尾取证',
        nativeTimeout: '单次调用超时 (ms)',
        nativeChars: '返回文本上限（字符）',
        keysTitle: 'API 密钥',
        keysHint: '密钥写入 DSH 凭证库（与 Models 页同一处），不会写进插件配置、不回显明文。只有勾选「转发」的引用才会作为环境变量传给 argo 子进程。「清除」只删凭证库里的值，环境变量或 .env 提供的 key 仍会生效。',
        commonKeys: '常用引擎',
        moreKeys: '更多引擎',
        configured: '已配置',
        notConfigured: '未配置',
        forward: '转发',
        keyPlaceholderSet: '输入新值（留空保留现有密钥）',
        keyPlaceholderEmpty: '粘贴 API Key',
        saveKey: '保存',
        clearKey: '清除',
        keySaved: '密钥已保存；下一次新建 argo 连接生效（池内旧连接最长 60s 回收）。',
        keyCleared: '密钥已清除。',
        keyUnavailable: '凭证服务不可用。',
        researchTitle: '研究编排',
        workers: '默认 worker 数',
        maxWorkers: 'worker 上限',
        tracks: '轨道上限',
        sources: '每轨信源上限',
        workerTokens: 'worker token 上限',
        synthTokens: '综合 token 上限',
        reportDir: '报告落盘目录',
        advancedTitle: '高级',
        allow: 'worker 工具白名单（一行一个）',
        allowHint: '留空表示使用内置默认（MCP 与原生工具双写）。wide_research 自身与 argo_research 会被硬排除。',
        deny: 'worker 工具拒绝名单（一行一个）',
        keyRefs: '凭证转发名单（一行一个）',
        keyRefsHint: '这些引用名会经 DSH credentials 解析后作为环境变量传给 argo 子进程；没有对应凭证的项会被跳过。',
        structural: '结构性字段',
        structuralHint: 'toolName / provider / searchProviderId / fetchProviderId 只能改 profile patch，改后重启 DSH 生效。',
        mcpHint: 'MCP 全量工具面（mcp__argo__*）是 profile 里的独立 mcp-argo 行，不在本页开关；需要时在 patch 中取消注释并重启。',
        noKey: '（未配置）',
        tabEngines: '引擎',
        enginesTitle: 'argo 引擎开关',
        enginesHint: '这些开关原先是 argo 的环境变量，只能改 ~/.config/argo/env 或启动环境；现在写进插件配置，随 argo 子进程生效。改动对下一次新建连接生效（池内旧连接按「连接空闲回收」时长退出，默认 60s）。密钥不在这里配，请用「密钥」页；本页也不收授权类开关（可复算执行、内网 URL）。',
        groupCache: '缓存与排序',
        groupFetch: '抓取层',
        groupSemantic: '语义与自适应',
        groupEngines: '引擎启停',
        groupNet: '网络出口',
        groupLocal: '本机检索',
        groupMisc: '其他',
        groupPaid: '额度与预算',
        groupPaidHint: '免费与付费源统一管理：金额计费的源设月度预算，按次数限额的源设日/月次数上限；0=不限制。额度或预算用尽后该源自动跳过并回落免费源。改动对下一次新建连接生效。',
        envDefault: '默认',
        envRange: '需在 {min}–{max} 之间',
        keySourceFile: '凭证库',
        keySourceEnv: '启动环境',
        keySourceProjectEnv: '项目 .env',
        keySourceUserEnv: '家目录 .env',
        keySourceOther: '其他层',
        keyReadonly: '只读',
        keyOverrideHint: '保存后将覆盖当前层',
        deepseekShared: '与宿主 web_search 官方后端共用同一把 key，改动同时影响两边',
      },
      en: {
        title: 'Argo Search Settings',
        intro: 'Access mode, native tools, API keys and research orchestration. Saves apply immediately; tool name and provider remain structural profile settings.',
        loading: 'Loading configuration…',
        unavailable: 'Configuration service unavailable (not mounted or read-only).',
        readonly: 'Configuration is read-only.',
        saved: 'Saved',
        refused: 'Host refused the write (read-only or not live-editable)',
        save: 'Save',
        saving: 'Saving…',
        on: 'on',
        off: 'off',
        showMore: 'Show more',
        showLess: 'Show less',
        tabAccess: 'Access',
        tabTools: 'Tools',
        tabKeys: 'Keys',
        tabResearch: 'Research',
        tabAdvanced: 'Advanced',
        tabDashboard: 'Dashboard',
        dashboardTitle: 'Runtime Monitoring & Health Dashboard',
        dashboardHint: 'Real-time overview of the resident MCP connection pool, upstream search engine matrix, and quick workflow presets.',
        poolTitle: 'Resident MCP Pool Monitoring',
        poolStatusHealthy: 'Ready (Healthy)',
        presetTitle: 'Workflow Presets',
        presetHint: 'One-click presets tailored for common research and search workflows.',
        presetFast: 'Fast & Free',
        presetFastDesc: 'Keyless & free sources, disable rerank & crosscheck for lowest latency',
        presetStandard: 'Balanced (Recommended)',
        presetStandardDesc: 'TF-IDF rerank, numeric crosscheck, mainstream paid sources, standard timeouts',
        presetDeep: 'Deep Research',
        presetDeepDesc: 'Full forensic tools, adaptive hedge grace, semantic evidence layer, max concurrency',
        presetApplied: 'Preset applied:',
        matrixTitle: 'Core Engine Topology & Coverage',
        matrixHint: 'Access tiers and roles of available engines. Keyless engines work without an API key.',
        matrixConfigured: 'Key Ready',
        matrixKeyless: 'Keyless Ready',
        matrixPending: 'Key Required',
        searchFilterPlaceholder: 'Filter by keyword or engine...',
        summary: 'Argo · web_search {search} · web_fetch {fetch} · {tools} native tools · research {workers}/{max}',
        accessTitle: 'Access',
        searchSeam: 'Route host web_search to argo',
        searchSeamHint: 'When off, web_search falls back to the host provider configured on the web row.',
        fetchSeam: 'Route host web_fetch to argo',
        fetchSeamHint: 'Requires fetchProvider: argo on the web row; otherwise two available providers trigger WEB_PROVIDER_AMBIGUOUS.',
        entryTitle: 'Engine entry',
        entryHint: 'npx mode: command npx, args -y / github:taxueseek/argo. Local mode: python3 plus the absolute mcp_server.py path.',
        command: 'Command',
        args: 'Args (one per line)',
        searchTimeout: 'Search timeout (ms)',
        fetchTimeout: 'Fetch timeout (ms)',
        fetchChars: 'Fetch body cap (chars)',
        idle: 'Idle recycle (ms)',
        concurrency: 'Concurrent connections',
        toolsTitle: 'Native tools',
        toolsHint: 'Checked tools register as native first-class tools (single-shot CLI, no MCP). Enable long-tail tools only when needed.',
        coreTools: 'Hot path',
        longTools: 'Long tail',
        nativeTimeout: 'Per-call timeout (ms)',
        nativeChars: 'Returned text cap (chars)',
        keysTitle: 'API keys',
        keysHint: 'Keys are stored in the DSH credential store (same place as the Models page), never in plugin config and never echoed. Only refs marked Forward reach the argo child process. Clear removes the stored value only; a key supplied by the environment or .env still applies.',
        commonKeys: 'Common engines',
        moreKeys: 'More engines',
        configured: 'Configured',
        notConfigured: 'Not configured',
        forward: 'Forward',
        keyPlaceholderSet: 'Enter a new value (leave blank to keep the current key)',
        keyPlaceholderEmpty: 'Paste API key',
        saveKey: 'Save',
        clearKey: 'Clear',
        keySaved: 'Key saved; effective on the next new argo connection (old pooled connections recycle within ~60s).',
        keyCleared: 'Key cleared.',
        keyUnavailable: 'Credential service unavailable.',
        researchTitle: 'Research',
        workers: 'Default workers',
        maxWorkers: 'Worker cap',
        tracks: 'Track cap',
        sources: 'Sources per track',
        workerTokens: 'Worker token cap',
        synthTokens: 'Synthesis token cap',
        reportDir: 'Report directory',
        advancedTitle: 'Advanced',
        allow: 'Worker tool allowlist (one per line)',
        allowHint: 'Empty uses the built-in default (MCP + native tools). wide_research itself and argo_research are always excluded.',
        deny: 'Worker tool denylist (one per line)',
        keyRefs: 'Credential forwarding refs (one per line)',
        keyRefsHint: 'These refs resolve through DSH credentials and reach the argo child process as environment variables. Missing refs are skipped.',
        structural: 'Structural fields',
        structuralHint: 'toolName / provider / searchProviderId / fetchProviderId are profile-patch only and need a DSH restart.',
        mcpHint: 'The full MCP tool face (mcp__argo__*) is a separate mcp-argo entry in the profile patch; enable it there and restart.',
        noKey: '(not configured)',
        tabEngines: 'Engines',
        enginesTitle: 'argo engine switches',
        enginesHint: 'These switches used to be argo environment variables, editable only in ~/.config/argo/env or the launch environment. They now live in plugin config and reach the argo child process. Changes apply to the next new connection (pooled connections retire after the idle recycle window, 60s by default). Keys do not belong here; use the Keys tab. Authorization switches (recompute execution, private URLs) are deliberately not exposed.',
        groupCache: 'Cache & ranking',
        groupFetch: 'Fetch layer',
        groupSemantic: 'Semantics & adaptivity',
        groupEngines: 'Engine on-off',
        groupNet: 'Network',
        groupLocal: 'Local search',
        groupMisc: 'Misc',
        groupPaid: 'Quotas & budgets',
        groupPaidHint: 'Free and paid sources share one manager: money-metered sources take a monthly budget, count-capped sources take daily/monthly call caps; 0 = unlimited. When a budget or cap is exhausted, that source is skipped and free fallbacks take over. Changes apply to the next new connection.',
        envDefault: 'default',
        envRange: 'must be between {min} and {max}',
        keySourceFile: 'credential store',
        keySourceEnv: 'launch environment',
        keySourceProjectEnv: 'project .env',
        keySourceUserEnv: 'home .env',
        keySourceOther: 'other layer',
        keyReadonly: 'read-only',
        keyOverrideHint: 'saving overrides the current layer',
        deepseekShared: 'Shared with the host web_search backend; changing it affects both',
      },
    };

    const TOOLS = [
      { id: 'argo_search', group: 'core', zh: '统一搜索', en: 'Unified search' },
      { id: 'argo_fetch', group: 'core', zh: '智能抓取', en: 'Smart fetch' },
      { id: 'argo_evidence', group: 'long', zh: '来源可信度评估', en: 'Source credibility' },
      { id: 'argo_local_search', group: 'long', zh: '本机文件搜索', en: 'Local file search' },
      { id: 'argo_local_read', group: 'long', zh: '本机文件读取', en: 'Local file read' },
      { id: 'argo_recompute', group: 'long', zh: '可复算执行器', en: 'Recompute' },
      { id: 'argo_clarify', group: 'long', zh: '意图消歧', en: 'Intent clarification' },
      { id: 'argo_crawl', group: 'long', zh: '站点级爬取', en: 'Site crawl' },
      { id: 'argo_social_search', group: 'long', zh: '社交平台搜索', en: 'Social search' },
      { id: 'argo_article', group: 'long', zh: '公众号全文', en: 'WeChat article' },
      { id: 'argo_job', group: 'long', zh: '招聘聚合', en: 'Job aggregation' },
      { id: 'argo_extract', group: 'long', zh: '结构化提取', en: 'Structured extract' },
      { id: 'argo_preflight', group: 'long', zh: '批量 URL 预检', en: 'URL preflight' },
      { id: 'argo_pdf', group: 'long', zh: 'PDF 提取', en: 'PDF extract' },
      { id: 'argo_screenshot', group: 'long', zh: '网页截图', en: 'Screenshot' },
      { id: 'argo_answer', group: 'long', zh: '带引用直答', en: 'Cited answer' },
      { id: 'argo_watch', group: 'long', zh: '网页变化监控', en: 'Page watch' },
      { id: 'argo_cite', group: 'long', zh: 'DOI 引用生成', en: 'DOI citation' },
    ];

    const KEYS = [
      { ref: 'DEEPSEEK_API_KEY', common: true, zh: 'DeepSeek 官方搜索', en: 'DeepSeek official search' },
      { ref: 'ARGO_YUANBAO_API_KEY', common: true, zh: '腾讯元宝搜索', en: 'Tencent Yuanbao' },
      { ref: 'TAVILY_API_KEY', common: true, zh: 'Tavily', en: 'Tavily' },
      { ref: 'EXA_API_KEY', common: true, zh: 'Exa', en: 'Exa' },
      { ref: 'BOCHA_API_KEY', common: true, zh: '博查', en: 'Bocha' },
      { ref: 'BRAVE_API_KEY', common: true, zh: 'Brave Search', en: 'Brave Search' },
      { ref: 'TINYFISH_API_KEY', common: true, zh: 'TinyFish（抓取渲染）', en: 'TinyFish (fetch render)' },
      { ref: 'WEB_SEARCH_API_KEY', zh: '火山 / ByteDance', en: 'ByteDance search' },
      { ref: 'OCTEN_API_KEY', zh: 'Octen', en: 'Octen' },
      { ref: 'FELO_API_KEY', zh: 'Felo', en: 'Felo' },
      { ref: 'METASO_API_KEY', zh: '秘塔 Metaso', en: 'Metaso' },
      { ref: 'ANYSEARCH_API_KEY', zh: 'AnySearch（可选）', en: 'AnySearch (optional)' },
      { ref: 'FIRECRAWL_API_KEY', zh: 'Firecrawl（可选，有免费层）', en: 'Firecrawl (optional, free tier)' },
      { ref: 'WEREAD_API_KEY', zh: '微信读书', en: 'WeRead' },
      { ref: 'GITHUB_TOKEN', zh: 'GitHub（仅提高限频）', en: 'GitHub (rate limit)' },
      { ref: 'ZHIHU_ACCESS_SECRET', zh: '知乎', en: 'Zhihu' },
      { ref: 'PARALLEL_API_KEY', zh: 'Parallel', en: 'Parallel' },
      { ref: 'SELTZ_API_KEY', zh: 'Seltz', en: 'Seltz' },
      { ref: 'YDC_API_KEY', zh: 'You.com', en: 'You.com' },
      { ref: 'EASTMONEY_APIKEY', zh: '东财妙想', en: 'Eastmoney Miaoxiang' },
      { ref: 'WOLFRAM_APPID', zh: 'Wolfram Alpha', en: 'Wolfram Alpha' },
      { ref: 'QWEATHER_KEY', zh: '和风天气', en: 'QWeather' },
      { ref: 'UNPAYWALL_EMAIL', zh: 'Unpaywall 邮箱（非密钥）', en: 'Unpaywall email (not secret)' },
    ];

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

    // argo 引擎开关（argoEnv 的 KEY=VALUE 行）。与 index.js 的 ARGO_ENV_SPECS
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
      searchCommand: 'npx',
      searchArgs: ['-y', 'github:taxueseek/argo'],
      searchTimeoutMs: 30000,
      fetchTimeoutMs: 25000,
      fetchMaxChars: 50000,
      searchIdleMs: 60000,
      searchConcurrency: 3,
      nativeTimeoutMs: 60000,
      nativeMaxChars: 24000,
      defaultWorkers: 6,
      maxWorkers: 9,
      maxTracks: 9,
      maxSourcesPerTrack: 5,
      workerMaxTokens: 5000,
      synthesisMaxTokens: 7000,
      reportDir: '',
    };

    const S = {
      wrap: {
        display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 800,
        color: 'var(--dsw-alias-label-primary, #1f2328)',
      },
      title: { margin: 0, fontSize: 18, fontWeight: 600, lineHeight: '26px' },
      hint: { margin: 0, fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-label-tertiary, #7b818b)' },
      notice: { margin: 0, fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-state-business-primary, #3b6ef6)' },
      error: { margin: 0, fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-state-error-primary, #d1242f)' },
      tabs: {
        display: 'flex', gap: 2, flexWrap: 'wrap',
        borderBottom: '.5px solid var(--dsw-alias-border-l2, #e5e7eb)',
      },
      tab: (active) => ({
        padding: '7px 12px 9px', border: 0, background: 'transparent', cursor: 'pointer',
        font: 'inherit', fontSize: 13, lineHeight: '20px', color: active
          ? 'var(--dsw-alias-label-primary, #1f2328)'
          : 'var(--dsw-alias-label-tertiary, #7b818b)',
        borderBottom: active ? '2px solid var(--dsw-alias-state-business-primary, #3b6ef6)' : '2px solid transparent',
        marginBottom: -1,
      }),
      card: {
        display: 'flex', flexDirection: 'column', gap: 10, padding: '14px 16px',
        borderRadius: 12, border: '.5px solid var(--dsw-alias-border-l3, #e5e7eb)',
        background: 'var(--dsw-alias-settings-card-fill, #fff)',
      },
      cardTitle: { margin: 0, fontSize: 14, fontWeight: 600, lineHeight: '20px' },
      row: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
      field: { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 },
      label: { fontSize: 13, fontWeight: 500, lineHeight: '18px' },
      sub: { fontSize: 12, lineHeight: '18px', color: 'var(--dsw-alias-label-tertiary, #7b818b)' },
      input: {
        width: '100%', boxSizing: 'border-box', font: 'inherit', fontSize: 13, lineHeight: '20px',
        padding: '7px 9px', borderRadius: 8,
        border: '.5px solid var(--dsw-alias-border-l4, #d0d7de)',
        background: 'var(--dsw-alias-bg-layer-1, #fff)', color: 'inherit',
      },
      mono: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12 },
      textarea: { minHeight: 64, resize: 'vertical' },
      grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 },
      toolGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: 6 },
      check: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' },
      btn: {
        padding: '6px 12px', borderRadius: 8, border: '.5px solid var(--dsw-alias-border-l4, #d0d7de)',
        background: 'var(--dsw-alias-bg-layer-1, #fff)', color: 'inherit', font: 'inherit',
        fontSize: 12, lineHeight: '18px', cursor: 'pointer',
      },
      btnPrimary: {
        padding: '6px 14px', borderRadius: 8, border: '.5px solid var(--dsw-alias-state-business-primary, #3b6ef6)',
        background: 'var(--dsw-alias-state-business-primary, #3b6ef6)', color: '#fff', font: 'inherit',
        fontSize: 12, lineHeight: '18px', cursor: 'pointer',
      },
      chip: (on) => ({
        display: 'inline-flex', alignItems: 'center', gap: 6, padding: '3px 9px',
        borderRadius: 999, fontSize: 11, lineHeight: '16px',
        border: '.5px solid ' + (on ? 'var(--dsw-alias-state-business-primary, #3b6ef6)' : 'var(--dsw-alias-border-l3, #e5e7eb)'),
        color: on ? 'var(--dsw-alias-state-business-primary, #3b6ef6)' : 'var(--dsw-alias-label-tertiary, #7b818b)',
        background: 'transparent',
      }),
      status: { margin: 0, fontSize: 12, minHeight: 18, lineHeight: '18px' },
      keyRow: {
        display: 'grid', gridTemplateColumns: 'minmax(150px, 1fr) minmax(0, 1.5fr)', gap: 10,
        alignItems: 'start', padding: '10px 0',
        borderTop: '.5px solid var(--dsw-alias-border-l2, #eee)',
      },
      keyHead: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 },
      code: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 10, color: 'var(--dsw-alias-label-tertiary, #7b818b)' },
      dot: (on) => ({
        display: 'inline-block', width: 6, height: 6, borderRadius: 999,
        background: on ? '#2da44e' : 'var(--dsw-alias-border-l4, #d0d7de)',
        marginRight: 5,
      }),
      actions: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
      switchWrap: { display: 'flex', alignItems: 'center', gap: 8 },
      divider: { height: '.5px', background: 'var(--dsw-alias-border-l2, #eee)', margin: '2px 0' },
      metricGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 },
      metricCard: {
        display: 'flex', flexDirection: 'column', gap: 6, padding: '12px 14px',
        borderRadius: 10, border: '.5px solid var(--dsw-alias-border-l3, #e5e7eb)',
        background: 'var(--dsw-alias-bg-layer-2, #f8fafc)',
      },
      metricLabel: { fontSize: 11, fontWeight: 500, color: 'var(--dsw-alias-label-tertiary, #7b818b)', textTransform: 'uppercase', letterSpacing: '0.04em' },
      metricValue: { fontSize: 18, fontWeight: 600, lineHeight: '22px', color: 'var(--dsw-alias-label-primary, #1f2328)' },
      metricSub: { fontSize: 11, lineHeight: '16px', color: 'var(--dsw-alias-label-secondary, #4b5563)' },
      presetGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 },
      presetCard: (active) => ({
        display: 'flex', flexDirection: 'column', gap: 6, padding: '12px 14px',
        borderRadius: 10, cursor: 'pointer', textAlign: 'left',
        border: active ? '1.5px solid var(--dsw-alias-state-business-primary, #3b6ef6)' : '.5px solid var(--dsw-alias-border-l3, #e5e7eb)',
        background: active ? 'var(--dsw-alias-bg-layer-1, #fff)' : 'var(--dsw-alias-settings-card-fill, #fff)',
        boxShadow: active ? '0 1px 3px rgba(59, 110, 246, 0.12)' : 'none',
      }),
      matrixGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8 },
      matrixCard: {
        display: 'flex', flexDirection: 'column', gap: 4, padding: '10px 12px',
        borderRadius: 8, border: '.5px solid var(--dsw-alias-border-l3, #e5e7eb)',
        background: 'var(--dsw-alias-bg-layer-1, #fff)',
      },
      badge: (color) => ({
        display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px',
        borderRadius: 999, fontSize: 10, fontWeight: 500, lineHeight: '14px',
        background: color === 'green' ? 'rgba(46, 160, 67, 0.12)' : (color === 'blue' ? 'rgba(59, 110, 246, 0.12)' : 'rgba(100, 116, 139, 0.12)'),
        color: color === 'green' ? '#2da44e' : (color === 'blue' ? '#3b6ef6' : '#64748b'),
        border: '.5px solid ' + (color === 'green' ? '#2da44e' : (color === 'blue' ? '#3b6ef6' : '#64748b')),
      }),
      filterInput: {
        width: '100%', boxSizing: 'border-box', font: 'inherit', fontSize: 12, lineHeight: '18px',
        padding: '6px 10px', borderRadius: 8,
        border: '.5px solid var(--dsw-alias-border-l4, #d0d7de)',
        background: 'var(--dsw-alias-bg-layer-1, #fff)', color: 'inherit',
        marginBottom: 8,
      },
    };

    function asString(value, fallback) {
      return typeof value === 'string' ? value : (fallback ?? '');
    }

    function asNumber(value, fallback) {
      const n = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10);
      return Number.isFinite(n) ? n : fallback;
    }

    function asArray(value) {
      return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
    }

    function lines(text) {
      return String(text ?? '').split('\n').map((line) => line.trim()).filter(Boolean);
    }

    function toDraft(value) {
      return {
        command: asString(value.searchCommand, DEFAULTS.searchCommand),
        argsText: asArray(value.searchArgs).join('\n'),
        searchTimeoutMs: String(asNumber(value.searchTimeoutMs, DEFAULTS.searchTimeoutMs)),
        fetchTimeoutMs: String(asNumber(value.fetchTimeoutMs, DEFAULTS.fetchTimeoutMs)),
        fetchMaxChars: String(asNumber(value.fetchMaxChars, DEFAULTS.fetchMaxChars)),
        searchIdleMs: String(asNumber(value.searchIdleMs, DEFAULTS.searchIdleMs)),
        searchConcurrency: String(asNumber(value.searchConcurrency, DEFAULTS.searchConcurrency)),
        nativeTimeoutMs: String(asNumber(value.nativeTimeoutMs, DEFAULTS.nativeTimeoutMs)),
        nativeMaxChars: String(asNumber(value.nativeMaxChars, DEFAULTS.nativeMaxChars)),
        defaultWorkers: String(asNumber(value.defaultWorkers, DEFAULTS.defaultWorkers)),
        maxWorkers: String(asNumber(value.maxWorkers, DEFAULTS.maxWorkers)),
        maxTracks: String(asNumber(value.maxTracks, DEFAULTS.maxTracks)),
        maxSourcesPerTrack: String(asNumber(value.maxSourcesPerTrack, DEFAULTS.maxSourcesPerTrack)),
        workerMaxTokens: String(asNumber(value.workerMaxTokens, DEFAULTS.workerMaxTokens)),
        synthesisMaxTokens: String(asNumber(value.synthesisMaxTokens, DEFAULTS.synthesisMaxTokens)),
        reportDir: asString(value.reportDir, DEFAULTS.reportDir),
        allowText: asArray(value.childToolAllow).join('\n'),
        denyText: asArray(value.childToolDeny).join('\n'),
        keyRefsText: asArray(value.keyRefs).join('\n'),
      };
    }

    function Card(props) {
      return h('section', { style: S.card }, [
        props.title ? h('h3', { key: 't', style: S.cardTitle }, props.title) : null,
        props.hint ? h('p', { key: 'h', style: S.hint }, props.hint) : null,
        props.children,
      ]);
    }

    function Field(props) {
      return h('label', { style: S.field }, [
        h('span', { key: 'l', style: S.label }, props.label),
        props.children,
        props.hint ? h('span', { key: 'h', style: S.sub }, props.hint) : null,
      ]);
    }

    function Switch(props) {
      return h('div', { style: S.row }, [
        h('div', { key: 'txt', style: { minWidth: 0 } }, [
          h('div', { key: 'l', style: S.label }, props.label),
          props.hint ? h('div', { key: 'h', style: S.sub }, props.hint) : null,
        ]),
        h('input', {
          key: 'i',
          type: 'checkbox',
          checked: props.checked === true,
          disabled: props.disabled === true,
          onChange: (event) => props.onChange(event.target.checked),
          'aria-label': props.label,
        }),
      ]);
    }

    function NumberField(props) {
      return h(Field, { label: props.label, hint: props.hint }, h('input', {
        type: 'number',
        style: S.input,
        value: props.value,
        min: props.min,
        step: props.step ?? 1,
        disabled: props.disabled === true,
        onChange: (event) => props.onChange(event.target.value),
      }));
    }

    function PrimaryButton(props) {
      return h('button', {
        type: 'button',
        style: props.primary ? S.btnPrimary : S.btn,
        disabled: props.disabled === true,
        onClick: props.onClick,
      }, props.children);
    }

    function ArgoPanel(props) {
      const state = props.useArgoForm((snapshot) => snapshot);
      const value = state && state.value ? state.value : {};
      const ready = state && state.status === 'ready';
      const writable = ready && state.writable !== false;
      const disabled = !writable;

      const locale = props.locale;
      const initialLang = locale && locale.getSnapshot && locale.getSnapshot().active === 'en' ? 'en' : 'zh';
      const [lang, setLang] = useState(initialLang);

      const [tab, setTab] = useState('dashboard');
      const [draft, setDraft] = useState(() => toDraft(value));
      const [busy, setBusy] = useState('');
      const [message, setMessage] = useState('');
      const [error, setError] = useState(false);

      const [credentials, setCredentials] = useState({});
      const [keyDraft, setKeyDraft] = useState({});
      const [keyBusy, setKeyBusy] = useState('');
      const [showAllKeys, setShowAllKeys] = useState(false);

      // argoEnv 引擎开关：bool 类即时写；num/str/list 类走草稿 + 分组保存。
      const [envDraft, setEnvDraft] = useState({});
      const [envBusy, setEnvBusy] = useState('');
      const [engineFilter, setEngineFilter] = useState('');
      const [keyFilter, setKeyFilter] = useState('');

      const dirty = useRef(new Set());
      // 读-改-写共享配置字段（keyRefs / nativeTools / argoEnv）时读实时快照，
      // 不读渲染时快照：保存是 await 之后的异步写，连续两次操作都从渲染值出发
      // 会把前一次的结果挤掉（丢一个转发勾选/工具勾选）。
      const liveValue = () => {
        const snapshot = typeof props.useArgoForm?.getSnapshot === 'function'
          ? props.useArgoForm.getSnapshot()
          : undefined;
        return snapshot && snapshot.value ? snapshot.value : value;
      };
      // describe 往返乱序防护：只认最新一次请求的响应（同官方 settings 页口径）。
      const credSeq = useRef(0);

      const dict = lang === 'en' ? DICT.en : DICT.zh;
      const t = (key) => {
        const text = dict[key] !== undefined ? dict[key] : DICT.zh[key];
        return text !== undefined ? text : key;
      };

      useEffect(() => {
        const off = locale && locale.subscribe
          ? locale.subscribe(() => {
            const next = locale.getSnapshot && locale.getSnapshot().active === 'en' ? 'en' : 'zh';
            setLang(next);
          })
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
          const response = await props.credentials.describe(KEYS.map((entry) => entry.ref));
          if (seq !== credSeq.current) return;
          if (response && response.ok) setCredentials(response.value || {});
        } catch {
          /* 凭证服务暂不可用：保留上一次状态 */
        }
      };

      useEffect(() => {
        if (props.view === 'summary') return undefined;
        void refreshCredentials();
        const off = props.remote && typeof props.remote.$on === 'function'
          ? props.remote.$on('credentials/reference-updated', () => { void refreshCredentials(); })
          : undefined;
        return typeof off === 'function' ? off : undefined;
      }, [state]);

      const edit = (key, text) => {
        dirty.current.add(key);
        setDraft((previous) => ({ ...previous, [key]: text }));
      };

      const report = (ok, text) => {
        setError(!ok);
        setMessage(text);
      };

      const write = async (ops) => {
        if (disabled) return false;
        setBusy('save');
        try {
          const ok = await props.mutate(ops);
          report(ok, ok ? t('saved') : t('refused'));
          return ok;
        } catch (cause) {
          report(false, cause && cause.message ? cause.message : String(cause));
          return false;
        } finally {
          setBusy('');
        }
      };

      const setField = async (field, next) => {
        if (disabled) return;
        setBusy(field);
        try {
          const ok = await props.set(field, next);
          report(ok, ok ? t('saved') : t('refused'));
        } catch (cause) {
          report(false, cause && cause.message ? cause.message : String(cause));
        } finally {
          setBusy('');
        }
      };

      const atLeast = (text, fallback, min) => Math.max(min, asNumber(text, fallback));
      const clearDirty = (keys) => {
        for (const key of keys) dirty.current.delete(key);
      };

      const saveAccess = async () => {
        const args = lines(draft.argsText);
        const ok = await write([
          { op: 'set', path: ['searchCommand'], value: draft.command.trim() || DEFAULTS.searchCommand },
          { op: 'set', path: ['searchArgs'], value: args.length ? args : DEFAULTS.searchArgs },
          { op: 'set', path: ['searchTimeoutMs'], value: atLeast(draft.searchTimeoutMs, DEFAULTS.searchTimeoutMs, 1000) },
          { op: 'set', path: ['fetchTimeoutMs'], value: atLeast(draft.fetchTimeoutMs, DEFAULTS.fetchTimeoutMs, 1000) },
          // argo_fetch 工具 schema 上限 50000（native-tools.mjs 由 mcp_tools.py
          // 生成）：配更大会被 argo clamp 回 50000，面板显示值与实效不符，这里就地截断。
          { op: 'set', path: ['fetchMaxChars'], value: Math.min(50_000, atLeast(draft.fetchMaxChars, DEFAULTS.fetchMaxChars, 500)) },
          { op: 'set', path: ['searchIdleMs'], value: atLeast(draft.searchIdleMs, DEFAULTS.searchIdleMs, 0) },
          { op: 'set', path: ['searchConcurrency'], value: atLeast(draft.searchConcurrency, DEFAULTS.searchConcurrency, 1) },
        ]);
        if (ok) clearDirty(['command', 'argsText', 'searchTimeoutMs', 'fetchTimeoutMs', 'fetchMaxChars', 'searchIdleMs', 'searchConcurrency']);
      };

      const saveTools = async () => {
        const ok = await write([
          { op: 'set', path: ['nativeTimeoutMs'], value: atLeast(draft.nativeTimeoutMs, DEFAULTS.nativeTimeoutMs, 1000) },
          { op: 'set', path: ['nativeMaxChars'], value: atLeast(draft.nativeMaxChars, DEFAULTS.nativeMaxChars, 500) },
        ]);
        if (ok) clearDirty(['nativeTimeoutMs', 'nativeMaxChars']);
      };

      const saveResearch = async () => {
        const workers = atLeast(draft.defaultWorkers, DEFAULTS.defaultWorkers, 1);
        const maxWorkers = Math.max(workers, atLeast(draft.maxWorkers, DEFAULTS.maxWorkers, 1));
        const ok = await write([
          { op: 'set', path: ['defaultWorkers'], value: workers },
          { op: 'set', path: ['maxWorkers'], value: maxWorkers },
          { op: 'set', path: ['maxTracks'], value: atLeast(draft.maxTracks, DEFAULTS.maxTracks, 2) },
          { op: 'set', path: ['maxSourcesPerTrack'], value: atLeast(draft.maxSourcesPerTrack, DEFAULTS.maxSourcesPerTrack, 1) },
          { op: 'set', path: ['workerMaxTokens'], value: atLeast(draft.workerMaxTokens, DEFAULTS.workerMaxTokens, 512) },
          { op: 'set', path: ['synthesisMaxTokens'], value: atLeast(draft.synthesisMaxTokens, DEFAULTS.synthesisMaxTokens, 512) },
          { op: 'set', path: ['reportDir'], value: draft.reportDir.trim() },
        ]);
        if (ok) clearDirty(['defaultWorkers', 'maxWorkers', 'maxTracks', 'maxSourcesPerTrack', 'workerMaxTokens', 'synthesisMaxTokens', 'reportDir']);
      };

      const saveAdvanced = async () => {
        const ok = await write([
          { op: 'set', path: ['childToolAllow'], value: lines(draft.allowText) },
          { op: 'set', path: ['childToolDeny'], value: lines(draft.denyText) },
          { op: 'set', path: ['keyRefs'], value: lines(draft.keyRefsText) },
        ]);
        if (ok) clearDirty(['allowText', 'denyText', 'keyRefsText']);
      };

      const toggleTool = (id) => {
        const current = new Set(asArray(liveValue().nativeTools));
        if (current.has(id)) current.delete(id);
        else current.add(id);
        const ordered = TOOLS.map((tool) => tool.id).filter((toolId) => current.has(toolId));
        for (const extra of current) if (!ordered.includes(extra)) ordered.push(extra);
        void setField('nativeTools', ordered);
      };

      const saveKey = async (ref) => {
        const key = asString(keyDraft[ref]).trim();
        if (!key || !props.credentials) return;
        setKeyBusy(ref);
        try {
          const response = await props.credentials.set(ref, key);
          if (!response || !response.ok) {
            throw new Error(response && response.error ? response.error.message : t('keyUnavailable'));
          }
          setKeyDraft((previous) => ({ ...previous, [ref]: '' }));
          const refs = asArray(liveValue().keyRefs);
          if (!refs.includes(ref)) await props.set('keyRefs', [...refs, ref]);
          report(true, t('keySaved'));
          await refreshCredentials();
        } catch (cause) {
          report(false, cause && cause.message ? cause.message : String(cause));
        } finally {
          setKeyBusy('');
        }
      };

      const clearKey = async (ref) => {
        if (!props.credentials) return;
        setKeyBusy(ref);
        try {
          const response = await props.credentials.unset(ref);
          if (!response || !response.ok) {
            throw new Error(response && response.error ? response.error.message : t('keyUnavailable'));
          }
          report(true, t('keyCleared'));
          await refreshCredentials();
        } catch (cause) {
          report(false, cause && cause.message ? cause.message : String(cause));
        } finally {
          setKeyBusy('');
        }
      };

      const setForward = (ref, on) => {
        const refs = new Set(asArray(liveValue().keyRefs));
        if (on) refs.add(ref);
        else refs.delete(ref);
        void setField('keyRefs', [...refs]);
      };

      // ── argoEnv 引擎开关：KEY=VALUE 行 ↔ 控件 ──
      // argoEnv 行集合是单一真源：每次写入都基于实时快照读-改-写，只碰白名单
      // 内的名字。白名单外的行**被清掉**，不是原样保留——apply 期对白名单外
      // 名字是 loud fail（整个插件加载失败），直接写 profile patch 写错的开关
      // 会让人卡在「改不回来」；这里任何一次面板写入都顺手清毒，是最容易碰到
      // 的恢复入口。
      const envMap = () => {
        const map = {};
        for (const entry of asArray(liveValue().argoEnv)) {
          const eq = entry.indexOf('=');
          if (eq > 0) map[entry.slice(0, eq)] = entry.slice(eq + 1);
        }
        return map;
      };
      // 与 argo 的 env_flag 判定对齐：空串/0/false/no/off/n/disable/disabled/none 算关。
      const ENV_FALSY = ['', '0', 'false', 'no', 'off', 'n', 'disable', 'disabled', 'none'];
      const envBoolState = (spec, map) => {
        const raw = map[spec.name];
        return raw === undefined
          ? spec.def === true
          : !ENV_FALSY.includes(String(raw).trim().toLowerCase());
      };
      const writeEnvLines = async (updates) => {
        const map = envMap();
        for (const name of Object.keys(updates)) {
          const value = updates[name];
          if (value === null || value === undefined || value === '') delete map[name];
          else map[name] = value;
        }
        const lines = Object.keys(map)
          .filter((name) => ENV_NAMES.has(name))
          .map((name) => `${name}=${map[name]}`);
        return props.set('argoEnv', lines);
      };
      const toggleEnv = (spec, on) => {
        if (disabled) return;
        // 开关状态直接落 0/1：文案与 env 同向（开=1，关=0），不做反转。
        const value = on ? '1' : '0';
        void writeEnvLines({ [spec.name]: value })
          .then((ok) => report(ok, ok ? t('saved') : t('refused')))
          .catch((cause) => report(false, cause && cause.message ? cause.message : String(cause)));
      };
      const saveEnvGroup = async (group) => {
        if (disabled) return;
        const updates = {};
        for (const spec of ENGINE_TOGGLES) {
          if (spec.group !== group || spec.kind === 'bool') continue;
          const raw = envDraft[spec.name];
          if (raw === undefined) continue;
          const text = String(raw).trim();
          if (text === '') {
            updates[spec.name] = null;
            continue;
          }
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
        } catch (cause) {
          report(false, cause && cause.message ? cause.message : String(cause));
        } finally {
          setEnvBusy('');
        }
      };

      const applyPreset = async (type) => {
        if (disabled) return;
        setBusy('preset');
        try {
          const map = envMap();
          const ops = [];
          if (type === 'fast') {
            map.ARGO_TFIDF_RERANK = '0';
            map.ARGO_NUMBER_CROSSCHECK = '0';
            map.ARGO_PAID_SEARCH = 'off';
            map.ARGO_ADAPTIVE_GRACE = '1';
            map.ARGO_SERIAL_STAGGER_S = '0.5';
            ops.push({ op: 'set', path: ['searchTimeoutMs'], value: 15000 });
            ops.push({ op: 'set', path: ['fetchTimeoutMs'], value: 15000 });
          } else if (type === 'standard') {
            map.ARGO_TFIDF_RERANK = '1';
            map.ARGO_NUMBER_CROSSCHECK = '1';
            map.ARGO_MINHASH_DEDUPE = '1';
            map.ARGO_PAID_SEARCH = 'auto';
            map.ARGO_ADAPTIVE_GRACE = '1';
            map.ARGO_SERIAL_STAGGER_S = '0.8';
            ops.push({ op: 'set', path: ['searchTimeoutMs'], value: 30000 });
            ops.push({ op: 'set', path: ['fetchTimeoutMs'], value: 25000 });
          } else if (type === 'deep') {
            map.ARGO_TFIDF_RERANK = '1';
            map.ARGO_NUMBER_CROSSCHECK = '1';
            map.ARGO_MINHASH_DEDUPE = '1';
            map.ARGO_SEMANTIC_EVIDENCE = '1';
            map.ARGO_ADAPTIVE_GRACE = '1';
            map.ARGO_PAID_SEARCH = 'auto';
            ops.push({ op: 'set', path: ['searchTimeoutMs'], value: 45000 });
            ops.push({ op: 'set', path: ['fetchTimeoutMs'], value: 35000 });
            ops.push({ op: 'set', path: ['nativeTimeoutMs'], value: 60000 });
          }
          const envLines = Object.keys(map)
            .filter((name) => ENV_NAMES.has(name))
            .map((name) => `${name}=${map[name]}`);
          ops.push({ op: 'set', path: ['argoEnv'], value: envLines });
          const ok = await props.mutate(ops);
          const presetName = type === 'fast' ? t('presetFast') : (type === 'standard' ? t('presetStandard') : t('presetDeep'));
          report(ok, ok ? `${t('presetApplied')} ${presetName}` : t('refused'));
        } catch (cause) {
          report(false, cause && cause.message ? cause.message : String(cause));
        } finally {
          setBusy('');
        }
      };

      if (props.view === 'summary') {
        const summary = t('summary')
          .replace('{search}', value.searchProviderEnabled !== false ? t('on') : t('off'))
          .replace('{fetch}', value.fetchProviderEnabled !== false ? t('on') : t('off'))
          .replace('{tools}', String(asArray(value.nativeTools).length))
          .replace('{workers}', String(asNumber(value.defaultWorkers, DEFAULTS.defaultWorkers)))
          .replace('{max}', String(asNumber(value.maxWorkers, DEFAULTS.maxWorkers)));
        return summary;
      }

      if (!ready) {
        return h('div', { style: S.wrap }, [
          h('h2', { key: 't', style: S.title }, t('title')),
          h('p', { key: 'p', style: S.hint }, state && state.status === 'unavailable' ? t('unavailable') : t('loading')),
        ]);
      }

      const tabButton = (id, label) => h('button', {
        key: id,
        type: 'button',
        style: S.tab(tab === id),
        onClick: () => setTab(id),
      }, label);

      const statusLine = message
        ? h('p', { style: error ? S.error : S.notice, role: 'status' }, message)
        : h('p', { style: S.status }, disabled ? t('readonly') : '');

      const coreTools = TOOLS.filter((tool) => tool.group === 'core');
      const longTools = TOOLS.filter((tool) => tool.group === 'long');
      const toolCheckbox = (tool) => h('label', { key: tool.id, style: S.check }, [
        h('input', {
          key: 'i',
          type: 'checkbox',
          checked: asArray(value.nativeTools).includes(tool.id),
          disabled,
          onChange: () => toggleTool(tool.id),
        }),
        h('span', { key: 't' }, [
          h('span', { key: 'n', style: S.mono }, tool.id),
          h('span', { key: 'd', style: S.sub }, '  ' + (lang === 'en' ? tool.en : tool.zh)),
        ]),
      ]);

      const keyRow = (entry) => {
        const info = credentials[entry.ref];
        const configured = info && info.configured === true;
        const writable = !info || info.writable !== false;
        const forwarded = asArray(value.keyRefs).includes(entry.ref);
        // 来源层：回答「这把 key 从哪来」。保存写凭证库文件层，优先级高于
        // 启动环境与 .env，所以来源不是 file 时提示保存后会覆盖当前层。
        const sourceLabel = configured && typeof info.source === 'string'
          ? t('keySource' + info.source.split('-').map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(''))
          : '';
        const statusParts = [configured ? t('configured') : t('notConfigured')];
        if (sourceLabel && sourceLabel !== 'keySource' + info.source) statusParts.push(sourceLabel);
        if (configured && !writable) statusParts.push(t('keyReadonly'));
        if (configured && writable && info.source !== undefined && info.source !== 'file') statusParts.push(t('keyOverrideHint'));
        return h('div', { key: entry.ref, style: S.keyRow }, [
          h('div', { key: 'head', style: S.keyHead }, [
            h('span', { key: 'n', style: S.label }, [
              h('span', { key: 'dot', style: S.dot(configured) }),
              lang === 'en' ? entry.en : entry.zh,
            ]),
            h('span', { key: 'ref', style: S.code }, entry.ref),
            h('span', { key: 's', style: S.sub }, statusParts.join(' · ')),
            entry.ref === 'DEEPSEEK_API_KEY'
              ? h('span', { key: 'shared', style: S.sub }, t('deepseekShared'))
              : null,
          ]),
          h('div', { key: 'body', style: { display: 'flex', flexDirection: 'column', gap: 6 } }, [
            h('input', {
              key: 'i',
              type: 'password',
              autoComplete: 'off',
              spellCheck: false,
              style: S.input,
              value: asString(keyDraft[entry.ref]),
              placeholder: configured ? t('keyPlaceholderSet') : t('keyPlaceholderEmpty'),
              disabled: disabled || !props.credentials || !writable,
              onChange: (event) => setKeyDraft((previous) => ({ ...previous, [entry.ref]: event.target.value })),
            }),
            h('div', { key: 'a', style: S.actions }, [
              h('label', { key: 'f', style: S.check }, [
                h('input', {
                  key: 'i',
                  type: 'checkbox',
                  checked: forwarded,
                  disabled,
                  onChange: (event) => setForward(entry.ref, event.target.checked),
                }),
                h('span', { key: 't' }, t('forward')),
              ]),
              h('span', { key: 'sp', style: { flex: 1 } }),
              h('button', {
                key: 's',
                type: 'button',
                style: S.btn,
                disabled: disabled || !writable || keyBusy === entry.ref || asString(keyDraft[entry.ref]).trim() === '',
                onClick: () => { void saveKey(entry.ref); },
              }, keyBusy === entry.ref ? t('saving') : t('saveKey')),
              configured ? h('button', {
                key: 'c',
                type: 'button',
                style: S.btn,
                disabled: disabled || !writable || keyBusy === entry.ref,
                onClick: () => { void clearKey(entry.ref); },
              }, t('clearKey')) : null,
            ]),
          ]),
        ]);
      };

      const commonKeys = KEYS.filter((entry) => entry.common);
      const visibleKeys = showAllKeys ? KEYS : commonKeys;

      const panel = [];

      if (tab === 'dashboard') {
        const configuredKeyCount = Object.values(credentials).filter((c) => c && c.configured).length;
        const keylessEngineCount = CORE_ENGINES.filter((e) => e.keyless).length;
        const seamSearchOn = value.searchProviderEnabled !== false;
        const seamFetchOn = value.fetchProviderEnabled !== false;
        const activeToolCount = asArray(value.nativeTools).length;
        const poolConcurrency = asNumber(value.searchConcurrency, DEFAULTS.searchConcurrency);
        const poolIdleMs = asNumber(value.searchIdleMs, DEFAULTS.searchIdleMs);

        panel.push(h('div', { key: 'metrics', style: S.metricGrid }, [
          h('div', { key: 'pool', style: S.metricCard }, [
            h('span', { key: 'l', style: S.metricLabel }, t('poolTitle')),
            h('div', { key: 'v', style: { display: 'flex', alignItems: 'center', gap: 6 } }, [
              h('span', { key: 'dot', style: S.dot(true) }),
              h('span', { key: 'val', style: S.metricValue }, `${poolConcurrency} 并发`),
            ]),
            h('span', { key: 's', style: S.metricSub }, `${t('poolStatusHealthy')} · 保活 ${poolIdleMs / 1000}s`),
          ]),
          h('div', { key: 'seam', style: S.metricCard }, [
            h('span', { key: 'l', style: S.metricLabel }, t('accessTitle')),
            h('span', { key: 'val', style: S.metricValue }, seamSearchOn && seamFetchOn ? '双路接管' : (seamSearchOn || seamFetchOn ? '单路接管' : '未接管')),
            h('span', { key: 's', style: S.metricSub }, `Search: ${seamSearchOn ? 'ON' : 'OFF'} · Fetch: ${seamFetchOn ? 'ON' : 'OFF'}`),
          ]),
          h('div', { key: 'tools', style: S.metricCard }, [
            h('span', { key: 'l', style: S.metricLabel }, t('toolsTitle')),
            h('span', { key: 'val', style: S.metricValue }, `${activeToolCount} / ${TOOLS.length}`),
            h('span', { key: 's', style: S.metricSub }, '已就绪原生 MCP 工具'),
          ]),
          h('div', { key: 'keys', style: S.metricCard }, [
            h('span', { key: 'l', style: S.metricLabel }, t('keysTitle')),
            h('span', { key: 'val', style: S.metricValue }, `${configuredKeyCount} 配置 · ${keylessEngineCount} 免密`),
            h('span', { key: 's', style: S.metricSub }, '覆盖全局多源引擎'),
          ]),
        ]));

        panel.push(h(Card, { key: 'presets', title: t('presetTitle'), hint: t('presetHint') }, [
          h('div', { key: 'grid', style: S.presetGrid }, [
            h('div', {
              key: 'fast',
              style: S.presetCard(false),
              onClick: () => { void applyPreset('fast'); },
            }, [
              h('div', { key: 't', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } }, [
                h('span', { key: 'title', style: S.label }, t('presetFast')),
                h('span', { key: 'badge', style: S.badge('blue') }, '15s 超时'),
              ]),
              h('span', { key: 'd', style: S.sub }, t('presetFastDesc')),
            ]),
            h('div', {
              key: 'standard',
              style: S.presetCard(true),
              onClick: () => { void applyPreset('standard'); },
            }, [
              h('div', { key: 't', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } }, [
                h('span', { key: 'title', style: S.label }, t('presetStandard')),
                h('span', { key: 'badge', style: S.badge('green') }, '标准生产'),
              ]),
              h('span', { key: 'd', style: S.sub }, t('presetStandardDesc')),
            ]),
            h('div', {
              key: 'deep',
              style: S.presetCard(false),
              onClick: () => { void applyPreset('deep'); },
            }, [
              h('div', { key: 't', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } }, [
                h('span', { key: 'title', style: S.label }, t('presetDeep')),
                h('span', { key: 'badge', style: S.badge('blue') }, '深度核验'),
              ]),
              h('span', { key: 'd', style: S.sub }, t('presetDeepDesc')),
            ]),
          ]),
        ]));

        panel.push(h(Card, { key: 'matrix', title: t('matrixTitle'), hint: t('matrixHint') }, [
          h('div', { key: 'grid', style: S.matrixGrid }, CORE_ENGINES.map((engine) => {
            const hasKey = engine.ref && credentials[engine.ref] && credentials[engine.ref].configured;
            const badgeType = hasKey ? 'green' : (engine.keyless ? 'blue' : 'gray');
            const badgeText = hasKey ? t('matrixConfigured') : (engine.keyless ? t('matrixKeyless') : t('matrixPending'));
            return h('div', { key: engine.id, style: S.matrixCard }, [
              h('div', { key: 'h', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6 } }, [
                h('span', { key: 'n', style: S.label }, lang === 'en' ? engine.nameEn : engine.nameZh),
                h('span', { key: 'b', style: S.badge(badgeType) }, badgeText),
              ]),
              h('span', { key: 'r', style: S.sub }, lang === 'en' ? engine.roleEn : engine.roleZh),
            ]);
          })),
        ]));
      }

      if (tab === 'access') {
        panel.push(h(Card, { key: 'seam', title: t('accessTitle') }, [
          h(Switch, {
            key: 's',
            checked: value.searchProviderEnabled !== false,
            disabled,
            label: t('searchSeam'),
            hint: t('searchSeamHint'),
            onChange: (next) => { void setField('searchProviderEnabled', next); },
          }),
          h(Switch, {
            key: 'f',
            checked: value.fetchProviderEnabled !== false,
            disabled,
            label: t('fetchSeam'),
            hint: t('fetchSeamHint'),
            onChange: (next) => { void setField('fetchProviderEnabled', next); },
          }),
        ]));
        panel.push(h(Card, { key: 'entry', title: t('entryTitle'), hint: t('entryHint') }, [
          h(Field, { key: 'c', label: t('command') }, h('input', {
            type: 'text',
            style: { ...S.input, ...S.mono },
            value: draft.command,
            disabled,
            spellCheck: false,
            onChange: (event) => edit('command', event.target.value),
          })),
          h(Field, { key: 'a', label: t('args') }, h('textarea', {
            style: { ...S.input, ...S.textarea, ...S.mono },
            value: draft.argsText,
            disabled,
            spellCheck: false,
            onChange: (event) => edit('argsText', event.target.value),
          })),
          h('div', { key: 'g', style: S.grid }, [
            h(NumberField, {
              key: 'st', label: t('searchTimeout'), min: 1000, value: draft.searchTimeoutMs, disabled,
              onChange: (next) => edit('searchTimeoutMs', next),
            }),
            h(NumberField, {
              key: 'ft', label: t('fetchTimeout'), min: 1000, value: draft.fetchTimeoutMs, disabled,
              onChange: (next) => edit('fetchTimeoutMs', next),
            }),
            h(NumberField, {
              key: 'fc', label: t('fetchChars'), min: 500, step: 500, value: draft.fetchMaxChars, disabled,
              onChange: (next) => edit('fetchMaxChars', next),
            }),
            h(NumberField, {
              key: 'id', label: t('idle'), min: 0, step: 1000, value: draft.searchIdleMs, disabled,
              onChange: (next) => edit('searchIdleMs', next),
            }),
            h(NumberField, {
              key: 'cc', label: t('concurrency'), min: 1, value: draft.searchConcurrency, disabled,
              onChange: (next) => edit('searchConcurrency', next),
            }),
          ]),
          h('div', { key: 'a2', style: S.actions }, [
            h(PrimaryButton, { primary: true, disabled: disabled || busy === 'save', onClick: () => { void saveAccess(); } },
              busy === 'save' ? t('saving') : t('save')),
          ]),
        ]));
      }

      if (tab === 'tools') {
        panel.push(h(Card, { key: 'tools', title: t('toolsTitle'), hint: t('toolsHint') }, [
          h('div', { key: 'core', style: { display: 'flex', flexDirection: 'column', gap: 4 } }, [
            h('span', { key: 'l', style: S.label }, t('coreTools')),
            h('div', { key: 'g', style: S.toolGrid }, coreTools.map(toolCheckbox)),
          ]),
          h('div', { key: 'div', style: S.divider }),
          h('div', { key: 'long', style: { display: 'flex', flexDirection: 'column', gap: 4 } }, [
            h('span', { key: 'l', style: S.label }, t('longTools')),
            h('div', { key: 'g', style: S.toolGrid }, longTools.map(toolCheckbox)),
          ]),
          h('div', { key: 'g2', style: S.grid }, [
            h(NumberField, {
              key: 'nt', label: t('nativeTimeout'), min: 1000, value: draft.nativeTimeoutMs, disabled,
              onChange: (next) => edit('nativeTimeoutMs', next),
            }),
            h(NumberField, {
              key: 'nc', label: t('nativeChars'), min: 500, step: 500, value: draft.nativeMaxChars, disabled,
              onChange: (next) => edit('nativeMaxChars', next),
            }),
          ]),
          h('div', { key: 'a', style: S.actions }, [
            h(PrimaryButton, { primary: true, disabled: disabled || busy === 'save', onClick: () => { void saveTools(); } },
              busy === 'save' ? t('saving') : t('save')),
          ]),
        ]));
      }

      if (tab === 'engines') {
        const map = envMap();
        // 渲染顺序即面板顺序。**不是**从 ENGINE_TOGGLES 去重推导——那样分组
        // 顺序会随白名单增删而漂移，用户每次升级都看到重排的面板。
        //
        // 历史 bug（2026-10-21 定位）：`paid` 一度不在这个清单里，于是
        // ARGO_PAID_SEARCH 与 ARGO_BUDGET_DEEPSEEK_SEARCH 两条开关在
        // index.js 白名单、client.js ENGINE_TOGGLES、双语文案三处都齐全，
        // 却在面板上完全不显示——用户配好了预算却找不到入口，还以为是插件
        // 没装。漂移门禁只核对「两边名单同集合、字段一致」，核对不到「这个
        // 分组到底渲不渲染」；已由 test/client.test.js 的「engines tab renders
        // a control for every ENGINE_TOGGLES switch」补上（见该测试注释）。
        const groups = ['cache', 'fetch', 'semantic', 'engines', 'net', 'local', 'paid', 'misc'];
        const groupTitle = (group) => t('group' + group.charAt(0).toUpperCase() + group.slice(1));
        panel.push(h('p', { key: 'hint', style: S.hint }, t('enginesHint')));
        panel.push(h('input', {
          key: 'filter',
          type: 'text',
          style: S.filterInput,
          placeholder: t('searchFilterPlaceholder'),
          value: engineFilter,
          onChange: (event) => setEngineFilter(event.target.value),
        }));
        const query = engineFilter.trim().toLowerCase();
        for (const group of groups) {
          let specs = ENGINE_TOGGLES.filter((spec) => spec.group === group);
          if (query) {
            const groupNameMatch = group.toLowerCase().includes(query) || groupTitle(group).toLowerCase().includes(query);
            if (!groupNameMatch) {
              specs = specs.filter((spec) => {
                const name = spec.name.toLowerCase();
                const zh = (spec.zh || '').toLowerCase();
                const en = (spec.en || '').toLowerCase();
                const hintZh = (spec.hintZh || '').toLowerCase();
                const hintEn = (spec.hintEn || '').toLowerCase();
                return name.includes(query) || zh.includes(query) || en.includes(query) || hintZh.includes(query) || hintEn.includes(query);
              });
            }
          }
          if (specs.length === 0) continue;
          const bools = specs.filter((spec) => spec.kind === 'bool');
          const fields = specs.filter((spec) => spec.kind !== 'bool');
          const children = [];
          if (group === 'paid') {
            children.push(h('p', { key: 'groupHint', style: S.hint }, t('groupPaidHint')));
          }
          for (const spec of bools) {
            // 未配置时显示 argo 默认态并标注「默认」，配置过的显示自定义值，
            // 让「这一行到底生不生效」在面板上一眼可辨。
            const custom = map[spec.name] !== undefined;
            const suffix = custom ? '' : (lang === 'en' ? ` (${t('envDefault')})` : `（${t('envDefault')}）`);
            children.push(h(Switch, {
              key: spec.name,
              checked: envBoolState(spec, map),
              disabled,
              label: (lang === 'en' ? spec.en : spec.zh) + suffix,
              hint: lang === 'en' ? spec.hintEn : spec.hintZh,
              onChange: (next) => toggleEnv(spec, next),
            }));
          }
          if (fields.length > 0) {
            const fieldNodes = fields.map((spec) => {
              const drafted = envDraft[spec.name];
              const current = drafted !== undefined
                ? drafted
                : (map[spec.name] !== undefined ? map[spec.name] : (spec.kind === 'num' ? String(spec.def) : ''));
              const control = spec.kind === 'num'
                ? h('input', {
                  key: 'i',
                  type: 'number',
                  style: S.input,
                  value: current,
                  min: spec.min,
                  max: spec.max,
                  step: spec.step ?? 1,
                  disabled,
                  onChange: (event) => setEnvDraft((previous) => ({ ...previous, [spec.name]: event.target.value })),
                })
                : h('input', {
                  key: 'i',
                  type: 'text',
                  style: { ...S.input, ...S.mono },
                  value: current,
                  disabled,
                  spellCheck: false,
                  onChange: (event) => setEnvDraft((previous) => ({ ...previous, [spec.name]: event.target.value })),
                });
              return h(Field, {
                key: spec.name,
                label: lang === 'en' ? spec.en : spec.zh,
                hint: lang === 'en' ? spec.hintEn : spec.hintZh,
              }, control);
            });
            children.push(h('div', { key: 'fields', style: S.grid }, fieldNodes));
            children.push(h('div', { key: 'save', style: S.actions }, [
              h(PrimaryButton, {
                key: 'b',
                primary: true,
                disabled: disabled || envBusy === group,
                onClick: () => { void saveEnvGroup(group); },
              }, envBusy === group ? t('saving') : t('save')),
            ]));
          }
          panel.push(h(Card, { key: group, title: groupTitle(group) }, children));
        }
      }

      if (tab === 'keys') {
        const query = keyFilter.trim().toLowerCase();
        const displayKeys = query
          ? KEYS.filter((entry) => entry.ref.toLowerCase().includes(query) || (entry.zh && entry.zh.toLowerCase().includes(query)) || (entry.en && entry.en.toLowerCase().includes(query)))
          : visibleKeys;
        panel.push(h(Card, { key: 'keys', title: t('keysTitle'), hint: t('keysHint') }, [
          h('input', {
            key: 'filter',
            type: 'text',
            style: S.filterInput,
            placeholder: t('searchFilterPlaceholder'),
            value: keyFilter,
            onChange: (event) => setKeyFilter(event.target.value),
          }),
          h('span', { key: 'l', style: S.label }, query ? t('keysTitle') : t('commonKeys')),
          ...displayKeys.map(keyRow),
          query ? null : h('div', { key: 'a', style: S.actions }, [
            h('button', {
              key: 'more',
              type: 'button',
              style: S.btn,
              onClick: () => setShowAllKeys((previous) => !previous),
            }, showAllKeys ? t('showLess') : t('showMore')),
            props.credentials ? null : h('span', { key: 'u', style: S.sub }, t('keyUnavailable')),
          ]),
        ]));
      }

      if (tab === 'research') {
        panel.push(h(Card, { key: 'research', title: t('researchTitle') }, [
          h('div', { key: 'g', style: S.grid }, [
            h(NumberField, {
              key: 'dw', label: t('workers'), min: 1, value: draft.defaultWorkers, disabled,
              onChange: (next) => edit('defaultWorkers', next),
            }),
            h(NumberField, {
              key: 'mw', label: t('maxWorkers'), min: 1, value: draft.maxWorkers, disabled,
              onChange: (next) => edit('maxWorkers', next),
            }),
            h(NumberField, {
              key: 'mt', label: t('tracks'), min: 2, value: draft.maxTracks, disabled,
              onChange: (next) => edit('maxTracks', next),
            }),
            h(NumberField, {
              key: 'ms', label: t('sources'), min: 1, value: draft.maxSourcesPerTrack, disabled,
              onChange: (next) => edit('maxSourcesPerTrack', next),
            }),
            h(NumberField, {
              key: 'wt', label: t('workerTokens'), min: 512, step: 512, value: draft.workerMaxTokens, disabled,
              onChange: (next) => edit('workerMaxTokens', next),
            }),
            h(NumberField, {
              key: 'st', label: t('synthTokens'), min: 512, step: 512, value: draft.synthesisMaxTokens, disabled,
              onChange: (next) => edit('synthesisMaxTokens', next),
            }),
          ]),
          h(Field, { key: 'd', label: t('reportDir') }, h('input', {
            type: 'text',
            style: { ...S.input, ...S.mono },
            value: draft.reportDir,
            disabled,
            spellCheck: false,
            onChange: (event) => edit('reportDir', event.target.value),
          })),
          h('div', { key: 'a', style: S.actions }, [
            h(PrimaryButton, { primary: true, disabled: disabled || busy === 'save', onClick: () => { void saveResearch(); } },
              busy === 'save' ? t('saving') : t('save')),
          ]),
        ]));
      }

      if (tab === 'advanced') {
        panel.push(h(Card, { key: 'adv', title: t('advancedTitle') }, [
          h(Field, { key: 'allow', label: t('allow'), hint: t('allowHint') }, h('textarea', {
            style: { ...S.input, ...S.textarea, ...S.mono },
            value: draft.allowText,
            disabled,
            spellCheck: false,
            onChange: (event) => edit('allowText', event.target.value),
          })),
          h(Field, { key: 'deny', label: t('deny') }, h('textarea', {
            style: { ...S.input, ...S.textarea, ...S.mono },
            value: draft.denyText,
            disabled,
            spellCheck: false,
            onChange: (event) => edit('denyText', event.target.value),
          })),
          h(Field, { key: 'refs', label: t('keyRefs'), hint: t('keyRefsHint') }, h('textarea', {
            style: { ...S.input, ...S.textarea, ...S.mono },
            value: draft.keyRefsText,
            disabled,
            spellCheck: false,
            onChange: (event) => edit('keyRefsText', event.target.value),
          })),
          h('div', { key: 'a', style: S.actions }, [
            h(PrimaryButton, { primary: true, disabled: disabled || busy === 'save', onClick: () => { void saveAdvanced(); } },
              busy === 'save' ? t('saving') : t('save')),
          ]),
          h('div', { key: 'div', style: S.divider }),
          h('div', { key: 'struct', style: { display: 'flex', flexDirection: 'column', gap: 4 } }, [
            h('span', { key: 'l', style: S.label }, t('structural')),
            h('span', { key: 'h', style: S.sub }, t('structuralHint')),
            h('span', { key: 'm', style: S.sub }, t('mcpHint')),
          ]),
        ]));
      }

      return h('div', { style: S.wrap }, [
        h('div', { key: 'head', style: { display: 'flex', flexDirection: 'column', gap: 4 } }, [
          h('h2', { key: 't', style: S.title }, t('title')),
          h('p', { key: 'i', style: S.hint }, t('intro')),
        ]),
        h('div', { key: 'tabs', style: S.tabs }, [
          tabButton('dashboard', t('tabDashboard')),
          tabButton('access', t('tabAccess')),
          tabButton('tools', t('tabTools')),
          tabButton('engines', t('tabEngines')),
          tabButton('keys', t('tabKeys')),
          tabButton('research', t('tabResearch')),
          tabButton('advanced', t('tabAdvanced')),
        ]),
        statusLine,
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
    }

    exports.apply = apply;
    exports.inject = inject;
    exports.ArgoPanel = ArgoPanel;
    // 静态数据导出供漂移门禁核对：TOOLS 必须与 native-tools.mjs（mcp_tools.py
    // 生成产物）同名同集合，ENGINE_TOGGLES 必须与 index.js 的 ARGO_ENV_SPECS
    // 同名同集合——两边任何一边增删而另一边没跟上，测试即红。
    exports.TOOLS = TOOLS;
    exports.ENGINE_TOGGLES = ENGINE_TOGGLES;

    return module.exports;
  }
});
