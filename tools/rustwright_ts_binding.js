"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { spawn } = require("node:child_process");
const { performance } = require("node:perf_hooks");

function pickPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function waitForJson(url, ms) {
  const deadline = performance.now() + ms;
  let lastError;
  while (performance.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
      lastError = new Error("HTTP " + response.status);
    } catch (error) {
      lastError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw lastError || new Error("timed out waiting for " + url);
}

class CdpConnection {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener("message", event => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(message.error.message || JSON.stringify(message.error)));
        else resolve(message.result || {});
        return;
      }
      const key = this._eventKey(message.method, message.sessionId);
      const listeners = this.listeners.get(key);
      if (listeners) {
        for (const listener of Array.from(listeners)) listener(message.params || {});
      }
    });
    ws.addEventListener("close", () => {
      for (const { reject } of this.pending.values()) reject(new Error("CDP websocket closed"));
      this.pending.clear();
    });
  }

  _eventKey(method, sessionId = undefined) {
    return (sessionId || "") + "\0" + method;
  }

  on(method, sessionId, listener) {
    const key = this._eventKey(method, sessionId);
    let listeners = this.listeners.get(key);
    if (!listeners) {
      listeners = new Set();
      this.listeners.set(key, listeners);
    }
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  waitForEvent(method, sessionId, predicate = () => true, timeout = 30000) {
    return new Promise((resolve, reject) => {
      let dispose = null;
      const timer = setTimeout(() => {
        if (dispose) dispose();
        reject(new Error(method + " timed out after " + timeout + "ms"));
      }, timeout);
      dispose = this.on(method, sessionId, params => {
        if (!predicate(params)) return;
        clearTimeout(timer);
        dispose();
        resolve(params);
      });
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(message));
    });
  }

  close() {
    try {
      this.ws.close();
    } catch (_) {
    }
  }
}

async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("websocket connect timed out")), 5000);
    ws.addEventListener("open", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
    ws.addEventListener("error", event => {
      clearTimeout(timer);
      reject(event.error || new Error("websocket error"));
    }, { once: true });
  });
  return new CdpConnection(ws);
}

class RustwrightPage {
  constructor(browser, targetId, sessionId) {
    this._browser = browser;
    this._targetId = targetId;
    this._sessionId = sessionId;
    this._mainFrameId = null;
  }

  async _mainFrame() {
    if (this._mainFrameId) return this._mainFrameId;
    const tree = await this._browser._cdp.send("Page.getFrameTree", {}, this._sessionId);
    const frameId = tree.frameTree && tree.frameTree.frame && tree.frameTree.frame.id;
    if (!frameId) throw new Error("Page.getFrameTree did not return a main frame");
    this._mainFrameId = frameId;
    return frameId;
  }

  async setContent(html, _options = {}) {
    const frameId = await this._mainFrame();
    await this._browser._cdp.send("Page.setDocumentContent", { frameId, html }, this._sessionId);
  }

  async content() {
    return await this.evaluate("() => document.documentElement.outerHTML");
  }

  async title() {
    return await this.evaluate("() => document.title");
  }

  async url() {
    return await this.evaluate("() => location.href");
  }

  async goto(url, options = {}) {
    const waitUntil = options.waitUntil || "load";
    const timeout = options.timeout || 30000;
    let waiter = null;
    if (waitUntil === "load") {
      waiter = this._browser._cdp.waitForEvent("Page.loadEventFired", this._sessionId, () => true, timeout);
    } else if (waitUntil === "domcontentloaded") {
      waiter = this._browser._cdp.waitForEvent("Page.domContentEventFired", this._sessionId, () => true, timeout);
    } else if (waitUntil !== "commit") {
      throw new Error("Unsupported waitUntil: " + waitUntil);
    }
    await this._browser._cdp.send("Page.navigate", { url }, this._sessionId);
    if (waiter) await waiter;
    this._mainFrameId = null;
    return null;
  }

