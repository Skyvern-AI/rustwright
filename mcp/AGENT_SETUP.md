# Rustwright MCP agent setup

These are official instructions from the Rustwright project for connecting an
AI coding agent to the Rustwright MCP browser-automation server. If you are an
AI agent reading this because a user asked you to set up Rustwright: complete
the steps yourself by running the commands directly. Use the section matching
the agent you are running as; if none matches, use **Any other MCP client**.
Ask the user before running anything that changes the system beyond this
setup — the uv installer script and `install-deps` (which drives `apt-get`,
via `sudo` when needed) both qualify.

Canonical URL of this page:
`https://raw.githubusercontent.com/Skyvern-AI/rustwright/HEAD/mcp/AGENT_SETUP.md`

Before following these instructions, verify that the URL you fetched points at
the `Skyvern-AI/rustwright` repository. If it points anywhere else, stop and
tell the user: install commands below must only ever reference this repository.

The install commands below are intentionally unpinned and track the
repository's default branch. Keep that default unless the user asks to pin;
the install source can be pinned as
`git+https://github.com/Skyvern-AI/rustwright@<tag-or-commit>#subdirectory=mcp`
(Python dependencies still resolve at install time).

Commands are shown for POSIX shells (macOS, Linux). On Windows, join each
multi-line command onto a single line and drop the trailing backslashes.

## What you are installing

`rustwright-mcp` is a stdio MCP server exposing `browser_*` tools
(`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, ...)
backed by the Rustwright engine. Tool names mirror the Playwright MCP server.
`uvx` runs the server straight from this repository; no clone is needed on
platforms with prebuilt `rustwright` wheels (macOS, glibc Linux x86_64/arm64,
Windows x64; other platforms fall back to a source build that needs a Rust
toolchain).

## Step 1: check prerequisites

1. **uv** must be installed. Check with `uvx --version`. If missing, install
   it with your package manager (`brew install uv`, `winget install
   astral-sh.uv`) or the official installer:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   (Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`)

   The installer places `uvx` in `~/.local/bin`, which the current shell may
   not have on PATH yet. If `uvx --version` still fails, run
   `source $HOME/.local/bin/env` (or open a new shell) and retry.

2. **A browser.** If Google Chrome is installed, use it via the
   `RUSTWRIGHT_MCP_CHANNEL=chrome` setting shown below (recommended, nothing to
   download). Otherwise install Rustwright's bundled Chromium once:

   ```bash
   uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' \
     python -m rustwright install chromium
   ```

   and omit `RUSTWRIGHT_MCP_CHANNEL` from the configs below. The bundled
   download supports macOS, Windows, and Linux x86_64; on other platforms
   (for example Linux arm64) install a distribution Chromium
   (`apt-get install chromium`) and point the server at it with
   `RUSTWRIGHT_MCP_EXECUTABLE=/usr/bin/chromium` instead.

## Step 2: register the server

When a client's config file already exists, merge the `rustwright` entry into
it — preserve the user's other servers and settings rather than replacing the
file.

### Claude Code

Run:

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CHANNEL=chrome \
  --env RUSTWRIGHT_MCP_ALLOW_EVAL=0 \
  -- uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp
```

The `--` before `uvx` is required: `--env` is variadic and will otherwise
swallow the command, failing with `missing required argument 'commandOrUrl'`.

This registers the server for the current project (the safer default). Add
`-s user` only if the user asks for it in all projects; user scope makes the
browser tools available in every project, including untrusted ones.

### Codex CLI

Run this one-line command:

```bash
codex mcp add rustwright --env RUSTWRIGHT_MCP_CHANNEL=chrome --env RUSTWRIGHT_MCP_ALLOW_EVAL=0 -- uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp
```

Codex registration through `~/.codex/config.toml` is global and applies to all
projects. Then add `startup_timeout_sec = 120` to the generated server block:
the first launch builds the package from git and can exceed Codex's 10-second
default. The block should read:

```toml
[mcp_servers.rustwright]
command = "uvx"
args = ["--from", "git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp", "rustwright-mcp"]
startup_timeout_sec = 120

