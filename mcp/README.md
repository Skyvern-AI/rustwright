# Rustwright MCP server

Give any MCP client `browser_*` tools over stdio with no clone or browser download.

## Install

### Claude Code

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  -- uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp
```

Uses the Chrome you already have — no browser download.

### Claude Desktop

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

### Any MCP client

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

## Verify it works

Ask your agent to “take a browser snapshot of example.com”.

## Tools

| Tool | Purpose |
|---|---|
| `browser_navigate(url)` | Open a URL, returns snapshot |
| `browser_resize(width, height)` | Change the page viewport and return a responsive snapshot |
| `browser_snapshot(target?, filename?, depth?, boxes?)` | Full or targeted outline with `[ref=eN]` handles |
| `browser_find(text?, regex?)` | Search one refreshed outline with paths, refs, and sibling context |
| `browser_click(target, element?, doubleClick?, button?, modifiers?)` | Click a ref or unique CSS selector |
| `browser_drag(startTarget, endTarget, startElement?, endElement?)` | Strict element-to-element drag with a fresh snapshot |
| `browser_drop(target, element?, paths?, data?)` | Best-effort synthetic DataTransfer drop with files and/or MIME strings |
| `browser_type(target, text, element?, submit?, slowly?, clear?)` | Fill or character-type into an input; `clear` is an extension |
| `browser_select_option(target, values, element?)` | Select one or more dropdown options; legacy `value` is accepted |
| `browser_fill_form(fields)` | Sequential, non-transactional typed form fill with one final snapshot |
| `browser_hover(target)` | Hover an element |
| `browser_press_key(key)` | Press a keyboard key |
| `browser_navigate_back()` | History back |
| `browser_reload()` | Reload the active page, returns snapshot |
| `browser_tabs(action, index?, url?)` | List, open, select, or close tabs |
| `browser_handle_dialog(accept, promptText?)` | Resolve the JavaScript dialog that is currently pending |
| `browser_file_upload(paths?)` | Resolve or cancel the currently pending file chooser |
| `browser_console_messages(level?, all?, filename?)` | Read thresholded console records inline or as an artifact |
| `browser_network_requests(static?, filter?, filename?)` | List current-navigation requests by stable index |
| `browser_network_request(index, part?, filename?)` | Read request/response details and lazy response bodies |
| `browser_wait_for(time?, text?, textGone?, timeout_ms?)` | Wait up to 30 seconds and/or for visible/hidden text |
| `browser_get_text(selector?)` | Visible text of a selector |
| `browser_evaluate(function, element?, target?, filename?)` | Run page-world JavaScript and return JSON plus a fresh snapshot |
| `browser_take_screenshot(element?, target?, type?, filename?, fullPage?, scale?)` | Save a confined page or element image |
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
| `RUSTWRIGHT_MCP_ALLOW_EVAL` | Page-world evaluation is on by default; accepts `1`, `true`, `yes`, `on` or `0`, `false`, `no`, `off`; any other value stops startup |
| `RUSTWRIGHT_MCP_CAPS` | Comma-separated capability groups; unavailable groups warn and are ignored |
| `RUSTWRIGHT_MCP_TOOLSET` | `mirror` (all 25 tools, default) or `lean` (core interaction loop, resize, and evaluate) |
| `RUSTWRIGHT_MCP_OUTPUT_DIR` | Root for files written by tools |
| `RUSTWRIGHT_MCP_OUTPUT_MAX_FILE_BYTES` | Shared per-file output and drop-input cap (default `20971520`, or 20 MiB) |
| `RUSTWRIGHT_MCP_OUTPUT_MAX_TOTAL_BYTES` | Shared total output and per-drop input cap (default `209715200`, or 200 MiB) |
| `RUSTWRIGHT_MCP_WORKSPACE` | Allowed absolute input root for file uploads and drops |

### File outputs

All tool-written files are confined to `RUSTWRIGHT_MCP_OUTPUT_DIR`. If that
variable is unset, each server process creates a private session directory at
`${XDG_CACHE_HOME:-~/.cache}/rustwright-mcp/output/<session-uuid>/`. Output
directories created by the server use mode `0700`; files are created exclusively
with mode `0600`. A pre-existing configured directory keeps its permissions, and
only files reserved by the current server process are eligible for eviction.
Artifact paths returned by tools are relative to the output root.

Each output is limited to 20 MiB by default, and all retained outputs together
are limited to 200 MiB. The byte-cap variables in the table above can override
those values. When the total cap is crossed, the oldest files are evicted first.

**Migration note:** screenshot `filename` values (and the legacy `path` alias) are interpreted inside the
output root. An absolute path is accepted only when it is beneath that root.
Paths outside it fail with `screenshot paths are confined to
RUSTWRIGHT_MCP_OUTPUT_DIR (<root>); got <path>` instead of being written.
Omitting `filename` still creates an image inside the output root.

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

- **SECURITY:** `browser_evaluate` performs page-world evaluation and is on by
  default for compatibility. Set `RUSTWRIGHT_MCP_ALLOW_EVAL=0` and restart the
  server to disable and remove the tool from `tools/list`.
- Snapshots reflect page state, including field values. Password input values
  are masked in snapshot output; other field values are included as-is.
- Snapshot refs are best-effort handles for cooperative pages, not a security
  boundary. Refs increase for the browser session and stale refs fail fast.
- Each server process controls a single local or remote browser session, which
  may have multiple tabs.
- JavaScript dialogs and file choosers are pending modal state. Responses show
  a `### Modal` section; use `browser_handle_dialog` or `browser_file_upload`
  before another DOM-evaluating tool. Downloads are confined automatically and
  reported once in `### Downloads`.

## Limitations

- Single browser session per server process.
- Snapshot refs are regenerated on every snapshot; after a page mutation,
  take a new snapshot before acting on refs. Stale refs fail fast with a
  message asking for a fresh snapshot.
- The snapshot script does not walk iframes (any origin) or shadow DOM;
  iframes appear as `- iframe "..." (content not captured)` markers. When
  enabled, `browser_evaluate` can reach into same-origin frames if needed.
