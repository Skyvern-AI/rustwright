# Rustwright cloud benchmark weaknesses

## Scope and standard of evidence

This review covers the cloud suite driven by `harness/run_suite.py`, with the
navigation, download, and form-fill workloads and the current smoke cases. The
intended claim is unusually strict: browser and website time are supposed to be
excluded so that the result represents local library latency and memory. The
current implementation does not yet support that claim. It is useful as a smoke
test and as a source of hypotheses, but its aggregate `library_latency_ms` is not
a clean estimate of library-only overhead.

Severity meanings:

- **Critical**: can reverse or invalidate the headline backend comparison.
- **High**: materially biases results or prevents reproducible attribution.
- **Medium**: weakens coverage, statistical confidence, or diagnosis.
- **Low**: reporting or maintainability issue that should be fixed before publication.

## 1. Metric validity

### M1 — **Critical**: `library_latency_ms` means different things in each workload

**Problem.** Navigation sums every span tagged `library`, which includes the
composite startup/connect span, click spans, and a final span containing probes
and teardown (`nav_workload.py:61-70`, `nav_workload.py:115-136`). Download uses
the same tag for startup/connect, click dispatch, and a mislabeled probe span
(`download_workload.py:51-69`, `download_workload.py:73-104`). Form-fill is more
severe: it defines `action_ms` as total wall time minus scripted sleeps and then
copies that value to `library_latency_ms` (`fill_form.py:469-482`). That total
still includes launch/connect, page navigation, selector readiness, rendering,
screenshots, validation, probes, and teardown. Although form-fill records
separate launch and navigation values (`fill_form.py:385-398`), it does not
subtract them from the headline library number. Results across categories are
therefore neither comparable nor library-only.

**Recommendation.** Define one metric contract and enforce it in shared code.
Emit non-overlapping spans for `python_import`, `api_startup`, `cdp_connect`,
`new_context`, `new_page`, `navigation`, `page_condition_wait`, each API
operation, `artifact_io`, `probe`, and `teardown`. Compute published metrics by
summing explicit operation spans, never as `total - pauses`. Keep end-to-end
workload time as a separate metric with a name that does not imply isolation.

### M2 — **Critical**: clicks classified as library work can wait on the website

**Problem.** Every navigation `click` is labeled `library`
(`nav_workload.py:85-103`), but both smoke clicks are anchors that cause a real
navigation (`cases/smoke_nav.json:7-11`, `cases/smoke_nav.json:19-23`). A click
can wait for attachment, visibility, stability, hit testing, scrolling, event
dispatch, and navigation triggered by the action. All of those can depend on
layout, page JavaScript, rendering, the target server, and remote browser
scheduling. The following `wait_for_selector` is tagged `page_wait`, but some or
all of the navigation may already have been paid inside `click`, so changing the
label on the next call does not remove the leak. Download similarly labels the
click as library work even though the clicked link is what initiates the
download (`download_workload.py:65-69`).

**Recommendation.** Do not use live navigational clicks in a library-only
aggregate. For action-dispatch probes, click a deterministic element whose
handler only increments an in-page counter and explicitly prevents navigation.
For navigation cases, wrap the action in an explicit expected-navigation span
and report actionability/dispatch separately from the navigation wait where the
API permits it. Treat any indivisible click-plus-navigation call as end-to-end,
not library-only.

### M3 — **High**: even the “render-free” probes mostly measure remote CDP RTT

**Problem.** `evaluate("1")` and `evaluate("document.title")` each require at
least a command/response exchange with the remote browser
(`harness/library_probe.py:37-47`). Their floor is WebSocket/TLS/proxy latency,
remote CDP scheduling, browser-main-thread availability, and serialization—not
local library execution. A large and variable RTT can make different local
implementations look identical, while different routes to two sessions can
make identical implementations look different. The aggregate then sums the
three operation medians and calls the result a single “median”
(`harness/library_probe.py:69-77`), which has no direct statistical
interpretation and hides which operation changed.

**Recommendation.** Report each probe independently. Capture raw samples and
CDP command counts/bytes. Estimate a per-run RTT floor with a minimal command,
then analyze intercepts and slopes over controlled payload sizes or operation
counts rather than presenting raw remote RTT as local library time. Add truly
client-side probes (for example, locator construction and option validation)
for Python/Rust/Node overhead that does not contact the browser. Describe CDP
probes as client-to-browser API latency, not library-only latency.

### M4 — **High**: the stable-click probe is not stable or page-independent

**Problem.** The probe injects a button into whatever live page the case happens
to leave open and then clicks it (`harness/library_probe.py:49-65`). Its timing
still depends on the live page's CSS cascade, global input listeners, animation,
layout load, overlays, transforms, body state, and renderer load. A high
`z-index` and fixed dimensions do not eliminate those dependencies. Probe order
is fixed, so click always runs after two evaluate loops and after the injection
evaluate; later probes see a warmer connection and library state
(`harness/library_probe.py:37-65`).

**Recommendation.** Run probes in a fresh page containing a versioned,
deterministic fixture with CSS reset, no external resources, no animation, and
verified state. Warm every probe explicitly, then interleave or randomize their
measured iterations. Verify the click counter after every batch. Keep live-page
actionability as a separate realism lane rather than using it as a calibration
probe.

