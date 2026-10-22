/*
 * @taxueseek/argo-dsh — wide_research + argo 原生工具 + 原生 web_search provider
 *
 * 本 bundle 默认不挂 argo MCP：搜索/抓取高频路径走原生工具与 web seam
 * （CLI 单发，同引擎同守卫，零常驻 token 开销）；MCP 19 工具全量面按需
 * 在 profile patch 中挂载。Workers 不调用 argo_research，避免嵌套扇出。
 * 只依赖公开服务 ctx.tools / ctx.subagents / ctx.web / ctx.settings；唯一
 * 的 @deepseek-ai/* 引用是可选的 schemastery（设置表单 schema，导入失败
 * 时插件照常加载，只是没有设置页）。
 *
 * 三形态接入（互为冗余，同一搜索能力，按宿主能力自动降级）：
 *   1. mcp__argo__*：profile 按需挂载的 stdio MCP，完整工具面（默认关）
 *   2. 原生一等工具 argo_search / argo_fetch（ctx.tools.register）：CLI 单发
 *      执行（bin call / mcp_server.py --call），不依赖 MCP 连接，默认入口；
 *      nativeTools 配置（空数组=关）
 *   3. 原生 web_search seam：web.registerSearchProvider 注册 "argo"
 *      provider，内置 web_search 经 argo 引擎链路由；默认启用
 *      （searchProviderEnabled: false 时注册但不可选）
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { NATIVE_TOOLS as NATIVE_TOOL_SPECS } from './native-tools.mjs'

import {
  DEFAULTS,
  PLUGIN_VERSION as RUNNER_PLUGIN_VERSION,
  setEnvResolver,
  resolveEntry,
  resolveNativeSpawn,
  makeNativeTool,
  nativeViaMcpPool,
  runNativeCall,
  disposeSharedMcp,
  searchViaArgoMCP,
  fetchViaArgo,
  mapArgoToWebResult,
  mapArgoToWebFetch,
  classifyFetchError,
  argoAborted,
  argoToolError,
} from './runner.js'

import {
  normalizeTracks,
  stageTracks,
  evaluateGates,
  normalizeSource,
  normalizeSourceUrl,
  buildLocalSources,
  parseJsonArray,
  parseJsonObject,
  extractValues,
  persistReport,
  startStructured,
  providerSupportsStructured,
  recomputeViaArgoMCP,
  createWideResearchTool,
} from './wide-research.js'

import {
  getMcpPoolStats,
  resetMcpPool,
} from './diagnostics.js'

/**
 * Schemastery 是可选依赖：装进 DSH profile 后由宿主依赖树提供，仓内单测
 * 没有 node_modules。导入失败时 Config 保持 undefined，宿主按「无 schema」
 * 处理——插件其余功能照常，只是设置页不会出现。不为了一个设置表单把
 * 插件变成不可加载。
 */
let Schema
try {
  const schemastery = await import('@deepseek-ai/schemastery')
  Schema = schemastery.default ?? schemastery.Schema ?? schemastery
} catch {
  Schema = undefined
}

/**
 * 本插件版本：从自己的 package.json 读，**不写死字面量**。
 */
function resolvePluginVersion() {
  try {
    const here = dirname(fileURLToPath(import.meta.url))
    const raw = readFileSync(join(here, '..', 'package.json'), 'utf8')
    const parsed = JSON.parse(raw)
    if (typeof parsed?.version === 'string' && parsed.version !== '') return parsed.version
  } catch { /* 打包裁剪/权限异常：回退，不阻断插件加载 */ }
  return RUNNER_PLUGIN_VERSION || '0.0.0-unknown'
}

export const PLUGIN_VERSION = resolvePluginVersion()

export const name = 'wide-research'
export const inject = ['tools', 'subagents', 'systemPrompt']

const MCP_CHILD_TOOLS = Object.keys(NATIVE_TOOL_SPECS).map(name => `mcp__argo__${name}`)
const DEFAULT_CHILD_TOOLS = Object.freeze([
  ...MCP_CHILD_TOOLS,
  'argo_search',
  'argo_fetch',
  'web_search',
  'web_fetch',
])

