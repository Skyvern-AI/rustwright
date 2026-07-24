# rustwright-mcp — the native Rustwright MCP server

A stateful [Model Context Protocol](https://modelcontextprotocol.io) stdio
server, written in Rust on the Rustwright engine. It gives any MCP client a
real Chromium browser through compact accessibility snapshots with element
refs (`e1`, `e2`, …), trusted physical input for clicks, and inline PNG
screenshots — no Python or Node runtime in the serving path.

This is the canonical Rustwright MCP server. The earlier Python server is
deprecated and will be removed once this server reaches full tool parity;
new capabilities land here only.

## Install

From source (needs a Rust toolchain):

```bash
cargo install --git https://github.com/Skyvern-AI/rustwright mcp-rs
```

The server binary is installed as `mcp-rs`. An npm distribution
(`rustwright-mcp`, prebuilt per-platform binaries, run with
`npx rustwright-mcp`) is prepared under [`npm/`](npm/) and is on the way.

### Browser

The server launches Chromium itself:

- Already have Chrome/Chromium? Set `RUSTWRIGHT_CHROMIUM` (or `CHROME` /
  `CHROMIUM`) to the executable path.
- Otherwise download a managed build once with
  `pip install rustwright && python -m rustwright install chromium` — the
  server finds it automatically.

## Configure your client

Claude Code:

```bash
claude mcp add rustwright -- mcp-rs
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows) or any
other MCP client:

```json
{
  "mcpServers": {
    "rustwright": {
      "command": "mcp-rs",
      "env": {
        "RUSTWRIGHT_CHROMIUM": "/path/to/chrome-or-chromium"
      }
    }
  }
}
```

Drop the `env` block if you installed the managed Chromium instead.

To verify: ask the client to list tools (the seven below should appear), then
try `browser_navigate` to `https://example.com` — the tool result is an
accessibility snapshot of the page.

## Tools

| Tool | What it does |
|---|---|
| `browser_navigate` | Navigate to a URL; returns a fresh snapshot. |
| `browser_navigate_back` | Go back in history; returns a fresh snapshot. |
| `browser_navigate_forward` | Go forward in history; returns a fresh snapshot. |
| `browser_snapshot` | Accessibility snapshot with element refs (`e1`, `e2`, …). |
| `browser_click` | Click an element by ref using trusted physical input; returns the post-click snapshot. |
| `browser_scroll` | Scroll an element into view by ref, or scroll the viewport by an amount; waits for the visual position to settle. |
| `browser_take_screenshot` | Capture the page as an inline PNG image content block. |

Refs are session-scoped and never reused, so a stale ref can never silently
point at a different element; snapshots include page values but mask password
fields. The tool set is growing toward parity with the deprecated Python
server (typing, dialogs, tabs, network introspection, and friends).

## Configuration

| Variable | Effect |
|---|---|
| `RUSTWRIGHT_CHROMIUM` / `CHROME` / `CHROMIUM` | Path to the browser executable to launch. |
| `RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES` | Largest screenshot returned inline. Oversized captures are written to a private (0600) temp file and the path is returned instead. |

## Development

`mcp-rs/` is a standalone Cargo workspace (it is not a member of the
repository's root workspace):

```bash
cd mcp-rs
cargo test --locked
```

The end-to-end tests launch Chromium; set `RUSTWRIGHT_CHROMIUM` if it is not
discoverable. The [`npm/`](npm/) directory holds the npm packaging for the
prebuilt-binary distribution.

## License

MIT, same as the repository. See [LICENSE](../LICENSE).