### M5 — **Critical**: probe and teardown spans are mislabeled and inconsistent

**Problem.** Navigation runs all probes and closes the context/browser before
creating the final `teardown` interval, so that one library span includes both
probe time and actual teardown (`nav_workload.py:115-125`). Download does the
opposite: it runs the probes, calls `finish_interval("teardown", "library")`,
and only then closes the context/browser in `finally`
(`download_workload.py:73-83`). Thus its “teardown” span contains probes but not
teardown, while the real close time appears in `total_ms` but in no timeline
span (`download_workload.py:85-105`). Form-fill folds validation, probes, close,
and teardown into a final action span (`fill_form.py:437-470`). The common probe
also dominates every case's aggregate, reducing sensitivity to the actual case.

**Recommendation.** Start and end an explicit span around each probe group and
around each close operation. Assert that intervals are contiguous and that the
last interval endpoint equals total wall time within clock precision. Exclude
probes and teardown from case-operation latency; publish them as independent
diagnostics.

### M6 — **High**: startup/connect attribution is too coarse for the observed 3x gap

**Problem.** In navigation, the span labeled “library startup and remote CDP
connect” includes entering `sync_playwright()`, `connect_over_cdp`, creating a
dedicated context, and obtaining or creating a page
(`nav_workload.py:61-70`). Download stops the same label before `new_page`, so
the boundary differs by workload (`download_workload.py:51-63`). Form-fill adds
context routes, an init script, and `new_page` before stopping its launch timer
(`fill_form.py:346-386`). Conversely, backend imports occur before the timer in
navigation/download (`nav_workload.py:10-14`, `download_workload.py:10-14`) and
before `total_start` in form-fill (`fill_form.py:285-322`), so cold Python import
cost is omitted. The observed approximately 1,336 ms Rustwright versus 449 ms
Playwright number therefore establishes a composite cold-start gap, not a
precise `connect_over_cdp` gap.

**Recommendation.** Time module import in a tiny launcher and split startup into
API context-manager entry, endpoint discovery/handshake, target discovery,
context creation, page acquisition, and close. Add protocol tracing that counts
commands and round trips in each phase. Report cold process startup separately
from warm reconnects in a long-lived process. Re-run enough matched pairs to
determine whether the 3x result is handshake work, extra CDP round trips, or
session/network variance.

### M7 — **High**: `page_wait`, `navigation`, and library boundaries are not semantically clean

**Problem.** Navigation's `goto` and history operations are reasonably tagged as
navigation, but `wait_for_selector` is treated as pure page wait even when the
element is already present and the call mostly consists of selector-engine and
CDP work (`nav_workload.py:77-103`). Conversely, form-fill executes both
`goto(..., wait_until="domcontentloaded")` and `wait_for_selector` inside its
navigation timer (`fill_form.py:388-398`) yet reports `page_wait_ms: 0`
(`fill_form.py:474-483`). Download includes `context.new_page()` in its
navigation span because the previous interval ends before page creation
(`download_workload.py:58-63`), whereas navigation includes page acquisition in
launch (`nav_workload.py:67-70`). Accessing `suggested_filename` is after the
page-wait interval and is silently charged to the following probe/mislabeled
teardown span (`download_workload.py:65-80`). Identically named output columns
therefore cover different APIs.

**Recommendation.** Give every API call its own raw span and attach orthogonal
attributes such as `operation=wait_for_selector`, `cause=page_condition`, and
`phase=navigation`; do not force each call into one lossy bucket at collection
time. Derive views later. Add invariants/tests for known synthetic timelines so
the same operation is classified identically across workloads.

### M8 — **Critical**: two suite repetitions cannot support medians or p95 claims

**Problem.** The suite defaults to two repetitions (`harness/run_suite.py:30-39`;
the cloud workflow also defaults to two at
`.github/workflows/form-fill-cloud-benchmark.yml:15-18`). With two values, the
reported median is merely their midpoint and the inclusive p95 is an
interpolated value close to the maximum, not an empirical tail estimate
(`harness/run_suite.py:79-82`, `harness/run_suite.py:157-191`). Within a probe,
the first sample is discarded and only 19 measured samples remain at the
workloads' default of 20 (`harness/library_probe.py:11-29`,
`nav_workload.py:115-118`). One warm-up does not stabilize imports, driver
startup, JITs, selector injection, connection state, or browser caches. Raw
probe samples are discarded, so variance, multimodality, drift, and paired
analysis cannot be recovered.

**Recommendation.** Use a predeclared sampling protocol: explicit cold trials,
a separate warm-up phase, and at least 20–30 matched run pairs for central
tendency. Use substantially more within-process samples for p95 (preferably at
least 100 after warm-up), retain every raw sample, and report paired ratios or
differences with bootstrap confidence intervals and median absolute deviation.
Do not publish p95 when the sample count is inadequate. Randomize or balance
backend order in blocks and show the time series to expose drift.

### M9 — **High**: silent probe failure creates survivorship bias

**Problem.** All three workloads catch every probe exception and replace the
entire result with `None` (`nav_workload.py:115-120`,
`download_workload.py:73-78`, `fill_form.py:459-464`). The run can still be
marked successful. The suite then omits missing probe values when computing the
summary (`harness/run_suite.py:163-183`). A backend that fails or times out on a
hard probe can consequently receive a summary based only on easier successful
runs.