export function buildChildToolFilters(config, toolName) {
  const base = Array.isArray(config?.childToolAllow) ? config.childToolAllow : DEFAULT_CHILD_TOOLS
  const blocked = new Set([toolName, 'mcp__argo__argo_research'])
  const deny = [...new Set([toolName, ...asStringArray(config?.childToolDeny, 100)])]
  const denySet = new Set(deny)
  const allow = [...new Set(asStringArray(base, 100).filter(tool => !blocked.has(tool) && !denySet.has(tool)))]
  return { allow, deny }
}

/**
 * 面板可露出的 argo 环境变量开关（curated 白名单）。
 *
 * 选录标准：日常会开合的行为开关，语义单一、默认值在 argo 侧稳定；授权位
 * （ARGO_ALLOW_RECOMPUTE / ARGO_ALLOW_PRIVATE_URLS，argo 要求 expand=False
 * + strict=True）与路径/状态类内部变量（ARGO_STATE_DIR / ARGO_HOME /
 * ARGO_DAEMON 等）刻意不收——前者不该一键放行，后者改了会动摇凭证 seam 的
 * 解析前提。每个名字都在 argo 仓 scripts/ 有读取点，由
 * tests/test_dsh_plugin_env_gate.py 对拍，防白名单漂移成 Fiction。
 *
 * kind：bool=开关（值 0/1）、num=数字，str=字符串，list=名单（逗号或路径分隔符分隔，照 argo 原语义）。
 * def=argo 侧默认值（仅面板展示用，缺省即不写该行、由 argo 自己决定）。
 *
 * 开关文案一律写「正向行为」或「明确的关闭动作」，**不要再用 invert 反转**：
 * 历史 bug（2026-10-22）——`关闭搜索结果缓存` 配 invert 后，未配置时面板显示
 * 为「开」，点一下反而把 ARGO_NO_CACHE 写成 0，缓存始终没关掉。
 */
export const ARGO_ENV_SPECS = Object.freeze([
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
])

const ARGO_ENV_NAMES = new Set(ARGO_ENV_SPECS.map(spec => spec.name))
const ARGO_ENV_BY_NAME = new Map(ARGO_ENV_SPECS.map(spec => [spec.name, spec]))

function parseArgoEnvLine(line) {
  const eq = line.indexOf('=')
  if (eq <= 0) return null
  const name = line.slice(0, eq).trim()
  const value = line.slice(eq + 1)
  if (!CREDENTIAL_REF_PATTERN.test(name)) return null
  return { name, value }
}

export function argoEnvOverlay(config = activeConfig) {
  const overlay = {}
  for (const entry of asStringArray(config?.argoEnv, 64)) {
    const parsed = parseArgoEnvLine(entry)
    if (parsed === null || !ARGO_ENV_NAMES.has(parsed.name)) continue
    overlay[parsed.name] = parsed.value
  }
  return overlay
}

const isObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value)

const VOLATILE_WRITE = Symbol.for('cosmokit.volatile.write')
const isVolatileRef = (value) =>
  typeof value === 'object' && value !== null && VOLATILE_WRITE in value && typeof value.get === 'function'

function liveConfig(provided) {
  const base = isObject(provided) ? provided : {}
  return new Proxy(base, {
    get(target, key) {
      const raw = Reflect.get(target, key)
      const value = isVolatileRef(raw) ? raw.get() : raw
      return value !== undefined ? value : DEFAULTS[key]
    },
    has(target, key) {
      return Reflect.has(target, key) || key in DEFAULTS
    },
    ownKeys(target) {
      return [...new Set([...Reflect.ownKeys(target), ...Object.keys(DEFAULTS)])]
    },
    getOwnPropertyDescriptor(target, key) {
      const desc = Reflect.getOwnPropertyDescriptor(target, key)
      if (desc !== undefined) return desc
      if (key in DEFAULTS) return { value: DEFAULTS[key], writable: true, enumerable: true, configurable: true }
      return undefined
    },
  })
}

const NATIVE_TOOL_NAMES = Object.keys(NATIVE_TOOL_SPECS)

