#!/usr/bin/env node
/**
 * argo-zcode 面板服务（零 npm 依赖，stdio MCP）。
 *
 * 角色：Argo 控制面板的通信后端。设计要点：
 * - tools/list 只登记面板自身的工具并标 visibility=["app"]：ZCode 的 MCP App
 *   桥在转发 callServerTool 前会核对 tools/list，空列表会被直接拒绝；app-only
 *   标记保证这些工具不进模型上下文。argo 的取证工具不在此挂载（模型走 skill+CLI）。
 * - 引擎清单/开关写入经 py_bridge.py 复用 argo 本体加载器；密钥经 env 文件。
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { readFile, writeFile, chmod, rename, stat } from "node:fs/promises";
import { createInterface } from "node:readline";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SERVER_ROOT = join(PLUGIN_ROOT, "server");
const PANEL_URI = "ui://argo/panel.html";
const PANEL_MIME = "text/html;profile=mcp-app";
const SERVER_NAME = "argo-panel";
const SERVER_INFO = { name: SERVER_NAME, version: "2.10.1" };

// 宿主 resources/read 只回文本，页面里的相对 import 解析不了：vendor 在服务端
// 内联进同一段脚本后返回。源文件保持可读可 diff，内联只做一次并缓存。
const PANEL_IMPORT = 'import { App } from "./vendor/ext-apps.mjs";';
const VENDOR_EXPORT = /export\{([^}]*)\};?\s*$/;

function inlineVendor(vendor) {
  const tail = VENDOR_EXPORT.exec(vendor);
  if (!tail) throw err(1, "ui/vendor/ext-apps.mjs 末尾缺少 export 语句，内联规则需更新");
  const pairs = tail[1]
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const [local, exported = local] = item.split(" as ").map((part) => part.trim());
      return `${exported}:${local}`;
    });
  const body = `${vendor.slice(0, tail.index)}globalThis.__EXT_APPS__={${pairs.join(",")}};`;
  if (body.toLowerCase().includes("</script")) {
    throw err(1, "vendor 含 </script>，内联前须转义");
  }
  return (
    "/* ===== vendored @modelcontextprotocol/ext-apps — inlined ===== */\n" +
    body +
    "\n/* ===== end vendored sdk ===== */\n" +
    "const { App } = globalThis.__EXT_APPS__;"
  );
}

let cachedPanel = null;
async function panelHtml() {
  if (cachedPanel !== null) return cachedPanel;
  let html = await readFile(join(PLUGIN_ROOT, "ui", "panel.html"), "utf8");
  if (html.includes(PANEL_IMPORT)) {
    const vendor = await readFile(join(PLUGIN_ROOT, "ui", "vendor", "ext-apps.mjs"), "utf8");
    // 替换值必须走函数形式：vendor 里带 $ 的序列会被当成替换模式。
    html = html.replace(PANEL_IMPORT, () => inlineVendor(vendor));
  }
  cachedPanel = html;
  return html;
}

// 版本只在插件清单里维护一份：升级时不再和 plugin.json 漂移。
let cachedVersion = null;
async function serverVersion() {
  if (cachedVersion !== null) return cachedVersion;
  try {
    const manifest = JSON.parse(
      await readFile(join(PLUGIN_ROOT, ".zcode-plugin", "plugin.json"), "utf8")
    );
    cachedVersion = String(manifest.version || "0.0.0");
  } catch {
    cachedVersion = "0.0.0";
  }
  return cachedVersion;
}

