# Rustwright MCP server

Exposes Rustwright browser automation as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so MCP-compatible agents (Claude Code, Claude Desktop, others) can browse
with Rustwright instead of Playwright.

Tool names mirror the Playwright MCP server (`browser_navigate`,
`browser_snapshot`, `browser_click`, ...) so agents can switch without
re-learning the surface. `browser_snapshot` returns an accessibility-style
outline where interactive elements carry `[ref=eN]` handles; pass a ref (or a
raw CSS selector) to the action tools.

## Quick start (no clone needed)

With [uv](https://docs.astral.sh/uv/) installed, register the server with
Claude Code in one command — `uvx` fetches and runs it straight from GitHub:

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  -- uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp
```

Note the `--` before the command: `--env` is variadic, so without the
separator it swallows the command and `claude mcp add` fails with
`missing required argument 'commandOrUrl'`.

Or add to any MCP client config (Claude Desktop, Cursor, etc.):

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
      "env": { "RUSTWRIGHT_MCP_CHANNEL": "chrome" }
    }
  }
}
```

`RUSTWRIGHT_MCP_CHANNEL=chrome` uses your installed Google Chrome. Drop it
to use rustwright's bundled Chromium instead (install once with
`uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' python -m rustwright install chromium`).

Without uv, install into a plain venv from git:

```bash
python3 -m venv ~/.rustwright-mcp
~/.rustwright-mcp/bin/pip install 'rustwright-mcp @ git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp'
```

## Install from a source checkout

```bash
cd mcp
python3 -m venv .venv && .venv/bin/pip install -e .
```

Then either install the bundled Chromium
(`.venv/bin/python -m rustwright install chromium`) or use
`RUSTWRIGHT_MCP_CHANNEL=chrome`.

## Register with Claude Code (installed binary)

Use the **absolute path** to the `rustwright-mcp` binary — the server is
spawned from arbitrary working directories, so relative paths break:

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  -- "$HOME/.rustwright-mcp/bin/rustwright-mcp"
```

Example with a source checkout at `~/code/rustwright`:

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  -- "$HOME/code/rustwright/mcp/.venv/bin/rustwright-mcp"
```

Verify with `claude mcp list` — the entry should show `✔ Connected`.

## Example session

What an agent sees. `browser_navigate` returns a snapshot; interactive
elements carry `[ref=eN]` handles that later calls act on:

```
> browser_navigate(url="https://example.com")
Page: Example Domain
URL: https://example.com/

- heading "Example Domain" [level=1]
- text: This domain is for use in documentation examples...
- link "Learn more" [href=https://iana.org/domains/example] [ref=e1]

> browser_click(target="e1")
Page: Example Domains
URL: https://www.iana.org/help/example-domains
...

> browser_get_text(selector="h1")
Example Domains
```

## Tools

| Tool | Purpose |
|---|---|
| `browser_navigate(url)` | Open a URL, returns snapshot |
| `browser_snapshot()` | Outline of the page with `[ref=eN]` handles |
| `browser_click(target)` | Click a ref or CSS selector |
| `browser_type(target, text, submit?)` | Fill or type into an input |
| `browser_select_option(target, value)` | Select a dropdown option |
| `browser_hover(target)` | Hover an element |
| `browser_press_key(key)` | Press a keyboard key |
| `browser_navigate_back()` | History back |
| `browser_reload()` | Reload the active page, returns snapshot |
| `browser_tabs(action, index?, url?)` | List, open, select, or close tabs |
| `browser_handle_dialog(accept, prompt_text?)` | Set a one-shot policy for the next dialog |
| `browser_wait_for(text?, timeout_ms?)` | Wait for text or load state |
| `browser_get_text(selector?)` | Visible text of a selector |
| `browser_evaluate(expression)` | Run JavaScript in the page (opt-in) |
| `browser_take_screenshot(path?)` | Save a confined PNG artifact, returns its output-root-relative path |
| `browser_close()` | End the browser session |

## Configuration

