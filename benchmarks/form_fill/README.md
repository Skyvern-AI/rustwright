# Form-fill benchmark

> **Responsible use is a requirement.** This workload fills a real job
> application with dummy data and never submits it by design. Only target a
> posting you have a legitimate reason and permission to test, respect the
> site's terms of service and rate limits, and never remove or weaken the
> no-submit guardrail.

This demo runs one backend-switchable Python workload through reference
Playwright and Rustwright. It scrolls to each configured field, highlights the
control, enters dummy applicant data, captures a screenshot, validates the
retained value, and writes `timeline.json` plus `timings.json`. The target is
always supplied through the required `BENCH_JOB_URL` environment variable.

The checked-in `field_map.example.json` is a declarative map written for one
specific Greenhouse posting. Its custom question selectors will not be
portable to every posting. Copy and adapt the map for the authorized target
instead of adding a target URL to the workload. Keep submit controls out of the
field list. Controls whose options require live search requests are marked
`network_dependent` and skipped because the actual fill phase runs offline.
Omit that flag only for controls whose options are already available offline.

## Safety guardrail

Before filling anything, the workload finds the configured submit controls,
intercepts both submit events and programmatic form submission before page
scripts run, disables service workers, and installs a browser-context route
that aborts every state-changing HTTP method during startup. Browser-level
WebSocket routing intercepts new sockets without connecting them to a network
peer, while pre-document guards disable page and worker constructors for
WebSocket, WebTransport, EventSource, WebRTC, and dedicated/shared workers. As
soon as the form reaches its ready selector, the
browser context is forced offline for the entire fill and validation phase. It
then disables the visible submit controls.
At the end it verifies that the page did not navigate, no submission was
attempted, all submit controls remain guarded, and no submission-confirmation
text appeared. The workload contains no action that clicks a submit control.
Reports include only the number of network requests blocked by the startup
interlock, never their URLs.

File fixtures are passed to the browser with `set_input_files`. If the site
tries to upload a selected file immediately, the network interlock blocks that
request; the benchmark never waits for or asserts a server-side upload. Some
sites clear their file widget after that blocked request, which is allowed only
for file-field highlight cleanup and is visible in the captured screenshot.

These checks are defense in depth, not permission to target arbitrary sites.

## What is measured

Each Docker run emits two 10 Hz memory series:

- `stack_pss.csv` sums proportional set size (PSS) for the workload Python
  process and its descendant driver/browser processes. This avoids
  double-counting shared pages and is categorized as the benchmark stack.
- `cgroup.csv` and `cgroup_memory_peak_bytes.txt` cover the whole capped
  container, including harness and recording overhead. The kernel peak can
  capture spikes between sampled points.

`epochs.json` aligns sampler, workload, and optional ffmpeg timestamps.
`timeline.json` separates launch, browser/network navigation, scripted pauses,
and actions. Rendered charts shade browser-time and scripted-pause bands so
they are not mistaken for library execution time.

Remote CDP runs only measure the local client/driver container. The remote
browser is outside both local PSS and cgroup scope, so remote memory is not an
end-to-end browser-memory comparison.

## Fairness protocol

For an A/B comparison:

1. Build one repository image and derive the recording image from it.
2. Run the exact same `fill_form.py` and field map for both backends.
3. Keep the container memory, swap, CPU, shared-memory cap, viewport, target,
   pauses, and run order fixed.
4. Set `BENCH_CHROMIUM_EXECUTABLE` to one executable for both backends. The
   Docker harness defaults both to the Rustwright image's Chromium symlink.
5. Run variants sequentially, repeat enough times, and report failures as
   failures rather than dropping them from the sample.
6. Treat local results as diagnostics. Per the repository's
   [`BENCHMARK.md`](../../BENCHMARK.md), durable claims must be reproduced in
   capped, sharded Docker workloads on the Testbox path with provenance.

Recording adds Xvfb and ffmpeg overhead. Compare recorded runs with recorded
runs, and non-recorded runs with non-recorded runs.

## Build the Docker images

From the repository root:

```bash
benchmarks/form_fill/harness/build_images.sh
```

The first build uses the repository's root `Dockerfile` and tags its result as
`rustwright-form-fill-base:latest`. `Dockerfile.record` then uses that image as
its `FROM` base and adds Xvfb plus the plotting dependency. Override the tags
with `FORM_FILL_BASE_IMAGE` and `FORM_FILL_RECORD_IMAGE` if needed.

## Run the capped local Docker comparison

```bash
export BENCH_JOB_URL="https://job-board.example/jobs/authorized-test-target"
benchmarks/form_fill/harness/run_pair.sh
```

Artifacts are written under the ignored `benchmarks/form_fill/out/` directory.
Defaults are an 8 GiB memory/swap cap, 4 CPUs, and 1 GiB shared memory; use
`BENCH_MEMORY_LIMIT`, `BENCH_CPUS`, and `BENCH_SHM_SIZE` to change them for all
variants. Use `BENCH_FIELD_CONFIG_HOST=/path/to/field-map.json` for an adapted
map. `BENCH_PAUSE_SCALE=0` is useful for a quick smoke test, but must be held
constant across comparisons.

To run one variant:

```bash
benchmarks/form_fill/harness/run_one.sh rustwright rustwright-smoke
```

## Record and render

```bash
benchmarks/form_fill/harness/record_one.sh playwright playwright-record
benchmarks/form_fill/harness/record_one.sh rustwright rustwright-record
benchmarks/form_fill/harness/render.sh
```

The renderer writes two synchronized animated PSS videos, a PSS/cgroup
comparison chart, and demo-grade statistics under `out/rendered/`. Videos,
screenshots, CSVs, logs, and generated reports are intentionally ignored.

## Run directly on the host

Create an isolated environment, install this checkout and the reference
Playwright package, install a compatible Chromium, then run:

```bash
python -m venv benchmarks/form_fill/.venv
source benchmarks/form_fill/.venv/bin/activate
python -m pip install -e . "playwright==1.59.0"
python -m playwright install chromium

BACKEND=rustwright \
BENCH_JOB_URL="https://job-board.example/jobs/authorized-test-target" \
python benchmarks/form_fill/fill_form.py

BACKEND=playwright \
BENCH_JOB_URL="https://job-board.example/jobs/authorized-test-target" \
python benchmarks/form_fill/fill_form.py
```

Set `BENCH_CHROMIUM_EXECUTABLE` to the same browser binary for both commands.
Host runs are convenient for development but are not durable benchmark
evidence.

## Connect to a remote browser over CDP

Any provider-neutral Chrome DevTools Protocol WebSocket URL works; the code
sends no provider-specific headers and makes no provider API calls:

```bash
export BENCH_JOB_URL="https://job-board.example/jobs/authorized-test-target"
export CDP_URL="wss://cdp-provider.example/session"
benchmarks/form_fill/harness/run_remote.sh rustwright rustwright-remote
```

For a direct host run, `fill_form_remote.py` is an explicit wrapper around the
same workload and requires both `BENCH_JOB_URL` and `CDP_URL`.

The endpoint must permit creation of a dedicated browser context. The workload
fails closed if it cannot create one; it never reuses, takes offline, or closes
a provider-owned context.

Remote uploads are skipped by default because provider file-transfer behavior
varies. Set `BENCH_SKIP_UPLOADS=0` when the endpoint supports local file upload.
A Skyvern browser session is one public way to obtain a CDP URL, but session
creation and credentials are deliberately outside this benchmark.
