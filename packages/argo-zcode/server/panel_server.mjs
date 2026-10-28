#!/usr/bin/env node
/**
 * argo-zcode 面板服务（零 npm 依赖，stdio MCP）。
 *
 * 角色：Argo 控制面板的通信后端。设计要点：
 * - tools/list 恒返回空数组：面板管理工具只经页面 callServerTool 受理，
 *   不进模型工具列表、零常驻 token；argo 的 19 个取证工具不在此挂载
 *   （ZCode 下模型走 skill+CLI，见 skills/argo）。
 * - 引擎清单/开关写入经 py_bridge.py 复用 argo 本体加载器；密钥经 env 文件。
 */
import { spawn } from "node:child_process";
import { readFile, writeFile, chmod, stat } from "node:fs/promises";
import { createInterface } from "node:readline";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SERVER_ROOT = join(PLUGIN_ROOT, "server");
const PANEL_URI = "ui://argo/panel.html";
const PANEL_MIME = "text/html;profile=mcp-app";
const SERVER_INFO = { name: "argo-panel", version: "2.10.0" };

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

async function handleRequest(msg) {
  switch (msg.method) {
    case "initialize": {
      const p = msg.params ?? {};
      return {
        protocolVersion: p.protocolVersion ?? "2025-06-18",
        capabilities: { tools: {}, resources: {} },
        serverInfo: SERVER_INFO,
      };
    }
    case "tools/list":
      return { tools: [] }; // 模型侧零暴露（设计约束，勿改）
    case "tools/call":
      return await callPanelTool(msg.params ?? {});
    case "resources/list":
      return {
        resources: [{ uri: PANEL_URI, name: "panel", mimeType: PANEL_MIME }],
      };
    case "resources/read": {
      if (msg.params?.uri !== PANEL_URI) throw err(-32602, `未知资源: ${msg.params?.uri}`);
      const text = await readFile(join(PLUGIN_ROOT, "ui", "panel.html"), "utf8");
      return { contents: [{ uri: PANEL_URI, mimeType: PANEL_MIME, text }] };
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
    const child = spawn(process.env.ARGO_PYTHON || "python3", [join(SERVER_ROOT, "py_bridge.py"), cmd, ...cmdArgs], {
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
  const [engines, env] = await Promise.all([
    bridge("list-engines", [], 30000),
    readEnvFile().catch(() => ({ path: null, keys: [] })),
  ]);
  return {
    engines: engines.engines,
    env_file: env.path,
    configured_keys: env.keys,
    hint: "引擎开关写入 config.local.yaml（argo 本机覆盖层，深度合并，优先级最高）；密钥写入 env 文件，均即时生效",
  };
}

// ── env 文件（dotenv 格式行操作；值永不回读展示）────────────────────────────

async function envFilePath() {
  const r = await bridge("env-path", [], 10000);
  if (!r.env_file) throw err(1, "argo 未报告 env 文件路径");
  return r.env_file;
}

async function readEnvFile() {
  const path = await envFilePath();
  let text = "";
  try {
    text = await readFile(path, "utf8");
  } catch {
    return { path, keys: [] };
  }
  const keys = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^([A-Z][A-Z0-9_]+)=/);
    if (m) keys.push(m[1]);
  }
  return { path, keys };
}

async function setEnvLine(key, value) {
  const path = await envFilePath();
  let lines = [];
  try {
    lines = (await readFile(path, "utf8")).split("\n");
  } catch {
    lines = ["# argo 引擎密钥唯一真源（勿入库勿分享）"];
  }
  const filtered = lines.filter((l) => !l.startsWith(`${key}=`));
  if (value !== null) filtered.push(`${key}=${value}`);
  const text = filtered.join("\n").replace(/\n{3,}/g, "\n\n") + "\n";
  await writeFile(path, text, { encoding: "utf8", mode: 0o600 });
  await chmod(path, 0o600).catch(() => {});
  return { ok: true, key, action: value === null ? "removed" : "set", path };
}
