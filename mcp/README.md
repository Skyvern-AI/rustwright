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
cargo install --git https://github.com/Skyvern-AI/rustwright rustwright-mcp
```

The server binary is installed as `rustwright-mcp`. An npm distribution
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
claude mcp add rustwright -- rustwright-mcp
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows) or any
other MCP client:

```json
{
  "mcpServers": {
    "rustwright": {
      "command": "rustwright-mcp",
      "env": {
        "RUSTWRIGHT_CHROMIUM": "/path/to/chrome-or-chromium"
      }
    }
  }
}
```

Drop the `env` block if you installed the managed Chromium instead.

To verify: ask the client to list tools (the 22 below should appear in the
default `mirror` profile), then
try `browser_navigate` to `https://example.com` — the tool result is an
accessibility snapshot of the page.

## Tools

| Tool | What it does |
|---|---|
| `browser_navigate` | Navigate to a URL; returns a fresh snapshot. |
| `browser_navigate_back` | Go back in history; returns a fresh snapshot. |
| `browser_navigate_forward` | Go forward in history; returns a fresh snapshot. |
| `browser_reload` | Reload the active page; returns a fresh snapshot. |
| `browser_resize` | Resize the active viewport in CSS pixels. |
| `browser_snapshot` | Full or targeted accessibility snapshot with element refs (`e1`, `e2`, …), optional depth limiting, and optional boxes. |
| `browser_find` | Search a fresh snapshot by text or JavaScript regular expression. |
| `browser_click` | Click or double-click an element by ref using trusted physical input. |
| `browser_scroll` | Scroll an element into view by ref, or scroll the viewport by an amount; waits for the visual position to settle. |
| `browser_type` | Clear, fill, append to, or slowly type into an element, optionally submitting with Enter. |
| `browser_select_option` | Select one or more option values or labels. |
| `browser_fill_form` | Fill a sequential batch of text, checkbox, radio, combobox, and slider fields. |
| `browser_hover` | Move Chromium's native pointer over an element. |
| `browser_press_key` | Press a native browser key or modifier chord. |
| `browser_drop` | Dispatch a `DataTransfer` drop containing MIME strings and/or workspace-confined files. |
| `browser_tabs` | List, open, select, or close tabs with stable indices. |
| `browser_handle_dialog` | Accept or dismiss the currently pending JavaScript dialog. |
| `browser_wait_for` | Wait for time, visible text, or text disappearance. |
| `browser_get_text` | Return rendered text for a unique CSS selector. |
| `browser_evaluate` | Evaluate a JavaScript function in the page or element-ref context. |
| `browser_take_screenshot` | Capture the page as inline PNG or JPEG image content. |
| `browser_close` | Close the browser; the next browser operation starts a fresh session. |

Refs are session-scoped and never reused, so a stale ref can never silently
point at a different element; snapshots include page values but mask password
fields. Operations that encounter a JavaScript modal return promptly with the
pending dialog details and defer further page work until
`browser_handle_dialog` resolves it.

Set `RUSTWRIGHT_MCP_TOOLSET=lean` to expose the smaller interaction-oriented
profile. The default `mirror` profile exposes all 27 native tools.
`browser_evaluate` can be removed from either profile by setting
`RUSTWRIGHT_MCP_ALLOW_EVAL=false`.

## Configuration