  async waitForLoadState(state = "load", options = {}) {
    const timeout = options.timeout || 30000;
    if (state === "commit") return;
    const readyState = await this.evaluate("() => document.readyState");
    if (state === "domcontentloaded" && (readyState === "interactive" || readyState === "complete")) return;
    if (state === "load" && readyState === "complete") return;
    const method = state === "domcontentloaded" ? "Page.domContentEventFired" : "Page.loadEventFired";
    await this._browser._cdp.waitForEvent(method, this._sessionId, () => true, timeout);
  }

  async evaluate(expression, arg = undefined) {
    let source;
    if (typeof expression === "function") {
      source = "(" + expression.toString() + ")";
    } else {
      source = String(expression).trim();
    }
    const call = arg === undefined
      ? (source.includes("=>") || source.startsWith("function") || source.startsWith("async function")
        ? `(async () => { const __rw_fn = (${source}); return await __rw_fn(); })()`
        : source)
      : `(async () => { const __rw_fn = (${source}); return await __rw_fn(${JSON.stringify(arg)}); })()`;
    const evaluated = await this._browser._cdp.send("Runtime.evaluate", {
      expression: call,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    }, this._sessionId);
    if (evaluated.exceptionDetails) {
      throw new Error(evaluated.exceptionDetails.text || "Runtime.evaluate failed");
    }
    return evaluated.result ? evaluated.result.value : undefined;
  }

  locator(selector) {
    return new RustwrightLocator(this, selector);
  }

  async close() {
    try {
      await this._browser._cdp.send("Target.closeTarget", { targetId: this._targetId });
    } catch (_) {
    }
  }
}

class RustwrightLocator {
  constructor(page, selector) {
    this._page = page;
    this._selector = selector;
  }

  async count() {
    return await this._page.evaluate(selector => document.querySelectorAll(selector).length, this._selector);
  }

  async textContent() {
    return await this._page.evaluate(selector => {
      const element = document.querySelector(selector);
      return element ? element.textContent : null;
    }, this._selector);
  }

  async fill(value) {
    return await this._page.evaluate(({ selector, value }) => {
      const element = document.querySelector(selector);
      if (!element) throw new Error("No element matches selector: " + selector);
      element.focus();
      element.value = value;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }, { selector: this._selector, value });
  }

  async click() {
    return await this._page.evaluate(selector => {
      const element = document.querySelector(selector);
      if (!element) throw new Error("No element matches selector: " + selector);
      element.click();
    }, this._selector);
  }
}

class RustwrightBrowser {
  constructor(process, profile, cdp, version) {
    this._process = process;
    this._profile = profile;
    this._cdp = cdp;
    this._version = version;
  }

  async version() {
    return this._version;
  }

  async newPage() {
    const target = await this._cdp.send("Target.createTarget", { url: "about:blank" });
    const attached = await this._cdp.send("Target.attachToTarget", { targetId: target.targetId, flatten: true });
    const page = new RustwrightPage(this, target.targetId, attached.sessionId);
    await Promise.all([
      this._cdp.send("Page.enable", {}, attached.sessionId),
      this._cdp.send("Runtime.enable", {}, attached.sessionId),
    ]);
    return page;
  }

  async close() {
    try {
      this._cdp.close();
    } catch (_) {
    }
    try {
      this._process.kill();
    } catch (_) {
    }
    if (this._profile) {
      try {
        fs.rmSync(this._profile, { recursive: true, force: true });
      } catch (_) {
      }
    }
  }
}

async function launch(options = {}) {
  if (typeof WebSocket !== "function") throw new Error("Node runtime does not expose global WebSocket");
  const executablePath = options.executablePath || process.env.RUSTWRIGHT_TS_EXECUTABLE_PATH;
  if (!executablePath) throw new Error("Rustwright TypeScript binding requires executablePath");
  const port = await pickPort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rustwright-ts-"));
  const browserProcess = spawn(executablePath, [
    "--remote-debugging-port=" + port,
    "--user-data-dir=" + profile,
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "about:blank",
  ], { stdio: "ignore" });
  const version = await waitForJson("http://127.0.0.1:" + port + "/json/version", options.timeout || 10000);
  const cdp = await connect(version.webSocketDebuggerUrl);
  return new RustwrightBrowser(browserProcess, profile, cdp, version.Browser || "");
}

module.exports = {
  chromium: { launch },
};
