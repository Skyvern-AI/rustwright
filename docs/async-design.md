# Async Concurrency Findings

## Update: high-concurrency fixes (2026-07)

The original findings below are retained as the historical record; the
bottleneck diagnosis led to two engine changes that supersede the original
no-go recommendation:

1. **GIL release during CDP waits.** Every PyO3-boundary blocking wait
   (the `BrowserInner::block_on` command funnel, launch/connect, close)
   now detaches from the interpreter while parked on Tokio. Non-Python
   consumers (the Rust API and Node bindings) use a raw, Python-free path,
   and destructors never attach, so drops during interpreter finalization
   stay safe.
2. **One event pump per page.** The per-(page, event) polling threads
   (~7 per page) were replaced by a Rust-side combined event stream per
   page — cursor-backed against the shared event log, sequence-ordered,
   with explicit overflow envelopes and immediate close wake-up — consumed
   by a single Python pump thread that dispatches through the pre-existing
   per-event handlers. Control paths (route, auth, binding, download,
   popup, worker, websocket, file chooser, crash) keep dedicated waiters.

Re-measurement on the same benchmark (macOS arm64, 10 cores, Python
3.13.5), all scenarios passing with zero task errors:

| Stack | Variant | N | tps | loop lag p99 | py threads | tree RSS MB |
|---|---|---:|---:|---:|---:|---:|
| Rustwright (before) | shared | 100 | 9.2 | 292 ms | 517 | 7,936 |
| Rustwright (after) | shared | 100 | 23.9 | 31 ms | 118 | 9,409 |
| Playwright | shared | 100 | 24.3 | 59 ms | 8 | 8,899 |
| Rustwright (after) | multi | 100 | 25.5 | 106 ms | 118 | 1,598 |
| Playwright | multi | 100 | 26.0 | 64 ms | 12 | 8,585 |

Shared-browser N=25 improved from 15.4 to 26.8 tps with loop lag p99
dropping from ~410 ms to well under 100 ms.

Memory: the client-side stack (no Node driver) idles at ~41 MB versus
~121 MB for playwright-python (31 MB Python + 89 MB Node driver), a
~3× reduction in the part the library controls. Whole-process-tree
peaks under load are dominated by Chromium and vary by scenario: in
this run Rustwright's tree peaked lower in six of eight scenarios —
most sharply in the four-browser variant (1.6 GB vs 8.6 GB at N=100)
— but slightly higher at shared N=5 and N=100. The multi-browser gap
is consistent across runs but not yet root-caused or reproduced on
CI-backed runners, so treat it as an observation, not a claim. Thread count now grows
O(pages) instead of O(pages × events); an idle page costs one pump
thread. `configure_async_executor(max_workers=32..100)` adds roughly
7–14% throughput at N=100 now that workers no longer contend on the GIL,
but the default executor remains the recommended configuration.

## Update: native-async engine (2026-07)

The native-async engine proposed at the end of this document is now
implemented for the default Chromium launch and supported configurations of
`new_context`/`new_page`, plus the page `goto`/`click`/`fill`/`evaluate`/
`wait_for_selector`/`screenshot`/`close` hot paths. Those paths register a
Rust future on the browser's Tokio runtime and return a real `asyncio.Future`
settled via `loop.call_soon_threadsafe`, instead of dispatching the sync call
on a thread pool. Unsupported option combinations fall back to the executor,
as do `connect`/`connect_over_cdp`, `wait_for_event`, and waits such as
`wait_for_url`, `wait_for_load_state`, and `wait_for_function`. Per-page event
delivery normally uses a single asyncio task over the combined Rust event
stream, with no per-page pump thread on these native paths.

Re-measurement on the same benchmark (macOS arm64, 10 cores, Python 3.13.5),
`benchmarks/async_concurrency_load.py --concurrency 100`, all scenarios
passing with zero task errors:

| Stack | Variant | N | active threads | loop lag p99 | errors |
|---|---|---:|---:|---:|---:|
| Rustwright (event-pump phase) | shared | 100 | 118 | 31 ms | 0 |
| Rustwright (native async) | shared | 100 | 18 | 138–296 ms | 0 |
| Rustwright (native async) | multi | 100 | 18–19 | 116–235 ms | 0 |

