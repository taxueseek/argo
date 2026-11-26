#!/usr/bin/env node
/**
 * npx / npm 入口：启动 Argo MCP Server 或单发 CLI 工具。
 * 运行时解析优先级（零手动配置优先）：
 *   1. ARGO_PYTHON / PYTHON 环境变量显式覆盖
 *   2. 仓库或安装目录自带 .venv
 *   3. PATH 中已安装 Python >= 3.9 且具备 PyYAML 的解释器（跳过 Windows Store 假桩）
 *   4. PATH 中 uv 自动兜底：`uv run --no-project --with pyyaml>=6.0 --python >=3.9 python`
 *   5. 全不可用时输出跨平台一键修复指引
 */
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const REPO_ROOT = path.join(__dirname, '..');
const PY_PROBE_CODE =
  'import sys\n' +
  'if sys.version_info < (3, 9): raise SystemExit(3)\n' +
  'try:\n' +
  '    import yaml\n' +
  'except Exception:\n' +
  '    raise SystemExit(4)\n';
const UV_PREFIX_ARGS = ['run', '--no-project', '--with', 'pyyaml>=6.0', '--python', '>=3.9', 'python'];

/**
 * 子进程的退出码。
 *
 * `code === null` 表示子进程是被**信号**杀死的（SIGPIPE/SIGKILL/OOM killer…），
 * 此时 node 把信号名放在第二个参数里。按 shell 惯例折算成 128 + 信号号。
 */
function exitCodeOf(code, signal) {
  if (code !== null && code !== undefined) return code;
  const num = (os.constants.signals || {})[signal];
  return typeof num === 'number' ? 128 + num : 128;
}

function defaultProbe(cmd, args, env) {
  try {
    const res = spawnSync(cmd, args, {
      env,
      stdio: 'ignore',
      timeout: 4000,
      windowsHide: true,
    });
    if (res.error) return { ok: false, error: res.error };
    return { ok: res.status === 0, status: res.status };
  } catch (error) {
    return { ok: false, error };
  }
}

/**
 * 解析可用的 Python / uv 运行时。纯函数，可注入 probe/existsSync 单测。
 */
function resolveRuntime(options = {}) {
  const env = options.env || process.env;
  const platform = options.platform || process.platform;
  const repoRoot = options.repoRoot || REPO_ROOT;
  const existsSync = options.existsSync || fs.existsSync;
  const probe = options.probe || ((cmd, args) => defaultProbe(cmd, args, env));

  if (env.ARGO_PYTHON) {
    return { ok: true, command: env.ARGO_PYTHON, prefixArgs: [], source: 'env' };
  }
  if (env.PYTHON) {
    return { ok: true, command: env.PYTHON, prefixArgs: [], source: 'env' };
  }

  const venvPython = platform === 'win32'
    ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
    : path.join(repoRoot, '.venv', 'bin', 'python');
  if (existsSync(venvPython)) {
    return { ok: true, command: venvPython, prefixArgs: [], source: 'venv' };
  }

  const candidates = platform === 'win32'
    ? [
        { command: 'python', prefixArgs: [] },
        { command: 'py', prefixArgs: ['-3'] },
        { command: 'python3', prefixArgs: [] },
      ]
    : [
        { command: 'python3', prefixArgs: [] },
        { command: 'python', prefixArgs: [] },
      ];

  let missingYamlCmd = null;
  let oldPythonCmd = null;
  for (const cand of candidates) {
    const r = probe(cand.command, [...cand.prefixArgs, '-c', PY_PROBE_CODE]);
    if (r && r.ok) {
      return { ok: true, command: cand.command, prefixArgs: cand.prefixArgs, source: 'path' };
    }
    if (r && r.status === 4 && !missingYamlCmd) {
      missingYamlCmd = [cand.command, ...cand.prefixArgs].join(' ');
    } else if (r && r.status === 3 && !oldPythonCmd) {
      oldPythonCmd = [cand.command, ...cand.prefixArgs].join(' ');
    }
  }

  // 自动兜底 uv：即使本机未装 Python 或缺 PyYAML，也能零配置即用即走
  const uvProbe = probe('uv', ['--version']);
  if (uvProbe && uvProbe.ok) {
    return { ok: true, command: 'uv', prefixArgs: [...UV_PREFIX_ARGS], source: 'uv' };
  }

  if (missingYamlCmd) {
    return { ok: false, reason: 'missing_yaml', detectedPython: missingYamlCmd };
  }
  if (oldPythonCmd) {
    return { ok: false, reason: 'python_too_old', detectedPython: oldPythonCmd };
  }
  return { ok: false, reason: 'missing_python' };
}

