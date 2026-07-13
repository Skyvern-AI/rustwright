# Rustwright for Node

This package exposes Rustwright's Rust CDP core to Node.js through napi-rs.
It is not published to npm from this worktree.

Build locally:

```bash
npm install
npm run build
```

Use it as a Playwright-shaped Chromium entrypoint:

```js
const { chromium } = require('rustwright');

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.goto('https://example.com');
console.log(await page.title());
await browser.close();
```

For existing Playwright code that only needs the bridged surface, the opt-in
swap is a one-line import change:

```diff
- const { chromium } = require('playwright');
+ const { chromium } = require('rustwright');
```

Currently bridged: `chromium.launch()`, `browser.newPage()`, `page.goto()`,
`page.click()`, `page.fill()`, `page.title()`, `page.textContent()`,
`page.evaluate()`, `page.screenshot()`, `page.close()`, and `browser.close()`.
Not yet bridged: browser contexts, routes, downloads, tracing, workers, event
waiters, JS handles, locators as first-class objects, Firefox, and WebKit.
