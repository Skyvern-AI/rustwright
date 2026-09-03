# Rustwright agent CLI

`rustwright-cli` is a native, agent-focused interface to Rustwright. It uses
the same browser actor as `rustwright-mcp` and keeps one local session alive.

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

Snapshots use the shared actor's accessibility hierarchy. The CLI renders actor
refs as `@eN` for command compatibility. A ref belongs to the latest snapshot.
Direct CSS selectors and `text=` selectors remain available.
References are stored as temporary DOM attributes so later CLI commands can resolve them; pages that
observe DOM attribute changes can detect those markers.

## MCP server

Looking for an MCP server? Rustwright ships `rustwright-mcp` as a separate
package (see [`mcp/`](../mcp)). The CLI and MCP server are thin transports over
the same native actor and CDP engine.