// py_bridge 需要一个带 pyyaml 的 Python。宿主 GUI 的 PATH 与终端不一致，
// 所以按 ARGO_PYTHON → argo 入口 shebang → PATH → 平台常见路径依次探测，
// 每个候选都实际 import yaml 验证一次。
let cachedPython = null;
function argoEntry() {
  for (const dir of (process.env.PATH || "").split(delimiter)) {
    if (!dir) continue;
    for (const name of ["argo", "argo.exe", "argo.cmd"]) {
      const candidate = join(dir, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  return "";
}

function shebangPython() {
  const entry = argoEntry();
  if (!entry) return "";
  try {
    const first = readFileSync(entry, "utf8").split("\n")[0].trim();
    if (!first.startsWith("#!")) return "";
    const parts = first.slice(2).trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "";
    if (parts[0].endsWith("/env") && parts[1]) return parts[1];
    return parts[0];
  } catch {
    return "";
  }
}

function resolvePython() {
  if (cachedPython) return cachedPython;
  const candidates = [];
  if (process.env.ARGO_PYTHON) candidates.push(process.env.ARGO_PYTHON);
  const shebang = shebangPython();
  if (shebang) candidates.push(shebang);
  candidates.push("python3", "python");
  if (process.platform === "darwin") {
    candidates.push("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3");
  }
  if (process.platform === "win32") candidates.push("py");
  for (const candidate of candidates) {
    const probe = spawnSync(
      candidate,
      ["-c", "import sys, yaml; sys.stdout.write(sys.version.split()[0])"],
      { timeout: 5000, encoding: "utf8" }
    );
    if (probe.status === 0) {
      cachedPython = candidate;
      return candidate;
    }
  }
  cachedPython = process.env.ARGO_PYTHON || "python3";
  return cachedPython;
}

// ── JSON-RPC over stdio（换行分帧）───────────────────────────────────────────

const pending = new Map(); // id → 在途请求计数（退出前须清零）
let stdinEnded = false;
const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const text = line.trim();
  if (!text) return;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    return; // 非法行忽略，不断流
  }
  if (msg && typeof msg.id !== "undefined" && msg.method) {
    pending.set(msg.id, true);
    handleRequest(msg).then(
      (result) => {
        send({ jsonrpc: "2.0", id: msg.id, result });
        settle(msg.id);
      },
      (error) => {
        send({
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: error.code ?? -32603, message: String(error.message ?? error) },
        });
        settle(msg.id);
      }
    );
  } else if (msg && typeof msg.id !== "undefined" && msg.error !== undefined) {
    // 客户端对服务端请求的响应——本服务不发起服务端请求，忽略
  } else {
    // 通知（notifications/initialized 等）——无需应答
  }
});
process.stdin.on("end", () => {
  stdinEnded = true;
  maybeExit(); // 等在途请求清零再退，防止掐死慢调用（如 get_panel 的子进程链）
});
function settle(id) {
  pending.delete(id);
  maybeExit();
}
function maybeExit() {
  if (stdinEnded && pending.size === 0) process.exit(0);
}
process.once("SIGTERM", () => process.exit(0));
process.once("SIGINT", () => process.exit(0));

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

// ZCode 的 MCP App 桥在转发页面 callTool 前会先查 tools/list：工具必须存在且
// visibility 含 "app"，否则直接拒绝（日志：MCP tool "x" is not available）。
// 因此不能返回空列表。这里把面板工具标成 app-only：模型侧按 "model" 过滤，
// 工具面不进模型上下文；visibility 同时写在顶层与 _meta.ui（宿主两种读法都兼容）。
const PANEL_TOOLS = [
  {
    name: "get_panel",
    title: "读取面板数据",
    description: "面板专用：引擎状态、密钥归属、运行时元信息。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    visibility: ["app"],
    _meta: { ui: { visibility: ["app"] } },
  },
  {
    name: "get_diagnostics",
    title: "运行诊断",
    description: "面板专用：版本、路径自检、daemon、预算与文件权限。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    visibility: ["app"],
    _meta: { ui: { visibility: ["app"] } },
  },
  {
    name: "set_engine",
    title: "切换引擎开关",
    description: "面板专用：写入 config.local.yaml 的 engines.<name>.enabled。",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "引擎 ID" },
        enabled: { type: "boolean", description: "true 启用，false 停用" },
      },
      required: ["name", "enabled"],
      additionalProperties: false,
    },
    visibility: ["app"],
    _meta: { ui: { visibility: ["app"] } },
  },
  {
    name: "set_env",
    title: "写入密钥",
    description: "面板专用：写入 argo env 文件（值不回显）。",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string", description: "全大写环境变量名" },
        value: { type: "string", description: "密钥值" },
      },
      required: ["key", "value"],
      additionalProperties: false,
    },
    visibility: ["app"],
    _meta: { ui: { visibility: ["app"] } },
  },
  {
    name: "unset_env",
    title: "清除密钥",
    description: "面板专用：从 argo env 文件移除指定 key。",
    inputSchema: {
      type: "object",
      properties: { key: { type: "string", description: "全大写环境变量名" } },
      required: ["key"],
      additionalProperties: false,
    },
    visibility: ["app"],
    _meta: { ui: { visibility: ["app"] } },
  },
  {
    name: "apply_preset",
    title: "应用工作流预设",
    description: "面板专用：一次写入一组 argo 运行时开关（极速 / 平衡 / 深度）。",
    inputSchema: {
      type: "object",
      properties: {
        preset: { type: "string", enum: ["fast", "standard", "deep"], description: "预设名" },
      },
      required: ["preset"],
      additionalProperties: false,
    },
    visibility: ["app"],
    _meta: { ui: { visibility: ["app"] } },
  },
];