**Recommendation.** Record each probe's status, exception category, timeout,
and raw samples without sensitive text. Make required-probe failure fail the
run, and report success rate beside latency with no success-only headline.
Predefine whether a failure is assigned a timeout-censored value or analyzed as
a separate reliability outcome; never silently drop it.

### M10 — **High**: PSS is a whole-client-stack, duration-biased metric rather than library-only memory

**Problem.** The memory sampler intentionally sums the workload Python process
and descendants, including Playwright's Node driver where present
(`harness/sample_stack_memory.py:74-147`). That is a defensible *local client
stack* footprint, but it also includes workload data, screenshots, Python JSON
objects, validation, and transient file I/O—not just the library. Sampling
starts only after the workload process has been launched, so early import/start
spikes can race the sampler (`harness/measure.py:201-225`). The 100 ms interval
can miss short peaks (`harness/sample_stack_memory.py:150-179`), and longer runs
have more opportunities to produce a high sample. Form-fill's 5-second pause
and per-field screenshots especially distort mean/peak comparisons
(`fill_form.py:399-428`). Although component columns are recorded, the suite
reads only total PSS and summarizes only peak PSS (`harness/run_suite.py:70-76`,
`harness/run_suite.py:129-136`, `harness/run_suite.py:190-191`). The remote
browser is correctly outside scope, as the README acknowledges
(`README.md:65-67`), so these numbers cannot make any browser-memory claim.

**Recommendation.** Rename the metric `local_client_stack_pss`. Start the
process under a barrier so sampling captures pre-import baseline, import,
startup, steady-state, operation, and post-close phases. Report baseline,
incremental PSS, phase-aligned peak, and retained PSS after GC/settling, split by
Python/driver categories. Use repeated object/context churn to measure a memory
slope and leaks. Increase sampling frequency or supplement sampled PSS with
kernel peaks, and compare equal-duration windows with screenshots/artifacts
disabled. Keep remote-browser memory explicitly out of the claim.

## 2. Fairness

### F1 — **Critical**: the backends never observe the same browser session or page state

**Problem.** Every `(case, repetition, backend)` call creates its own
`SkyvernSession` (`harness/run_suite.py:85-100`), and all calls are submitted as
independent futures (`harness/run_suite.py:224-228`). This avoids direct
cross-client interference, but it also means there is no matched browser, cache,
cookie jar, renderer state, connection route, or site response. Session creation
sends only a timeout, with no pinned image, region, or proxy in the request
(`harness/skyvern_session.py:259-277`). Separate sessions can therefore differ
in cold-start state and infrastructure even when launched near each other.

**Recommendation.** Use a matched-pair protocol. If the provider permits two
clients on one browser, run each backend in a fresh context in the same pinned
session using balanced AB/BA order. If it does not, provision an explicit pair
with identical image, region, proxy policy, and session options, synchronize the
measurement start, and repeat with randomized backend/session assignment. Never
claim identical state; quantify pair-to-pair variance and use paired statistics.

### F2 — **High**: provider/browser/proxy equivalence is assumed rather than verified

**Problem.** Run records contain case, category, backend, repetition, and only
the interpreter's basename (`harness/run_suite.py:85-92`,
`harness/run_suite.py:124-137`). They do not capture Chromium product/build,
CDP protocol version, launch flags, locale/time zone, provider region, proxy or
egress geography, user agent, session-ready age, or endpoint network latency.
Different cloud sessions may land on different hosts or browser rollouts; live
sites may then return geo-specific, bot-specific, or A/B-specific content.

**Recommendation.** Pin whatever the provider exposes and record a sanitized
environment manifest for every run: browser product/build, protocol version,
user agent, viewport/device scale factor, locale/time zone, public region/POP,
proxy policy identifier, session creation/ready/connect timestamps, runner
image, Python version, CPU, and backend package/build revision. Reject a matched
pair when required browser/session fields differ. Record network calibration
RTT and jitter without retaining secret endpoint URLs or headers.

### F3 — **High**: parallel execution adds uncontrolled local and remote contention

**Problem.** The default concurrency is four (`harness/run_suite.py:37-39`), so
multiple workload processes, PSS samplers, WebSocket connections, and cloud
browsers compete at once. Submission order is deterministic, but there is no
barrier that makes a Rustwright/Playwright pair begin its measured phase at the
same time (`harness/run_suite.py:224-228`). Faster tasks free worker slots first,
changing which later cases overlap. Host CPU scheduling and network queueing can
affect latency, while concurrency also makes PSS timing less stable. The cloud
workflow even allows materially different runner classes
(`.github/workflows/form-fill-cloud-benchmark.yml:19-30`).

**Recommendation.** Run publication-grade latency and memory trials at
concurrency one on a pinned runner, using randomized balanced order. If parallel
pairs are needed to control temporal drift, dedicate equal resources to each
backend and synchronize them at a barrier; do not mix unrelated cases in the
same contention window. Add a separate load/concurrency benchmark rather than
letting incidental suite concurrency contaminate single-client results.

