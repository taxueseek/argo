#!/usr/bin/env node
const { spawn } = require('child_process');
const path = require('path');

const PYTHON = '/usr/bin/python3';
const SCRIPT = path.join(__dirname, '..', 'scripts', 'mcp_server.py');

const proc = spawn(PYTHON, [SCRIPT], {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: { ...process.env }
});

process.stdin.pipe(proc.stdin);
proc.stdout.pipe(process.stdout);

proc.on('error', (err) => {
    console.error(`argo-search: ${err.message}`);
    process.exit(1);
});

proc.on('exit', (code) => {
    process.exit(code || 0);
});