// 工作流预设：与 DSH 插件同一组 argo 运行时开关，写进同一个 env 文件。
const PRESETS = {
  fast: {
    ARGO_TFIDF_RERANK: "0",
    ARGO_NUMBER_CROSSCHECK: "0",
    ARGO_PAID_SEARCH: "off",
    ARGO_ADAPTIVE_GRACE: "1",
    ARGO_SERIAL_STAGGER_S: "0.5",
  },
  standard: {
    ARGO_TFIDF_RERANK: "1",
    ARGO_NUMBER_CROSSCHECK: "1",
    ARGO_MINHASH_DEDUPE: "1",
    ARGO_PAID_SEARCH: "auto",
    ARGO_ADAPTIVE_GRACE: "1",
    ARGO_SERIAL_STAGGER_S: "0.8",
  },
  deep: {
    ARGO_TFIDF_RERANK: "1",
    ARGO_NUMBER_CROSSCHECK: "1",
    ARGO_MINHASH_DEDUPE: "1",
    ARGO_SEMANTIC_EVIDENCE: "1",
    ARGO_PAID_SEARCH: "auto",
    ARGO_ADAPTIVE_GRACE: "1",
  },
};

async function handleRequest(msg) {
  switch (msg.method) {
    case "initialize": {
      const p = msg.params ?? {};
      return {
        protocolVersion: p.protocolVersion ?? "2025-06-18",
        capabilities: { tools: {}, resources: {} },
        serverInfo: { name: SERVER_NAME, version: await serverVersion() },
      };
    }
    case "tools/list":
      return { tools: PANEL_TOOLS };
    case "tools/call":
      return await callPanelTool(msg.params ?? {});
    case "resources/list":
      return {
        resources: [{ uri: PANEL_URI, name: "panel", mimeType: PANEL_MIME }],
      };
    case "resources/read": {
      if (msg.params?.uri !== PANEL_URI) throw err(-32602, `未知资源: ${msg.params?.uri}`);
      return { contents: [{ uri: PANEL_URI, mimeType: PANEL_MIME, text: await panelHtml() }] };
    }
    case "ping":
      return {};
    default:
      throw err(-32601, `未知方法: ${msg.method}`);
  }
}

function err(code, message) {
  const e = new Error(message);
  e.code = code;
  return e;
}

// ── 面板工具（仅页面可调）────────────────────────────────────────────────────

async function callPanelTool({ name, arguments: args = {} }) {
  try {
    let result;
    switch (name) {
      case "get_panel":
        result = await getPanel();
        break;
      case "get_diagnostics":
        result = await bridge("diagnostics", [], 40000);
        break;
      case "set_engine":
        requireString(args.name, "name");
        result = await bridge("set-engine", [String(args.name), args.enabled ? "true" : "false"]);
        break;
      case "set_env": {
        const key = requireEnvKey(args.key);
        const value = requireString(args.value, "value");
        if (/[\r\n]/.test(value)) throw err(-32602, "value 不能含换行");
        result = await setEnvLine(key, value);
        break;
      }
      case "unset_env":
        result = await setEnvLine(requireEnvKey(args.key), null);
        break;
      case "apply_preset": {
        const preset = String(args.preset || "");
        const values = PRESETS[preset];
        if (!values) throw err(-32602, `未知预设: ${preset}`);
        result = await setEnvLines(values);
        break;
      }
      default:
        throw err(-32601, `未知面板工具: ${name}`);
    }
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  } catch (e) {
    return { content: [{ type: "text", text: String(e.message ?? e) }], isError: true };
  }
}

