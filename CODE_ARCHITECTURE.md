# Rustwright Code Architecture

Last updated: 2026-05-25

## Design Goals

- Preserve Playwright-compatible Python behavior at the boundary.
- Keep the Rust core responsible for direct CDP, process management, and
  high-throughput browser protocol work.
- Keep Python responsible for Playwright-shaped ergonomics, option validation,
  event/context manager behavior, and compatibility imports.
- Add abstractions only when they reduce duplication or isolate real protocol
  complexity.
- Favor parity tests over speculative rewrites.

## Current Layout

| Path | Responsibility |
| --- | --- |
| `src/lib.rs` | PyO3 extension, Chromium launch/connect, CDP client/session, browser/context/page primitives, protocol event handling, input/network/screenshot/PDF/tracing helpers. |
| `python/rustwright/sync_api.py` | Main Playwright-compatible sync Python API: option normalization, public classes, locators, contexts, pages, requests, routes, assertions, event waiters, artifacts. |
| `python/rustwright/async_api.py` | Async Playwright-compatible facade over the sync implementation. |
| `python/rustwright/_devices.py` | Device descriptor data. |
| `python/rustwright/cli.py` | CLI entry points. |
| `python/rustwright/pytest_plugin.py` | Pytest fixtures. |
| `python/playwright/*`, `python/patchright/*`, `python/cloakbrowser/*` | Compatibility import packages. Public alpha compatibility imports should be enabled only through opt-in compatibility mode. |
| `benchmarks/automation_cases.py` | 408 shared Playwright-style automation/parity cases and the 15-case benchmark subset, including WebVoyager/Mind2Web-style workflow cases. |
| `benchmarks/run_benchmarks.py` | Rustwright and Playwright benchmark runner for the 15-case comparable workload. |
| `tests/test_rustwright_sync_api.py` | Main behavior/regression suite. |
| `tests/test_playwright_parity_cases.py` | Shared parity harness test entry point. |
| `tools/api_surface_audit.py` | Public API surface comparison against reference Playwright. |
| `tools/run_parity_cases.py` | Runs shared parity cases against Rustwright or reference Playwright. |
| `tools/run_antibot_benchmarks.py` | Anti-bot benchmark runner covering Tier 0 local smoke signals, Tier 1 public fingerprint adapters for SannySoft, CreepJS, BrowserScan, and DeviceAndBrowserInfo, and local Tier 4 fresh/warm profile matrix checks across Rustwright and Playwright. |

## Known Architecture Debt

The current implementation intentionally optimized for fast parity iteration.
The largest monoliths are now large enough to slow development:

| File | Current size | Debt |
| --- | ---: | --- |
| `src/lib.rs` | 8,397 lines | CDP transport, browser state, event routing, DOM helpers, stealth/dedicated-worker identity wiring, network shaping, and PyO3 exports are all colocated. |
| `python/rustwright/sync_api.py` | 23,663 lines | Public API classes, option validators, event waiters, routing, locators, assertions, artifacts, and request helpers are colocated. |
| `benchmarks/automation_cases.py` | 16,158 lines | Shared parity cases and benchmark workflows are useful but increasingly hard to scan by subsystem. |
| `tests/test_rustwright_sync_api.py` | 29,011 lines | Broad regression coverage is useful but hard to navigate by subsystem. |

This is acceptable for alpha while behavior is moving quickly, but the beta
bar should include splitting by stable ownership boundaries.

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

1. Extract Python option validators into `python/rustwright/options.py`.
2. Extract Python event waiters/context managers into `python/rustwright/events.py`.
3. Extract Rust launch/process code into `src/browser/launch.rs`.
4. Extract Rust CDP transport/session code into `src/cdp/`.
5. Split tests by subsystem after the corresponding code module split.

The first two Python splits are likely the safest because recent work has
added many Playwright-style option validators and event waiter fixes. They can
be moved mechanically with low behavior risk if the full suite is kept green.
