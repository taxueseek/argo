#!/usr/bin/env node
/**
 * npx / npm 入口：启动 Argo MCP Server。
 * 需要本机 Python 3.10+；可用 ARGO_PYTHON / PYTHON 指定解释器。
 */
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

function resolvePython() {
  if (process.env.ARGO_PYTHON) return process.env.ARGO_PYTHON;
  if (process.env.PYTHON) return process.env.PYTHON;
  // 优先 PATH 中的 python3，避免写死 /usr/bin
  return process.platform === 'win32' ? 'python' : 'python3';
}

const PYTHON = resolvePython();
const SCRIPT = path.join(__dirname, '..', 'scripts', 'mcp_server.py');

if (!fs.existsSync(SCRIPT)) {
  console.error(`argo-search: 找不到 MCP 入口 ${SCRIPT}`);
  process.exit(1);
}

// PEP 540 UTF-8 模式：Windows 默认 GBK 控制台/文件编码会打崩含中文的
// JSON 读取与 stderr 输出，显式打开 UTF-8 模式（Python 3.7+ 支持）。
const env = { ...process.env, PYTHONUTF8: process.env.PYTHONUTF8 || '1' };
const proc = spawn(PYTHON, [SCRIPT], {
  stdio: ['pipe', 'pipe', 'inherit'],
  env,
});

process.stdin.pipe(proc.stdin);
proc.stdout.pipe(process.stdout);

proc.on('error', (err) => {
  console.error(`argo-search: 无法启动 Python (${PYTHON}): ${err.message}`);
  console.error('请确认已安装 Python 3.10+，并已执行: pip install pyyaml');
  process.exit(1);
});

proc.on('exit', (code) => {
  process.exit(code || 0);
});