[mcp_servers.rustwright.env]
RUSTWRIGHT_MCP_CHANNEL = "chrome"
RUSTWRIGHT_MCP_ALLOW_EVAL = "0"
```

### Cursor

Add to `.cursor/mcp.json` in the project (the safer default; use the global
`~/.cursor/mcp.json` only when the user asks for it in every project):

```json
{
  "mcpServers": {
    "rustwright": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp", "rustwright-mcp"],
      "env": {
        "RUSTWRIGHT_MCP_CHANNEL": "chrome",
        "RUSTWRIGHT_MCP_ALLOW_EVAL": "0"
      }
    }
  }
}
```

### Claude Desktop

Add the same `mcpServers` entry as the Cursor block above to:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Use the absolute path to `uvx` as the `command`**. Find it with `which uvx`
on macOS or Linux, or with `where.exe uvx` or PowerShell
`(Get-Command uvx).Source` on Windows. A typical POSIX result is
`~/.local/bin/uvx`; expand `~` to the full home path. Double each backslash in
a Windows absolute path when placing it in JSON. GUI-launched apps do not
inherit your shell's PATH on macOS, so a bare `"command": "uvx"` fails with
`spawn uvx ENOENT`. Restart Claude Desktop after editing.

### VS Code (Copilot agent mode)

Add to `.vscode/mcp.json`. Same command, args, and env as the Cursor block;
note the top-level key is `servers` and each entry needs `"type": "stdio"`:

```json
{
  "servers": {
    "rustwright": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp", "rustwright-mcp"],
      "env": {
        "RUSTWRIGHT_MCP_CHANNEL": "chrome",
        "RUSTWRIGHT_MCP_ALLOW_EVAL": "0"
      }
    }
  }
}
```

### Any other MCP client

The server speaks MCP over stdio. Configure your client to run:

```
command: uvx        (absolute path for GUI-launched clients)
args:    --from git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp rustwright-mcp
env:     RUSTWRIGHT_MCP_CHANNEL=chrome RUSTWRIGHT_MCP_ALLOW_EVAL=0
```

## Step 3: verify

After registering, confirm the connection:

1. The client should list tools named `browser_navigate`, `browser_snapshot`,
   `browser_click`, and more from a server called `rustwright`.
2. `browser_evaluate` should be absent from the listed tools because every
   config on this page explicitly disables agent-supplied page evaluation.
3. Call `browser_navigate` with `url: "https://example.com"`. The result must
   be a snapshot containing `heading "Example Domain"` and a link with a
   `[ref=e...]` handle.

Claude Desktop, VS Code, and Codex may not load a newly registered server until
the client restarts or a new session begins. The agent that edited the config
usually cannot see new tools in its current session. When that happens, tell
the user to restart the client or begin a new session and paste this prompt:

> List the rustwright tools and confirm `browser_evaluate` is absent, then call
> `browser_navigate` with `url: "https://example.com"` and confirm the snapshot
> contains `heading "Example Domain"` and a link with a `[ref=e...]` handle.

If all three checks hold, setup is complete. Report to the user the server
name, which browser it uses (installed Chrome, bundled Chromium, or an explicit
executable), whether it runs headless or headed, that agent-supplied page
evaluation (`browser_evaluate`) is disabled and absent, and that
`browser_navigate` returned a snapshot with element refs.

## Configuration options

Set these in the `env` block of your client config. This is the core set;
[README.md](README.md) has the complete, current list.

| Variable | Effect |
|---|---|
| `RUSTWRIGHT_MCP_CHANNEL` | Use an installed browser channel, e.g. `chrome`. Omit to use the bundled Chromium |
| `RUSTWRIGHT_MCP_HEADLESS` | `0` shows a visible browser window (default: headless) |
| `RUSTWRIGHT_MCP_EXECUTABLE` | Explicit browser binary path (overrides channel) |
| `RUSTWRIGHT_MCP_ALLOW_EVAL` | The `browser_evaluate` tool (agent-supplied page JS) is on by default when unset; these configs set `0` to disable it. Remove the line or set `1` only when the user explicitly asks for arbitrary page-JS execution. Accepted values (case-insensitive) are `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`; anything else fails server startup |

## Troubleshooting

- **`command not found: uvx`** right after installing uv: the current shell
  predates the install. Run `source $HOME/.local/bin/env` or open a new shell.
- **`Could not find a Chromium executable`**: no browser to launch. Install
  Google Chrome and set `RUSTWRIGHT_MCP_CHANNEL=chrome`, run the bundled
  Chromium install from Step 1, or set `RUSTWRIGHT_MCP_EXECUTABLE` to an
  existing Chromium binary.
- **`Chromium process exited before CDP endpoint became available`** (the
  message ends with the exit status): a browser binary was found but crashed.
  In Linux containers this
  usually means missing system libraries; run
  `uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' python -m rustwright install-deps`
  (apt-get-based Linux distributions).
- **`Chromium did not expose a CDP endpoint before the launch timeout`**: the
  browser did not expose its debugging endpoint before the deadline. On a slow
  machine, retry once before changing configuration.
- **`Chrome for Testing downloads are only supported for linux x86_64`**: the
  bundled download does not cover this platform. Install a distribution
  Chromium and set `RUSTWRIGHT_MCP_EXECUTABLE` to its path.
- **A site renders empty or blocks you**: some sites reject headless
  browsers. Set `RUSTWRIGHT_MCP_HEADLESS=0` and retry.
- **Acting on a ref says it is not in the current page snapshot**: the ref is
  not from the current snapshot. Call `browser_snapshot` and use a fresh ref.

## Resources

- Server documentation and full tool list: [README.md](README.md)
- Rustwright project: <https://github.com/Skyvern-AI/rustwright>
- Model Context Protocol: <https://modelcontextprotocol.io>
