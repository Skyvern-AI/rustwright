# Agent CLI

`rustwright` is a command-line interface for driving a Chromium browser over CDP
from an AI agent or a shell. It is pure Python, adds no third-party runtime
dependencies, and is installed with the package. Named sessions keep one browser
alive across invocations, so a sequence of commands drives a single browser.

It is Chromium-only (Rustwright automates Chromium over CDP).

> An MCP server for Rustwright is maintained as a separate, opt-in package
> (`rustwright-mcp`) rather than part of the core wheel. This document covers the
> CLI.

## MCP stdio server

Install both `rustwright` and `rustwright-mcp`, then start the server with
`rustwright mcp`. Any arguments after `mcp`, including `--caps=...`, pass through
to the MCP entry point unchanged.

## The accessibility snapshot and element refs

The CLI works from a compact accessibility snapshot rather than raw HTML or
pixels. A snapshot looks like:

```
- heading "Rustwright" [level=1] [ref=e1]
- textbox "Email" [ref=e2]
- button "Sign in" [ref=e3]
```

Each interactive node has a stable `ref` (`e1`, `e2`, …). You take a snapshot,
then act on a node by its ref (`click e3`). After any action that changes the
page, a fresh snapshot is returned so the next ref you use is current.

Refs are produced by a single in-page pass that tags each emitted node with a
data attribute, so the ref you see in the text and the element that gets clicked
always agree. Refs are:

- **session-scoped and monotonic** — a ref is never reused, so a stale ref can
  never silently point at a different element.
- **invalidated by navigation and by the next snapshot** — resolving a stale ref
  returns a `stale_ref` error telling you to snapshot again.

Ref resolution is **best-effort for cooperative pages, not a security boundary.**
A page that deliberately copies or moves the tag attribute can defeat the
resolution checks. Do not point the CLI at untrusted pages while handling
sensitive credentials or state.

Snapshots reflect the current page state, including the values of text inputs.
**Password field values are masked.** Other field values are shown, so treat
snapshot output as potentially sensitive.

### Scope in this version

- The main document is snapshotted. `<iframe>` elements appear as nodes, but
  content inside child frames (including same-origin frames) is not addressable
  by a ref yet.
- Open shadow DOM content is omitted from the snapshot.
- Closed shadow roots are not accessible.

## Using it

```bash
rustwright open https://example.com   # launch + navigate; prints a snapshot
rustwright snapshot                    # accessibility tree with refs
rustwright click e3                     # click by ref
rustwright fill e2 "user@example.com"  # fill by ref
rustwright close                        # shut the session down
```

Chromium must be available. If you have not already installed a browser for
Rustwright, run `rustwright install chromium` once.

### How persistence works

`open` launches a small background owner process that holds the browser. Each
later command connects to that browser over a local CDP endpoint, performs one
action, and detaches. State for a session lives in a per-user runtime directory
(`$XDG_RUNTIME_DIR/rustwright/agent` or a temporary directory), with the state
file created private to your user.

**Threat model: a single trusted local user.** The browser's CDP endpoint listens
on a loopback port. Any process running as any user on the same host that can
reach that port can control the browser — the state file's permissions protect
the file, not the browser. Do not use the CLI on a shared or untrusted host for
sensitive sessions.

If a command process is interrupted mid-action, the session records that it may
be mid-change; the next command clears its refs and re-snapshots before acting,
so you never act on a ref from an uncertain state.

Persistent sessions require macOS or Linux; non-session commands remain available on Windows.

### Verbs

| Verb | Example | Notes |
|---|---|---|
| `open [URL]` | `rustwright open example.com` | Start/attach a session; optional navigate. |
| `navigate URL` | `rustwright navigate example.com` | Navigate the active tab. |
| `back` / `reload` | | History + reload. |
| `snapshot` | `rustwright snapshot --depth 6` | Print the accessibility tree with refs. |
| `click REF` | `rustwright click e3` | Click by ref. |
| `fill REF TEXT` | `rustwright fill e2 hello` | Clear and fill (text is not echoed back). |
| `type REF TEXT` | | Type with optional `--delay-ms`. |
| `select REF VALUE…` | | Select `<option>` values. |
| `hover REF` / `press KEY` | | Hover / keyboard press. |
| `wait …` | `rustwright wait --text Loaded` | Wait for time / text / text-gone / load state. |
| `tabs …` | `rustwright tabs new example.com` | `list` / `new` / `use` / `close`. |
| `screenshot [PATH]` | `rustwright screenshot shot.png --full` | Save a screenshot. |
| `status` | `rustwright status` | Show whether a session is running (endpoint redacted). |
| `close [--force]` | `rustwright close --force` | Shut down; `--force` also clears a wedged session. |
| `eval EXPR` | `rustwright --allow-eval eval "document.title"` | Requires `--allow-eval`. |

Global flags include `--session NAME` (default `default`), `--json` (emit one
JSON object per command), `--timeout-ms`, `--navigation-timeout-ms`, `--headed`,
`--executable-path`, `--browser-arg`, and `--allow-eval`.

### Output and exit codes

Human output is terse. `--json` emits exactly one compact object per command:
`{"version":1,"success":…,"command":…,"session":…,"data":…,"error":…,"warnings":[]}`.
Errors print `error[code]: message` on standard error.

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | Browser/action failure |
| 2 | Invalid arguments |
| 3 | Session start / attach / state failure |
| 4 | Timeout or session busy |
| 5 | Stale, missing, or ambiguous ref |
| 130 | Interrupted |

## Troubleshooting

- **No browser found** — run `rustwright install chromium` once.
- **`session_lost`** — the owner process is gone or the endpoint is unreachable.
  Run `rustwright close --force` to clear the session, then `open` again.
- **`session_busy`** — another command holds the session lock; retry after it
  finishes.
- **Stale ref** — take a new `snapshot` and use a ref from it.

## Not included yet

`find`/`read` helpers, batch execution, cookie/storage/network/console tools,
PDF export, file upload, viewport resize, child-frame and shadow-DOM refs, and
Windows support for the persistent CLI are planned follow-ups.

## Differences from the playwright CLI

`rustwright open` starts a persistent session; pass `--headed` before `open` for
a visible window, and run `rustwright close` to end the session. `screenshot`
keeps the two-argument, one-shot `screenshot <url> <file>` form and adds a
session form, `screenshot [file]`. Emulation flags such as `--device` remain on
the one-shot `screenshot` and `pdf` commands but no longer exist on `open`; use
the Python API when full emulation is required.