For the native benchmark hot paths, active threads are O(browsers + event
loop) rather than O(pages): 18 threads drive 100 concurrent pages, down from
118. This does not cover executor-backed operations and waits; pages adopted
through synchronous popup waiters can also retain a legacy pump thread.
Throughput is comparable to the thread-pool facade and load-sensitive on a
shared machine; the win here is thread footprint and real single-loop
integration, not raw tps. The `cargo` unit suite (34 tests) and the async
regression selection (596 tests) pass. Subinterpreters remain unsupported
(see the shutdown-settlement limitation below).

### Shutdown settlement guarantees and known limitation

Native future settlement now registers a repository-owned `atexit` gate.
Orderly interpreter shutdown closes that gate before finalization, releases
the GIL while draining settlement callbacks already inside the attachment
window (bounded to two seconds), and makes later workers leak their final
process-lifetime Python references instead of attempting a background
attachment. If an owner asyncio loop has already closed during normal
runtime, failed `call_soon_threadsafe` delivery directly cancels the pending
future so it does not remain pending forever.

**Subinterpreters are unsupported.** The settlement gate and active-settlement
counter are process-global, so shutting down one subinterpreter can disable
future delivery in another still-running subinterpreter. Use one main Python
interpreter per process for the async API.

**Known limitation — embedding teardown that bypasses Python `atexit`:** an
embedding host that begins `Py_FinalizeEx` (or destroys a subinterpreter)
without running the registered `atexit` callback, at the exact time a Rust
operation enters its settlement attachment window, still depends on PyO3's
best-effort `Python::try_attach` finalization detection. The same residual
applies if a settlement callback remains inside that window longer than the
two-second drain bound. Removing this last pathological race requires host
lifecycle integration (an explicit pre-finalize hook for every embedding and
subinterpreter), which this library cannot install portably from an extension
module.

---

## Scope

This section records the original measurement-first evaluation of
`rustwright.async_api` under a high-concurrency browser-automation profile,
before any engine change. The native-async rewrite it deferred has since
been implemented — see "Update: native-async engine (2026-07)" above.

The benchmark is `benchmarks/async_concurrency_load.py`. It starts a local
HTTP server and runs concurrent workflows at N=5, 25, 50, and 100. Each
workflow uses one shared browser or one of four capped browsers and performs:

`new_context -> new_page -> goto -> wait_for_selector -> click -> fill -> inner_text -> screenshot -> close`

The benchmark records JSON after every scenario so interrupted runs preserve
raw evidence.

## Reproduction

```bash
python -m venv .venv
.venv/bin/pip install maturin pytest
VIRTUAL_ENV="$PWD/.venv" PATH="$PWD/.venv/bin:$PATH" .venv/bin/maturin develop --release

.venv/bin/python benchmarks/async_concurrency_load.py \
  --impl rustwright \
  --concurrency 5 25 50 100 \
  --variants shared multi \
  --output benchmark-results/rustwright-before.json \
  --traceback-dir benchmark-results/tracebacks

.venv_playwright_compare/bin/python \
  benchmarks/async_concurrency_load.py \
  --impl playwright \
  --concurrency 5 25 50 100 \
  --variants shared multi \
  --output benchmark-results/playwright-baseline.json \
  --traceback-dir benchmark-results/tracebacks
```

Executor probes:

```bash
.venv/bin/python benchmarks/async_concurrency_load.py \
  --impl rustwright \
  --concurrency 5 25 50 100 \
  --variants shared multi \
  --rustwright-executor-workers 100 \
  --output benchmark-results/rustwright-executor100.json

.venv/bin/python benchmarks/async_concurrency_load.py \
  --impl rustwright \
  --concurrency 5 25 50 100 \
  --variants shared multi \
  --rustwright-executor-workers 32 \
  --output benchmark-results/rustwright-executor32.json
```

## Environment

- macOS 26.0.1 arm64
- Python 3.13.5
- CPU count: 10
- Default asyncio ThreadPoolExecutor ceiling observed: `min(32, cpu+4) = 14`
- Chromium instances were bounded: one browser for `shared`, four browsers for `multi`.

## Primary Results

