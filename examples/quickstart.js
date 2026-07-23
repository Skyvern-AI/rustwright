// --- Source-checkout bootstrap -------------------------------------------
// Installed from npm? Delete this block and use:
// const { chromium } = require('rustwright');
// ESM projects ("type": "module") use: import { chromium } from 'rustwright';
let rustwrightModule = 'rustwright';
try {
  require.resolve(rustwrightModule);
} catch (error) {
  const firstLine = error instanceof Error ? error.message.split(/\r?\n/, 1)[0] : '';
  if (error?.code !== 'MODULE_NOT_FOUND' || firstLine !== "Cannot find module 'rustwright'") {
    throw error;
  }
  rustwrightModule = '../node/index.cjs';
}
const { chromium } = require(rustwrightModule);
// --- End source-checkout bootstrap ---------------------------------------

async function main() {
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const screenshotPath = join(tmpdir(), `rustwright-quickstart-${process.pid}.png`);

  // Browser discovery checks RUSTWRIGHT_CHROMIUM, CHROME, then CHROMIUM.
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    try {
      await page.goto('data:text/html,%3Ctitle%3ERustwright%20works%3C/title%3E');
      const title = await page.title();
      await page.screenshot({ path: screenshotPath });
      console.log(`title: ${title}`);
      console.log(`screenshot: ${screenshotPath}`);
    } finally {
      await page.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
