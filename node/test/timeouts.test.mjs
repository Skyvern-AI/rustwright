import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const nativePath = require.resolve('../native.cjs');
const cachedNative = require.cache[nativePath];

// These tests exercise only the JavaScript marshalling and context veneer.
require.cache[nativePath] = {
  id: nativePath,
  filename: nativePath,
  loaded: true,
  exports: {}
};

let Browser;
let Page;
try {
  ({ Browser, Page } = require('../index.cjs'));
} finally {
  if (cachedNative) require.cache[nativePath] = cachedNative;
  else delete require.cache[nativePath];
}

class RecordingPage {
  constructor() {
    this.calls = [];
  }

  record(name, args) {
    this.calls.push({ name, args });
  }

  setDefaultTimeout(...args) {
    this.record('setDefaultTimeout', args);
  }

  setDefaultNavigationTimeout(...args) {
    this.record('setDefaultNavigationTimeout', args);
  }

  setContextDefaultTimeout(...args) {
    this.record('setContextDefaultTimeout', args);
  }

  setContextDefaultNavigationTimeout(...args) {
    this.record('setContextDefaultNavigationTimeout', args);
  }

  async goto(...args) {
    this.record('goto', args);
    return 'null';
  }

  async click(...args) {
    this.record('click', args);
  }

  async fill(...args) {
    this.record('fill', args);
  }

  async title(...args) {
    this.record('title', args);
    return 'title';
  }

  async textContent(...args) {
    this.record('textContent', args);
    return null;
  }

  async evaluate(...args) {
    this.record('evaluate', args);
    return 'null';
  }

  async screenshot(...args) {
    this.record('screenshot', args);
    return Uint8Array.from([1, 2, 3]);
  }

  async close(...args) {
    this.record('close', args);
  }
}

class RecordingBrowser {
  constructor() {
    this.pages = [];
  }

  async newPage() {
    const page = new RecordingPage();
    this.pages.push(page);
    return page;
  }

  async close() {}

  wsEndpoint() {
    return 'ws://test.invalid';
  }
}

function callsNamed(inner, name) {
  return inner.calls.filter((call) => call.name === name).map((call) => call.args);
}

test('page setters marshal coerced values into native page slots', () => {
  const inner = new RecordingPage();
  const page = new Page(inner);

  page.setDefaultTimeout('125.5');
  page.setDefaultNavigationTimeout(undefined);

  assert.deepEqual(callsNamed(inner, 'setDefaultTimeout'), [[125.5]]);
  assert.equal(callsNamed(inner, 'setDefaultNavigationTimeout').length, 1);
  assert.ok(Number.isNaN(callsNamed(inner, 'setDefaultNavigationTimeout')[0][0]));
  assert.equal(page._resolveTimeout, undefined);
  assert.equal(page._resolveNavigationTimeout, undefined);
  assert.equal(Object.hasOwn(page, '_defaultTimeout'), false);
  assert.equal(Object.hasOwn(page, '_defaultNavigationTimeout'), false);
});

test('page methods forward only explicit user timeouts', async () => {
  const inner = new RecordingPage();
  const page = new Page(inner);

  await page.goto('https://example.com');
  await page.goto('https://example.com/explicit', { timeout: '11' });
  await page.click('#button');
  await page.fill('#field', 'value', { timeout: '12' });
  await page.title({ timeout: '13' });
  await page.textContent('#message');
  await page.evaluate('1', undefined, { timeout: '14' });
  await page.screenshot({ timeout: '15', fullPage: 1 });

  assert.equal(callsNamed(inner, 'goto')[0][2], undefined);
  assert.equal(callsNamed(inner, 'goto')[1][2], 11);
  assert.equal(callsNamed(inner, 'click')[0][1], undefined);
  assert.equal(callsNamed(inner, 'fill')[0][2], 12);
  assert.deepEqual(callsNamed(inner, 'title')[0], [13]);
  assert.equal(callsNamed(inner, 'textContent')[0][1], undefined);
  assert.equal(callsNamed(inner, 'evaluate')[0][2], 14);
  assert.deepEqual(JSON.parse(callsNamed(inner, 'screenshot')[0][0]), {
    fullPage: true,
    timeout: 15
  });
});

test('context defaults populate context slots on existing and future pages', async () => {
  const inner = new RecordingBrowser();
  const browser = new Browser(inner);
  const context = await browser.newContext({ timeout: '100', navigationTimeout: '200' });
  const first = await context.newPage();
  const firstInner = inner.pages[0];

  assert.deepEqual(callsNamed(firstInner, 'setContextDefaultTimeout'), [[100]]);
  assert.deepEqual(callsNamed(firstInner, 'setContextDefaultNavigationTimeout'), [[200]]);
  assert.deepEqual(callsNamed(firstInner, 'setDefaultTimeout'), []);
  assert.deepEqual(callsNamed(firstInner, 'setDefaultNavigationTimeout'), []);

  first.setDefaultTimeout('150');
  context.setDefaultTimeout('300');
  context.setDefaultNavigationTimeout('400');

  assert.deepEqual(callsNamed(firstInner, 'setDefaultTimeout'), [[150]]);
  assert.deepEqual(callsNamed(firstInner, 'setContextDefaultTimeout'), [[100], [300]]);
  assert.deepEqual(
    callsNamed(firstInner, 'setContextDefaultNavigationTimeout'),
    [[200], [400]]
  );

  await context.newPage();
  const secondInner = inner.pages[1];
  assert.deepEqual(callsNamed(secondInner, 'setContextDefaultTimeout'), [[300]]);
  assert.deepEqual(callsNamed(secondInner, 'setContextDefaultNavigationTimeout'), [[400]]);

  assert.deepEqual(browser.contexts(), [context]);
  await context.close();
  assert.deepEqual(browser.contexts(), []);
  assert.equal(callsNamed(firstInner, 'close').length, 1);
  assert.equal(callsNamed(secondInner, 'close').length, 1);
});