### F4 — **High**: reference Playwright is reproducibly pinned in CI, but baseline provenance is not enforced by the suite

**Problem.** The cloud workflow does create separate virtual environments and
pins reference Playwright 1.59.0
(`.github/workflows/form-fill-cloud-benchmark.yml:57-68`), then explicitly maps
each backend to its interpreter (`.github/workflows/form-fill-cloud-benchmark.yml:70-82`).
That is a good isolation measure. However, `run_suite.py` defaults both backends
to its own interpreter unless optional mappings are supplied
(`harness/run_suite.py:41-55`, `harness/run_suite.py:85-92`), and the broader
project dependency only says `playwright>=1.50` (`../../pyproject.toml:26-33`).
The suite neither verifies nor records the loaded module path, package version,
driver revision, Rustwright build mode, or commit. A pin is reproducible only if
the artifact and configuration are reported; it is not automatically the right
baseline for every Rustwright revision.

**Recommendation.** Require distinct backend interpreters for comparative
runs. At workload startup, assert the expected backend marker and module origin,
then record Playwright package/driver version, Rustwright version and commit,
Python version, build profile, and dependency-lock digest. Define why 1.59.0 is
the compatibility target and update it deliberately; report additional
Playwright versions if Rustwright claims a broader compatibility/performance
range.

### F5 — **Medium**: Rustwright's opt-in Playwright shim is a real contamination risk outside the CI path

**Problem.** The workload's conditional imports are direct and each workload is
a new subprocess (`nav_workload.py:10-14`, `harness/run_suite.py:113-121`), so
the checked-in CI path is less vulnerable than an in-process A/B harness.
Nevertheless, Rustwright can explicitly alias `playwright`,
`playwright.sync_api`, and related modules in `sys.modules`
(`../../python/rustwright/_compat/__init__.py:11-29`,
`../../python/rustwright/_compat/__init__.py:44-61`). If both packages share an
environment and a startup hook, site customization, or future refactor enables
compatibility before the backend import, the supposed Playwright branch can
resolve to Rustwright. The default same-interpreter suite configuration does not
detect this.

**Recommendation.** Keep the two-backend virtual-environment split mandatory,
start each workload with a clean environment, and fail unless
`playwright.sync_api` resolves inside the pinned reference distribution for the
Playwright run. Also fail if any Rustwright compatibility alias is active. Store
sanitized module origins and backend markers in the result manifest.

### F6 — **Medium**: the comparison scope is CDP client architecture, not general Playwright performance

**Problem.** Both backends use `connect_over_cdp` (`nav_workload.py:61-69`),
which is appropriate for the stated cloud-CDP use case but does not represent
Playwright's native protocol connection or its bundled-browser configuration.
The PSS comparison also includes Playwright's Node driver but Rustwright's Rust
core lives in the Python process (`harness/sample_stack_memory.py:86-96`,
`harness/sample_stack_memory.py:132-147`). This is fair if the claim is
“installed local client stack for remote CDP automation,” but misleading if
presented as intrinsic language-library memory or general browser automation
performance.

**Recommendation.** State the scope in every report and chart. Use names such as
`remote_cdp_call_latency` and `local_client_stack_pss`. If broader claims are
desired, add separate lanes for each backend's recommended/native transport and
for a controlled common local Chromium; do not merge those lanes into the
remote-CDP result.

## 3. Case quality and coverage

### C1 — **Critical**: the default cloud suite is a two-case navigation smoke test, not a representative benchmark

**Problem.** `run_suite.py` defaults to `smoke_nav.json`, so an unmodified cloud
run contains only two cases (`harness/run_suite.py:30-39`;
`.github/workflows/form-fill-cloud-benchmark.yml:7-18`). Those cases each perform
one `goto`, one click, one selector wait, and one history operation
(`cases/smoke_nav.json:1-26`). The one download case lives in a separate file and
is not included by default (`cases/smoke_download.json:1-12`), while no checked-in
cloud case supplies the `BENCH_JOB_URL` required by `fill_form.py`. The cases
README itself says the roughly 100-case catalog is future work
(`cases/README.md:1-3`). A result from the default invocation therefore says
almost nothing about form filling, downloads, or the wider API surface and must
not be presented as a general Rustwright-versus-Playwright result.

**Recommendation.** Make the publication entry point require a versioned suite
manifest rather than silently defaulting to smoke. The manifest should enumerate
all categories and their weights, fixture revision, expected outcome, required
capabilities, and whether the case is latency, conformance, reliability, or
memory evidence. Keep `smoke_nav.json` as a presubmit only and label every smoke
report prominently as non-comparative.

### C2 — **High**: the live-site selectors and content are not controlled evidence

**Problem.** The navigation smoke test depends on exact CSS and link targets on
three independently served pages: `h1.heading`, `#content h3`, an exact IANA
link, and `#example-domains` (`cases/smoke_nav.json:5-23`). The first case even
targets an endpoint named `abtest`, so content variation is part of the site by
design. The download case assumes a specific relative `href` and filename on a
shared public demo (`cases/smoke_download.json:3-10`). A harmless markup change,
redirect, CDN/proxy response, consent page, bot challenge, outage, rate limit, or
removed sample file can turn a library comparison into a site-availability
comparison. Calling a public demo “automation-friendly” is not a reproducibility
guarantee (`cases/README.md:1-2`).

