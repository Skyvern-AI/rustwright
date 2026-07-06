# Limitations

Rustwright is an alpha, not a complete Playwright replacement.

- Chromium only. Firefox and WebKit entry points currently raise unsupported
  browser errors.
- Behavioral parity is not fully proven. Rustwright exposes a broad
  Playwright-shaped API, but API-surface coverage is not the same as complete
  browser behavior parity.
- Async support currently wraps the sync implementation through Python thread
  execution. It is not recommended above roughly 25 concurrent workflows per
  process. Native async is planned; see [`docs/async-design.md`](docs/async-design.md).
- OOPIF support is new. Cross-origin frame locator actions work in covered
  cases, but non-main-frame remote `JSHandle` follow-up operations remain a
  gap, and drag, screenshot, and bounding-box behavior in OOPIFs is not yet
  claimed as full parity.
- Anti-bot and stealth behavior is partial. Rustwright suppresses some common
  automation signals, but recent public fingerprint checks were only clean on
  about 2 of 4 targets. Rustwright does not promise undetectability.
- Drop-in compatibility import names are intended to be opt-in for the public
  alpha. The final compatibility-mode API is being finalized separately.
- The implementation still has large monolithic files. A module split is
  planned before beta.
