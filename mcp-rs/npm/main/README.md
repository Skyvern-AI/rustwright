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

- `RUSTWRIGHT_MCP_CDP_ENDPOINT`: connect to an existing browser through its HTTP CDP endpoint instead of launching locally.
- `RUSTWRIGHT_MCP_CDP_HEADERS`: JSON object containing headers for the remote CDP connection.
- `RUSTWRIGHT_MCP_CDP_TIMEOUT_MS`: positive remote connection timeout in milliseconds.
- `RUSTWRIGHT_MCP_TOOL_TIMEOUT_MS`: browser tool timeout in milliseconds.

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

