'use strict';

const native = require('./native.cjs');

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function normalizeLaunchOptions(options = {}) {
  if (options == null) options = {};
  const out = {};
  if (hasOwn(options, 'headless')) out.headless = Boolean(options.headless);
  if (hasOwn(options, 'executablePath')) out.executablePath = String(options.executablePath);
  if (hasOwn(options, 'channel')) out.channel = String(options.channel);
  if (hasOwn(options, 'args')) out.args = Array.from(options.args || [], String);
  if (hasOwn(options, 'ignoreAllDefaultArgs')) {
    out.ignoreAllDefaultArgs = Boolean(options.ignoreAllDefaultArgs);
  }
  if (hasOwn(options, 'ignoreDefaultArgs')) {
    out.ignoreDefaultArgs = Array.from(options.ignoreDefaultArgs || [], String);
  }
  if (hasOwn(options, 'timeout')) out.timeout = Number(options.timeout);
  if (hasOwn(options, 'userDataDir')) out.userDataDir = String(options.userDataDir);
  if (hasOwn(options, 'env')) {
    out.env = Object.fromEntries(
      Object.entries(options.env || {}).map(([key, value]) => [key, String(value)])
    );
  }
  if (hasOwn(options, 'chromiumSandbox')) out.chromiumSandbox = Boolean(options.chromiumSandbox);
  if (options.proxy) {
    out.proxy = {
      server: String(options.proxy.server || ''),
      bypass: options.proxy.bypass == null ? undefined : String(options.proxy.bypass),
      username: options.proxy.username == null ? undefined : String(options.proxy.username),
      password: options.proxy.password == null ? undefined : String(options.proxy.password)
    };
  }
  return out;
}

function normalizeContextOptions(options = {}) {
  if (options == null) options = {};
  const out = {};
  if (hasOwn(options, 'ignoreHTTPSErrors')) out.ignoreHTTPSErrors = Boolean(options.ignoreHTTPSErrors);
  if (hasOwn(options, 'timeout')) out.timeout = Number(options.timeout);
  if (hasOwn(options, 'navigationTimeout')) out.navigationTimeout = Number(options.navigationTimeout);
  return out;
}

function normalizeScreenshotOptions(options = {}) {
  if (options == null) return {};
  const out = {};
  if (hasOwn(options, 'path')) out.path = String(options.path);
  if (hasOwn(options, 'fullPage')) out.fullPage = Boolean(options.fullPage);
  if (hasOwn(options, 'clip')) out.clip = options.clip;
  if (hasOwn(options, 'timeout')) out.timeout = Number(options.timeout);
  if (hasOwn(options, 'type')) out.type = String(options.type);
  if (hasOwn(options, 'quality')) out.quality = Number(options.quality);
  if (hasOwn(options, 'omitBackground')) out.omitBackground = Boolean(options.omitBackground);
  return out;
}

function encodeEvaluateArg(arg) {
  if (arguments.length === 0 || typeof arg === 'undefined') return undefined;
  return JSON.stringify(arg);
}

function decodeWireValue(value, seen = new Map()) {
  if (Array.isArray(value)) return value.map((item) => decodeWireValue(item, seen));
  if (!value || typeof value !== 'object') return value;

  if (hasOwn(value, '__rustwright_cdp_ref__')) {
    return seen.get(value.__rustwright_cdp_ref__);
  }
  if (hasOwn(value, '__rustwright_cdp_array__')) {
    const ref = value.__rustwright_cdp_array__;
    const result = [];
    seen.set(ref, result);
    for (const item of value.items || []) result.push(decodeWireValue(item, seen));
    return result;
  }
  if (hasOwn(value, '__rustwright_cdp_object__')) {
    const ref = value.__rustwright_cdp_object__;
    const result = {};
    seen.set(ref, result);
    for (const [key, item] of Object.entries(value.entries || {})) {
      result[key] = decodeWireValue(item, seen);
    }
    return result;
  }
  if (hasOwn(value, '__rustwright_cdp_undefined__')) return undefined;
  if (hasOwn(value, '__rustwright_cdp_symbol__')) return undefined;
  if (hasOwn(value, '__rustwright_cdp_function__')) return undefined;
  if (hasOwn(value, '__rustwright_cdp_date__')) return new Date(value.__rustwright_cdp_date__);
  if (hasOwn(value, '__rustwright_cdp_regexp__')) {
    const spec = value.__rustwright_cdp_regexp__ || {};
    return new RegExp(String(spec.p || ''), String(spec.f || ''));
  }
  if (hasOwn(value, '__rustwright_cdp_url__')) return new URL(value.__rustwright_cdp_url__);
  if (hasOwn(value, '__rustwright_cdp_error__')) {
    const spec = value.__rustwright_cdp_error__ || {};
    const error = new Error(String(spec.message || ''));
    error.name = String(spec.name || 'Error');
    error.stack = String(spec.stack || '');
    return error;
  }
  if (hasOwn(value, '__rustwright_cdp_unserializable_value__')) {
    const marker = value.__rustwright_cdp_unserializable_value__;
    if (marker === 'NaN') return NaN;
    if (marker === 'Infinity') return Infinity;
    if (marker === '-Infinity') return -Infinity;
    if (marker === '-0') return -0;
    if (typeof marker === 'string' && marker.endsWith('n')) return BigInt(marker.slice(0, -1));
  }

  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, decodeWireValue(item, seen)])
  );
}

