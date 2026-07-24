#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const npmDirectory = path.dirname(scriptDirectory);
const crateDirectory = path.dirname(npmDirectory);
const repositoryDirectory = path.dirname(crateDirectory);
const targetsPath = path.join(npmDirectory, 'targets.json');
const mainDirectory = path.join(npmDirectory, 'main');

function fail(message) {
  console.error(`build-npm: ${message}`);
  process.exit(1);
}

function optionValue(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) return undefined;
  const value = process.argv[index + 1];
  if (!value || value.startsWith('--')) fail(`${name} requires a value`);
  return value;
}

function crateVersion() {
  const manifest = fs.readFileSync(path.join(crateDirectory, 'Cargo.toml'), 'utf8');
  const packageSection = manifest.match(/\[package\]([\s\S]*?)(?=\n\[|$)/);
  const version = packageSection?.[1].match(/^version\s*=\s*"([^"]+)"\s*$/m)?.[1];
  if (!version) fail('could not read the mcp-rs package version from Cargo.toml');
  return version;
}

function readTargets() {
  const targets = JSON.parse(fs.readFileSync(targetsPath, 'utf8'));
  if (!Array.isArray(targets) || targets.length === 0) fail('targets.json is empty');

  for (const field of ['id', 'runtimeKey', 'rustTarget', 'os', 'cpu', 'binary', 'package', 'runner']) {
    const values = targets.map((target) => target[field]);
    if (values.some((value) => typeof value !== 'string' || value.length === 0)) {
      fail(`every target must have a non-empty ${field}`);
    }
    if (['id', 'runtimeKey', 'rustTarget', 'package'].includes(field) && new Set(values).size !== values.length) {
      fail(`target ${field} values must be unique`);
    }
  }
  for (const target of targets) {
    if (target.zig && (typeof target.zigTarget !== 'string' || target.zigTarget.length === 0)) {
      fail(`Zig target ${target.id} must have a non-empty zigTarget`);
    }
  }
  return targets;
}

function writeJson(destination, value) {
  fs.writeFileSync(destination, `${JSON.stringify(value, null, 2)}\n`);
}

function writeMainPackage(targets, version) {
  const optionalDependencies = Object.fromEntries(
    targets.map((target) => [target.package, version]),
  );
  const platforms = Object.fromEntries(
    targets.map((target) => [
      target.runtimeKey,
      { package: target.package, binary: target.binary },
    ]),
  );

  const packageJson = {
    name: 'rustwright-mcp',
    version,
    description: 'Native Rustwright Model Context Protocol server',
    license: 'MIT',
    repository: {
      type: 'git',
      url: 'git+https://github.com/Skyvern-AI/rustwright.git',
      directory: 'mcp-rs/npm',
    },
    type: 'commonjs',
    bin: { 'rustwright-mcp': 'bin/rustwright-mcp.js' },
    files: ['bin', 'platforms.json', 'LICENSE'],
    engines: { node: '>=18' },
    optionalDependencies,
    publishConfig: { access: 'public' },
  };

  writeJson(path.join(mainDirectory, 'package.json'), packageJson);
  writeJson(path.join(mainDirectory, 'platforms.json'), platforms);
  fs.copyFileSync(
    path.join(repositoryDirectory, 'LICENSE'),
    path.join(mainDirectory, 'LICENSE'),
  );
}

function writePlatformPackage(target, binaryPath, outputRoot, version) {
  const source = path.resolve(binaryPath);
  const sourceStat = fs.statSync(source, { throwIfNoEntry: false });
  if (!sourceStat?.isFile()) fail(`built binary does not exist: ${source}`);

  const outputDirectory = path.resolve(outputRoot, target.id);
  const binaryDirectory = path.join(outputDirectory, 'bin');
  fs.rmSync(outputDirectory, { recursive: true, force: true });
  fs.mkdirSync(binaryDirectory, { recursive: true });

  const packageJson = {
    name: target.package,
    version,
    description: `Native rustwright-mcp binary for ${target.id}`,
    license: 'MIT',
    repository: {
      type: 'git',
      url: 'git+https://github.com/Skyvern-AI/rustwright.git',
      directory: 'mcp-rs/npm',
    },
    os: [target.os],
    cpu: [target.cpu],
    files: ['bin', 'LICENSE'],
    publishConfig: { access: 'public' },
  };
  if (target.libc) packageJson.libc = [target.libc];

  writeJson(path.join(outputDirectory, 'package.json'), packageJson);
  fs.copyFileSync(source, path.join(binaryDirectory, target.binary));
  if (target.os !== 'win32') {
    fs.chmodSync(path.join(binaryDirectory, target.binary), 0o755);
  }
  fs.copyFileSync(
    path.join(repositoryDirectory, 'LICENSE'),
    path.join(outputDirectory, 'LICENSE'),
  );
  return outputDirectory;
}

const targets = readTargets();
const version = crateVersion();
writeMainPackage(targets, version);

if (process.argv.includes('--main-only')) {
  console.log(`Assembled rustwright-mcp ${version} in ${mainDirectory}`);
  process.exit(0);
}

const targetId = optionValue('--target');
const binaryPath = optionValue('--binary');
const outputRoot = optionValue('--out') ?? path.join(npmDirectory, 'packages');
if (!targetId || !binaryPath) {
  fail('usage: build-npm.mjs --target <target-id> --binary <path> [--out <directory>]');
}

const target = targets.find((candidate) => candidate.id === targetId);
if (!target) fail(`unknown target ${targetId}`);
const outputDirectory = writePlatformPackage(target, binaryPath, outputRoot, version);
console.log(`Assembled ${target.package}@${version} in ${outputDirectory}`);
