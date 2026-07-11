<div align="center">

<img src="docs/assets/banner.png" alt="Rustwright — Keep the Playwright API. Drop the driver." width="840" />

**A Playwright-shaped API backed by an in-process Rust CDP engine — without Playwright's Node driver subprocess or its detectable automation fingerprint. Python first; Node.js bindings are experimental.**

[![status: alpha](https://img.shields.io/badge/status-alpha-orange)](#project-status)
[![tests](https://img.shields.io/github/actions/workflow/status/Skyvern-AI/rustwright/test.yml?label=tests)](https://github.com/Skyvern-AI/rustwright/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Node.js](https://img.shields.io/badge/node.js-experimental-5FA04E?logo=node.js&logoColor=white)](node/)
[![Chromium only](https://img.shields.io/badge/browser-Chromium-4285F4?logo=googlechrome&logoColor=white)](#limitations)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/fG2XXEuQX3)

</div>

---

> [!WARNING]
> **Alpha.** Rustwright is Chromium-only and must currently be built from source. Need Firefox, WebKit, or production maturity today? Use [`playwright-python`](https://github.com/microsoft/playwright-python). See [Limitations](#limitations).

## Change one import

For Python code that stays within Rustwright's supported API surface, migration often starts with one import change.

**Python**

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

**Node.js (experimental subset)**

```diff
- import { chromium } from 'playwright';
+ import { chromium } from 'rustwright';

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('https://example.com');
  console.log(await page.title());
  await browser.close();
```

The growing Python parity suite runs the same cases against Rustwright and real Playwright; full behavioral parity is still in progress. `rustwright.async_api` provides a Playwright-shaped async facade for the supported Python surface (concurrency notes in [Limitations](#limitations)).

Prefer not to touch imports at all? Python offers an opt-in shim — `rustwright.enable_playwright_compat()` — that redirects `import playwright...` to Rustwright at runtime. This compatibility API may evolve before beta.

## What is Rustwright?

Rustwright is a browser automation library with a broad Playwright-shaped Python API and an experimental Node.js subset. It drives Chromium from a **native Rust engine** speaking raw [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) (CDP) — no driver subprocess in the path:

```text
playwright-python:  your code ──pipe──► Node driver (separate process) ──CDP──► Chromium
rustwright:         your code ────────────────── raw CDP ─────────────────────► Chromium
```

That two-line diagram is the entire architecture: one Rust core — an async CDP client built on Tokio (WebSocket, with opt-in Unix-pipe transport) — talks to Chromium directly, and thin [PyO3](https://pyo3.rs) (Python) and [napi-rs](https://napi.rs) (Node) bindings expose it in-process.

## Why Rustwright?

- **No Node driver subprocess.** `playwright-python` drives the browser through a bundled Node driver process, so a call crosses your code → Node → Chromium. Rustwright's browser-control code runs in-process and talks to Chromium directly — it removes that middle process (and the Node.js runtime from your Python container image), not the browser process itself.
- **No Playwright driver signatures.** The driver never loads, so pages can't fingerprint it. See [Signal hygiene](#signal-hygiene).
- **Trusted input by default.** Clicks and typing go through real CDP input events (`Input.dispatchMouseEvent`), so pages receive the same trusted events a human produces — not synthetic `element.click()` DOM calls. Untrusted DOM shortcuts are opt-in only.
- **Cross-origin iframes (OOPIF).** Out-of-process iframes are attached automatically and reachable through `frame_locator()` across origins.
- **Raw CDP, in Rust.** A from-scratch async CDP client, not a wrapper around another automation library — no second automation stack underneath to version-match or debug through.
- **One engine, many languages.** The same Rust core can back bindings in many languages; today it powers Python and an experimental Node.js binding, so a fix or speedup in the core lands in every binding at once.

**Use Rustwright when** you automate Chromium and the Playwright driver subprocess — its footprint, its process management, or its fingerprint — is your problem. **Use [`playwright-python`](https://github.com/microsoft/playwright-python) when** you need Firefox/WebKit, high async fan-out, or production maturity today.

## Quickstart (Python)

### Ask Claude Code or Codex

Paste this into your coding agent:

> Set up Rustwright for Python from https://github.com/Skyvern-AI/rustwright. If the repository already exists, use the current checkout; otherwise clone it. Read `QUICKSTART.md` and `LIMITATIONS.md` first. Use a repository-local `.venv` and do not change global Python packages or shell configuration. Verify Python 3.8+ and Rust 1.85+, build with maturin, install Chromium, run `python examples/quickstart.py`, and report the exact output or blocker. Do not modify or commit source files.

### Or set it up manually

You need [Git](https://git-scm.com/), Python 3.8+, and a [Rust toolchain](https://rustup.rs/) 1.85+ with the platform build tools `rustup` recommends. Expect the first build to compile the Rust engine (a few minutes) and the browser install to download Chromium, so plan for network access.

```bash
git clone https://github.com/Skyvern-AI/rustwright
cd rustwright
python3 -m venv .venv   # Windows: use `python` · Debian/Ubuntu: requires the python3-venv package
```

Activate the environment:

```bash
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows PowerShell (if activation is blocked: Set-ExecutionPolicy -Scope Process Bypass)
.\.venv\Scripts\Activate.ps1
```

Then build and install Rustwright:

```bash
python -m pip install -U pip maturin
maturin develop --release               # compiles the Rust engine — first build takes a few minutes
```

On **Debian/Ubuntu**, now install Chromium's system libraries — this uses the Rustwright CLI you just built (apt-based distros only; it self-elevates with `sudo`). Other distros install the equivalent packages through their own package manager; macOS and Windows need none:

```bash
python -m rustwright install-deps chromium   # Debian/Ubuntu only
```

Then, on any platform, download Chromium and run the included smoke example:

```bash
python -m rustwright install chromium   # downloads a Chromium build
python examples/quickstart.py
# Rustwright works
```

For platform notes, an experimental Node.js path, and troubleshooting, see the [full quickstart](QUICKSTART.md).

## Node.js (experimental)

The Node.js package is not published and only a small Playwright-shaped surface is bridged. You need a recent Node.js (LTS recommended) **plus the Rust toolchain from the Python prerequisites** — `npm run build` compiles the native addon via napi-rs. The Node binding does not download a browser, so **before the smoke step** install Chrome/Chromium or point `RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM` at an existing executable. Then, from the repository root:

```bash
cd node
npm install
npm run build
npm run smoke
```

See [`node/README.md`](node/README.md) for the supported browser-discovery methods and local-package setup.

Publishing to PyPI and npm is the top roadmap item; [star or watch the repo](https://github.com/Skyvern-AI/rustwright) to catch the release.

## Signal hygiene

*Signal hygiene* means not leaving the incidental fingerprints an automation stack ships with — a narrower promise than bypassing bot defenses, which Rustwright does not make. Because Rustwright never loads Playwright's Node driver, it never emits the signatures that ship with it:

- **No Playwright driver signatures** — no `__playwright__binding__` globals in pages or in Playwright's hidden script contexts ("utility worlds"), no driver bootstrap. The backend reports `playwright_driver: "none"`.
- **No `Runtime.enable` on the default path** — a normal launch + navigate never enables the CDP Runtime domain. Enabling it changes how Chromium serializes console output, and public probes ([DeviceAndBrowserInfo's](https://deviceandbrowserinfo.com/are_you_a_bot) `isAutomatedWithCDP` check) detect exactly that. Console/page-error/binding opt-ins still enable it lazily — detectable by design.
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

Rustwright does not headline a speed number yet. Performance claims are held to a simple standard: reproducible runs on isolated, resource-capped CI runners, with the environment, versions, case list, and per-case results published alongside the number — and producing that evidence is a [roadmap](#roadmap) item. Benchmark methodology and the capped-Docker path: [`BENCHMARK.md`](BENCHMARK.md).

## Rustwright vs the alternatives

| | Rustwright | [playwright-python](https://github.com/microsoft/playwright-python) | [Puppeteer](https://pptr.dev/) | [Patchright](https://pypi.org/project/patchright/) |
|---|---|---|---|---|
| **API** | Playwright-shaped (Py + Node) | Official Python Playwright | JS/TS Puppeteer | Playwright drop-in fork |
| **Engine / transport** | Rust core, raw CDP | Python → Node driver | Node over CDP | Patched PW driver |
| **In-process engine (no driver subprocess)** | ✅ | ❌ bundled Node driver | ✅ Node is the runtime | ❌ Playwright-style driver |
| **Browsers** | Chromium only | Chromium, Firefox, WebKit | Chrome, Firefox | Chromium-based |
| **Default input** | Trusted CDP events | Browser-level | Browser / CDP | Playwright + stealth |
| **Cross-origin iframes** | OOPIF (alpha) | Mature | Frame APIs | Inherits Playwright |
| **Playwright driver signatures** | Not loaded | Loaded | n/a | Patched |
| **Maturity** | 🟠 Alpha | 🟢 Mature | 🟢 Mature | 🟡 Focused fork |

Rustwright's lane: **a Rust CDP engine under the Playwright API, for Chromium.**

## Limitations

See [`LIMITATIONS.md`](LIMITATIONS.md) for detail.

- **Alpha** — API shape covered; full **behavioral** parity not yet proven.
- **Chromium only** — Firefox and WebKit error explicitly.
- **Node bindings are early** — a small Playwright-shaped subset is bridged; [`node/README.md`](node/README.md) keeps the current method list. Contexts, routing, tracing, and locators are Python-only for now.
- **Async concurrency (Python)** — the async API wraps the sync engine via threads; recommended for **≈≤25 concurrent workflows/process**, not high fan-out.
- **OOPIF** — residual gaps in non-main-frame `JSHandle` follow-ups and drag/screenshot/bounding-box.
- **Signal hygiene is partial** — 3 of 4 public fingerprint targets clean in local runs (CreepJS still detects headless). **No undetectability promise.**

## Roadmap

- [ ] **Publish to PyPI and npm** — top priority
- [ ] CI-backed benchmark evidence (isolated, resource-capped runners; published run records)
- [ ] Native async engine (remove the Python thread-pool bridge)
- [ ] Broaden the Node.js surface (contexts, routing, locators)
- [ ] Close remaining OOPIF gaps
- [ ] Split the core into maintainable modules

Recently shipped:

- [x] Cross-origin iframe (OOPIF) auto-attach
- [x] Shared Python parity suite green against real Playwright
- [x] `Runtime.enable` console-serialization leak closed on the default path

Firefox and WebKit are **not planned** — Rustwright is deliberately Chromium-only.

## Contributing

Rustwright is Rust + Python + Node. `cargo` builds the engine; `maturin develop --release` installs the Python package; `cd node && npm run build` builds the Node addon; the Python suite exercises the engine against real Chromium. CI (`test.yml`) runs a representative subset on every PR; the heavier Docker and cross-library parity gates are documented in the contributor guide.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for build details and the code-layout reality.

## Project status

Rustwright is an early alpha from [Skyvern](https://github.com/Skyvern-AI), developed in the open. If the architecture resonates, [give it a ⭐](https://github.com/Skyvern-AI/rustwright).

Questions, ideas, or want to help? Join the Skyvern community on [**Discord**](https://discord.gg/fG2XXEuQX3).

## License

[MIT](LICENSE) © 2026 Ikonomos Inc (dba Skyvern)

<div align="center">
<sub>Built with 🦀🐉 and a lot of CDP frames · <a href="https://github.com/Skyvern-AI/rustwright">Skyvern-AI/rustwright</a></sub>
</div>