function formatMissingRuntimeHelp(result, platform = process.platform) {
  const uvInstall = platform === 'win32'
    ? 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    : 'curl -LsSf https://astral.sh/uv/install.sh | sh';
  if (result && result.reason === 'missing_yaml') {
    const py = result.detectedPython || (platform === 'win32' ? 'python' : 'python3');
    return [
      `argo-search: 检测到 ${py}，但缺少依赖 PyYAML。`,
      `一键修复（任选其一）：`,
      `  1) ${py} -m pip install pyyaml`,
      `  2) 安装 uv 后自动零配置运行：${uvInstall}`,
    ].join('\n');
  }
  if (result && result.reason === 'python_too_old') {
    return [
      `argo-search: 检测到 ${result.detectedPython} 版本低于 3.9（需要 Python 3.9+ 与 PyYAML）。`,
      `推荐安装 uv 自动管理兼容运行时：`,
      `  ${uvInstall}`,
    ].join('\n');
  }
  return [
    'argo-search: 未检测到可用的 Python 3.9+ 运行时（已跳过无效或未安装解释器）。',
    '推荐方式（装完即用，自动补齐 Python 3.9+ 与 PyYAML）：',
    `  ${uvInstall}`,
    '或手动安装 Python 3.9+ 后执行：pip install pyyaml',
  ].join('\n');
}

function main() {
  const SCRIPT = path.join(REPO_ROOT, 'scripts', 'mcp_server.py');

  if (!fs.existsSync(SCRIPT)) {
    console.error(`argo-search: 找不到 MCP 入口 ${SCRIPT}`);
    process.exit(1);
  }

  // PEP 540 UTF-8 模式：Windows 默认 GBK 控制台/文件编码会打崩含中文的
  // JSON 读取与 stderr 输出，显式打开 UTF-8 模式（Python 3.7+ 支持）。
  const env = { ...process.env, PYTHONUTF8: process.env.PYTHONUTF8 || '1' };
  const argv = process.argv.slice(2);

  // 快速参数与帮助支持（无需探测 Python）
  if (argv[0] === '-v' || argv[0] === '--version') {
    try {
      const pkg = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8'));
      console.log(pkg.version);
    } catch (_) {
      console.log('unknown');
    }
    process.exit(0);
  }

  if (argv[0] === '-h' || argv[0] === '--help') {
    console.log(`Argo Search — 专为 Agent 打造的统一搜索与证据核验工具

用法:
  argo-search                         启动 MCP Stdio 服务（默认）
  argo-search mcp                     显式启动 MCP Stdio 服务
  argo-search call <tool> [json-args] CLI 单发调用指定 MCP 工具
  argo-search -v, --version           查看当前版本
  argo-search -h, --help              查看本帮助信息

环境依赖:
  需要 Python 3.9+ 及 PyYAML；若本机已安装 uv，缺省时会自动通过 uv 零配置拉起。
  零门槛即用即走: npx -y argo-search
`);
    process.exit(0);
  }

  const runtime = resolveRuntime({ env });
  if (!runtime.ok) {
    console.error(formatMissingRuntimeHelp(runtime));
    process.exit(1);
  }

  // 子命令分发：`argo-search call <tool> '<json-args>'` —— CLI 单发，与 stdio
  // 服务共用 mcp_server.py --call / execute_tool 入口（同引擎同压缩），供 DSH
  // 原生工具等调用方免 MCP 帧协议复用；无子命令或 'mcp' 保持原行为：启动 stdio 服务。
  if (argv[0] === 'call') {
    const tool = argv[1];
    if (!tool) {
      console.error('usage: argo-search call <tool> [json-args]');
      process.exit(2);
    }
    const cli = spawn(runtime.command, [...runtime.prefixArgs, SCRIPT, '--call', tool, argv[2] ?? '{}'], {
      stdio: 'inherit',
      env,
    });
    cli.on('error', (err) => {
      console.error(`argo-search: 无法启动运行时 (${runtime.command}): ${err.message}`);
      process.exit(1);
    });
    cli.on('exit', (code, signal) => {
      process.exit(exitCodeOf(code, signal));
    });
  } else {
    // 'mcp' 子命令作为 stdio 服务的显式别名，去除该前导参数
    const forwardedArgs = argv[0] === 'mcp' ? argv.slice(1) : argv;
    const proc = spawn(runtime.command, [...runtime.prefixArgs, SCRIPT, ...forwardedArgs], {
      stdio: ['pipe', 'pipe', 'inherit'],
      env,
    });

    process.stdin.pipe(proc.stdin);
    proc.stdout.pipe(process.stdout);

    proc.on('error', (err) => {
      console.error(`argo-search: 无法启动运行时 (${runtime.command}): ${err.message}`);
      console.error(formatMissingRuntimeHelp({ ok: false, reason: 'missing_python' }));
      process.exit(1);
    });

    proc.on('exit', (code, signal) => {
      process.exit(exitCodeOf(code, signal));
    });
  }
}

module.exports = { exitCodeOf, resolveRuntime, formatMissingRuntimeHelp, UV_PREFIX_ARGS };

if (require.main === module) {
  main();
}
