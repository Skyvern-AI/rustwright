# Changelog

All notable user-facing changes to Rustwright are documented in this file.

## [Unreleased]

### Added

- Added a persistent `rustwright` CLI for browser sessions, accessibility snapshots, and element-reference actions.
- Added `rustwright-mcp`, an MCP stdio server that exposes browser automation tools to MCP clients.
- Added native console-message capture and the mirror-profile `browser_console_messages` MCP tool.
- Added native network-record capture and the mirror-profile `browser_network_requests` and `browser_network_request` MCP tools.
- Added native pending-file-chooser events and mirror-profile MCP file upload and cancellation with workspace-confined paths.
- Added native physical element dragging and the mirror-profile `browser_drag` MCP tool.
- Added alpha bindings for Go, Java, C#/.NET, Ruby, and PHP, plus a native Rust API, backed by the shared Rust engine.
- Added shadow DOM support to frame enumeration: iframes attached inside a shadow root now appear in `page.frames` and `frame.child_frames`, so widgets that mount their cross-origin iframe that way (Cloudflare Turnstile, hCaptcha, and embedded payment fields among them) are reachable.

### Changed

- Moved actionability waits for supported, optionless `AsyncPage.click()` and `AsyncPage.fill()` calls into the Rust core while preserving trusted browser input and a single action deadline.
- Centralized evaluation value decoding in the Rust core for the Go/C-ABI and native Rust surfaces, and added structured timeout, closed, crashed, and disconnected errors for the Python API.
- Promoted the native Rust MCP server (`mcp/`) into the open-source tree as the canonical `rustwright-mcp` implementation; documentation now targets it, the `rustwright mcp` CLI verb launches it, and its cargo package and binary are named `rustwright-mcp` to match the npm distribution.

### Deprecated

- Deprecated the Python MCP server; it remains available until the native server reaches full tool parity, after which it will be removed.

### Fixed

- Fixed `page.evaluate()` treating an already-invoked IIFE as a function literal when its body contained an arrow function anywhere, which wrapped and re-called the value it had returned and failed with `__rw_fn is not a function`.
- Fixed the best-effort frame-tree refresh spending the caller's whole timeout per session, which made `Request.frame` inside a route handler stall the navigation it belonged to until that timeout expired, and hang indefinitely when the timeout was disabled.
- Fixed the stealth user-agent override pinning `Accept-Language` to `en-US,en`, which silently overrode `--accept-lang`/`--lang` and left a browser configured for one region reporting that region's timezone alongside an `en-US` locale.
- Fixed dedicated workers being given their identity by rewriting the page's `Worker` constructor to load a generated blob that `importScripts` the real script, which moved every worker off its own URL and changed its `location` and origin; worker identity is now installed over the worker's own CDP session, as it already was for service workers, and the worker keeps its real script URL.
- Fixed the stealth init script removing `navigator.webdriver` from browsers that already report `false`, replacing a value every real Chrome exposes with a missing property.
- Fixed child-frame enumeration pairing the DOM query and the protocol's children by position, which gave a light-DOM frame the identity of a shadow-root frame that preceded it; the two are now correlated by frame identity.
- Fixed locator waits so they re-arm after mid-wait navigation against the original timeout instead of surfacing execution-context errors.
- Fixed remote-CDP actionability probes so they receive the full remaining action budget rather than a short per-probe cap.
- Fixed Node.js evaluation decoding for special numeric values, BigInt, and regular expressions; Go/C-ABI and native Rust now use the core's canonical wire decoder.

## [0.1.1] - 2026-07-15

### Added

- Published the experimental Node.js binding to npm.
- Added native async execution for Chromium launch, context and page creation, and common page operations, with an executor fallback for unsupported cases.

### Changed

- Aligned Chromium launch defaults with Playwright while retaining Rustwright's automation-signal suppression and CDP transport choices.

### Fixed

- Fixed `Locator.fill()` for React-controlled inputs by using the browser input path for ordinary editable text.
- Fixed trusted pointer actions and frame remapping during navigation in nested cross-origin iframes.

## [0.1.0] - 2026-07-14

### Added

- Published the Python package on PyPI for installation with `pip install rustwright`.
- Added a documented parity map for the Python sync and async Playwright API surfaces.

## [0.1.0-alpha.4] - 2026-07-13

### Fixed

- Fixed the npm release command so the assembled package tarball is treated as a local file.

## [0.1.0-alpha.3] - 2026-07-13

### Added

- Released the initial Chromium-only alpha with an in-process Rust CDP core and Playwright-shaped Python sync and async APIs.
- Added trusted CDP input, cross-origin iframe support, and opt-in Python compatibility imports for existing Playwright code.
- Added an experimental Node.js binding for launching Chromium and performing core page navigation, interaction, evaluation, screenshot, and lifecycle operations.

[Unreleased]: https://github.com/Skyvern-AI/rustwright/commits/main
[0.1.1]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.1
[0.1.0]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.0
[0.1.0-alpha.4]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.0-alpha.4
[0.1.0-alpha.3]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.0-alpha.3