function requireString(v, label) {
  if (typeof v !== "string" || !v.trim()) throw err(-32602, `缺参数 ${label}`);
  return v.trim();
}

function requireEnvKey(v) {
  const key = requireString(v, "key");
  if (!/^[A-Z][A-Z0-9_]{2,}$/.test(key)) {
    throw err(-32602, "key 须为全大写下划线形式（如 EXA_API_KEY）");
  }
  return key;
}

// ── py_bridge 子进程 ─────────────────────────────────────────────────────────

function bridge(cmd, cmdArgs = [], timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const child = spawn(resolvePython(), [join(SERVER_ROOT, "py_bridge.py"), cmd, ...cmdArgs], {
      cwd: SERVER_ROOT,
      env: process.env,
      timeout: timeoutMs,
    });
    let out = "", errText = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (errText += d));
    child.on("error", reject);
    child.on("close", (code) => {
      const text = out.trim();
      if (code !== 0) {
        reject(err(1, `py_bridge ${cmd} 失败(code ${code})：${errText.trim() || text || "无输出"}`));
        return;
      }
      try {
        resolve(JSON.parse(text));
      } catch {
        reject(err(1, `py_bridge ${cmd} 输出不是 JSON：${text.slice(0, 200)}`));
      }
    });
  });
}

async function getPanel() {
  // panel-data 一次给全：引擎状态（与 argo --detail 同口径）、密钥归属与来源、
  // 运行时元信息。密钥值不回传，只回 key 名与来源层。
  return bridge("panel-data", [], 40000);
}

// ── env 文件（dotenv 格式行操作；值永不回读展示）────────────────────────────

let cachedEnvPath = null;
async function envFilePath() {
  if (cachedEnvPath) return cachedEnvPath;
  const r = await bridge("env-path", [], 10000);
  if (!r.env_file) throw err(1, "argo 未报告 env 文件路径");
  cachedEnvPath = r.env_file;
  return cachedEnvPath;
}

async function writeAtomic600(path, text) {
  const tmp = `${path}.tmp.${process.pid}.${Date.now()}`;
  await writeFile(tmp, text, { encoding: "utf8", mode: 0o600 });
  await chmod(tmp, 0o600).catch(() => {});
  await rename(tmp, path);
}

async function setEnvLines(values) {
  const path = await envFilePath();
  let lines = [];
  try {
    lines = (await readFile(path, "utf8")).split("\n");
  } catch {
    lines = ["# argo 引擎密钥唯一真源（勿入库勿分享）"];
  }
  const keys = Object.keys(values);
  const matchers = keys.map((key) => new RegExp(`^\\s*(?:export\\s+)?${key}\\s*=`));
  const filtered = lines.filter((line) => !matchers.some((matcher) => matcher.test(line)));
  for (const key of keys) filtered.push(`${key}=${values[key]}`);
  const text = filtered.join("\n").replace(/\n{3,}/g, "\n\n") + "\n";
  await writeAtomic600(path, text);
  return { ok: true, path, keys };
}

async function setEnvLine(key, value) {
  const path = await envFilePath();
  let lines = [];
  try {
    lines = (await readFile(path, "utf8")).split("\n");
  } catch {
    lines = ["# argo 引擎密钥唯一真源（勿入库勿分享）"];
  }
  // env 文件可能写 `export KEY=...`（历史写法与手工维护都常见）：
  // 保存/清除必须同口径命中，否则会留下旧行让面板状态与文件内容打架。
  const matcher = new RegExp(`^\\s*(?:export\\s+)?${key}\\s*=`);
  const filtered = lines.filter((l) => !matcher.test(l));
  if (value !== null) filtered.push(`${key}=${value}`);
  const text = filtered.join("\n").replace(/\n{3,}/g, "\n\n") + "\n";
  await writeAtomic600(path, text);
  return { ok: true, key, action: value === null ? "removed" : "set", path };
}
