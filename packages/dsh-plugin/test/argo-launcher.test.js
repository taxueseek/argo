import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const require = createRequire(import.meta.url)
const PKG_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const REPO_ROOT = dirname(dirname(PKG_ROOT))
const LAUNCHER_PATH = join(REPO_ROOT, 'bin', 'argo.js')

test('bin/argo.js exports pure runtime resolver without side effects on require', () => {
  const launcher = require(LAUNCHER_PATH)
  assert.equal(typeof launcher.resolveRuntime, 'function')
  assert.equal(typeof launcher.exitCodeOf, 'function')
  assert.equal(typeof launcher.formatMissingRuntimeHelp, 'function')
})

test('resolveRuntime: ARGO_PYTHON / PYTHON env override wins with zero probes', () => {
  const { resolveRuntime } = require(LAUNCHER_PATH)
  let probes = 0
  const res = resolveRuntime({
    env: { ARGO_PYTHON: '/custom/python3.12' },
    existsSync: () => true,
    probe: () => { probes += 1; return { ok: true } },
  })
  assert.equal(res.command, '/custom/python3.12')
  assert.deepEqual(res.prefixArgs, [])
  assert.equal(res.source, 'env')
  assert.equal(probes, 0)
})

test('resolveRuntime: prefers local .venv over system PATH (POSIX and win32)', () => {
  const { resolveRuntime } = require(LAUNCHER_PATH)
  let probes = 0
  const posix = resolveRuntime({
    env: {},
    platform: 'darwin',
    repoRoot: '/repo',
    existsSync: (p) => p === '/repo/.venv/bin/python',
    probe: () => { probes += 1; return { ok: true } },
  })
  assert.equal(posix.command, '/repo/.venv/bin/python')
  assert.equal(posix.source, 'venv')
  assert.equal(probes, 0)

  const win = resolveRuntime({
    env: {},
    platform: 'win32',
    repoRoot: 'C:\\argo',
    existsSync: (p) => p.endsWith('Scripts\\python.exe') || p.endsWith('Scripts/python.exe'),
    probe: () => { probes += 1; return { ok: true } },
  })
  assert.equal(win.source, 'venv')
  assert.match(win.command, /python\.exe$/)
  assert.equal(probes, 0)
})

test('resolveRuntime: skips Windows Store stub / old Python and selects working py -3', () => {
  const { resolveRuntime } = require(LAUNCHER_PATH)
  const tried = []
  const res = resolveRuntime({
    env: {},
    platform: 'win32',
    repoRoot: 'C:\\argo',
    existsSync: () => false,
    probe: (cmd, args) => {
      tried.push([cmd, ...args.slice(0, 1)])
      if (cmd === 'python') return { ok: false, status: 9009 } // Windows Store stub
      if (cmd === 'py' && args[0] === '-3') return { ok: true, status: 0 }
      return { ok: false, status: 1 }
    },
  })
  assert.equal(res.command, 'py')
  assert.deepEqual(res.prefixArgs, ['-3'])
  assert.equal(res.source, 'path')
  assert.ok(tried.length >= 2)
})

test('resolveRuntime: falls back to uv run --with pyyaml when Python lacks PyYAML or is missing', () => {
  const { resolveRuntime } = require(LAUNCHER_PATH)
  const res = resolveRuntime({
    env: {},
    platform: 'darwin',
    repoRoot: '/repo',
    existsSync: () => false,
    probe: (cmd) => {
      if (cmd === 'python3') return { ok: false, status: 4 } // Python >=3.9 但缺 PyYAML
      if (cmd === 'python') return { ok: false, error: { code: 'ENOENT' } }
      if (cmd === 'uv') return { ok: true, status: 0 }
      return { ok: false }
    },
  })
  assert.equal(res.command, 'uv')
  assert.deepEqual(res.prefixArgs, ['run', '--no-project', '--with', 'pyyaml>=6.0', '--python', '>=3.9', 'python'])
  assert.equal(res.source, 'uv')
})

test('resolveRuntime: returns structured missing_yaml / missing_python diagnostics when uv is absent', () => {
  const { resolveRuntime, formatMissingRuntimeHelp } = require(LAUNCHER_PATH)
  const noYaml = resolveRuntime({
    env: {},
    platform: 'darwin',
    repoRoot: '/repo',
    existsSync: () => false,
    probe: (cmd) => {
      if (cmd === 'python3') return { ok: false, status: 4 }
      return { ok: false, error: { code: 'ENOENT' } }
    },
  })
  assert.equal(noYaml.ok, false)
  assert.equal(noYaml.reason, 'missing_yaml')
  assert.equal(noYaml.detectedPython, 'python3')
  const helpYaml = formatMissingRuntimeHelp(noYaml, 'darwin')
  assert.match(helpYaml, /pyyaml/i)
  assert.match(helpYaml, /uv/)

  const noPy = resolveRuntime({
    env: {},
    platform: 'win32',
    repoRoot: 'C:\\argo',
    existsSync: () => false,
    probe: () => ({ ok: false, error: { code: 'ENOENT' } }),
  })
  assert.equal(noPy.ok, false)
  assert.equal(noPy.reason, 'missing_python')
  const helpWin = formatMissingRuntimeHelp(noPy, 'win32')
  assert.match(helpWin, /Python 3\.9\+/)
  assert.match(helpWin, /uv/)
})

test('dsh-plugin wires zero-build checkJs tsconfig into npm run typecheck', () => {
  const pkg = require(join(PKG_ROOT, 'package.json'))
  const tsconfig = require(join(PKG_ROOT, 'tsconfig.json'))
  assert.equal(tsconfig.compilerOptions.allowJs, true)
  assert.equal(tsconfig.compilerOptions.checkJs, true)
  assert.equal(tsconfig.compilerOptions.noEmit, true)
  assert.match(pkg.scripts.typecheck, /tsc\s+-p\s+tsconfig\.json\s+--noEmit/)
})
