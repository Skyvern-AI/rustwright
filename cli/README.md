# Rustwright agent CLI

`rustwright-cli` is a native, agent-focused interface to the Rustwright CDP engine. CLI commands
share a persistent local Chromium session.

## Install

The quickest path is the install script, which grabs a prebuilt binary (and falls back to building
from source when a prebuilt binary is not published for your platform):

```bash
curl -fsSL https://raw.githubusercontent.com/Skyvern-AI/rustwright/main/install.sh | sh
```

To build from source explicitly (requires Rust 1.88 or newer):

```bash
cargo install --path cli
```

Install Chromium with `python -m rustwright install chromium`, use a system Chrome/Chromium, or set
`RUSTWRIGHT_CHROMIUM`, `CHROME`, or `CHROMIUM` to an executable path.

## CLI

```bash
rustwright-cli open https://example.com
rustwright-cli snapshot
rustwright-cli click @e1
rustwright-cli fill @e2 "hello"
rustwright-cli text body
rustwright-cli title
rustwright-cli url
rustwright-cli eval "document.querySelectorAll('a').length"
rustwright-cli screenshot page.png --full-page
rustwright-cli close
```

The first browser command starts a localhost daemon. Its authenticated connection metadata is stored
in a temporary directory; on Unix, the directory and state files use user-only permissions. A stale
state file is discarded automatically. Useful global options:

- `--session <name>` isolates concurrent sessions. `RUSTWRIGHT_SESSION` sets the default.
- `--json` returns one JSON response per command.
- `open --headed` shows the browser.
- `open --executable-path <path>` selects a Chromium executable for a new session.

Snapshots list visible page content and attach `@eN` references to interactive elements. References
belong to the latest snapshot and should be refreshed after navigation or major page changes. Direct
CSS selectors and `text=` selectors remain available when a snapshot reference is not convenient.
References are stored as temporary DOM attributes so later CLI commands can resolve them; pages that
observe DOM attribute changes can detect those markers.

## MCP server

Looking for an MCP server? Rustwright ships a dedicated, standalone MCP server as the
`rustwright-mcp` package (see [`mcp-rs/`](../mcp-rs)). This CLI focuses on the interactive agent
command surface; the two share the same Rustwright CDP engine.
