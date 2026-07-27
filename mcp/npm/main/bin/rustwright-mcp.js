#!/usr/bin/env node
'use strict';

const { spawn } = require('node:child_process');
const platforms = require('../platforms.json');

const platformKey = `${process.platform}-${process.arch}`;
const target = platforms[platformKey];

if (!target) {
  console.error(
    `[rustwright-mcp] No native binary is published for ${platformKey}. ` +
      'Supported platforms: ' +
      `${Object.keys(platforms).join(', ')}. ` +
      'Please report unsupported platforms at ' +
      'https://github.com/Skyvern-AI/rustwright/issues/new',
  );
  process.exit(1);
}

let binaryPath;
try {
  binaryPath = require.resolve(`${target.package}/bin/${target.binary}`);
} catch (error) {
  console.error(
    `[rustwright-mcp] The native package ${target.package} is missing for ` +
      `${platformKey}. Reinstall rustwright-mcp with optional dependencies ` +
      'enabled. If the problem persists, report it at ' +
      'https://github.com/Skyvern-AI/rustwright/issues/new',
  );
  process.exit(1);
}

const child = spawn(binaryPath, process.argv.slice(2), {
  env: process.env,
  stdio: 'inherit',
  windowsHide: false,
});

child.once('error', (error) => {
  console.error(`[rustwright-mcp] Failed to start ${target.package}: ${error.message}`);
  process.exit(1);
});

child.once('exit', (code, signal) => {
  if (signal) {
    try {
      process.kill(process.pid, signal);
    } catch {
      process.exit(1);
    }
    return;
  }
  process.exit(code ?? 1);
});