function parseRustJson(json) {
  return decodeWireValue(JSON.parse(json));
}

class Browser {
  constructor(inner) {
    this._inner = inner;
    this._contexts = [];
  }

  async newPage() {
    return new Page(await this._inner.newPage());
  }

  // Compatibility shim. The alpha Node engine drives a single Chromium process,
  // so contexts created here do NOT provide the storage/cookie isolation a real
  // Playwright BrowserContext does — they exist to keep the familiar
  // `browser.newContext().newPage()` shape working and to carry default timeouts.
  async newContext(options = {}) {
    const context = new BrowserContext(this, normalizeContextOptions(options));
    this._contexts.push(context);
    return context;
  }

  contexts() {
    return this._contexts.slice();
  }

  async close() {
    await this._inner.close();
  }

  wsEndpoint() {
    return this._inner.wsEndpoint();
  }
}

let warnedIgnoreHttpsErrors = false;

class BrowserContext {
  constructor(browser, options = {}) {
    this._browser = browser;
    this._options = options;
    this._pages = [];
    this._defaultTimeout = options.timeout;
    this._defaultNavigationTimeout = options.navigationTimeout;

    if (options.ignoreHTTPSErrors && !warnedIgnoreHttpsErrors) {
      warnedIgnoreHttpsErrors = true;
      // Certificate handling is a launch-time concern for the current engine and
      // cannot be toggled per-context after the browser is running. Warn rather
      // than silently pretend a security-relevant flag took effect.
      console.warn(
        'rustwright: newContext({ ignoreHTTPSErrors: true }) is accepted for API ' +
          'compatibility but not yet enforced by the Node engine. Launch Chromium with ' +
          "args: ['--ignore-certificate-errors'] if you need to bypass TLS errors."
      );
    }
  }

  browser() {
    return this._browser;
  }

  pages() {
    return this._pages.slice();
  }

  async newPage() {
    const page = new Page(await this._browser._inner.newPage());
    if (this._defaultTimeout != null) {
      page._inner.setContextDefaultTimeout(this._defaultTimeout);
    }
    if (this._defaultNavigationTimeout != null) {
      page._inner.setContextDefaultNavigationTimeout(this._defaultNavigationTimeout);
    }
    this._pages.push(page);
    return page;
  }

  setDefaultTimeout(timeout) {
    this._defaultTimeout = Number(timeout);
    for (const page of this._pages) {
      page._inner.setContextDefaultTimeout(this._defaultTimeout);
    }
  }

  setDefaultNavigationTimeout(timeout) {
    this._defaultNavigationTimeout = Number(timeout);
    for (const page of this._pages) {
      page._inner.setContextDefaultNavigationTimeout(this._defaultNavigationTimeout);
    }
  }

  async close() {
    const pages = this._pages.splice(0);
    await Promise.all(
      pages.map((page) => page.close().catch(() => {}))
    );
    const index = this._browser._contexts.indexOf(this);
    if (index !== -1) this._browser._contexts.splice(index, 1);
  }
}

class Page {
  constructor(inner) {
    this._inner = inner;
  }

  setDefaultTimeout(timeout) {
    this._inner.setDefaultTimeout(Number(timeout));
  }

  setDefaultNavigationTimeout(timeout) {
    this._inner.setDefaultNavigationTimeout(Number(timeout));
  }

  async goto(url, options = {}) {
    const response = await this._inner.goto(
      String(url),
      options.waitUntil == null ? undefined : String(options.waitUntil),
      options.timeout == null ? undefined : Number(options.timeout),
      options.referer == null ? undefined : String(options.referer)
    );
    return response === 'null' ? null : parseRustJson(response);
  }

  async click(selector, options = {}) {
    await this._inner.click(String(selector), options.timeout == null ? undefined : Number(options.timeout));
  }

  async fill(selector, value, options = {}) {
    await this._inner.fill(
      String(selector),
      String(value),
      options.timeout == null ? undefined : Number(options.timeout)
    );
  }

  async title(options = {}) {
    return this._inner.title(options.timeout == null ? undefined : Number(options.timeout));
  }

  async textContent(selector, options = {}) {
    return this._inner.textContent(
      String(selector),
      options.timeout == null ? undefined : Number(options.timeout)
    );
  }

  async evaluate(expression, arg, options = {}) {
    const source = typeof expression === 'function' ? expression.toString() : String(expression);
    const timeout = options.timeout == null ? undefined : Number(options.timeout);
    const json = await this._inner.evaluate(source, encodeEvaluateArg(arg), timeout);
    return parseRustJson(json);
  }

  async screenshot(options = {}) {
    const normalized = normalizeScreenshotOptions(options);
    const bytes = await this._inner.screenshot(JSON.stringify(normalized));
    return Buffer.from(bytes);
  }

  async close(options = {}) {
    await this._inner.close(
      options.timeout == null ? undefined : Number(options.timeout),
      options.runBeforeUnload == null ? undefined : Boolean(options.runBeforeUnload)
    );
  }
}

const chromium = {
  async launch(options = {}) {
    const inner = await native.launchChromium(JSON.stringify(normalizeLaunchOptions(options)));
    return new Browser(inner);
  },
  async executablePath() {
    return (await native.chromiumExecutablePath()) || '';
  }
};

module.exports = {
  chromium,
  Browser,
  BrowserContext,
  Page,
  _decodeWireValue: decodeWireValue,
  _native: native
};
