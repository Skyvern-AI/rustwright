# Changelog

All notable user-facing changes to Rustwright are documented in this file.

## [Unreleased]

### Breaking

- Removed `disable_playwright_compat()`. Compatibility aliases are now a one-way
  process opt-in.
- Removed `RUSTWRIGHT_UNSAFE_DOM_FASTPATH`. Click, fill, check, and select
  actions always use the trusted native action path.
- Promoted one MCP behavior profile for every client. Snapshot distillation,
  page headers, console deduplication, network notes, compact descriptions, and
  9 KiB/200-line response limits are now the defaults.

### Changed

- `Browser.new_page()` now creates a native ephemeral browser context for each
  page. Closing the page disposes its context, matching Playwright isolation.
- Navigation internals now retain response bodies and drain console and
  page-error history before each navigation.
- Chromium launches now include a broader resource-safe default switch set.
- User-provided feature-list launch arguments now merge with defaults into one
  switch per feature flag.

- Network request filters now accept the standard Rust `regex` syntax.

- Console and page-error event waiters now block in Rust and apply exact or
  substring text filters without Python polling. Console and page-error
  listeners use the persistent event pump. Once/remove cleanup detaches worker
  console forwarding and page close reaps worker listener threads.

### Fixed

- Fixed native drag-and-drop to stop dispatching trailing pointer and mouse release events after a completed drop.

## [0.3.0] - 2026-08-16

### Added

- Added opt-in MCP response budgets, bounded snapshot distillation, page-state
  digests, console-message deduplication, and static-network notes.
- Added client-aware lean MCP tool descriptions. They default to on for
  recognized Codex clients and off for other clients, and remain configurable
  through `RUSTWRIGHT_MCP_LEAN_DESCRIPTIONS`.

### Breaking

- `BrowserType.connect()` no longer forwards raw-CDP
  endpoints to `connect_over_cdp()`. It now rejects every endpoint before
  network I/O because Rustwright does not implement Playwright wire. Migrate
  sync code from `chromium.connect(endpoint)` to
  `chromium.connect_over_cdp(endpoint)`. Migrate async code from
  `await chromium.connect(endpoint)` to
  `await chromium.connect_over_cdp(endpoint)`. Services that use
  `playwright run-server` or `BrowserType.launchServer()` must change to a raw
  Chromium CDP service. See
  [`docs/REMOTE_BROWSERS.md`](docs/REMOTE_BROWSERS.md).

### Changed

- Added public browser-action failure and retry details for Rust, Python, and
  MCP callers. Python errors expose `kind`, `phase`, `target_kind`,
  `command_written`, and `retryable`. Python raises `UnknownOutcomeError` when
  an input command's delivery is unknown. Callers must not retry that error
  blindly because replay can duplicate the action.

- Made frame-tree caching event-maintained: the main page session populates at attachment, OOPIF sessions populate on first locator use and after invalidation, steady-state locators issue no `Page.getFrameTree` calls, and context-bound frame actions retry once during iframe process changes.
- Reduced steady-state object-valued `evaluate()` results to one browser-protocol command after per-realm serializer setup; the first call in a new or recreated realm re-establishes that setup.
- Passive page-error history now records page-authored errors without enabling Chromium's `Runtime` domain; `LIMITATIONS.md` documents the full-detail limitation for evaluate-created asynchronous callbacks.
- Verified support for standard CPython 3.9 through 3.14 and CPython 3.15.0rc1. Python 3.15 support remains pre-release, and CI tracks 3.15-dev until GA.
- Browser launch and persistent-context launch now add Chromium's `--use-mock-keychain` and `--password-store=basic` defaults unless callers provide those switches or disable default arguments.

### Fixed

- Fixed `get_by_role()` accessible-name matching over nested content and `get_by_label()` zero-width normalization to match upstream Playwright.
- Fixed sync and async `Page` objects to support Playwright-compatible context managers that close the page on exit.
- Fixed `enable_playwright_compat()` on installations without the optional pytest development dependency. `enable_playwright_compat()` now returns a `PlaywrightCompatEnableResult` describing what was registered instead of `None`.
- Fixed `type()`, `press()`, and ASCII `fill()` to dispatch trusted CDP key
  events so page listeners receive real keyboard input.
- Fixed navigation completion to use one deadline, reconcile state when a load
  event is missed, and ignore navigation events from unrelated iframes.

## [0.2.0] - 2026-08-03

### Added

