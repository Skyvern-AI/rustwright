Please review this pull request for the **Rustwright** repository and give concise, actionable feedback.

Rustwright is a Rust engine (PyO3 for Python, napi-rs for Node) that speaks raw Chrome DevTools Protocol and exposes a **Playwright-compatible** API. It is alpha, Chromium-only, MIT-licensed. The Rust core lives in `src/lib.rs`; the Python API in `python/rustwright/`; the Node bindings in `node/`.

Review for:

- **Correctness & bugs** — logic errors, unhandled edge cases, error handling. Pay special attention to:
  - the FFI boundaries — PyO3 `Bound<'py, T>` usage and GIL handling (`py.detach` around blocking work), and napi async/threadsafe-function usage;
  - the CDP layer — session/frame routing (OOPIF), the tokio-tungstenite WebSocket transport, and message dispatch/correlation.
- **Memory & concurrency safety** — any new `unsafe`; `unwrap()`/`expect()`/`panic!` on fallible paths that could crash a user's process across the FFI boundary; races or deadlocks in the async CDP client; holding the GIL across I/O.
- **Security** (this is browser-automation software) — untrusted-input handling, JS/CDP injection, and anything that silently changes the **trusted-input-by-default** behavior. Rustwright must **not overclaim on anti-detection**: flag any change that adds "undetectable" / "bypass Cloudflare" / "stealth" style claims, or that weakens the honest signal-hygiene framing (it currently passes 3/4 public fingerprint targets and says so plainly).
- **API compatibility** — the Python/Node surface mirrors Playwright. Flag divergence from Playwright's shape/behavior that isn't justified, and missing parity coverage.
- **Performance** — avoidable allocations/copies on the per-CDP-message hot path; blocking calls inside async paths.
- **Tests** — is the change covered by the pytest suite (`tests/`)? Does it warrant a parity test against real Playwright?
- **Honesty of claims** — if the PR touches README/docs/benchmarks, verify the claims match what the code and tests actually support (no inflated numbers, no unverified platform/behavior claims, MSRV kept accurate).

Use `CONTRIBUTING.md` (and `CLAUDE.md` if present) for conventions. Be constructive.

Keep the review **concise and scannable**:
- Start with a 1–2 sentence overall assessment.
- Group feedback by severity using GitHub `<details>`/`<summary>` collapsible sections.
- Expand 🔴 Critical by default; keep 🟡 Suggestions and 📝 Minor collapsed.
- If there are no issues, post a brief LGTM (with the marker).

Format:

```
<!-- claude-code-review -->
## Summary
Brief overall assessment.

<details open>
<summary>🔴 Critical Issues (X)</summary>

- ...

</details>

<details>
<summary>🟡 Suggestions (X)</summary>

- ...

</details>

<details>
<summary>📝 Minor / Style (X)</summary>

- ...

</details>
```

IMPORTANT: Start your comment with the exact marker `<!-- claude-code-review -->` on the first line — this lets a later commit replace the previous review instead of stacking comments.

Use `gh pr comment` (via your Bash tool) to post the review as a single comment on the PR.