export const Config = Schema === undefined ? undefined : Schema.object({
  toolName: Schema.string().default(DEFAULTS.toolName)
    .description('研究工具名（结构性设置，改动需重启）'),
  provider: Schema.string().default(DEFAULTS.provider)
    .description('wide_research 使用的子代理 provider（结构性设置，改动需重启）'),
  searchProviderId: Schema.string().default(DEFAULTS.searchProviderId)
    .description('web_search provider id（结构性设置；改动需同步 web 行 searchProvider）'),
  fetchProviderId: Schema.string().default(DEFAULTS.fetchProviderId)
    .description('web_fetch provider id（结构性设置；改动需同步 web 行 fetchProvider）'),
  nativeTools: Schema.array(Schema.union(NATIVE_TOOL_NAMES)).default([...DEFAULTS.nativeTools])
    .description('注册为原生一等工具的 argo 工具名；空数组关闭。默认 argo_search / argo_fetch')
    .volatile(),
  nativeTimeoutMs: Schema.number().default(DEFAULTS.nativeTimeoutMs)
    .description('原生工具单次进程超时（毫秒）')
    .volatile(),
  nativeMaxChars: Schema.number().default(DEFAULTS.nativeMaxChars)
    .description('原生工具返回文本上限（字符）')
    .volatile(),
  searchProviderEnabled: Schema.boolean().default(DEFAULTS.searchProviderEnabled)
    .description('宿主 web_search 走 argo provider')
    .volatile(),
  fetchProviderEnabled: Schema.boolean().default(DEFAULTS.fetchProviderEnabled)
    .description('宿主 web_fetch 走 argo provider（启用需在 web 行 pin fetchProvider: argo）')
    .volatile(),
  searchCommand: Schema.string().default(DEFAULTS.searchCommand)
    .description('argo MCP 入口命令（npx 或本机 python3）')
    .volatile(),
  searchArgs: Schema.array(Schema.string()).default([...DEFAULTS.searchArgs])
    .description('argo MCP 入口参数，一行一个')
    .volatile(),
  searchTimeoutMs: Schema.number().default(DEFAULTS.searchTimeoutMs)
    .description('单次搜索超时（毫秒）')
    .volatile(),
  searchIdleMs: Schema.number().default(DEFAULTS.searchIdleMs)
    .description('常驻 MCP 连接空闲回收（毫秒，0=不回收）')
    .volatile(),
  searchConcurrency: Schema.number().default(DEFAULTS.searchConcurrency)
    .description('MCP 连接池上限（并发搜索度）')
    .volatile(),
  fetchTimeoutMs: Schema.number().default(DEFAULTS.fetchTimeoutMs)
    .description('web_fetch seam 单次预算（毫秒）')
    .volatile(),
  fetchMaxChars: Schema.number().default(DEFAULTS.fetchMaxChars)
    .description('web_fetch seam 正文上限（字符）')
    .volatile(),
  defaultWorkers: Schema.number().default(DEFAULTS.defaultWorkers)
    .description('wide_research 默认并发 worker 数')
    .volatile(),
  maxWorkers: Schema.number().default(DEFAULTS.maxWorkers)
    .description('wide_research 并发 worker 上限')
    .volatile(),
  maxTracks: Schema.number().default(DEFAULTS.maxTracks)
    .description('wide_research 研究轨道上限')
    .volatile(),
  maxSourcesPerTrack: Schema.number().default(DEFAULTS.maxSourcesPerTrack)
    .description('每条轨道最多保留的信源数')
    .volatile(),
  workerMaxTokens: Schema.number().default(DEFAULTS.workerMaxTokens)
    .description('单个 worker 的输出 token 上限')
    .volatile(),
  synthesisMaxTokens: Schema.number().default(DEFAULTS.synthesisMaxTokens)
    .description('综合阶段输出 token 上限')
    .volatile(),
  reportDir: Schema.string().default(DEFAULTS.reportDir)
    .description('研究报告落盘目录')
    .volatile(),
  childToolAllow: Schema.array(Schema.string()).default([...DEFAULT_CHILD_TOOLS])
    .description('worker 可见工具白名单（allow 语义）')
    .volatile(),
  childToolDeny: Schema.array(Schema.string()).default([...DEFAULTS.childToolDeny])
    .description('worker 工具拒绝名单')
    .volatile(),
  keyRefs: Schema.array(Schema.string()).default([...DEFAULTS.keyRefs])
    .description('转发进 argo 子进程的宿主凭证引用名（环境变量名）')
    .volatile(),
  argoEnv: Schema.array(Schema.string()).default([...DEFAULTS.argoEnv])
    .description('argo 引擎开关（一行一个 KEY=VALUE，名字限 ARGO_ENV_SPECS 白名单）')
    .volatile(),
})