**Recommendation.** Use a pinned, content-addressed fixture application served
from infrastructure controlled by the benchmark, with immutable HTML/JS/assets,
deterministic delays, stable test IDs, and recorded response hashes. Run it in
the same provider region as the browsers. Retain a small, separately reported
live-site canary lane for ecological validity, with rate limits and permission,
but never use live canaries for the headline library delta.

### C3 — **High**: the form-fill lane is both non-portable and stripped of its hardest cloud operations

**Problem.** The default field map was captured from one particular Greenhouse
posting and explicitly warns that its custom selectors are not portable
(`field_map.example.json:1-5`; `README.md:15-21`). In remote mode, file inputs are
skipped by default, and every field marked `network_dependent` is unconditionally
removed (`fill_form.py:303-319`). In the checked-in map that eliminates both file
inputs and all dynamic comboboxes (`field_map.example.json:35-44`,
`field_map.example.json:53-87`, `field_map.example.json:103-141`,
`field_map.example.json:151-185`). What remains is mostly repeated text `fill`
calls plus screenshots. The nominal “form-fill” cloud benchmark therefore does
not cover uploads, autocomplete request/response behavior, option virtualization,
or selection under DOM churn.

**Recommendation.** Replace the external job posting in the measurement lane
with an authorized deterministic fixture reproducing realistic widgets. Include
separate cases for native selects, ARIA comboboxes, debounced remote autocomplete,
virtualized options, keyboard-only selection, multi-select, file chooser and
`set_input_files` at several sizes, and input replacement after upload. Keep the
no-submit guards, but make safe synthetic GET/upload endpoints part of the
fixture so the operation is measured rather than skipped.

### C4 — **High**: cases do not prove that the two backends completed equivalent work

**Problem.** `nav_workload.py` discards every `evaluate` result and records only
the operation name, kind, and duration (`nav_workload.py:97-113`). It does not
assert the destination URL, history state, click side effect, selector count, or
evaluated value. After `back`/`forward`, it performs no state assertion. A backend
can return early, implement weaker auto-wait/actionability semantics, click the
wrong element, or serialize a value incorrectly and still produce `ok: true`.
The download workload treats receipt of any download event as success and merely
records the suggested filename; it does not check the expected name, byte count,
hash, MIME type, or completed file (`download_workload.py:65-71`,
`download_workload.py:99-110`). Performance without a shared correctness oracle
can reward doing less work.

**Recommendation.** Give every case explicit postconditions and compare a
sanitized semantic transcript: URL/history entry, DOM state, event counts and
order, selected values, download metadata and SHA-256, and expected exception
type/message category. Fail the trial before adding its latency to the comparison
if either backend violates the oracle. For APIs whose semantics differ, report
compatibility first and do not compare their speed as if the operations were
equivalent.

### C5 — **High**: the case DSL cannot express most of the API surface that needs benchmarking

**Problem.** Navigation cases support only `goto`, `wait`, `click`, `back`,
`forward`, and unconstrained `eval` (`nav_workload.py:73-101`). They cannot
directly request locator construction/chaining, `count`, text/attribute reads,
`fill`, keyboard or mouse input, select/check operations, hover, drag-and-drop,
screenshots, popups, downloads, dialogs, multiple pages, frame locators, shadow
DOM, handles, request interception, or asynchronous overlap. Using arbitrary
`eval` as an escape hatch measures page JavaScript rather than the public library
API. The form workload adds only three hard-coded field kinds
(`fill_form.py:154-178`, `fill_form.py:189-246`), and the download workload adds
one click-triggered flow (`download_workload.py:21-29`).

**Recommendation.** Define a typed, versioned operation schema covering the
public APIs under claim. Each operation should declare preconditions,
postconditions, timeout policy, expected protocol work, whether it may wait on
page state, and payload size. Reject unknown keys and incompatible backend
options. Keep raw JavaScript cases in a serialization category rather than using
them to stand in for missing operations.

### C6 — **High**: tiny, mostly single-operation cases cannot reveal scaling behavior

**Problem.** Each smoke navigation has only one click and each download has only
one trigger (`cases/smoke_nav.json:6-23`; `cases/smoke_download.json:5-10`). The
same three generic probes then run after every case (`harness/library_probe.py:33-78`),
while connect/setup is charged once per case. That structure is dominated by a
fixed cost and cannot distinguish per-call overhead, amortization, cache effects,
or nonlinear degradation. It also gives no answer for “one API call is fast but
10,000 locator reads leak memory” or “nested frame cost grows with depth.”

**Recommendation.** For each important operation, measure a geometric series
inside one prepared session (for example 1, 10, 100, and 1,000 calls; DOM sizes
10, 1,000, 10,000, and 100,000; payloads 1 byte through 10 MiB). Fit and publish
the fixed intercept and per-operation/per-element slope, retaining raw samples.
Add long-lived churn cases for contexts, pages, locators, handles, downloads, and
event listeners. Do not aggregate unlike operations into a case total.

### C7 — **High**: missing negative and race cases hide both latency tails and compatibility failures