`tps` is completed workflow tasks per second. `task p99` and `loop lag p99` are milliseconds. `py threads` is `threading.active_count()` peak. `tree RSS` is process-tree RSS peak.

### Shared Browser

| Stack | N | Status | tps | task p99 | loop lag p99 | py threads | tree RSS MB | errors |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| Rustwright | 5 | passed | 5.11 | 978 | 306 | 25 | 286 | 0 |
| Playwright | 5 | passed | 0.20 | 24,352 | 1,353 | 5 | 592 | 0 |
| Rustwright | 25 | passed | 8.71 | 2,854 | 561 | 201 | 414 | 0 |
| Playwright | 25 | passed | 1.79 | 13,961 | 684 | 6 | 2,404 | 0 |
| Rustwright | 50 | passed | 6.06 | 8,247 | 541 | 393 | 428 | 0 |
| Playwright | 50 | failed | 0.69 | 71,560 | 2,083 | 8 | 4,095 | 17 |
| Rustwright | 100 | failed | 8.65 | 11,561 | 903 | 694 | 531 | 100 |
| Playwright | 100 | failed | 0.94 | 104,763 | 1,189 | 9 | 7,648 | 30 |

Rustwright shared-browser N=100 is the headline failure: all 100 tasks failed. The raw JSON records `CDP websocket is closed` for every task sample, with peak Python active threads at 694 and ps self threads at 701.

### Four-Browser Cap

| Stack | N | Status | tps | task p99 | loop lag p99 | py threads | tree RSS MB | errors |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| Rustwright | 5 | passed | 0.36 | 13,811 | 5,643 | 38 | 898 | 0 |
| Playwright | 5 | passed | 0.47 | 8,570 | 593 | 9 | 936 | 0 |
| Rustwright | 25 | passed | 0.56 | 44,256 | 13,938 | 42 | 802 | 0 |
| Playwright | 25 | passed | 1.23 | 20,162 | 598 | 10 | 3,053 | 0 |
| Rustwright | 50 | passed | 1.21 | 40,282 | 5,499 | 99 | 1,071 | 0 |
| Playwright | 50 | passed | 0.55 | 83,322 | 1,757 | 11 | 3,995 | 0 |
| Rustwright | 100 | failed | 1.32 | 75,469 | 5,500 | 18 | 1,023 | 3 |
| Playwright | 100 | failed | 1.02 | 97,510 | 987 | 13 | 8,033 | 19 |

Rustwright multi-browser N=100 failed with `Locator.click: Timeout 10000ms exceeded.` samples. It had fewer task errors than Playwright in this run, but event-loop lag and task tail latency were materially worse than Playwright at lower N.

## Bottleneck Diagnosis

Confirmed first: the default executor ceiling is 14 on this machine. Every `rustwright.async_api` operation was using `asyncio.to_thread`, so each in-flight browser call occupied one default executor worker while waiting for the sync API to return.

That ceiling is real, but it is not the only or primary high-N failure mode:

- Rustwright creates many Python threads outside the default executor. A `Page` starts sync listener threads for request, requestfinished, requestfailed, console, and pageerror events. This drives hundreds of Python threads at N=50/100.
- Rustwright shared N=100 peaked at 694 Python active threads and then lost the CDP websocket. This is not a simple "only 14 workers queued" profile.
- The Rust CDP core multiplexes calls through one `CdpClient` per browser with one websocket transport and a pending-map mutex. I did not find a broad Python global lock serializing page operations.
- PyO3 sync methods commonly call `BrowserInner::block_on(...)` without an obvious `py.allow_threads` release around the wait. That means executor threads may block on Rust/Tokio CDP work while holding the GIL, which matches the observed event-loop lag under load.
- Playwright keeps Python thread count low, but pushes pressure into Chromium/browser processes. Its shared-browser N=50/100 also failed in this local setup, but with partial action timeouts rather than Rustwright's all-task CDP websocket closure at shared N=100.

## Cheap Fix Applied

I added a configurable executor to `rustwright.async_api`:

```python
from rustwright.async_api import configure_async_executor
configure_async_executor(max_workers=100)
```

By default behavior is unchanged. If configured, async wrapper calls use the Rustwright-owned executor instead of the event loop default executor. This is intentionally a measurement/tuning hook, not a native-async rewrite.

