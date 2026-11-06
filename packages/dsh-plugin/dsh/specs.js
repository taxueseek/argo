import { NATIVE_TOOLS as NATIVE_TOOL_SPECS } from "./native-tools.mjs"
import { DEFAULTS } from "./runner.js"

const asString = (value, fallback = "") => typeof value === "string" ? value.trim() : fallback
const asStringArray = (value, maximum) => Array.isArray(value)
  ? value.filter(entry => typeof entry === "string").map(entry => entry.trim()).filter(Boolean).slice(0, maximum)
  : []

const CREDENTIAL_REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

let hostCredentials = undefined
let activeConfig = DEFAULTS

export function setHostCredentials(creds) {
  hostCredentials = creds
}

export function setActiveConfig(config) {
  activeConfig = config
}

export function getHostCredentials() {
  return hostCredentials
}

export function getActiveConfig() {
  return activeConfig
}

const MCP_CHILD_TOOLS = Object.keys(NATIVE_TOOL_SPECS).map(name => `mcp__argo__${name}`)
export const DEFAULT_CHILD_TOOLS = Object.freeze([
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

export const ARGO_ENV_NAMES = new Set(ARGO_ENV_SPECS.map(spec => spec.name))
export const ARGO_ENV_BY_NAME = new Map(ARGO_ENV_SPECS.map(spec => [spec.name, spec]))

export function parseArgoEnvLine(line) {
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



export async function resolveArgoEnv(config, options = {}) {
  const cfg = config ?? activeConfig
  const overlay = argoEnvOverlay(cfg)
  const refs = asStringArray(cfg?.keyRefs, 20)
  if (refs.length > 0) {
    const provider = options.credentials ?? cfg?.credentials ?? hostCredentials
    if (provider !== undefined) {
      for (const ref of refs) {
        try {
          const resolved = await provider.resolve(ref)
          if (typeof resolved?.value === "string" && resolved.value !== "") {
            overlay[ref] = resolved.value
          }
        } catch { /* ignore */ }
      }
    }
  }
  return Object.keys(overlay).length > 0 ? { ...process.env, ...overlay } : undefined
}