**Problem.** Current smoke selectors are expected to exist and actions are
expected to succeed. There are no intentional strict-mode violations, missing or
detached elements, hidden/disabled/covered controls, animations, stale frames,
navigation races, execution-context destruction, timeouts, cancellation, closed
pages, malformed selectors, large errors, interrupted downloads, or server
disconnects. Broad exception suppression in the probes further erases these
outcomes (`harness/library_probe.py:37-67`). Happy-path medians cannot establish
whether a backend polls too aggressively, overshoots timeouts, leaks resources
after failure, or returns earlier because it skipped required checks.

**Recommendation.** Add deterministic fault cases with bounded expected timing
and error-class oracles. Measure timeout overshoot, polling/CDP command count,
cleanup latency, retained PSS, and recovery on the next operation. Report failure
and semantic-conformance rates as first-class outcomes; only compare latency for
trials with equivalent expected results.

### C8 — **Medium**: the larger checked-in catalog is still a correlated single-site sample

**Problem.** The non-default `nav_the_internet.json` broadens the smoke actions,
but every case still targets the same Heroku demo host
(`cases/nav_the_internet.json:1-378`). Several entries merely evaluate a count or
boolean after one selector wait, and the nominal infinite-scroll case scrolls
once and immediately reads `scrollY` without waiting for or asserting newly
loaded content (`cases/nav_the_internet.json:312-332`). This is not independent
site diversity and is especially vulnerable to one host's load, deployment, or
rate limiting. Repeating correlated cases does not create 100 independent pieces
of evidence.

**Recommendation.** Organize cases by mechanism, not URL count. A single
controlled fixture can intentionally implement multiple DOM engines and network
profiles; each case must have an operation-specific oracle. If live sites are
retained, cap their aggregate weight, diversify ownership and technology only
with permission, and report each domain separately rather than letting one host
dominate a pooled score.

### C9 — **High**: there is no predeclared coverage and weighting plan for the promised 100 cases

**Problem.** The runner groups summaries by case and backend but never produces a
category-balanced comparison or checks required coverage
(`harness/run_suite.py:157-194`). As the catalog grows, adding many cheap cases
from one category can arbitrarily move an overall narrative, and missing or
failed hard cases can disappear from probe summaries. A “100-case” label alone
does not make the sample representative.

**Recommendation.** Freeze a public coverage matrix before collecting results.
One defensible 100-case *deterministic core* would allocate:

| Cases | Category | Required variation |
|---:|---|---|
| 12 | Lifecycle/connect | cold process, warm process, direct WebSocket, context/page create and close |
| 16 | Evaluate/serialization | scalar, handles, nested/cyclic objects, typed arrays, 1 B–10 MiB payloads, exceptions |
| 20 | Locators/actionability | CSS/text/role/test-id, strict 0/1/many, heavy DOM, detach, overlay, animation |
| 16 | Forms/input | fill/type, keyboard events, select/check/radio, debounced and virtualized combobox, uploads |
| 12 | Navigation/events/download | document and SPA navigation, redirects/history, popup, dialog, download and cancellation |
| 10 | Frames/shadow DOM | same-origin, nested cross-origin/OOPIF, detach/reattach, open and closed shadow roots |
| 8 | Multipage/concurrency | parallel evaluates/actions, event bursts, cancellation, backpressure |
| 6 | Memory/churn | repeated context/page/handle/listener lifecycles and post-GC retained PSS |

Weight categories explicitly (equal category weight is a reasonable default),
publish per-operation results before any score, and require every mandatory case
to pass on both backends. Run live-site canaries outside this 100-case core so
site availability cannot change the headline score.

## 4. Rustwright weaknesses the benchmark reveals or should target

### R1 — **Critical**: the observed roughly 3x connect gap is actionable, but the current span cannot identify its cause

**Problem.** The reported approximately 1,336 ms Rustwright versus 449 ms
Playwright result is a roughly 2.98x gap and should be treated as a serious
lead. It is not yet a clean `connect_over_cdp` measurement: the navigation timer
starts before entering `sync_playwright()` and ends after connection, context
creation, and page selection/creation (`nav_workload.py:41-70`). Rustwright's
connect path itself builds a two-worker Tokio runtime, resolves the endpoint,
opens the authenticated WebSocket, and starts service-worker auto-attach
(`../../src/lib.rs:7074-7106`). The benchmark cannot say whether the gap comes
from Python API startup, runtime construction, endpoint resolution, TLS/proxy
handshake, extra protocol initialization, context/page discovery, or simply the
two unmatched cloud routes.

**Recommendation.** Add a `connect_only` workload with monotonic timestamps
around import, `sync_playwright()` entry, endpoint resolution, socket open,
first-CDP-response, auto-attach initialization, `connect_over_cdp` return,
context creation, page discovery, and close. Run cold-process and repeated
warm-process variants against matched pre-created sessions in balanced AB/BA
order. Place a local transparent WebSocket/CDP recorder between each client and
its endpoint to collect sanitized method counts, bytes, in-flight depth, and RTT
without recording headers or payload values. The first optimization target is
the phase and command sequence that explains the gap, not the current composite.

### R2 — **High**: Rustwright may pay a frame-tree round trip before ordinary locator operations