const CREDENTIAL_REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

function readHostCredentials(ctx) {
  try {
    const service = typeof ctx?.get === 'function' ? ctx.get('credentials') : ctx?.credentials
    return service !== undefined && service !== null && typeof service.resolve === 'function'
      ? service
      : undefined
  } catch {
    return undefined
  }
}

function runEffect(ctx, fn) {
  if (typeof ctx?.effect !== 'function') return undefined
  return ctx.effect(fn)
}

let hostCredentials = undefined
let activeConfig = DEFAULTS

export async function resolveArgoEnv(config = activeConfig, options = {}) {
  const overlay = argoEnvOverlay(config)
  const refs = asStringArray(config?.keyRefs, 20)
  if (refs.length > 0) {
    const provider = options.credentials ?? config?.credentials ?? hostCredentials
    if (provider !== undefined) {
      for (const ref of refs) {
        try {
          const resolved = await provider.resolve(ref)
          if (typeof resolved?.value === 'string' && resolved.value !== '') overlay[ref] = resolved.value
        } catch { /* ignore */ }
      }
    }
  }
  return Object.keys(overlay).length > 0 ? { ...process.env, ...overlay } : undefined
}

const asString = (value, fallback = '') => typeof value === 'string' ? value.trim() : fallback
const asStringArray = (value, maximum) => Array.isArray(value)
  ? value.filter(entry => typeof entry === 'string').map(entry => entry.trim()).filter(Boolean).slice(0, maximum)
  : []

// 注入执行器的动态环境变量解析逻辑
setEnvResolver(async (options, config) => {
  return resolveArgoEnv(config || activeConfig, options)
})