| Variable | Effect |
|---|---|
| `RUSTWRIGHT_MCP_HEADLESS` | `0` shows the browser window (default headless) |
| `RUSTWRIGHT_MCP_CHANNEL` | Chromium channel, e.g. `chrome`, `chrome-beta` |
| `RUSTWRIGHT_MCP_EXECUTABLE` | Explicit browser binary path (overrides channel) |
| `RUSTWRIGHT_MCP_CDP_ENDPOINT` | Remote browser CDP endpoint; enables remote mode when set |
| `RUSTWRIGHT_MCP_CDP_HEADERS` | Optional JSON object of extra CDP connection headers |
| `RUSTWRIGHT_MCP_CDP_TIMEOUT_MS` | Remote connection timeout in milliseconds (default `60000`) |
| `RUSTWRIGHT_MCP_ALLOW_EVAL` | `1`, `true`, or `yes` exposes `browser_evaluate` (default off) |
| `RUSTWRIGHT_MCP_OUTPUT_DIR` | Root for files written by tools |
| `RUSTWRIGHT_MCP_OUTPUT_MAX_FILE_BYTES` | Per-file output cap (default `20971520`, or 20 MiB) |
| `RUSTWRIGHT_MCP_OUTPUT_MAX_TOTAL_BYTES` | Total output cap (default `209715200`, or 200 MiB) |
| `RUSTWRIGHT_MCP_WORKSPACE` | Allowed input root for future file-upload tools |

### File outputs

All tool-written files are confined to `RUSTWRIGHT_MCP_OUTPUT_DIR`. If that
variable is unset, each server process creates a private session directory at
`${XDG_CACHE_HOME:-~/.cache}/rustwright-mcp/output/<session-uuid>/`. Output
directories use mode `0700`; files are created exclusively with mode `0600`.
Artifact paths returned by tools are relative to the output root.

Each output is limited to 20 MiB by default, and all retained outputs together
are limited to 200 MiB. The byte-cap variables in the table above can override
those values. When the total cap is crossed, the oldest files are evicted first.

**Migration note:** screenshot `path` values are now interpreted inside the
output root. An absolute path is accepted only when it is beneath that root.
Paths outside it fail with `screenshot paths are confined to
RUSTWRIGHT_MCP_OUTPUT_DIR (<root>); got <path>` instead of being written.
Omitting `path` still creates a temporary PNG, now inside the output root.

### Remote browsers over CDP

Set `RUSTWRIGHT_MCP_CDP_ENDPOINT` to attach to an existing Chromium browser
over CDP. `RUSTWRIGHT_MCP_CDP_HEADERS` accepts a JSON object of extra connection
headers, and `RUSTWRIGHT_MCP_CDP_TIMEOUT_MS` controls the connection timeout.
The server adopts the remote browser's default context and an existing page,
creating a page only when the context has none.

```bash
RUSTWRIGHT_MCP_CDP_ENDPOINT='wss://browser.example.com/devtools/browser/<session-id>' \
RUSTWRIGHT_MCP_CDP_HEADERS='{"Authorization":"Bearer <token>"}' \
RUSTWRIGHT_MCP_CDP_TIMEOUT_MS=60000 \
rustwright-mcp
```

In CDP mode, `RUSTWRIGHT_MCP_HEADLESS`, `RUSTWRIGHT_MCP_CHANNEL`, and
`RUSTWRIGHT_MCP_EXECUTABLE` are ignored. If the initial connection fails or a
remote session stops responding, the tool fails loudly; it never silently
launches a local browser. `browser_close` detaches from the remote browser
without terminating the remotely owned process.

For example, a hosted browser provider such as Skyvern Browser Sessions exposes
a CDP address plus an `x-api-key` header; configure the header with
`RUSTWRIGHT_MCP_CDP_HEADERS='{"x-api-key":"<key>"}'`.

### Headless vs headed

The browser runs **headless** by default: no window, suited to CI and
background agents. Set `RUSTWRIGHT_MCP_HEADLESS=0` to run **headed** with a
visible browser window — useful for watching the agent work, debugging
selectors, and for sites whose bot detection blocks headless sessions:

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  --env RUSTWRIGHT_MCP_HEADLESS=0 \
  -- uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp
```

The mode is fixed for the lifetime of the server process; to switch, change
the env var and restart the MCP server (in Claude Code: re-add the server or
restart the session).

## Security & scope

- `browser_evaluate` is off by default because it runs arbitrary JavaScript
  in the page. Set `RUSTWRIGHT_MCP_ALLOW_EVAL=1` and restart the server to
  expose it.
- Snapshots reflect page state, including field values. Password input values
  are masked in snapshot output; other field values are included as-is.
- Snapshot refs are best-effort handles for cooperative pages, not a security
  boundary. Refs increase for the browser session and stale refs fail fast.
- Each server process controls a single local or remote browser session, which
  may have multiple tabs.

## Limitations

- Single browser session per server process.
- Snapshot refs are regenerated on every snapshot; after a page mutation,
  take a new snapshot before acting on refs. Stale refs fail fast with a
  message asking for a fresh snapshot.
- The snapshot script does not walk iframes (any origin) or shadow DOM;
  iframes appear as `- iframe "..." (content not captured)` markers. When
  enabled, `browser_evaluate` can reach into same-origin frames if needed.