**Problem.** Rustwright's locator evaluation calls `resolve_locator_session`,
which unconditionally attempts `refresh_page_frame_tree` before determining
whether the locator contains a frame (`../../src/lib.rs:2987-3013`,
`../../src/lib.rs:3052-3063`). The refresh sends `Page.getFrameTree` sequentially
for every known session (`../../src/lib.rs:7841-7855`). On a remote browser this
can add at least one full CDP round trip to a simple main-frame locator and more
as OOPIF sessions accumulate. The current `evaluate_rtt_ms` probes never touch a
locator, while `stable_click_ms` combines this cost with actionability, layout,
input dispatch, and the live page (`harness/library_probe.py:37-65`), so it cannot
isolate the suspected tax.

**Recommendation.** Add prepared-page probes for `locator("#id").count()`,
`text_content()`, `is_visible()`, and click on the same static element, repeated
with 0, 1, 10, and 50 unrelated frames/OOPIFs. Record exact CDP methods and plot
latency against known-session count and network RTT. Include cold first access
and warm cached access. This will show whether frame metadata can be event-driven
or refreshed only when a locator actually crosses a frame boundary.

### R3 — **High**: object-valued evaluation can require extra remote serialization work and round trips

**Problem.** Rustwright evaluates with `returnByValue: false`
(`../../src/lib.rs:2856-2875`). When CDP returns an object handle, it invokes a
custom recursive serializer through `Runtime.callFunctionOn`, awaits
`Runtime.releaseObject`, and only then returns to Python
(`../../src/lib.rs:11781-11881`, `../../src/lib.rs:12181-12218`). That path is
materially different from the scalar `evaluate("1")` and
`evaluate("document.title")` probes, which may return inline values. Large,
deep, cyclic, accessor-heavy, typed-array, or DOM-adjacent values can expose
extra RTT, renderer CPU, serialization bytes, client decoding CPU, and temporary
memory that the present probe entirely misses.

**Recommendation.** Add an evaluate matrix returning scalars, flat arrays,
nested/cyclic objects, typed arrays, DOM geometry objects, and strings/objects
from 1 KiB to 10 MiB. Include getters that throw and objects with repeated
references to test correctness. Capture command count, wire bytes, renderer
execution time where available, local CPU, latency, and incremental/retained PSS.
Report size-normalized slopes and semantic hashes, not one aggregate RTT.

### R4 — **High**: heavy-DOM locator work and strict-mode cardinality are untested scaling risks

**Problem.** Rustwright's simple-CSS count fast path scans `querySelectorAll('*')`
to look for any shadow root and then executes the requested selector
(`../../python/rustwright/sync_api.py:20103-20127`). Its actionability loop also
returns the total match count and enforces strictness after evaluating target
state (`../../python/rustwright/sync_api.py:21493-21650`,
`../../python/rustwright/sync_api.py:21688-21739`). These choices may be cheap on
the tiny smoke DOMs but scale with page size or match cardinality. The current
“challenging DOM” candidate merely counts ten table rows via raw JavaScript, so
it does not exercise public locator cost (`cases/nav_the_internet.json:206-220`).

**Recommendation.** Generate deterministic DOMs at 10, 1,000, 10,000, and
100,000 nodes with zero, one, many, and late-position matches; repeat with one
irrelevant open shadow root. Probe CSS, text, role, test-id, filtered, chained,
and strict locators, including a 10,000-match strict violation. Verify equivalent
errors, count CDP calls, and fit latency/PSS against DOM size and matches. This
case should catch full-document scans, repeated selector-engine injection, and
oversized error construction.

### R5 — **High**: frame and OOPIF operations have multi-step cold paths that the suite never exercises

**Problem.** None of the smoke cases enters an iframe or shadow root
(`cases/smoke_nav.json:1-26`; `cases/smoke_download.json:1-12`). Rustwright's
frame evaluation creates an isolated world before evaluating and serializing the
result (`../../src/lib.rs:2901-2923`, `../../src/lib.rs:2952-2968`). OOPIF locator
resolution may inspect owners, wait for an attached session, attach an iframe
target, or fall back to a frame execution context
(`../../src/lib.rs:3116-3202`). These are precisely the event-ordering and
round-trip-heavy paths most likely to differ on remote Chromium, especially on
the first access or after detach/reattach.

**Recommendation.** Build a frame matrix with same-origin iframe, cross-origin
iframe forced into an OOPIF, 1/3/10 nested frames, multiple matching frame
owners, sandboxed frames, navigation inside a frame, and detach/reattach during
an action. Run `count`, evaluate, fill, click, and screenshot through
`frame_locator` and shadow boundaries. Separate first-access setup from warm
operations; record target/session events, CDP methods, errors, and semantic
oracles. Plot latency against depth and OOPIF-session count.

### R6 — **Critical**: actionability and navigation semantics can make a faster result mean less work