export function apply(ctx, providedConfig = {}) {
  const config = liveConfig(providedConfig)
  const toolName = asString(config.toolName, DEFAULTS.toolName)
  const providerName = asString(config.provider, DEFAULTS.provider)

  const nativeTools = asStringArray(config.nativeTools, 20)
  if (config.searchProviderEnabled !== false || nativeTools.length > 0) {
    if (typeof config.searchCommand !== 'string' || config.searchCommand === '') {
      throw new Error('argo-dsh: searchCommand must be a non-empty string when searchProviderEnabled')
    }
    if (!Array.isArray(config.searchArgs) || config.searchArgs.length === 0) {
      throw new Error('argo-dsh: searchArgs must be a non-empty array when searchProviderEnabled')
    }
    if (!Number.isFinite(config.searchTimeoutMs) || config.searchTimeoutMs <= 0) {
      throw new Error('argo-dsh: searchTimeoutMs must be a positive number')
    }
    if (!Number.isFinite(config.nativeTimeoutMs) || config.nativeTimeoutMs <= 0) {
      throw new Error('argo-dsh: nativeTimeoutMs must be a positive number')
    }
  }

  for (const ref of asStringArray(config.keyRefs, 20)) {
    if (!CREDENTIAL_REF_PATTERN.test(ref)) {
      throw new Error(`argo-dsh: keyRefs entry "${ref}" must be a POSIX-style identifier (e.g. DEEPSEEK_API_KEY)`)
    }
  }

  for (const entry of asStringArray(config.argoEnv, 64)) {
    const parsed = parseArgoEnvLine(entry)
    if (parsed === null) {
      throw new Error(`argo-dsh: argoEnv entry "${entry}" must be KEY=VALUE with a POSIX-style key`)
    }
    if (!ARGO_ENV_NAMES.has(parsed.name)) {
      throw new Error(`argo-dsh: argoEnv key "${parsed.name}" is not a known argo switch. Known: ${[...ARGO_ENV_NAMES].join(', ')}`)
    }
  }

  hostCredentials = readHostCredentials(ctx)
  activeConfig = config

  runEffect(ctx, () => () => {
    disposeSharedMcp()
    hostCredentials = undefined
    activeConfig = DEFAULTS
  })

  const childIds = new Set()
  const trackChild = (id) => {
    if (typeof id !== 'string' || id === '') return () => {}
    childIds.add(id)
    return () => { childIds.delete(id) }
  }
  if (typeof ctx.tools?.guard === 'function') {
    ctx.tools.guard((exec) => {
      const liveToolName = asString(config.toolName, DEFAULTS.toolName)
      if (exec?.name === liveToolName && exec?.agent && childIds.has(exec.agent.id)) {
        return `${liveToolName} is not available inside a research worker`
      }
      return undefined
    })
  }

  const web = ctx.get?.('web') ?? ctx.web
  if (web && typeof web.registerSearchProvider === 'function') {
    web.registerSearchProvider({
      id: asString(config.searchProviderId, DEFAULTS.searchProviderId),
      available: () => config.searchProviderEnabled !== false,
      search: (request, signal) => {
        const entry = resolveEntry(config)
        return searchViaArgoMCP(request.query, request.maxResults, signal, {
          command: entry.command,
          args: entry.args,
          timeoutMs: config.searchTimeoutMs,
          idleMs: config.searchIdleMs,
          max: config.searchConcurrency,
          config,
        })
      },
    })
    if (typeof web.registerFetchProvider === 'function') {
      web.registerFetchProvider({
        id: asString(config.fetchProviderId, DEFAULTS.fetchProviderId),
        available: () => config.fetchProviderEnabled !== false,
        fetch: (request, signal) => {
          const entry = resolveEntry(config)
          return fetchViaArgo(request.url, signal, {
            command: entry.command,
            args: entry.args,
            timeoutMs: config.fetchTimeoutMs,
            idleMs: config.searchIdleMs,
            max: config.searchConcurrency,
            maxChars: config.fetchMaxChars,
            config,
          })
        },
      })
    }
  }

  const nativeRegistrations = new Map()
  const syncNativeTools = () => {
    const desired = asStringArray(config.nativeTools, 20)
    for (const [kind, dispose] of nativeRegistrations) {
      if (desired.includes(kind)) continue
      try { dispose?.() } catch { /* ignore */ }
      nativeRegistrations.delete(kind)
    }
    for (const kind of desired) {
      if (nativeRegistrations.has(kind)) continue
      nativeRegistrations.set(kind, ctx.tools.register(makeNativeTool(config, kind)))
    }
  }
  syncNativeTools()
  if (typeof ctx.on === 'function') {
    runEffect(ctx, () => ctx.on('loader/volatile-update', syncNativeTools))
  }
  runEffect(ctx, () => () => {
    for (const dispose of nativeRegistrations.values()) {
      try { dispose?.() } catch { /* ignore */ }
    }
    nativeRegistrations.clear()
  })

  ctx.tools.register(createWideResearchTool(ctx, config, {
    toolName,
    providerName,
    trackChild,
    buildChildToolFilters,
  }))

  if (ctx.systemPrompt && typeof ctx.systemPrompt.section === 'function') {
    ctx.systemPrompt.section({
      name: `tool:${toolName}`,
      order: 116.6,
      text: () => `Use ${toolName} for broad factual research that needs independent source checks, contrasting viewpoints, or a citation-ready evidence ledger. State a bounded question and scope. Do not use it for simple answers, personal-data collection, speculative brainstorming, or irreversible actions. Treat the report as evidence synthesis, inspect key sources for high-stakes decisions, and disclose uncertainty.`,
    })
  }

  try {
    ctx.effect(() => {
      const settings = typeof ctx.get === 'function' ? ctx.get('settings') : undefined
      if (!settings || typeof settings.configure !== 'function') return undefined
      return settings.configure({ auto: false }, ctx.fiber)
    })
  } catch { /* ignore */ }

  return { childIds, syncNativeTools }
}

export {
  normalizeTracks,
  stageTracks,
  evaluateGates,
  normalizeSource,
  normalizeSourceUrl,
  buildLocalSources,
  parseJsonArray,
  parseJsonObject,
  extractValues,
  nativeViaMcpPool,
  runNativeCall,
  resolveNativeSpawn,
  makeNativeTool,
  disposeSharedMcp,
  startStructured,
  providerSupportsStructured,
  fetchViaArgo,
  mapArgoToWebResult,
  mapArgoToWebFetch,
  classifyFetchError,
  searchViaArgoMCP,
  persistReport,
  getMcpPoolStats,
  resetMcpPool,
}
