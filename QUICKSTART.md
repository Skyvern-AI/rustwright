# Rustwright quickstart

Rustwright is an alpha, Chromium-only project. The Python package is on PyPI
and the Node.js binding is on npm; you can also
build from source, as shown below. Review the known
[limitations](LIMITATIONS.md) before depending on it in production.

## Agent-assisted setup

Paste this into Claude Code or Codex:

> Set up Rustwright for Python from https://github.com/Skyvern-AI/rustwright. If the repository already exists, use the current checkout; otherwise clone it. Read `QUICKSTART.md` and `LIMITATIONS.md` first. Use a repository-local `.venv` and do not change global Python packages or shell configuration. Verify Python 3.8+ and Rust 1.85+, build with maturin, install Chromium, run `python examples/quickstart.py`, and report the exact output or blocker. Do not modify or commit source files.

## Manual Python setup

### 1. Install prerequisites

You need:

- [Git](https://git-scm.com/)
- Python 3.8 or newer
- A [Rust toolchain](https://rustup.rs/) 1.85 or newer, including the platform
  build tools recommended by `rustup`

### 2. Clone and build

```bash
git clone https://github.com/Skyvern-AI/rustwright
cd rustwright
python3 -m venv .venv   # Windows: use `python` · Debian/Ubuntu: requires the python3-venv package
```

Activate the virtual environment on macOS or Linux:

```bash
source .venv/bin/activate
```

Or activate it in Windows PowerShell (if activation is blocked by the execution
policy, run `Set-ExecutionPolicy -Scope Process Bypass` first):

```powershell
.\.venv\Scripts\Activate.ps1
```

With the environment active, build and install Rustwright. The first build
compiles the Rust engine and typically takes a few minutes:

```bash
python -m pip install -U pip maturin
maturin develop --release
```

### 3. Install Chromium and verify the build

On **Debian/Ubuntu**, install Chromium's system libraries first (apt-based
distros only; the command self-elevates with `sudo`). Other distros install the
equivalent Chromium runtime packages through their own package manager, and
macOS/Windows need none:

```bash
python -m rustwright install-deps chromium   # Debian/Ubuntu only
```

Then, on any platform, download Chromium and run the smoke example:

```bash
python -m rustwright install chromium
python examples/quickstart.py
```

The example uses a local `data:` URL, so it makes no network requests once the
build dependencies and Chromium are installed. A successful run prints:

```text
Rustwright works
```

If Chrome or Chromium is already installed, Rustwright can usually discover it.
You can also set `RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM` to the browser
executable before running the example.

### 4. Try it in existing Playwright code

For Python code that stays within Rustwright's supported surface, start by
changing the import:

```diff
- from playwright.sync_api import sync_playwright
+ from rustwright.sync_api import sync_playwright
```

The async entrypoint is available from `rustwright.async_api`. If changing
imports is inconvenient, call `rustwright.enable_playwright_compat()` before
importing `playwright`; this compatibility mode is opt-in and may evolve before
beta.

### 5. Drive a browser from an agent (CLI)

The `rustwright` CLI keeps one browser alive across agent commands:

```bash
rustwright open example.com    # launch + navigate; prints an accessibility snapshot
rustwright snapshot            # accessibility tree with refs (e1, e2, …)
rustwright click e3            # act on an element by its ref
rustwright --json snapshot     # one JSON object, for scripting
rustwright close               # shut the session down
```

The CLI verbs and the MCP server's tools are the same surface.

See [docs/agent-interfaces.md](docs/agent-interfaces.md) for the CLI verbs,
configuration, threat model, and current scope. An MCP server for Rustwright is
available as a separate, opt-in package (`rustwright-mcp`).
After installing both packages, start the stdio server with `rustwright mcp`.

## Node.js (experimental)

The Node.js binding is published to npm. It
has no browser downloader and exposes only the subset listed in
[`node/README.md`](node/README.md). Install Chrome/Chromium or set
`RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM` to an existing executable, then:

```bash
npm install rustwright
```

To build from source instead — you need a recent Node.js (LTS recommended) plus
the Rust toolchain from the Python prerequisites — from the repository root:

```bash
cd node
npm install
npm run build
npm run smoke
```

## Troubleshooting

### `ModuleNotFoundError: No module named 'rustwright'`

Activate the same virtual environment used for the build, then rerun:

```bash
maturin develop --release
```

### Rustwright cannot find Chromium

Run `python -m rustwright install chromium`, install Chrome/Chromium manually,
or set `RUSTWRIGHT_CHROMIUM` to the executable's absolute path.

### Firefox or WebKit fails

Rustwright deliberately supports Chromium only. Firefox and WebKit entrypoints
return an explicit unsupported-browser error.
