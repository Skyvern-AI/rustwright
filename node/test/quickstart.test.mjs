import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const testDirectory = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(testDirectory, '../..');
const examplePath = resolve(repoRoot, 'examples/quickstart.js');

test('Node quickstart runs from the repository root', { timeout: 45_000 }, async (t) => {
  assert.ok(
    existsSync(examplePath),
    'examples/quickstart.js is missing; add the runnable Node.js quickstart example',
  );

  const quickstartSource = readFileSync(examplePath, 'utf8');
  assert.match(
    quickstartSource,
    /^\/\/ const \{ chromium \} = require\('rustwright'\);$/m,
    'npm-user guidance must use CommonJS syntax because the example is CommonJS',
  );

  const probeRoot = mkdtempSync(join(tmpdir(), 'rustwright-quickstart-test-'));
  try {
    const commonjsProject = join(probeRoot, 'commonjs-project');
    const commonjsPackage = join(commonjsProject, 'node_modules', 'rustwright');
    mkdirSync(commonjsPackage, { recursive: true });
    writeFileSync(join(commonjsProject, 'package.json'), '{"type":"commonjs"}\n');
    writeFileSync(
      join(commonjsPackage, 'package.json'),
      '{"name":"rustwright","main":"index.cjs"}\n',
    );
    writeFileSync(join(commonjsPackage, 'index.cjs'), "exports.chromium = 'stub';\n");
    const commonjsProbe = join(commonjsProject, 'quickstart.js');
    writeFileSync(
      commonjsProbe,
      "const { chromium } = require('rustwright');\nif (chromium !== 'stub') process.exitCode = 1;\n",
    );

    const syntaxResult = spawnSync(process.execPath, ['--check', commonjsProbe], {
      encoding: 'utf8',
    });
    assert.equal(syntaxResult.status, 0, syntaxResult.stderr);
    const commonjsResult = spawnSync(process.execPath, [commonjsProbe], { encoding: 'utf8' });
    assert.equal(commonjsResult.status, 0, commonjsResult.stderr);

    const brokenProject = join(probeRoot, 'broken-package-project');
    const brokenPackage = join(brokenProject, 'node_modules', 'rustwright');
    mkdirSync(brokenPackage, { recursive: true });
    writeFileSync(
      join(brokenPackage, 'package.json'),
      '{"name":"rustwright","main":"index.cjs"}\n',
    );
    writeFileSync(join(brokenPackage, 'index.cjs'), "require('./missing-internal.cjs');\n");
    writeFileSync(join(brokenProject, 'quickstart.js'), quickstartSource);

    const localFallback = join(probeRoot, 'node');
    mkdirSync(localFallback);
    writeFileSync(
      join(localFallback, 'index.cjs'),
      `exports.chromium = {
  async launch() {
    return {
      async newPage() {
        return {
          async goto() {},
          async title() { return 'unexpected fallback'; },
          async screenshot() {},
          async close() {},
        };
      },
      async close() {},
    };
  },
};
`,
    );

    const brokenResult = spawnSync(process.execPath, ['quickstart.js'], {
      cwd: brokenProject,
      encoding: 'utf8',
    });
    assert.notEqual(brokenResult.status, 0, 'a broken installed package must not use the fallback');
    assert.match(brokenResult.stderr, /Cannot find module '\.\/missing-internal\.cjs'/);
    assert.match(brokenResult.stderr, /node_modules[\\/]rustwright[\\/]index\.cjs/);
  } finally {
    rmSync(probeRoot, { recursive: true, force: true });
  }

  let chromium;
  try {
    ({ chromium } = await import('../index.mjs'));
  } catch (error) {
    if (error instanceof Error && error.message.includes('native addon is not built')) {
      t.skip('Rustwright native addon is not built');
      return;
    }
    throw error;
  }

  if (!await chromium.executablePath()) {
    t.skip('Chromium/Chrome executable not found');
    return;
  }

  const result = spawnSync(process.execPath, ['examples/quickstart.js'], {
    cwd: repoRoot,
    encoding: 'utf8',
    env: process.env,
    timeout: 30_000,
  });

  assert.equal(result.error, undefined, `quickstart process error: ${result.error?.message}`);
  assert.equal(
    result.status,
    0,
    `quickstart exited with status ${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
  );

  // Output contract: the quickstart prints the title from its inline page.
  assert.match(result.stdout, /^title: Rustwright works$/m);
});
