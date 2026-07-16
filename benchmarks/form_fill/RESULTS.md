# Form-fill benchmark — results

## Cloud CDP benchmark (2026-07-16) — replication-grade

Three green CI runs on Blacksmith 8-vCPU Ubuntu 24.04 runners (run IDs below
are on the private `Skyvern-AI/rustwright-cloud` CI, accessible to org
members; the per-lane summary tables those runs produced are committed
verbatim under
[`results/cloud_cdp_2026-07-16/`](results/cloud_cdp_2026-07-16/) for public
audit). Each rep drives a fresh remote Skyvern cloud browser over CDP
(browser memory off-box): reference Playwright 1.59.0 vs rustwright built
from source — engine and suite at rustwright-cloud commit `e63087b` (its
`main` of 2026-07-16). **182/182 sessions completed, zero failures, 36
cases.** Website time is excluded from the latency metric
(navigation and page waits are timed as separate bands); the live lane shows
both backends wait comparably (0.94×), and since rustwright waited slightly
*less*, the exclusion removes a small rustwright advantage — it cannot flatter
rustwright's latency numbers. Full methodology, per-case tables, and caveats:
[`reports/rustwright_benchmark_report.html`](reports/rustwright_benchmark_report.html).

| Lane (Actions run ID) | Sessions | Client PSS peak, rw ÷ pw | Library latency, rw ÷ pw |
|---|---:|---:|---:|
| controlled ×10, reps=3 (`29465108042`) | 60/60 | 0.25× | 1.80× |
| controlled_more ×9, reps=3 (`29476022036`) | 54/54 | 0.24× | 1.29× |
| live sites ×17, reps=2 (`29476022706`) | 68/68 | 0.27× | 1.79× |

- **Memory: ~4× smaller client footprint** — 28 vs 110 MB median peak PSS,
  smaller in every one of the 36 cases (per-case range 25–69 vs 86–152 MB).
  This confirms and strengthens the demo-era "−71% client memory" figure
  (−75% here, on real Linux PSS).
- **Latency: slower on every case over remote CDP** (1.13–2.58×). Raw CDP
  `evaluate` round-trips are at parity (55 vs 56 ms), so the per-message
  transport itself is not the gap: the deficit concentrates in connection
  setup (2.31×) and object-returning `evaluate` (~2.1–2.6×), over a ~1.2×
  per-operation baseline — consistent with extra round trips and
  serialization work on those paths, whose cost the remote link amplifies.
  This supersedes the single-pair demo "actions −19.9% / −30.8%"
  observations below, which came from an unpinned June-alpha build.
- Optimization targets tracked for follow-up work: connect/attach setup,
  object-eval serialization, and `query_selector_all` handle materialization
  (~270 ms/handle in the smoke-stage weakness lane; not yet re-measured on
  Linux).

Reproduce (dispatch-only workflow; requires the `SKYVERN_CLOUD_API_KEY`
repository secret). One dispatch per lane:

```bash
gh workflow run form-fill-cloud-benchmark.yml \
  -f cases_file=benchmarks/form_fill/cases/controlled.json \
  -f backends="rustwright playwright" -f reps=3 -f concurrency=6
gh workflow run form-fill-cloud-benchmark.yml \
  -f cases_file=benchmarks/form_fill/cases/controlled_more.json \
  -f backends="rustwright playwright" -f reps=3 -f concurrency=6
gh workflow run form-fill-cloud-benchmark.yml \
  -f cases_file=benchmarks/form_fill/cases/nav_the_internet.json \
  -f backends="rustwright playwright" -f reps=2 -f concurrency=6
```

These commands dispatch the current default branch and track current stable
Rust, Python 3.11.x, the runner image, and the remote browser service — they
reproduce the *protocol*, not the recorded environment. Expect drift; the
figures above correspond to the suite/engine commit recorded in this section,
with the raw per-lane tables those runs produced committed under
[`results/cloud_cdp_2026-07-16/`](results/cloud_cdp_2026-07-16/).
"Replication" here means the finding — direction and rough magnitude —
reproduced across three independent runs and two different case mixes, not
that identical conditions can be replayed bit-for-bit.

## Recorded demo results (2026-07-14, June-alpha build)

Demo-grade results from single run pairs of this harness, recorded 2026-07-14.
Per [`BENCHMARK.md`](../../BENCHMARK.md), these are **illustrative demo numbers,
not durable benchmark claims** — durable claims should come from capped,
repeated testbox runs. They are recorded here so that published figures (README
media, demo GIFs) have a citable, archived source; the unpinned June-alpha
build means they are auditable but not experimentally reproducible. Raw data:
[`results/`](results/).

