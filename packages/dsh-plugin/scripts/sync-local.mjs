#!/usr/bin/env node
/**
 * sync-local.mjs — 跨平台（Windows/macOS/Linux）将本仓 packages/dsh-plugin 同步到本机 DSH profile。
 * 用法：
 *   node scripts/sync-local.mjs            # 同步 web,desktop（或 DSH_PROFILES）
 *   node scripts/sync-local.mjs web        # 只同步指定 profile
 *   node scripts/sync-local.mjs --check    # 只比对不写，有漂移退出 1
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(__dirname, '..');
const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');

let mode = 'sync';
const profiles = [];
for (const arg of process.argv.slice(2)) {
  if (arg === '--check') mode = 'check';
  else if (arg === '--restart') {
    console.error('[removed] --restart 已移除：同步产物不等于激活 Host');
    process.exit(2);
  } else if (arg === '-h' || arg === '--help') {
    console.log('Usage: node scripts/sync-local.mjs [--check] [profile...]');
    process.exit(0);
  } else profiles.push(arg);
}

if (profiles.length === 0) {
  const raw = process.env.DSH_PROFILES || 'web,desktop';
  profiles.push(...raw.split(',').map((s) => s.trim()).filter(Boolean));
}

function filesEqual(a, b) {
  if (!fs.existsSync(a) || !fs.existsSync(b)) return false;
  const sa = fs.statSync(a), sb = fs.statSync(b);
  if (sa.isDirectory() && sb.isDirectory()) {
    const ea = fs.readdirSync(a).sort(), eb = fs.readdirSync(b).sort();
    if (ea.length !== eb.length || ea.some((x, i) => x !== eb[i])) return false;
    return ea.every((name) => filesEqual(path.join(a, name), path.join(b, name)));
  }
  if (sa.isFile() && sb.isFile()) {
    if (sa.size !== sb.size) return false;
    return Buffer.compare(fs.readFileSync(a), fs.readFileSync(b)) === 0;
  }
  return false;
}

const ts = new Date().toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15);
let drift = 0;

for (const p of profiles) {
  const dest = path.join(DSH_HOME, 'profiles', p, 'node_modules', '@taxueseek', 'argo-dsh');
  let lst;
  try { lst = fs.lstatSync(dest); } catch {
    console.log(`[skip] ${p}: 未安装 ${dest}`);
    continue;
  }
  if (lst.isSymbolicLink() && fs.realpathSync(dest) === fs.realpathSync(SRC)) {
    console.log(mode === 'check' ? `[ok] ${p} (symlink)` : `[skip] ${p}: 已是直连 symlink`);
    continue;
  }
  if (mode === 'check') {
    const same = filesEqual(path.join(SRC, 'dsh'), path.join(dest, 'dsh'))
      && filesEqual(path.join(SRC, 'package.json'), path.join(dest, 'package.json'));
    if (same) console.log(`[ok] ${p}`);
    else {
      console.log(`[drift] ${p}: 源码与已安装 bundle 不一致，请运行 npm run sync-local`);
      drift = 1;
    }
    continue;
  }
  const bakDir = path.join(DSH_HOME, 'backups', `argo-dsh-sync-${ts}-${p}`);
  fs.mkdirSync(path.dirname(bakDir), { recursive: true });
  fs.cpSync(dest, bakDir, { recursive: true, force: true });
  fs.rmSync(path.join(dest, 'dsh'), { recursive: true, force: true });
  fs.cpSync(path.join(SRC, 'dsh'), path.join(dest, 'dsh'), { recursive: true, force: true });
  for (const f of ['package.json', 'cordis.patch.yml', 'README.md']) {
    fs.copyFileSync(path.join(SRC, f), path.join(dest, f));
  }
  console.log(`[synced] ${p}`);
}

if (mode === 'check') process.exit(drift);
console.log('[artifact-synced] 产物已同步；请核对 Host 运行态');
