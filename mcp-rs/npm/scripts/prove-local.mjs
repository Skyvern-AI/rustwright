#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const npmDirectory = path.dirname(scriptDirectory);
const targetOption = process.argv.indexOf('--target');
const targetId = targetOption === -1 ? undefined : process.argv[targetOption + 1];
if (!targetId || targetId.startsWith('--')) {
  throw new Error('usage: prove-local.mjs --target <target-id>');
}

const targets = JSON.parse(fs.readFileSync(path.join(npmDirectory, 'targets.json'), 'utf8'));
const target = targets.find((candidate) => candidate.id === targetId);
assert(target, `unknown target ${targetId}`);
assert.equal(`${process.platform}-${process.arch}`, target.runtimeKey, 'proof target must match this host');

const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'rustwright-mcp-npm-proof-'));
const artifactsDirectory = path.join(temporaryDirectory, 'artifacts');
const installDirectory = path.join(temporaryDirectory, 'install');
fs.mkdirSync(artifactsDirectory);
fs.mkdirSync(installDirectory);

function runNpm(args, cwd) {
  const command = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const result = spawnSync(command, args, { cwd, encoding: 'utf8' });
  if (result.status !== 0) {
    throw new Error(`npm ${args.join(' ')} failed:\n${result.stdout}\n${result.stderr}`);
  }
  return result.stdout;
}

function pack(packageDirectory) {
  const output = runNpm(
    ['pack', '--json', '--pack-destination', artifactsDirectory],
    packageDirectory,
  );
  const result = JSON.parse(output);
  assert.equal(result.length, 1);
  return path.join(artifactsDirectory, result[0].filename);
}

const platformTarball = pack(path.join(npmDirectory, 'packages', target.id));
const mainTarball = pack(path.join(npmDirectory, 'main'));
const mainPackage = JSON.parse(fs.readFileSync(path.join(npmDirectory, 'main', 'package.json'), 'utf8'));

fs.writeFileSync(
  path.join(installDirectory, 'package.json'),
  `${JSON.stringify({
    private: true,
    dependencies: {
      [mainPackage.name]: `file:${mainTarball}`,
      [target.package]: `file:${platformTarball}`,
    },
  }, null, 2)}\n`,
);
runNpm(
  ['install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', '--no-package-lock'],
  installDirectory,
);

const launcher = path.join(
  installDirectory,
  'node_modules',
  mainPackage.name,
  mainPackage.bin['rustwright-mcp'],
);
const child = spawn(process.execPath, [launcher], {
  cwd: installDirectory,
  env: process.env,
  stdio: ['pipe', 'pipe', 'pipe'],
});
const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
const iterator = lines[Symbol.asyncIterator]();
let stderr = '';
child.stderr.setEncoding('utf8');
child.stderr.on('data', (chunk) => {
  stderr += chunk;
});

const transcript = [];
function send(message) {
  const line = JSON.stringify(message);
  transcript.push(`C> ${line}`);
  child.stdin.write(`${line}\n`);
}

async function receive() {
  let timer;
  const next = await Promise.race([
    iterator.next(),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('timed out waiting for MCP response')), 15_000);
    }),
  ]);
  clearTimeout(timer);
  assert.equal(next.done, false, `server stdout closed early; stderr: ${stderr}`);
  transcript.push(`S> ${next.value}`);
  const message = JSON.parse(next.value);
  assert.equal(message.jsonrpc, '2.0', 'every stdout line must be JSON-RPC 2.0');
  return message;
}

send({
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'npm-native-proof', version: '0' },
  },
});
const initialized = await receive();
assert.equal(initialized.id, 1);
assert.equal(typeof initialized.result.capabilities.tools, 'object');

send({ jsonrpc: '2.0', method: 'notifications/initialized' });
send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });
const listed = await receive();
assert.equal(listed.id, 2);
assert(Array.isArray(listed.result.tools));
assert(listed.result.tools.length > 0);

child.stdin.end();
const exit = await new Promise((resolve, reject) => {
  const timer = setTimeout(() => {
    child.kill('SIGKILL');
    reject(new Error('server did not exit after stdin EOF'));
  }, 15_000);
  child.once('error', reject);
  child.once('exit', (code, signal) => {
    clearTimeout(timer);
    resolve({ code, signal });
  });
});
assert.deepEqual(exit, { code: 0, signal: null }, `server failed; stderr: ${stderr}`);

for (;;) {
  const next = await iterator.next();
  if (next.done) break;
  transcript.push(`S> ${next.value}`);
  const message = JSON.parse(next.value);
  assert.equal(message.jsonrpc, '2.0', 'every trailing stdout line must be JSON-RPC 2.0');
}

console.log(`PACKAGES> ${mainPackage.name}@${mainPackage.version} + ${target.package}@${mainPackage.version}`);
for (const line of transcript) console.log(line);
console.log('EOF> closed launcher stdin');
console.log('EXIT> 0');
console.log(`ASSERT> ${listed.result.tools.length} tools; every stdout line was JSON-RPC 2.0`);
fs.rmSync(temporaryDirectory, { recursive: true, force: true });
