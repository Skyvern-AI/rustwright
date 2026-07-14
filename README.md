<div align="center">

<img src="docs/assets/banner.png" alt="Rustwright — Keep the Playwright API. Drop the driver." width="840" />

**A Rust rewrite of Playwright** for Python and Node.js. Keep the API you know while an in-process Rust CDP engine drives Chromium — no Node driver subprocess and no Playwright-specific automation fingerprint. Alpha; Chromium-only.

[![status: alpha](https://img.shields.io/badge/status-alpha-orange)](#project-status)
[![tests](https://img.shields.io/github/actions/workflow/status/Skyvern-AI/rustwright/test.yml?label=tests)](https://github.com/Skyvern-AI/rustwright/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Node.js](https://img.shields.io/badge/node.js-experimental-5FA04E?logo=node.js&logoColor=white)](node/)
[![Chromium only](https://img.shields.io/badge/browser-Chromium-4285F4?logo=googlechrome&logoColor=white)](#limitations)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/fG2XXEuQX3)

</div>

---

## What is Rustwright?

Rustwright is a browser automation library for Python and Node.js that keeps the Playwright API you already know but drives Chromium from a **native Rust engine** speaking raw [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) — no driver subprocess in the path.

```text
playwright-python:  your code ──pipe──► Node driver (separate process) ──CDP──► Chromium
rustwright:         your code ────────────────── raw CDP ─────────────────────► Chromium
```

## Quickstart

Rustwright is interoperable with Playwright — install it, change one import, and your existing code runs on the Rust engine.

**Python**

```bash
pip install rustwright
python -m rustwright install chromium
```

```diff
- from playwright.sync_api import sync_playwright
+ from rustwright.sync_api import sync_playwright

  with sync_playwright() as p:
      browser = p.chromium.launch(headless=True)
      page = browser.new_page()
      page.goto("https://example.com")
      print(page.title())
      browser.close()
```

**Node.js** (experimental)

The Node.js binding is **not yet published on npm**. Build it from source:

```bash
git clone https://github.com/Skyvern-AI/rustwright && cd rustwright/node
npm install
npm run build
```

```diff
- import { chromium } from 'playwright';
+ import { chromium } from 'rustwright';

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('https://example.com');
  console.log(await page.title());
  await browser.close();
```

The build produces a local `rustwright` package; consume it from another project with `npm install /path/to/rustwright/node` (or `npm link`). Only a subset of the API surface is bridged — see [Limitations](#limitations).

**515/515** shared parity cases pass against real Playwright (growing suite; full behavioral parity in progress). `rustwright.async_api` mirrors Playwright's async API (concurrency notes in [Limitations](#limitations)).

## Why Rustwright?

- **No Node driver subprocess.** `playwright-python` launches and pipes to a bundled Node driver. Rustwright's engine is native — the browser-control code runs in-process.
- **Raw CDP, in Rust.** A from-scratch async CDP client — not a wrapper around another automation library.
- **No Playwright automation fingerprint.** The driver never loads, so its signatures never appear. See [Anti-bot](#anti-bot).
- **Trusted input by default.** Clicks and typing go through real CDP input events (`Input.dispatchMouseEvent`), not synthetic `element.click()` DOM calls. Untrusted DOM shortcuts are opt-in only.
- **Cross-origin iframes (OOPIF).** Auto-attaches out-of-process iframe targets with flattened CDP sessions and routes `frame_locator()` across origins.
- **One engine, two languages.** The same Rust core backs the Python and Node bindings.

## How it works

One Rust core — an async CDP client built on Tokio (WebSocket, with opt-in Unix-pipe transport) — talks to Chromium directly, and thin [PyO3](https://pyo3.rs) (Python) and [napi-rs](https://napi.rs) (Node) bindings expose it in-process. The two-line diagram above is the entire architecture.

Already have a Chromium/Chrome binary? Point Rustwright at it with `RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM`.

## Remote browsers (Skyvern)

Rustwright removes the local driver process; [Skyvern Browser Sessions](https://www.skyvern.com/docs/developers/features/browser-sessions) (from the team behind Rustwright) address the other half — hosting the browser. A session is a persistent cloud browser whose login, cookie, and tab state carry across runs, with configurable timeouts from 5 minutes to 24 hours (60 by default), proxies in 21 countries, and a live view in the Skyvern Cloud UI; sessions bill while open. Creating one returns a `browser_address` CDP endpoint, and Rustwright connects to it like any remote Chromium.

To get a `browser_address`: install the [Skyvern SDK](https://www.skyvern.com/docs/developers/getting-started/quickstart) (`pip install skyvern`) and copy an API key from [app.skyvern.com](https://app.skyvern.com) → Settings. Then:

```python
import asyncio
from rustwright.async_api import async_playwright
from skyvern import Skyvern

async def main():
    session = await Skyvern(api_key="<SKYVERN_API_KEY>").create_browser_session()

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(session.browser_address)
        page = await browser.new_page()
        await page.goto("https://example.com")

asyncio.run(main())
```

The sync API connects the same way: `p.chromium.connect_over_cdp(browser_address)`.

## Anti-bot

Because Rustwright never loads Playwright's Node driver, it never emits the automation signatures that ship with it:

- **No Playwright driver signatures** — no `__playwright__binding__` / utility-world globals, no driver bootstrap. The backend reports `playwright_driver: "none"`.
- **No `Runtime.enable` on the default path** — a normal launch + navigate never enables the CDP Runtime domain, closing the `Runtime.enable` console-serialization leak behind `isAutomatedWithCDP`. (Console/page-error/binding opt-ins still enable it lazily — detectable by design.)
- **Headless identity normalized by default** — launches with `--disable-blink-features=AutomationControlled`, rewrites `HeadlessChrome/` → `Chrome/` in the UA and client hints, and installs a `navigator.webdriver` cleanup init script.

Local fingerprint runs — default Playwright failed webdriver/headless checks that Rustwright passed; these are local diagnostics, not a guarantee:

| Probe | Result |
|---|---|
| SannySoft | ✅ Clean |
| BrowserScan | ✅ Clean |
| DeviceAndBrowserInfo | ✅ Clean (after the Runtime-domain cleanup) |
| CreepJS | ⚠️ Detects headless |

> [!IMPORTANT]
> **Rustwright is not "undetectable."** It is not a CAPTCHA or Cloudflare bypass, and it is not fully CDP-invisible — it still uses CDP primitives (`Target.setAutoAttach`, init scripts, and lazy `Runtime.enable` for console event/pageerror event/binding opt-ins). The claim is narrow: **no Playwright-specific automation fingerprint**, plus baseline signal hygiene.

## Benchmarks

Rustwright does not headline a speed number yet: launch-facing claims are held to reproducible, isolated CI evidence (Testbox + capped Docker), which is not yet published. One local dev-host diagnostic exists today, where Rustwright won 16/17 case means:

| Run | Cases | Rustwright | playwright-python | Speedup |
|---|---:|---:|---:|---:|
| Local dev host (warm browser, 5 iterations) | 17 | 5,256 ms | 13,418 ms | **[2.55×](BENCHMARK.md#local-diagnostic-trusted-input-default)** |

Treat it as a diagnostic, not a launch claim — it is not capped-Docker/CI evidence. Methodology: [`BENCHMARK.md`](BENCHMARK.md).

A separate local async-concurrency diagnostic measured [~41 MB for Rustwright versus ~121 MB for playwright-python's Python process plus Node driver — about 66% less client-side memory](docs/async-design.md#update-high-concurrency-fixes-2026-07). That comparison covers the part the library controls; whole-process memory is Chromium-dominated and varied by scenario. It is also diagnostic, not CI-backed launch evidence.

## Alternatives

| | Rustwright | playwright-python | Puppeteer | Patchright |
|---|---|---|---|---|
| **API** | Playwright-shaped (Py + Node) | Official Python Playwright | JS/TS Puppeteer | Playwright drop-in fork |
| **Engine / transport** | Rust core, raw CDP | Python → Node driver | Node over CDP | Patched PW driver |
| **In-process engine (no driver subprocess)** | ✅ | ❌ bundled Node driver | ✅ Node is the runtime | ❌ Playwright-style driver |
| **Browsers** | Chromium only | Chromium, Firefox, WebKit | Chrome, Firefox | Chromium-based |
| **Default input** | Trusted CDP events | Browser-level | Browser / CDP | Playwright + stealth |
| **Cross-origin iframes** | OOPIF (alpha) | Mature | Frame APIs | Inherits Playwright |
| **Playwright fingerprint** | No | Yes | n/a | Patched |
| **Maturity** | 🟠 Alpha | 🟢 Mature | 🟢 Mature | 🟡 Focused fork |

Rustwright's lane: **a Rust CDP engine under the Playwright API, for Chromium.**

## Limitations

See [`LIMITATIONS.md`](LIMITATIONS.md) for detail.

- **Alpha** — API shape covered; full **behavioral** parity not yet proven.
- **Chromium only** — Firefox and WebKit error explicitly.
- **Node bindings are early** — a subset of the surface is bridged (`launch`, `newPage`, `goto`, `click`, `fill`, `title`, `textContent`, `evaluate`, `screenshot`, `close`); contexts, routing, tracing, and locators are Python-only for now.
- **Async concurrency (Python)** — the async API wraps the sync engine via threads; recommended for **≈≤25 concurrent workflows/process**, not high fan-out.
- **OOPIF** — residual gaps in non-main-frame `JSHandle` follow-ups and drag/screenshot/bounding-box.
- **Anti-bot is partial** — 3 of 4 public fingerprint targets clean in local runs (CreepJS still detects headless). **No undetectability promise.**

## Roadmap

- [ ] **Publish the Node.js binding to npm**
- [ ] CI / Testbox-backed benchmark evidence
- [ ] Native async engine (remove the Python thread-pool bridge)
- [ ] Broaden the Node.js surface (contexts, routing, locators)
- [ ] Close remaining OOPIF gaps
- [ ] Split the core into maintainable modules

Recently shipped:

- [x] Python package published to PyPI
- [x] OOPIF auto-attach with flattened CDP sessions
- [x] 515/515 shared parity suite green against real Playwright
- [x] `Runtime.enable` console-serialization leak closed on the default path

Firefox and WebKit are **not planned** — Rustwright is deliberately Chromium-only.

## Contributing

Rustwright is Rust + Python + Node. `cargo` builds the engine; `maturin develop --release` installs the Python package; `cd node && npm run build` builds the Node addon; the Python suite exercises the engine against real Chromium. Full Docker gate: **1,046 tests pass** (6 skipped), plus **515/515** shared parity cases run against real Playwright; CI (`test.yml`) runs a fast representative subset on every PR.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for build details and the code-layout reality.

## Project status

Rustwright is an early alpha from [Skyvern](https://github.com/Skyvern-AI), developed in the open. If the architecture resonates, [give it a ⭐](https://github.com/Skyvern-AI/rustwright).

Questions, ideas, or want to help? Join the Skyvern community on [**Discord**](https://discord.gg/fG2XXEuQX3).

## License

[MIT](LICENSE) © 2026 Ikonomos Inc (dba Skyvern)

<div align="center">
<sub>Built with 🦀🐉 and a lot of CDP frames · <a href="https://github.com/Skyvern-AI/rustwright">Skyvern-AI/rustwright</a></sub>
</div>
