# Rustwright Code Architecture

Last updated: 2026-07-21

## Design Goals

- Preserve Playwright-compatible Python behavior at the boundary.
- Keep the Rust core responsible for direct CDP, process management, and
  high-throughput browser protocol work.
- **Keep every language shim as light as possible.** A shim owns marshalling,
  handle/memory ownership, and idiomatic naming — nothing else. Engine
  behavior (timing, deadlines, retries, actionability, wire encoding/decoding,
  option defaults, error taxonomy) lives once in `rustwright-core` and is
  exposed to all bindings; it is never re-implemented per language.
- Keep Python responsible for Playwright-shaped ergonomics, option validation,
  event/context manager behavior, and compatibility imports — but not for
  engine behavior (see the Shim Lightness Principle below).
- Add abstractions only when they reduce duplication or isolate real protocol
  complexity.
- Favor parity tests over speculative rewrites.

## Shim Lightness Principle

The single most expensive architecture failure mode in this repo is engine
logic accreting inside one language shim. It gets rewritten as other bindings
mature, and the copies drift. Two concrete failures shaped the current split:

- The remote-CDP premature-timeout bug (#96) existed because the actionability
  deadline lived in Python while the per-probe cap lived in the core — two
  timeout engines disagreed across the FFI boundary.
- The Node evaluate decoder used an outdated tag vocabulary. That Node tag drift
  is fixed.

The compatibility split is intentional:

- Core `decode_wire_value` and C ABI `rw_decode_wire` remain the flattened plain
  JSON path. They duplicate repeated references and use the cycle sentinel.
- Go and rust-native already use core structural decoding.
- Python and Node use native graph adapters that preserve host object identity
  and cycles.
- Java, C#/.NET, Ruby, and PHP retain full structural copies until the
  remaining C-ABI binding migration.

The rule, applied to every change:

1. Anything expressible as a pure function of JSON-in/JSON-out — option
   normalization and defaulting, evaluate-wire encoding/decoding,
   timeout-precedence resolution, data-URL construction, structural result
   comparison, and graph parsing — is implemented once in
   `rustwright-core` and exposed through the relevant binding surfaces. The C
   ABI keeps its flattened decoder for compatibility; native graph adapters
   preserve identity and cycles.
2. Anything that owns a deadline, poll cadence, retry policy, or CDP
   round-trip sequencing lives in the core. A shim never contains a wait loop
   whose correctness depends on transport latency.
3. New engine-semantic surface (contexts, default timeouts, actionability
   states, trusted input) lands in the core first and is exposed to all
   bindings; a shim-only implementation of engine semantics is not accepted,
   even as a stopgap, unless explicitly gated as experimental for one binding
   with a core issue on file.
4. What legitimately stays in a shim: argument marshalling to the documented
   wire shapes, native-value coercion (e.g. JS `Boolean()`), handle lifetime /
   ownership, idiomatic naming, docstrings, and language-parity error-message
   formatting driven by structured payloads from the core.

`bindings/CONTRACT.md` carries the enforceable version of this rule for the
C-ABI bindings.

## Current Layout

| Path | Responsibility |
| --- | --- |
| `src/lib.rs` | `rustwright-core`: PyO3 extension, Chromium launch/connect, CDP client/session, browser/context/page primitives, protocol event handling, input/network/screenshot/PDF/tracing helpers. Compiled as the Python cdylib and as an rlib consumed by every other binding. |
| `python/rustwright/sync_api.py` | Main Playwright-compatible sync Python API: option normalization, public classes, locators, contexts, pages, requests, routes, assertions, event waiters, artifacts. |
| `python/rustwright/async_api.py` | Async Playwright-compatible facade over the sync implementation. |
| `python/rustwright/_devices.py` | Device descriptor data. |
| `python/rustwright/cli.py` | CLI entry points. |
| `python/rustwright/pytest_plugin.py` | Pytest fixtures. |
| `python/playwright/*`, `python/patchright/*`, `python/cloakbrowser/*` | Compatibility import packages. Public alpha compatibility imports should be enabled only through opt-in compatibility mode. |
| `rust-native/` | Native Rust facade crate over `rustwright-core` (crates.io `rustwright`); also the facade `mcp/` consumes. |
| `mcp/` | `rustwright-mcp`: the canonical native Rust MCP stdio server on the promoted engine facade, with its npm packaging under `mcp/npm/`. |
| `node/` | napi-rs binding (in-process, links `rustwright-core` directly). |
| `capi/` | Shared C ABI (`librustwright_capi`) over `rustwright-core`; the boundary for the Go/Java/C#/Ruby/PHP bindings. |
| `go/`, `java/`, `csharp/`, `ruby/`, `php/` | C-ABI language bindings (alpha surface) + per-language conformance runners. |
| `bindings/` | Cross-binding contract (`CONTRACT.md`) and shared conformance case data. |
| `benchmarks/automation_cases.py` | Shared Playwright-style automation/parity cases and benchmark workflows, including WebVoyager/Mind2Web-style workflow cases. |
| `benchmarks/run_benchmarks.py` | Rustwright and Playwright benchmark runner for a comparable workload. |
| `tests/test_rustwright_sync_api.py` | Main behavior/regression suite. |
| `tests/test_playwright_parity_cases.py` | Shared parity harness test entry point. |
| `tools/api_surface_audit.py` | Public API surface comparison against reference Playwright. |
| `tools/run_parity_cases.py` | Runs shared parity cases against Rustwright or reference Playwright. |
| `tools/run_antibot_benchmarks.py` | Anti-bot benchmark runner covering local smoke signals, public fingerprint adapters, and fresh/warm profile matrix checks across Rustwright and Playwright. |

## Known Architecture Debt

The current implementation intentionally optimized for fast parity iteration.
The largest monoliths are now large enough to slow development:

| File | Debt |
| --- | --- |
| `src/lib.rs` | CDP transport, browser state, event routing, DOM helpers, stealth/dedicated-worker identity wiring, network shaping, facade promotions, and PyO3 exports are all colocated. |
| `python/rustwright/sync_api.py` | Public API classes, option validators, event waiters, routing, locators, assertions, artifacts, and request helpers are colocated. |
| `python/rustwright/async_api.py` | Hand-written async control paths sit beside generated mechanical delegation mixins; ownership rules must stay synchronized with the generator. |
| `benchmarks/automation_cases.py` | Shared parity cases and benchmark workflows are useful but increasingly hard to scan by subsystem. |
| `tests/test_rustwright_sync_api.py` | Broad regression coverage is useful but hard to navigate by subsystem. |

This is acceptable for alpha while behavior is moving quickly, but the beta
bar should include splitting by stable ownership boundaries.

## Binding Ownership Audit

The core graph parser establishes the evaluate-wire and launch-parser ownership split:

| Binding | Current ownership |
| --- | --- |
| Python | PyO3 native graph adapter for evaluate results; Playwright-shaped validation and ergonomics remain in Python. |
| Go | C ABI binding that already uses the core's flattened structural decoder and maps leaf values. |
| rust-native | In-process facade that already uses core structural decoding and maps leaf values. |
| C ABI | Legacy flattened JSON decoder and core launch parser; it does not preserve host graph identity. |
| Java / C#/.NET / Ruby / PHP | C-ABI bindings with full structural copies pending migration to the core decoder. |
| Node | napi native graph adapter; the typed camelCase coercion veneer remains in Node. |

The remaining cross-shim concerns have these owners:

| Concern | Current owner | Remaining work |
| --- | --- | --- |
| Evaluate wire | Core parser; C ABI uses its flattened compatibility decoder, while Python and Node use native graph adapters. | Migrate Java, C#/.NET, Ruby, and PHP to the core decoder. |
| Launch options | Core parser accepts canonical snake_case and current camelCase aliases. | Keep C callers on canonical snake_case; retain language-specific validation and coercion only where required for compatibility. |
| Screenshot options | Core parser. | Keep language veneers limited to marshalling and native coercion. |
| Data URL / JSON equality / manifest validation | Binding harnesses. | Move behind C-ABI helpers when harness work next opens. |
| Error mapping | Core-owned error strings and structured payloads where available. | Add a structured taxonomy in the later error track. |

## Shim Lightening Roadmap

Ordered tracks; each is independently landable and keeps parity tests green.

1. **Core evaluate-wire graph and launch parsing (this change).** Keep the
   legacy C ABI decoder flattened for compatibility. Use native graph adapters
   in Python and Node, and retain core structural decoding in Go and
   rust-native.
2. **Migrate the remaining C-ABI bindings.** Move Java, C#/.NET, Ruby, and PHP
   from full structural copies to the core graph contract. This remains
   pending; do not treat those migrations as complete.
3. **Maintain the generated async Python facade.** Keep mechanical delegation
   methods generated from `sync_api.py` signatures, keep hand-written code
   limited to methods with real async semantics, and keep the checked-in
   freshness contract synchronized with public context-manager methods.
4. **Native actionability.** Land the in-flight native-actionability branch
   (Tokio-side waits, shared probe templates, trusted CDP mouse dispatch,
   structured `ActionTimeoutError`), reconciled with the #96 probe-budget
   semantics. Then extend to the sync path: native
   `wait_for_actionable`/`wait_for_fill_ready`/`click_actionable`/
   `fill_actionable` with an optional `on_poll` Python callback so
   locator-handler pages keep working, and structured timeout payloads so
   Python formats parity messages without string-sniffing. Trusted keyboard
   dispatch (`Input.dispatchKeyEvent`/`insertText`) is the missing input
   primitive.
5. **Move the DOM fast paths into the core** as semantic native operations.
6. **Bundle the injected probe/action JavaScript core-side** (single injected
   script, no per-poll string assembly).
7. **Consolidate event waiters** behind one generic native waiter plus a small
   Python descriptor table.
8. **Native `expect()` polling** returning `(passed, actual)`; Python keeps
   assertion API plus message formatting.
9. **Structured error taxonomy across the boundary** (typed timeout/crash/
   closed payloads; retire substring classification).
10. **Core-side default-timeout register** (page/context), so
    contexts/default timeouts land once in the core instead of per shim.

Sequencing rule: track 1 is the contract baseline for track 2. Track 3 is
independent. Track 4 gates tracks 5–8; track 9 rides along with track 4; track
10 pairs with the first binding that needs contexts.

## Target Rust Module Split

When the next behavior slices stabilize, split `src/lib.rs` into modules along
these boundaries:

| Target module | Contents |
| --- | --- |
| `lib.rs` | PyO3 module registration and thin re-exports only. |
| `error.rs` | `RwError`, Python error mapping, timeout/error helpers. |
| `runtime.rs` | Tokio runtime construction and blocking Python boundary helpers. |
| `cdp/client.rs` | WebSocket transport, command IDs, session routing, send/receive loops. |
| `cdp/session.rs` | Target/session wrappers and CDP session lifecycle. |
| `browser/launch.rs` | Chromium executable resolution, launch args, env/proxy/default arg handling, process lifecycle. |
| `browser/mod.rs` | Browser state, context creation, close/disconnect behavior. |
| `context.rs` | Browser context state, permissions, emulation, proxy, storage hooks. |
| `page/mod.rs` | Page state, frame tree, lifecycle events, navigation. |
| `page/dom.rs` | DOM querying, selectors, element handles, screenshots/PDF helpers. |
| `network.rs` | Request/response shaping, routing, HAR-facing metadata, auth/proxy support. |
| `stealth.rs` | Default identity controls, webdriver suppression, user-agent/client-hint coherence, dedicated-worker identity wrappers, and anti-bot smoke helpers that belong in the Rust layer. |
| `input.rs` | Keyboard, mouse, touchscreen CDP dispatch. |
| `artifacts.rs` | Downloads, video, tracing, file output helpers. |
| `events.rs` | Internal event types and event dispatch helpers. |
| `serialization.rs` | JS value serialization/deserialization and handle previews. |

Split rule: move code only when tests are green before and after the move, and
avoid mixing behavior changes with mechanical module extraction.

## Target Python Module Split

Split `python/rustwright/sync_api.py` into modules once API behavior in the
target area has enough coverage:

| Target module | Contents |
| --- | --- |
| `sync_api.py` | Public imports/re-exports and compatibility class assembly. |
| `errors.py` | Public `Error`, `TimeoutError`, error message helpers. |
| `options.py` | Shared option normalization and Playwright-style validation. |
| `events.py` | Event emitter, waiters, context manager helpers. |
| `browser_type.py` | `BrowserType`, launch/connect validation and wiring. |
| `browser.py` | `Browser` lifecycle and context/page factories. |
| `context.py` | `BrowserContext`, storage, permissions, tracing/video/HAR hooks. |
| `page.py` | `Page`, navigation, waiters, dialog/download/file chooser events. |
| `frame.py` | `Frame` and `FrameLocator`. |
| `locator.py` | `Locator` and selector composition. |
| `element_handle.py` | `ElementHandle` and DOM handle actions. |
| `js_handle.py` | `JSHandle` evaluation and serialization helpers. |
| `network.py` | `Request`, `Response`, `Route`, WebSocket route objects. |
| `api_request.py` | `APIRequest`, `APIRequestContext`, API response objects. |
| `assertions.py` | `expect` implementation and assertion classes. |
| `artifacts.py` | `Download`, `FileChooser`, tracing/video artifact wrappers. |

Avoid circular imports by keeping low-level validators, event primitives, and
error types dependency-free. Higher-level modules may import lower-level
primitives, not the reverse.

## Testing Architecture

The test layout should move toward subsystem files without losing the current
full-suite safety net:

| Target test file | Focus |
| --- | --- |
| `tests/test_browser_type.py` | Launch/connect, option validation, browser engine support. |
| `tests/test_context.py` | Context creation, storage, permissions, proxy, emulation. |
| `tests/test_page.py` | Navigation, events, dialogs, downloads, file chooser. |
| `tests/test_locator.py` | Selectors, locators, actionability, assertions. |
| `tests/test_network.py` | Request/response, routing, HAR, proxy/auth. |
| `tests/test_artifacts.py` | Screenshots, PDF, tracing, video, downloads. |
| `tests/test_api_request.py` | API request context, cookies, redirect/retry/body behavior. |
| `tests/test_async_api.py` | Async wrapper parity and lifecycle. |
| `tests/test_api_surface.py` | API surface audit and import compatibility. |
| `tests/test_antibot_benchmarks.py` | Anti-bot benchmark target selection, text adapters, signal classification, and matrix aggregation. |

Until then, `tests/test_rustwright_sync_api.py` remains the canonical broad
regression suite.

Authoritative verification should run through the standard single-container
Docker path defined by `Dockerfile` and `tools/docker_verify.sh`. The script
keeps the container modes explicit: `pycompile` for the cheapest Docker check,
`focused` for a targeted pytest selector, `sampled` for focused iteration plus
a stratified nearby parity sample, `full` for full pytest and parity, `bench`
for the comparable benchmark matrix, and `antibot-smoke` for Tier 0/static
matrix anti-bot checks. Docker pytest modes load Rustwright's pytest plugin
explicitly and disable unrelated pytest plugin autoloading to reduce startup
cost for focused and sampled checks. The Dockerfile keeps tests/tools/docs
and README content outside the Rust package build layer and uses separate
Rustwright/Playwright browser caches so the reference install cannot prune
Rustwright's browser cache. Linux arm64 images use the Playwright arm64
Chromium as Rustwright's runtime browser because the Rustwright
Chrome-for-Testing installer is linux x86_64-only.

## Iteration Rules

- New behavior must have a focused pytest test and, when feasible, a shared
  Playwright/Rustwright parity case.
- For Playwright-compatible validation, sample real Playwright first and copy
  the first-line error shape.
- Do not add another large helper inside `src/lib.rs` or `sync_api.py` if it
  belongs to one of the target modules and can be introduced cleanly.
- Do not split files while a behavior slice is failing.
- Run slim focused tests during iteration, then shared parity, then the
  appropriate Docker verification mode before considering release-sensitive
  evidence authoritative.
- Keep public status and benchmark docs honest when test counts, benchmark
  numbers, supported functionality, or the compatibility contract change.

## Current Refactor Priority

1. Preserve and extend the core evaluate-wire and launch-parser contract
   without changing the legacy C ABI behavior.
2. Migrate the remaining C-ABI bindings; this is not complete.
3. Maintain the generated async Python facade and its ownership checks.
4. Extract Python event waiters/context managers into
   `python/rustwright/events.py` (pairs with roadmap track 7).
5. Extract Rust launch/process code into `src/browser/launch.rs`, then CDP
   transport/session code into `src/cdp/`.
6. Split tests by subsystem after the corresponding code module split.

The module splits move code without changing ownership; the lightening
roadmap changes ownership (shim → core). When the two conflict, lightening
wins — there is no point splitting a Python file whose contents are scheduled
to move into the core.