### Protocol

- One Python script, byte-identical for both backends; only the import differs
  (`BACKEND=playwright|rustwright`). See [`fill_form.py`](fill_form.py).
- Workload: a public Greenhouse job-application form (22 fields: text, combobox,
  EEOC dropdowns, resume + cover-letter PDF uploads), filled with dummy data at
  a 400×600 viewport with per-field highlight/pan choreography. Never submitted
  (hard guardrail).
- Same Chromium 1217 binary for both backends. Reference Playwright 1.59.0
  (pinned). Rustwright: a 0.1.0-alpha development build baked into a local
  Docker image (`rustwright-verify`, image ID `9123a56066a7`, built
  2026-06-13; recording derivative `rustwright-bench-record`, image ID
  `dabfd27d62a9`). The exact source commit of that build was not recorded —
  a provenance gap that is part of why these numbers are demo-grade. The
  build predates the Chromium launch-flag alignment and `Locator.fill`
  changes now on `main`; re-running against a current build is the
  recommended way to obtain citable numbers.
- Containers: one per backend, sequential, `--memory=8g --memory-swap=8g
  --cpus=4`, headed under Xvfb with ffmpeg screen capture.
- Memory sampled at 10 Hz: cgroup v2 plus per-process PSS with
  python/driver/chromium attribution (`harness/sample_stack_memory.py`).
- Scripted demo pauses (~11.7 s per run) are identical constants in both runs
  and are excluded from "actions" time.

### Local recorded pair (`results/stats_local_recorded.json`)

| Metric | Playwright | Rustwright | Δ |
|---|---:|---:|---:|
| Wall time | 22.53 s | 18.72 s | −16.9% |
| Actions (library-controlled) | 8.59 s | 5.95 s | −30.8% |
| Browser launch | 1.24 s | 0.30 s | −75.9% |
| Tool-stack peak memory (PSS: python + driver + chromium) | 662.5 MiB | 646.5 MiB | −2.4% |
| Client-library share at stack peak (PSS: python + driver) | 130.0 MiB | 37.8 MiB | −71.0% |
| …of which driver (Node) | 102.3 MiB | 0 | — |

Client-library values are the python + driver components at the stack-peak
sample in `results/stats_local_recorded.json`. Together with the remote
pair below (133.5 vs 40.6 MiB, −69.6%, measured directly), they are the
source of the "~71% less client memory" figure used in demo media. The
full-stack numbers are close because both backends drive the same
Chromium; rustwright's chromium tree measured heavier in this pair due to
launch-flag differences since aligned with Playwright's defaults.

### Remote CDP pair (`results/stats_remote_cdp.json`)

Same workload via `connect_over_cdp` to a fresh remote browser session per run
(WAN), so container memory contains only the client stack. File-upload fields
were skipped in both runs (remote `DOM.setFileInputFiles` requires
browser-host paths); 20 fields filled per run.

| Metric | Playwright | Rustwright | Δ |
|---|---:|---:|---:|
| Wall time | 117.2 s | 96.3 s | −17.8% |
| Actions | 102.4 s | 82.0 s | −19.9% |
| Client memory peak (PSS) | 133.5 MiB | 40.6 MiB | −69.6% |
| Connect | 1.01 s | 1.24 s | +22% |

These are single-pair observations over a WAN and network conditions were
not controlled; they should not be read as a general protocol-efficiency
result. The cloud CDP section above supersedes this pair's latency deltas
with repeated, multi-case measurement over the same class of remote
connection (it does not add network shaping or latency control).

### Reproduce (demo pairs)

```bash
# a Greenhouse-style posting you are authorized to test against:
export BENCH_JOB_URL="https://job-board.example/jobs/authorized-test-target"

# local recorded pair (docker, headed under Xvfb):
benchmarks/form_fill/harness/record_one.sh playwright playwright-record
benchmarks/form_fill/harness/record_one.sh rustwright rustwright-record

# remote pair (also requires CDP_URL per run, see README "Remote mode"):
benchmarks/form_fill/harness/run_remote.sh playwright playwright-remote
benchmarks/form_fill/harness/run_remote.sh rustwright rustwright-remote
```

The recorded figures above came from a Greenhouse posting with 22 fields;
results depend on the chosen posting's field mix (see
[`field_map.example.json`](field_map.example.json)).

See [`README.md`](README.md) for prerequisites and the responsible-use note.
