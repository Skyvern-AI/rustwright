<div align="center">

<img src="docs/assets/banner.png" alt="Rustwright — a drop-in replacement for Playwright" width="840" />

**A Rust rewrite of Playwright**, a popular browser automation library. Rustwright is interoperable with Playwright but runs on an in-process Rust CDP engine — **[2.55× faster](#benchmarks)** and **[70% less memory](BENCHMARK.md#client-memory-form-fill-diagnostic)** (no Node driver), with no Playwright automation fingerprint. Alpha; Chromium-only.

[![status: alpha](https://img.shields.io/badge/status-alpha-orange)](#project-status)
[![tests](https://img.shields.io/github/actions/workflow/status/Skyvern-AI/rustwright/test.yml?label=tests)](https://github.com/Skyvern-AI/rustwright/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![npm](https://img.shields.io/npm/v/rustwright?logo=npm&label=npm)](https://www.npmjs.com/package/rustwright)
[![Chromium only](https://img.shields.io/badge/browser-Chromium-4285F4?logo=googlechrome&logoColor=white)](#limitations)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/fG2XXEuQX3)

</div>

---

## What is Rustwright?

Rustwright is a browser automation library for Python and Node.js that keeps the Playwright API you already know but drives Chromium from a **native Rust engine** speaking raw [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) — no driver subprocess in the path.

```text
playwright-python:  your code ──pipe──► Node driver ──CDP──► Chromium
rustwright:         your code ────────── raw CDP ──────────► Chromium
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

Install from npm:

```bash
npm install rustwright
```

The Node binding drives an existing Chromium/Chrome — point Rustwright at it with `RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM`. Prefer to build from source? `git clone` the repo and run `npm install && npm run build` in `node/`.

```diff
- import { chromium } from 'playwright';
+ import { chromium } from 'rustwright';

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('https://example.com');
  console.log(await page.title());
  await browser.close();
```

Only a subset of the API surface is bridged — see [Limitations](#limitations).

## Why Rustwright?

<div align="center">

<img src="docs/assets/rustwright_vs_playwright.gif" alt="Rustwright vs playwright-python live demo" width="360" />

</div>

- **No Node driver subprocess.** `playwright-python` launches and pipes to a bundled Node driver. Rustwright's engine is native — the browser-control code runs in-process.
- **Raw CDP, in Rust.** A from-scratch async CDP client — not a wrapper around another automation library.
- **No Playwright automation fingerprint.** The driver never loads, so its signatures never appear. See [Automation detection](#automation-detection).
- **Trusted input by default.** Clicks and typing go through real CDP input events (`Input.dispatchMouseEvent`), not synthetic `element.click()` DOM calls. Untrusted DOM shortcuts are opt-in only.
- **Cross-origin iframes (OOPIF).** Auto-attaches out-of-process iframe targets with flattened CDP sessions and routes `frame_locator()` across origins.
- **One engine, two languages.** The same Rust core backs the Python and Node bindings.

## How it works

One Rust core — an async CDP client built on Tokio (WebSocket, with opt-in Unix-pipe transport) — talks to Chromium directly, and thin [PyO3](https://pyo3.rs) (Python) and [napi-rs](https://napi.rs) (Node) bindings expose it in-process. The two-line diagram above is the entire architecture.

Already have a Chromium/Chrome binary? Point Rustwright at it with `RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM`.

## Browser automation for AI agents

Give an agent or shell script a browser through compact accessibility snapshots with element refs (`e1`, `e2`, …), instead of raw HTML or screenshots. Refs are session-scoped, never reused, and best-effort rather than a security boundary; snapshots include page values but mask password fields.

### MCP server — give your agent a browser

`rustwright-mcp` gives any MCP client `browser_*` tools over stdio.

#### Fastest path

##### Claude Code

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  -- uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp
```

Uses the Chrome you already have — no browser download.

##### Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows, then use:

```json
{
  "mcpServers": {
    "rustwright": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp",
        "rustwright-mcp"
      ],
      "env": {
        "RUSTWRIGHT_MCP_CHANNEL": "chrome"
      }
    }
  }
}
```

##### Any MCP client

```json
{
  "mcpServers": {
    "rustwright": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp",
        "rustwright-mcp"
      ],
      "env": {
        "RUSTWRIGHT_MCP_CHANNEL": "chrome"
      }
    }
  }
}
```

| If... | Do this |
|---|---|
| You do not have Chrome installed | Drop `RUSTWRIGHT_MCP_CHANNEL`, then run `uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' python -m rustwright install chromium`. |
| You want a visible browser | Set `RUSTWRIGHT_MCP_HEADLESS=0`. |
| You want to disable page evaluation | Set `RUSTWRIGHT_MCP_ALLOW_EVAL=0`; see [Security & scope](mcp/README.md#security--scope). |

PyPI package coming soon — the command shrinks to `uvx rustwright-mcp`.

See [the MCP guide](mcp/README.md) for the full tool list and configuration.

Setting up via an AI agent? Point it at the one-page instructions in
[mcp/AGENT_SETUP.md](mcp/AGENT_SETUP.md): telling the agent "Fetch
`https://raw.githubusercontent.com/Skyvern-AI/rustwright/HEAD/mcp/AGENT_SETUP.md`
and follow it" installs the server in any major MCP client and walks through
verification.

### CLI — drive a browser from your shell

Drive one persistent Chromium session straight from the `rustwright` command, with no application code.

#### Try it in 60 seconds

```bash
pip install rustwright
python -m rustwright install chromium

rustwright open example.com
rustwright snapshot
rustwright click e1001
rustwright close
```

- `snapshot` shows refs; act by ref, then use the fresh snapshot returned after the action.
- Add `--json` for one JSON object per command when scripting.
- Use `--session NAME` for named browser sessions.

See [the agent interface guide](docs/agent-interfaces.md) for every verb, flag, and security detail.

## Remote browsers (Skyvern)

Rustwright drives browsers — but you still need somewhere to run them. Skyvern (the team behind Rustwright) offers hosted **[Browser Sessions](https://www.skyvern.com/docs/developers/features/browser-sessions)** as a paid service that funds this project.

**Features:**

- **Persistent cloud browsers** — logins, cookies, and tab state carry across runs
- **Configurable timeouts** — 5 minutes to 24 hours (60 min default)
- **Proxies in 21 countries**
- **Live view** — watch and interact with the session in the Skyvern Cloud UI

Each session returns a `browser_address` CDP endpoint that Rustwright connects to like any remote Chromium (sessions bill while open).

**Get started:**

1. Make an account at [app.skyvern.com](https://app.skyvern.com)
2. Grab your API key from **Settings**
3. `pip install skyvern`

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

> Remote sessions are Python-only for now — Rustwright's Node binding doesn't support `connect_over_cdp` yet (it's on the [Roadmap](#roadmap)).

## Automation detection

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

The headline numbers are local diagnostics, not yet capped-CI evidence. On speed, one dev-host run (warm browser, 5 iterations) won 16 of 17 case means:

| Run | Cases | Rustwright | playwright-python | Speedup |
|---|---:|---:|---:|---:|
| Local dev host (warm browser, 5 iterations) | 17 | 5,256 ms | 13,418 ms | **[2.55×](BENCHMARK.md#local-diagnostic-trusted-input-default)** |

Treat it as a diagnostic, not a launch claim — it is not capped-Docker/CI evidence. Methodology: [`BENCHMARK.md`](BENCHMARK.md).

On memory, a [form-fill diagnostic](BENCHMARK.md#client-memory-form-fill-diagnostic) recorded the client library's footprint at **133.5 MiB for playwright-python (Python + Node driver) versus 40.6 MiB for Rustwright (no driver) — about 70% less**; a separate [async-concurrency diagnostic](docs/async-design.md#update-high-concurrency-fixes-2026-07) measured ~66% less on the same client-stack basis. Both cover the part the library controls — Chromium-dominated whole-process memory is roughly equal — and both are demo-grade diagnostics, not capped-CI evidence.

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
- **API coverage** — ~96% of Playwright's Python sync API (**515 of 536** methods; **411** exercised by the shared parity registry); the async API provides **488 of 536**. Full report: [`docs/PARITY.md`](docs/PARITY.md).
- **Chromium only** — Firefox and WebKit error explicitly.
- **Node bindings are early** — a subset of the surface is bridged (`launch`, `newPage`, `goto`, `click`, `fill`, `title`, `textContent`, `evaluate`, `screenshot`, `close`); contexts, routing, tracing, and locators are Python-only for now.
- **Async concurrency (Python)** — the async API wraps the sync engine via threads; recommended for **≈≤25 concurrent workflows/process**, not high fan-out.
- **OOPIF** — residual gaps in non-main-frame `JSHandle` follow-ups and drag/screenshot/bounding-box.
- **Automation detection is partial** — 3 of 4 public fingerprint targets clean in local runs (CreepJS still detects headless). **No undetectability promise.**

## Roadmap

- [ ] **Kotlin binding** — idiomatic Kotlin wrapper (Kotlin/JVM can already consume the Java FFM binding)
- [ ] Grow the new language bindings beyond the alpha subset (contexts, routing, locators)
- [x] **Rustwright MCP server** — expose browser automation as tools for MCP-compatible AI agents ([mcp/](mcp/))
- [ ] CI / Testbox-backed benchmark evidence
- [ ] Broaden the Node.js surface (contexts, routing, locators)
- [ ] Close remaining OOPIF gaps

Recently shipped:

- [x] **Language bindings (alpha)** — Go, Java, C#/.NET, Ruby, and PHP over a shared C ABI, plus a native Rust API ([`bindings/CONTRACT.md`](bindings/CONTRACT.md)); cross-language equivalence gated in CI
- [x] Python package published to PyPI
- [x] Node.js binding published to npm
- [x] Native async engine over the Tokio CDP core
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