### Remeasurement

| Config | Variant | N | Status | tps | task p99 | loop lag p99 | py threads | errors |
|---|---|---:|---|---:|---:|---:|---:|---:|
| default | shared | 50 | passed | 6.06 | 8,247 | 541 | 393 | 0 |
| executor=32 | shared | 50 | passed | 0.96 | 52,207 | 19,999 | 35 | 0 |
| executor=100 | shared | 50 | failed | 0.47 | 105,085 | 41,515 | 407 | 11 |
| default | shared | 100 | failed | 8.65 | 11,561 | 903 | 694 | 100 |
| executor=32 | shared | 100 | failed | 1.21 | 82,154 | 14,353 | 35 | 19 |
| executor=100 | shared | 100 | failed | 0.70 | 140,703 | 48,827 | 61 | 76 |
| default | multi | 50 | passed | 1.21 | 40,282 | 5,499 | 99 | 0 |
| executor=32 | multi | 50 | passed | 1.23 | 40,272 | 14,312 | 43 | 0 |
| executor=100 | multi | 50 | passed | 1.78 | 27,916 | 10,526 | 54 | 0 |
| default | multi | 100 | failed | 1.32 | 75,469 | 5,500 | 18 | 3 |
| executor=32 | multi | 100 | failed | 0.54 | 181,808 | 44,460 | 35 | 45 |
| executor=100 | multi | 100 | failed | 0.65 | 152,063 | 57,022 | 551 | 86 |

Conclusion: increasing executor size is not a safe default fix. It can improve a few four-browser mid-concurrency cases, but it regresses shared-browser throughput and does not make N=100 reliable. Keep the configurability for experiments, but do not present it as solving high-concurrency workloads.

## Recommendation (superseded — see the 2026-07 update above)

At the time of the original measurement, `asyncio.to_thread` Rustwright was **no-go** for high-concurrency replacement paths.

Concrete threshold:

- Acceptable only for controlled experiments at <=25 concurrent workflows per process, and preferably only with a shared browser.
- Risky at 50 concurrent workflows: Rustwright shared passed but required ~400 Python threads, which is operationally fragile.
- Not acceptable at 100 concurrent workflows: shared-browser Rustwright failed every task with `CDP websocket is closed`; four-browser Rustwright also failed and had very high event-loop lag.

The surprising result is that Rustwright can be faster than Playwright in the shared-browser happy path up to N=50, but its failure mode at N=100 is not acceptable for production. Numbers over narrative: the current model is fast until it collapses.

## Native-Async Design Proposal

Target architecture:

1. Keep the Rust CDP client as the source of truth. It already has Tokio, `oneshot` pending calls, a websocket reader, and event broadcast channels.
2. Expose Python async APIs as real awaitables/Futures instead of blocking sync calls inside executor threads.
3. For each Python call, register a Rust future on the browser Tokio runtime and return an `asyncio.Future`.
4. Complete the Python future with `loop.call_soon_threadsafe(...)` when the Rust future resolves.
5. Release the GIL for any remaining blocking boundary.
6. Replace per-page Python listener threads with async event subscriptions backed by the existing Rust broadcast/event log.
7. Keep the sync API as a blocking facade over the async core, not the other way around.

Implementation options:

- Use `pyo3-async-runtimes` if it fits the repo's PyO3 version and runtime ownership.
- Or implement a small bridge manually: capture the Python event loop, allocate an `asyncio.Future`, spawn the Rust future, and settle the future through `Python::with_gil` plus `loop.call_soon_threadsafe`.

Estimated scope:

- 1 week: prototype one browser/page path (`launch`, `new_context`, `new_page`, `goto`, `click`) and prove no Python worker thread is occupied during CDP waits.
- 2-4 weeks: convert core hot-path operations and remove per-page listener threads for request/console/pageerror events.
- 4-8+ weeks: parity hardening across locators, downloads, routes, event context managers, cancellation, timeouts, and sync facade compatibility.

The native-async success criterion should be explicit: at N=100, no all-task CDP websocket failure, Python thread count close to O(browsers + event-loop support) rather than O(pages), p99 event-loop lag under 1s on this local benchmark, and task error rate no worse than Playwright's baseline under the same Chromium pressure.
