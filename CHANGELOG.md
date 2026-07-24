# Changelog

All notable user-facing changes to Rustwright are documented in this file.

## [Unreleased]

### Added

- Added a persistent `rustwright` CLI for browser sessions, accessibility snapshots, and element-reference actions.
- Added `rustwright-mcp`, an MCP stdio server that exposes browser automation tools to MCP clients.
- Added alpha bindings for Go, Java, C#/.NET, Ruby, and PHP, plus a native Rust API, backed by the shared Rust engine.

### Changed

- Moved actionability waits for supported, optionless `AsyncPage.click()` and `AsyncPage.fill()` calls into the Rust core while preserving trusted browser input and a single action deadline.
- Centralized evaluation value decoding in the Rust core for the Go/C-ABI and native Rust surfaces, and added structured timeout, closed, crashed, and disconnected errors for the Python API.
- Promoted the native Rust MCP server (`mcp-rs/`) into the open-source tree as the canonical `rustwright-mcp` implementation; documentation now targets it.

### Deprecated

- Deprecated the Python MCP server; it remains available until the native server reaches full tool parity, after which it will be removed.

### Fixed

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
