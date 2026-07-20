import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { chromium } from './index.mjs';

const screenshotPath = join(tmpdir(), `rustwright-node-smoke-${process.pid}.png`);
const html = `<!doctype html>
<html>
  <head><title>Rustwright Node Smoke</title></head>
  <body>
    <h1 id="message">ready</h1>
    <input id="name" />
    <button id="go" onclick="document.querySelector('#message').textContent = document.querySelector('#name').value">Go</button>
  </body>
</html>`;

const browser = await chromium.launch({ headless: true });
try {
  // Exercise the BrowserContext shim and page-level default timeouts alongside
  // the core page methods.
  const context = await browser.newContext();
  context.setDefaultTimeout(30_000);
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(30_000);
  await page.goto(`data:text/html,${encodeURIComponent(html)}`);
  const title = await page.title();
  const before = await page.textContent('#message');
  await page.fill('#name', 'Rustwright for Node');
  await page.click('#go');
  const after = await page.textContent('#message');
  const value = await page.evaluate(() => document.querySelector('#name').value);
  const screenshot = await page.screenshot({ path: screenshotPath });
  const contextPages = context.pages().length;
  await context.close();
  console.log(JSON.stringify({
    title,
    before,
    after,
    value,
    contextPages,
    screenshotBytes: screenshot.length,
    screenshotPath
  }, null, 2));
} finally {
  await browser.close();
}
