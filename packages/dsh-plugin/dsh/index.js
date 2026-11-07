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

import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { NATIVE_TOOLS as NATIVE_TOOL_SPECS } from './native-tools.mjs'
import {
  DEFAULT_CHILD_TOOLS,
  buildChildToolFilters,
  ARGO_ENV_SPECS,
  ARGO_ENV_NAMES,
  parseArgoEnvLine,
  resolveArgoEnv,
  setHostCredentials,
  setActiveConfig,
  CREDENTIAL_REF_PATTERN,
  asStringArray,
} from './specs.js'

const require = createRequire(import.meta.url)

import {
  DEFAULTS,
  PLUGIN_VERSION as RUNNER_PLUGIN_VERSION,
  setEnvResolver,
  resetEnvResolver,
  makeNativeTool,
  disposeSharedMcp,
  fetchViaArgo,
  mapArgoToWebResult,
  classifyFetchError,
} from './runner.js'

import {
  createWideResearchTool,
} from './wide-research.js'

import {
  getRouteHealth,
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
    const parsed = require('../package.json')
    if (typeof parsed?.version === 'string' && parsed.version !== '') return parsed.version
  } catch { /* 打包裁剪/权限异常：回退，不阻断插件加载 */ }
  return RUNNER_PLUGIN_VERSION || '0.0.0-unknown'
}

const PLUGIN_VERSION = resolvePluginVersion()

export const name = 'wide-research'
export const inject = ['tools', 'subagents', 'systemPrompt', 'web']

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

const asString = (value, fallback = '') => typeof value === 'string' ? value.trim() : fallback

// 注入执行器的动态环境变量解析逻辑
setEnvResolver(async (options, config) => {
  return resolveArgoEnv(config, options)
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

  const hostCredentials = readHostCredentials(ctx)
  setHostCredentials(hostCredentials)
  setActiveConfig(config)

  runEffect(ctx, () => () => {
    disposeSharedMcp()
    setHostCredentials(undefined)
    setActiveConfig(DEFAULTS)
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
          // issue #41：显式带上宿主凭证服务，否则池内连接 envResolver 解析
          // keyRefs 时 provider 为 undefined，所有需 key 引擎静默 skipped。
          credentials: hostCredentials,
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
            // issue #41：与 search 同理，fetch 池连接也要带上凭证服务。
            credentials: hostCredentials,
          })
        },
      })
    }
  }

  const nativeRegistrations = new Map()
  // 路由观测推送：原生工具执行后读 argo 使用日志，经 remote 事件推给设置面板。
  // 失败静默（诊断是增值，不是依赖）；无 remote 事件总线时跳过。
  const emitRouteHealth = async () => {
    try {
      if (!ctx.remote || typeof ctx.remote.$emit !== 'function') return
      const data = await getRouteHealth()
      if (Array.isArray(data.samples) && data.samples.length > 0) {
        ctx.remote.$emit('route-health/updated', data)
      }
    } catch { /* ignore */ }
  }

  const syncNativeTools = () => {
    const desired = asStringArray(config.nativeTools, 20)
    for (const [kind, dispose] of nativeRegistrations) {
      if (desired.includes(kind)) continue
      try { dispose?.() } catch { /* ignore */ }
      nativeRegistrations.delete(kind)
    }
    for (const kind of desired) {
      if (nativeRegistrations.has(kind)) continue
      const tool = makeNativeTool(config, kind)
      const originalExecute = tool.execute
      if (typeof originalExecute === 'function') {
        tool.execute = async (args, exec) => {
          const result = await originalExecute(args, exec)
          emitRouteHealth()
          return result
        }
      }
      nativeRegistrations.set(kind, ctx.tools.register(tool))
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
    resetEnvResolver()
    disposeSharedMcp()
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
