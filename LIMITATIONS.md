# Limitations

Rustwright is an alpha, not a complete Playwright replacement.

- Chromium only. Firefox and WebKit entry points currently raise unsupported
  browser errors.
- Playwright wire endpoints from `playwright run-server` and
  `BrowserType.launchServer()` are unsupported. Remote Chromium requires raw
  CDP through `chromium.connect_over_cdp()`. HTTP discovery can identify a
  Playwright endpoint definitively. Direct WebSocket detection is heuristic
  and can only report that the first reply resembles Playwright wire. See
  [`docs/REMOTE_BROWSERS.md`](docs/REMOTE_BROWSERS.md).
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
- Chromium security-masks window `ErrorEvent` detail (`message='Script error.'`,
  `error=null`) for asynchronous callbacks created by inspector-compiled
  `Runtime.callFunctionOn` declarations. With `Runtime.enable` disabled
  (Rustwright's stealth default), passive page-error history does not promise
  full detail for errors thrown by evaluate-created async callbacks.
  Same-origin, same-document page-authored scripts retain full detail. Standard
  web-platform cross-origin masking still applies to external scripts loaded
  without CORS. Subscribing to the `pageerror` event enables the Runtime domain
  and provides full detail for evaluate-created asynchronous callbacks.
- The implementation still has large monolithic files. A module split is
  planned before beta.