| Variable | Effect |
|---|---|
| `RUSTWRIGHT_CHROMIUM` / `CHROME` / `CHROMIUM` | Path to the browser executable to launch. |
| `RUSTWRIGHT_MCP_CDP_ENDPOINT` | Connect to an existing browser through its HTTP CDP endpoint instead of launching locally. |
| `RUSTWRIGHT_MCP_CDP_HEADERS` | JSON object containing headers for the remote CDP connection. |
| `RUSTWRIGHT_MCP_CDP_TIMEOUT_MS` | Positive remote connection timeout in milliseconds. |
| `RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS` | Browser tool timeout in milliseconds. |
| `RUSTWRIGHT_MCP_TOOLSET` | Tool profile: `mirror` (default) or `lean`. |
| `RUSTWRIGHT_MCP_ALLOW_EVAL` | Enable or disable `browser_evaluate`; defaults to enabled. |
| `RUSTWRIGHT_MCP_WORKSPACE` | Absolute directory that confines file paths supplied to `browser_drop`. |
| `RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES` | Largest screenshot returned inline. Oversized captures are written to a private (0600) temp file and the path is returned instead. |
| `RUSTWRIGHT_MCP_BUDGET` | `on` enables client-aware text response shaping; defaults to `off`. Codex product ids (`codex`, `codex-mcp-client`, and `codex-*`) use exact 9 KiB/200-line JSON-RPC limits. Unknown clients remain unbounded unless an explicit nonzero limit is set. |
| `RUSTWRIGHT_MCP_MAX_RESPONSE_BYTES` | Overrides the profile byte limit. `0` disables this dimension; nonzero values below 4096 or invalid values warn and use the profile default. |
| `RUSTWRIGHT_MCP_MAX_RESPONSE_LINES` | Overrides the decoded text line limit. `0` disables this dimension; nonzero values below 16 or invalid values warn and use the profile default. |
| `RUSTWRIGHT_MCP_CONSOLE_DEDUP` | `on` collapses adjacent inline console duplicates by severity and normalized text. File exports remain verbatim. Defaults to `off`. |
| `RUSTWRIGHT_MCP_NET_NOTE` | `on` reports successful static requests hidden after regex filtering. `static:true` still returns them. Defaults to `off`. |
| `RUSTWRIGHT_MCP_DISTILL` | `on` enables bounded full-tree construction, full-subset refs/find, and render-only snapshot distillation. `off` selects the retained legacy traversal before page-side ref mutation. Defaults to `off`. |
| `RUSTWRIGHT_MCP_HEADER` | `on` prepends a change-triggered `### Page` digest with the active URL, title when available, navigation status, and console error/warning counts. Unchanged page state is not repeated. Defaults to `off`. |
| `RUSTWRIGHT_MCP_LEAN_DESCRIPTIONS` | `on` serves shorter descriptions with narrowing guidance; `off` serves the byte-compatible legacy catalog. When unset or invalid, recognized Codex clients default to `on` and other clients to `off`; invalid values warn. |

Budgeting is applied to text success results, browser errors, and validation or
unknown-tool JSON-RPC errors. Image blocks and explicit file-output contracts are
unchanged. Explicit response dimensions take precedence over the matched client
profile; `RUSTWRIGHT_MCP_BUDGET=off` bypasses all response shaping.

The Codex treatment deployment is an explicit all-on configuration; peer
identification alone defaults only lean descriptions to `on`:

```text
RUSTWRIGHT_MCP_BUDGET=on
RUSTWRIGHT_MCP_DISTILL=on
RUSTWRIGHT_MCP_HEADER=on
RUSTWRIGHT_MCP_CONSOLE_DEDUP=on
RUSTWRIGHT_MCP_NET_NOTE=on
RUSTWRIGHT_MCP_LEAN_DESCRIPTIONS=on
RUSTWRIGHT_MCP_TOOLSET=mirror
```

Treated console presentation attributes messages to the first page frame;
legacy presentation preserves Chromium's raw top-frame attribution.

The hermetic configuration regression fixture records the initialize frame and
capture metadata observed from Codex CLI 0.146.0. CI parses that static fixture;
it does not execute Codex or require network access, authentication, or a
browser. Live recapture is a manual release compatibility check when updating
supported client metadata. The fixture verifies profile selection only and does
not establish or quantify token savings.

Snapshot traversal executes in the page's main JavaScript world. This preserves
the existing trust boundary: page scripts can observe or patch the traversal.
With distillation enabled, construction stops after 50,000 visited elements or
250 ms in-page and reports incomplete coverage. Refs cover only the constructed
subset. Semantic and explicit click markers are detected, followed by the
nearest `cursor:pointer` boundary; targets discoverable only through
`addEventListener` remain a known miss. Render distillation never shortens hrefs.

## Development

`mcp/` is a standalone Cargo workspace (it is not a member of the
repository's root workspace):

```bash
cd mcp
cargo test --locked
```

The end-to-end tests launch Chromium; set `RUSTWRIGHT_CHROMIUM` if it is not
discoverable. The [`npm/`](npm/) directory holds the npm packaging for the
prebuilt-binary distribution.

## License

MIT, same as the repository. See [LICENSE](../LICENSE).
