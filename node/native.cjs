'use strict';

const { existsSync, readdirSync } = require('node:fs');
const { join } = require('node:path');

function platformTriples() {
  const platform = process.platform;
  const arch = process.arch;
  const libc = platform === 'linux' ? ['gnu', 'musl'] : [];
  const msvc = platform === 'win32' ? ['msvc'] : [];
  const suffixes = ['', ...libc, ...msvc];
  return suffixes.map((suffix) => (suffix ? `${platform}-${arch}-${suffix}` : `${platform}-${arch}`));
}

function candidatePaths() {
  const names = new Set(['rustwright.node']);
  for (const triple of platformTriples()) {
    names.add(`rustwright.${triple}.node`);
    names.add(`rustwright-node.${triple}.node`);
    names.add(`index.${triple}.node`);
  }
  for (const name of readdirSync(__dirname)) {
    if (/^(rustwright|rustwright-node|index)\..+\.node$/.test(name)) {
      names.add(name);
    }
  }
  return [...names].map((name) => join(__dirname, name));
}

const attempted = [];
for (const candidate of candidatePaths()) {
  attempted.push(candidate);
  if (!existsSync(candidate)) continue;
  try {
    module.exports = require(candidate);
    return;
  } catch (error) {
    error.message = `Failed to load Rustwright native addon at ${candidate}: ${error.message}`;
    throw error;
  }
}

throw new Error(`Rustwright native addon is not built. Tried:\n${attempted.join('\n')}`);
