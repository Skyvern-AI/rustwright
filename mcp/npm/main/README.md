# rustwright-mcp

`rustwright-mcp` is the native Rustwright Model Context Protocol server. The npm package selects and runs one prebuilt Rust executable for the current operating system and CPU. It has no Python dependency, install script, or runtime download.

## Run it

```bash
npx -y rustwright-mcp
```

The process is a stdio MCP server, so it waits for JSON-RPC messages on standard input rather than displaying an interactive prompt.

## Claude Code

```bash
claude mcp add rustwright -- npx -y rustwright-mcp
```

Pass configuration through environment variables when needed:

```bash
claude mcp add rustwright \
  --env RUSTWRIGHT_MCP_CDP_ENDPOINT=http://127.0.0.1:9222 \
  -- npx -y rustwright-mcp
```

## Claude Desktop

Add the server to the `mcpServers` object in the Claude Desktop configuration:

```json
{
  "mcpServers": {
    "rustwright": {
      "command": "npx",
      "args": ["-y", "rustwright-mcp"]
    }
  }
}
```

Restart Claude Desktop after changing its configuration.

## Configuration

The launcher forwards its environment unchanged to the native server. Supported variables are:

- `RUSTWRIGHT_CHROMIUM` / `CHROME` / `CHROMIUM`: path to the browser executable to launch.
- `RUSTWRIGHT_MCP_CDP_ENDPOINT`: connect to an existing browser through its HTTP CDP endpoint instead of launching locally.
- `RUSTWRIGHT_MCP_CDP_HEADERS`: JSON object containing headers for the remote CDP connection.
- `RUSTWRIGHT_MCP_CDP_TIMEOUT_MS`: positive remote connection timeout in milliseconds.
- `RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS`: browser tool timeout in milliseconds.
- `RUSTWRIGHT_MCP_TOOLSET`: tool profile, either `mirror` (default) or `lean`.
- `RUSTWRIGHT_MCP_ALLOW_EVAL`: enable or disable `browser_evaluate`; defaults to enabled.
- `RUSTWRIGHT_MCP_WORKSPACE`: absolute directory that confines paths supplied to `browser_drop`.
- `RUSTWRIGHT_MCP_SCREENSHOT_MAX_BYTES`: largest screenshot returned inline; oversized captures use a private temporary file.
- `RUSTWRIGHT_MCP_MAX_RESPONSE_BYTES` / `RUSTWRIGHT_MCP_MAX_RESPONSE_LINES`: override the default 9 KiB and 200-line response limits. `0` disables that dimension. Minimum nonzero values are 4096 bytes and 16 lines.

The server uses one behavior profile for every client. It distills snapshots,
adds page headers, deduplicates adjacent inline console records, reports filtered
static network requests, and serves compact tool descriptions. Images are never
shaped.

For example, a Desktop entry can include an `env` object:

```json
{
  "command": "npx",
  "args": ["-y", "rustwright-mcp"],
  "env": {
    "RUSTWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:9222"
  }
}
```

Prebuilt packages are provided for macOS arm64/x64, Linux glibc arm64/x64, and Windows x64.