**Problem.** Rustwright's click path first polls a substantial target-state
script for attachment, visibility, enabled state, stability, and hit testing
(`../../python/rustwright/sync_api.py:21493-21650`,
`../../python/rustwright/sync_api.py:21688-21739`). Its ordinary safe path then
dispatches a mouse click and returns after post-action handlers/slow motion; that
path shows no explicit navigation wait after dispatch
(`../../python/rustwright/sync_api.py:22040-22098`,
`../../python/rustwright/sync_api.py:22138-22160`). The benchmark labels the
entire click as library time and the next selector wait as page time
(`nav_workload.py:85-103`). If reference Playwright and Rustwright place
actionability, input dispatch, and implicit navigation waiting at different API
boundaries, the suite can move the same website delay between columns and reward
an earlier-returning implementation. Conversely, the injected probe button can
make Rustwright look slow because its legitimate actionability checks are
included while the probe is described as pure library overhead.

**Recommendation.** Establish conformance before timing. Add a no-op static
button fixture with an event-sequence counter; then variants that are offscreen,
covered, disabled, moving, detached/replaced, and enabled after a deterministic
delay. Add a separate anchor case using an explicit expected-navigation/commit
primitive and record dispatch, first navigation event, commit, and final
readiness as distinct timestamps. Require equal event sequences and return
semantics. Compare actionability latency only for equivalent success/failure
behavior, and never mix implicit navigation wait with dispatch cost.

### R7 — **High**: independent suite concurrency does not test one library's multiplexing or backpressure

**Problem.** Suite concurrency launches independent subprocesses and independent
cloud sessions (`harness/run_suite.py:85-121`, `harness/run_suite.py:224-228`).
Every workload imports the synchronous API (`nav_workload.py:10-14`,
`download_workload.py:10-13`). This creates host contention but never tests
multiple pages or simultaneous commands through one client connection. It cannot
reveal head-of-line blocking, command-ID contention, event backlog, cancellation
behavior, fairness between pages, or whether Rustwright's two-worker runtime at
connect becomes a throughput limit (`../../src/lib.rs:7083-7096`).

**Recommendation.** Add a separate async throughput lane using each backend's
async API against one matched browser/session. Run 1, 2, 8, and 32 pages with
simultaneous scalar evaluates, locator reads, downloads, and event bursts;
include cancellation and one deliberately slow page. Measure operations/second,
p50/p95/p99, per-page fairness, maximum in-flight commands, event lag, failures,
CPU, and PSS. Keep this lane separate from single-operation latency so useful
parallelism is not confused with incidental runner contention.

### R8 — **High**: one-shot processes cannot expose retained memory, task, handle, or listener leaks

**Problem.** Each trial creates one process, performs one short case, closes the
context/browser, and exits (`harness/run_suite.py:85-124`). PSS sampling stops
immediately after the workload exits (`harness/measure.py:201-230`). Peak PSS can
show a transient difference, but process exit masks resources retained by the
library across reconnects: Tokio tasks, WebSocket buffers, frame/session maps,
remote handles, callbacks, contexts, pages, or Python wrapper cycles. The current
suite therefore cannot support a claim about long-running automation workers.

**Recommendation.** Add a long-lived lifecycle probe with 100–1,000 iterations
of connect/disconnect, context create/close, page create/close, locator/handle
create/dispose, listener add/remove, failed actions, and download cancellation.
Insert explicit quiescence/GC checkpoints and record phase-aligned PSS, file
descriptors, threads/tasks, callback counts, and remote object/session counts.
Publish retained-memory slope and post-close baseline, not just the maximum.

### R9 — **Medium**: there is no browser-free probe of Python wrapper, validation, and object-construction cost

**Problem.** All three library probes cross the remote connection
(`harness/library_probe.py:37-65`). Even `stable_click` includes renderer and CDP
work. This prevents attribution to Rustwright's Python normalization, JSON
encoding/decoding, PyO3 boundary, sync bridge, locator-object construction, or
error construction—the local pieces the benchmark's “library-only” language
most directly implies. A remote RTT floor can hide a meaningful per-call local
regression when automation performs thousands of small operations.

**Recommendation.** Add verified zero-I/O microprobes: construct and chain
100,000 CSS/text/role/test-id locators; apply `nth`, `filter`, `and_`, and `or_`;
normalize action options and headers; validate malformed arguments; register and
remove callbacks; and create/dispose wrapper objects without sending a command.
Use the CDP recorder to assert zero wire messages. Measure wall time, CPU time,
allocations, and retained PSS in fresh and warmed processes. Report these as
client-library CPU/object costs, separately from remote API latency.

### R10 — **Medium**: remote file-transfer behavior and large binary paths are effectively unbenchmarked

**Problem.** The form workload supports `set_input_files`, but remote mode skips
uploads by default (`fill_form.py:303-319`) and the smoke download checks only
that an event produced a suggested filename (`download_workload.py:65-71`). This
avoids provider-specific failures at the cost of leaving file payload encoding,
transport behavior, temporary memory, completion, cancellation, and cleanup
untested. Rustwright could regress badly on large files or leaked downloads
without affecting any headline number.

**Recommendation.** On a provider configuration with explicitly supported file
transfer, use generated fixtures at 0 B, 1 KiB, 1 MiB, 10 MiB, and the documented
limit. Verify browser-observed name, size, and SHA-256 through an isolated fixture
endpoint; exercise single/multiple files, buffer/path input, replacement,
cancellation, and repeated download cleanup. Record bytes, local peak and
retained PSS, elapsed time, and failures. Keep unsupported-provider runs marked
`not_applicable`, never silently skipped into a passing score.
