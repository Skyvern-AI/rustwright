# Contributing

Rustwright is an alpha Rust/PyO3 project with a Playwright-shaped Python API.
Expect some rough edges, large files, and compatibility behavior that is still
being proven.

PRs with user-facing changes should add a bullet under `## [Unreleased]` in `CHANGELOG.md`.

## Local Setup

Use a virtual environment and build the native extension with maturin:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip maturin pytest pytest-benchmark
maturin develop --release
python -m rustwright install chromium
```

Run Rust checks with:

```bash
cargo check --locked
cargo test --locked
```

The repository also ships a native agent CLI in [`cli/`](cli/), a standalone Cargo workspace with
its own lockfile. Build and exercise it with:

```bash
cargo run --manifest-path cli/Cargo.toml -- open https://example.com
cargo run --manifest-path cli/Cargo.toml -- close
cargo test --manifest-path cli/Cargo.toml
```

Once Chromium is installed, run the real-browser CLI and daemon cases with
`cargo test --manifest-path cli/Cargo.toml -- --ignored`.

## Tests

The full pytest suite is heavy: **1,046 tests pass** in the full Docker gate,
with 6 skipped in the current internal gate. CI (`test.yml`) runs a fast
representative subset on every PR. Prefer a focused `-k` subset while
iterating:

```bash
pytest tests/test_rustwright_sync_api.py -k "launch_goto_title_and_url or click_updates_dom or fill_sets_value_and_dispatches_events or frame_locator_scopes_locators_to_iframe_content"
```

For a broader local smoke before opening a PR:

```bash
pytest tests/test_rustwright_sync_api.py -k "launch_goto_title_and_url or goto_returns_real_response_status_headers_and_body or click_updates_dom or locator_click_dispatches_trusted_mouse_sequence_by_default or fill_sets_value_and_dispatches_events or locator_count_nth_and_text or frame_api_can_query_and_act_in_same_origin_iframe or frame_locator_scopes_locators_to_iframe_content or focus_press_keyboard_and_hover or page_and_locator_click_support_mouse_options or unsupported_browser_types_raise or async_api_basic_flow"
```

Avoid local Docker for routine iteration. The Docker paths are useful for
release evidence, but local runs can consume a lot of memory and have OOMed in
practice.

## Code Layout

The implementation is intentionally compact for alpha iteration, but two
monoliths dominate the project today:

- `src/lib.rs`: PyO3 extension, Chromium process/CDP management, browser
  primitives, events, input, network, screenshot/PDF, and artifact helpers.
- `python/rustwright/sync_api.py`: Playwright-shaped Python API, option
  validation, locators, pages, contexts, event waiters, requests, routes,
  assertions, and artifact wrappers.

A module split is planned once behavior stabilizes. Keep changes scoped, add
focused tests for behavior changes, and avoid mixing mechanical refactors with
compatibility fixes.
