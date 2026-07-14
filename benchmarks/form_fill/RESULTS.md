# Form-fill benchmark — recorded demo results

Demo-grade results from single run pairs of this harness, recorded 2026-07-14.
Per [`BENCHMARK.md`](../../BENCHMARK.md), these are **illustrative demo numbers,
not durable benchmark claims** — durable claims should come from capped,
repeated testbox runs. They are recorded here so that published figures (README
media, demo GIFs) have a citable, reproducible source. Raw data:
[`results/`](results/).

## Protocol

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

## Local recorded pair (`results/stats_local_recorded.json`)

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

## Remote CDP pair (`results/stats_remote_cdp.json`)

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
result. Repeated runs under controlled latency would be needed to
establish that.

## Reproduce

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