- Added anonymous, opt-out launch telemetry; set `DISABLE_TELEMETRY=1` or `RUSTWRIGHT_DISABLE_TELEMETRY=1` to disable it.
- Added shadow-piercing CSS selectors backed by a real selector lexer and DOM-tree ancestry.
- Added a persistent `rustwright` CLI for browser sessions, accessibility snapshots, and element-reference actions.
- Added `rustwright-mcp`, an MCP stdio server that exposes browser automation tools to MCP clients.
- Added native console-message capture and the mirror-profile `browser_console_messages` MCP tool.
- Added native network-record capture and the mirror-profile `browser_network_requests` and `browser_network_request` MCP tools.
- Added native pending-file-chooser events and mirror-profile MCP file upload and cancellation with workspace-confined paths.
- Added native physical element dragging and the mirror-profile `browser_drag` MCP tool.
- Added alpha bindings for Go, Java, C#/.NET, Ruby, and PHP, plus a native Rust API, backed by the shared Rust engine.

### Changed

- Moved actionability waits for supported, optionless `AsyncPage.click()` and `AsyncPage.fill()` calls into the Rust core while preserving trusted browser input and a single action deadline.
- Centralized evaluation value decoding in the Rust core for the Go/C-ABI and native Rust surfaces, and added structured timeout, closed, crashed, and disconnected errors for the Python API.
- Promoted the native Rust MCP server (`mcp/`) into the open-source tree as the canonical `rustwright-mcp` implementation; documentation now targets it, the `rustwright mcp` CLI verb launches it, and its cargo package and binary are named `rustwright-mcp` to match the npm distribution.

### Deprecated

- Deprecated the Python MCP server; it remains available until the native server reaches full tool parity, after which it will be removed.

### Fixed

- Fixed `evaluate()` string handling by restoring the statement-forms runtime probe an internal refactor had reverted: IIFEs, declaration-plus-invocation programs, and other program-form strings evaluate correctly again in every binding, and arguments apply only when the evaluated value is invoked as a function.
- Fixed `fill()` on React-controlled inputs so the native engine's writes survive controlled-component re-renders.
- Fixed `evaluate()` arguments and comparisons to use JavaScript number semantics across the language bindings.
- Fixed locator waits so they re-arm after mid-wait navigation against the original timeout instead of surfacing execution-context errors.
- Fixed remote-CDP actionability probes so they receive the full remaining action budget rather than a short per-probe cap.
- Fixed Node.js evaluation decoding for special numeric values, BigInt, and regular expressions; Go/C-ABI and native Rust now use the core's canonical wire decoder.
- Fixed select-option locator helpers so they wait on the caller's timeout instead of falling back to the 30s default and then reporting an error naming the caller's budget.
- Fixed non-finite timeouts, which failed after a single millisecond with a misleading `Timeout(1)` instead of falling back to the default budget.
- Fixed MCP cancellation, which stopped taking effect for the rest of a request once any physical action had committed: cancelling a `browser_fill_form` after an early checkbox or radio click kept typing every remaining field into the page. A cancelled request now stops at the next field and reports which fields were written before it stopped.
- Fixed MCP requests that exceed their deadline, which reported a bare cancellation, leaving callers unable to tell an operator cancel from a budget overrun. They now report a timeout naming the budget.
- Fixed an interrupted `browser_fill_form` losing the detail of what it had written: the per-field report was replaced at completion by the bare cancellation or timeout error, so the caller was told the request stopped but not where. The detail now survives — including when the budget expires before the deadline is announced, which previously reported the expiry as a field-specific failure — is emitted even when no field completed, and the form's final snapshot is still returned after the deadline so the caller can see the state it must reconcile.
- Fixed a fully written `browser_fill_form` being reported as cancelled. A form of text, combobox, and slider fields commits no physical action, so a cancellation or deadline arriving while the closing snapshot was in flight discarded the successful result and returned a bare cancellation — sending the caller to reconcile a form that had been written completely and correctly.
- Fixed key presses and typed text reporting a failure when the browser had already executed the input and only its reply was lost or late, which made a retry press the key twice.
- Fixed MCP password masking, which covered only the password field itself: a site that echoed the typed secret elsewhere on the page — into a heading, a button label, or another input — leaked it through the automatic post-action snapshot. Snapshot-visible content that gained the secret during the write is now masked wherever it is reachable from a node the snapshot renders, and the secret is never stored in the page.
- Fixed MCP write tools so a password write that fails or times out after the keystrokes were dispatched reports whether the value actually landed, and returns the masked page state, instead of a bare failure that hid a committed secret.
- Fixed MCP password writes that raise a dialog: the dialog blocks the page, so the write previously returned a bare snapshot-capture failure and rendered the alert text — which may be the secret itself — unmasked. Such a write now returns the pending-modal notice with the secret masked, and the masking is applied to the stored dialog text rather than to that one reply, so a tool called before `browser_handle_dialog` — a plain `browser_snapshot`, say — cannot echo back what the write had just masked.

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
[0.3.0]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.3.0
[0.2.0]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.2.0
[0.1.1]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.1
[0.1.0]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.0
[0.1.0-alpha.4]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.0-alpha.4
[0.1.0-alpha.3]: https://github.com/Skyvern-AI/rustwright/releases/tag/v0.1.0-alpha.3
