# Benchmark Policy

Authoritative Rustwright benchmark evidence should run through Blacksmith
Testboxes using capped, sharded Docker workloads. Do not use the developer
machine's main Chrome profile, a long-lived host Chrome process, or a purely
local Docker run for durable benchmark claims.

## Default Path

Use a Blacksmith Testbox warmed from
`.github/workflows/benchmark-testbox.yml` instead of running against the host
machine. The helper accepts either the workflow filename or the full
`.github/workflows/...` path; start from the repo root:

```bash
tools/run_benchmark_testbox.sh
```

Before spending time on a full run, verify that GitHub and Blacksmith can both
see the workflow:

```bash
python tools/check_testbox_visibility.py --json
python tools/check_testbox_visibility.py --probe-warmup --json
```

Then run benchmark commands inside the prepared testbox:

```bash
blacksmith testbox run --id <ID> "TEST_DOCKER_MEMORY_LIMIT=8g RUSTWRIGHT_DOCKER_IMAGE=rustwright-verify-testbox BENCHMARK_FULL_ITERATIONS=1 tools/docker_test.sh bench-full --suite strict --lifecycle warm-browser --repetitions 3 --json"
```

The helper can also run a command immediately after warmup by passing `--`.
Use this for the repeatable launch-evidence path once Blacksmith Testbox
warmup and DNS/SSH reachability are working:

```bash
tools/run_benchmark_testbox.sh -- "<command to run inside the warmed testbox>"
```

For example, a strict launch-evidence run should execute the benchmark and then
write both Phase 2 and launch-claim reports from inside the Testbox:

```bash
RUSTWRIGHT_TESTBOX_DOWNLOAD_RESULTS=1 tools/run_benchmark_testbox.sh -- 'set -euo pipefail; mkdir -p .benchmark-data/results .benchmark-data/reports; timestamp="$(date -u +%Y%m%dT%H%M%SZ)"; BENCHMARK_FULL_ITERATIONS=10 TEST_DOCKER_MEMORY_LIMIT=8g RUSTWRIGHT_DOCKER_IMAGE=rustwright-verify-testbox tools/docker_test.sh bench-full --impl rustwright --impl playwright --suite strict --lifecycle warm-browser --repetitions 3 --output ".benchmark-data/results/bench-full-strict-testbox-${timestamp}.json" --json; python tools/check_benchmark_artifacts.py --source testbox --runner blacksmith-testbox --artifact rustwright-testbox-results --run-url "testbox:${HOSTNAME:-unknown}" --enforce-phase2 --enforce-launch --json'
```

If the Dockerfile, Rust sources, Python package metadata, or browser-cache setup
changed after the testbox was warmed, rebuild the image inside the testbox after
the local changes sync:

```bash
blacksmith testbox run --id <ID> "TEST_DOCKER_MEMORY_LIMIT=8g RUSTWRIGHT_DOCKER_IMAGE=rustwright-verify-testbox INSTALL_PUPPETEER=1 tools/docker_test.sh build ."
```

For large task suites such as Mind2Web, prefer the sharded Docker runner so each
shard gets a bounded, clean browser/container lifecycle and the run can resume
from completed shard artifacts:

```bash
blacksmith testbox run --id <ID> "RUSTWRIGHT_DOCKER_IMAGE=rustwright-verify-testbox python tools/run_mind2web_sharded.py --impl all --shard-size 25 --repetitions 1 --json"
```

Blacksmith sync follows git state and does not upload gitignored raw datasets,
manifests, or previous `.benchmark-data` results. For Mind2Web/WebVoyager, run
the download/import step inside the testbox first, or download the needed
ignored artifact into the testbox before starting the sharded benchmark.

The Testbox warmup requires this checkout to have a GitHub `origin` remote with
the workflow file available on the dispatched ref. A purely local worktree
cannot start a Testbox because Blacksmith dispatches GitHub Actions before file
sync begins.

Keep the warmup workflow parser-friendly for Blacksmith: use a plain
`runs-on: blacksmith-...` runner label, run `actions/checkout` before
`useblacksmith/begin-testbox`, and keep `useblacksmith/run-testbox` after the
Docker image setup. PR #6 updates the workflow to that shape. If `gh api` can
read the workflow/ref but `blacksmith testbox warmup ...` still returns a
GitHub contents API 404, treat that as a Blacksmith GitHub-app/repo workflow
fetch authorization issue rather than a local missing-file problem.
`tools/run_benchmark_testbox.sh` reports
`blacksmith_repo_visibility_blocked` and prints diagnostics for this case:
origin, repo, ref, workflow path, job, `BLACKSMITH_ORG`, warmup args,
Blacksmith version, `gh workflow list`, and GitHub workflow content SHA/error.
On the latest 2026-06-13 retry, GitHub could read the workflow on `main` at SHA
`b12abf5d4788a34f8b26c4f3671321dc6e322281`, Blacksmith auth was
`Skyvern-AI`, and Blacksmith CLI `0.4.41` still returned the workflow-fetch
404.
Fresh 2026-07-03 probes reproduced the same pre-dispatch 404 for `main`, PR
#6's `codex/fix-testbox-workflow-order`, and the current pushed branch even
though GitHub's contents API can read the fixed workflow on both branch refs.
`gh api orgs/Skyvern-AI/installations` shows `blacksmith-sh` is installed with
`repository_selection=selected`; the current `gh` token cannot list those
selected repositories without `read:user`, but this symptom is most consistent
with the Blacksmith GitHub App not having access to `Skyvern-AI/rustwright`.
Add the repo to the selected Blacksmith app installation, then rerun
`python tools/check_testbox_visibility.py --probe-warmup --json` before trying
the strict benchmark command.

The tracked GitHub workflow for the Testbox benchmark path is
`.github/workflows/benchmark-testbox.yml`. It warms a clean Testbox and builds
the capped Docker image; it does not by itself define the benchmark matrix.
Run the selected `bench-full`, Mind2Web, WebVoyager, or targeted hotspot command
inside the prepared Testbox with `blacksmith testbox run --id <ID> ...`.
`.github/workflows/benchmark.yml` is the dispatchable PR-optional benchmark
workflow. It supports strict, equivalent, Mind2Web-sharded, and defensible-speed
workloads on a Blacksmith runner, uploads `.benchmark-data/results/`, and writes
strict-suite Phase 2 and launch-claim guardrail reports under
`.benchmark-data/reports/` through `tools/check_benchmark_artifacts.py`.
Its default dispatch runner is `blacksmith-4vcpu-ubuntu-2404`; when that runner
pool is unavailable, `workflow_dispatch` can set `runner_label=ubuntu-latest`
for provenance/debug runs only. Those artifacts are smoke/provenance checks
unless the saved benchmark also passes the Testbox-backed launch-evidence
checker. Local
`tools/docker_test.sh bench-full ...` runs are still useful preflights and
debugging checks, but they should be labeled local diagnostics until reproduced
through the Testbox path.

When a remote Docker host is available, use it for preflights instead of
stressing the local machine:

```bash
RUSTWRIGHT_REMOTE_HOST=<remote-host> RUSTWRIGHT_REMOTE_WORKDIR=<remote-checkout> python tools/run_remote_docker_test.py -- bench-full --suite strict --lifecycle warm-browser --repetitions 3 --json
```

Remote Docker runs are still preflight evidence for benchmark claims unless the
result is later reproduced through the Testbox path.

Some Docker Desktop hosts can hang in `docker-credential-desktop` when using
the default Docker config. Use an isolated config for remote Docker preflights
and builds:

```bash
tailscale ssh <user>@<remote-host> 'mkdir -p /tmp/rustwright-docker-config && printf "{}" > /tmp/rustwright-docker-config/config.json'
DOCKER_CONFIG=/tmp/rustwright-docker-config python tools/run_remote_docker_test.py --host <user>@<remote-host> --transport tailscale-ssh --check-only --remote-docker-check --memory-limit 7g --remote-pull-check python:3.13-slim-bookworm --remote-pull-timeout 120 --json
```

If that host still lacks a working BuildKit/buildx component, set
`RUSTWRIGHT_DOCKER_LEGACY=1 DOCKER_BUILDKIT=0` for the remote image build. The
normal Dockerfile path keeps BuildKit cache mounts for regular build velocity.

The benchmark model should be:

- one capped Docker container per implementation, repetition, and shard;
- sequential execution unless a benchmark explicitly measures contention;
- the same selected tasks/cases for every implementation;
- recorded Docker image id, memory/swap cap, CPU quota, browser executable, and
  browser version;
- failed benchmark runs reported as failures, not timing samples.

Before using a benchmark in launch-facing latency copy or progress claims, run:

```bash
python tools/check_benchmark_artifacts.py --source testbox --runner blacksmith-testbox --artifact <artifact-name> --run-url <run-url> --enforce-phase2 --enforce-launch --json
python tools/check_launch_latency_claim.py
python tools/check_launch_latency_claim.py --benchmark-json .benchmark-data/results/<testbox-bench-full-strict>.json --source testbox --artifact <artifact-name> --run-url <run-url>
python tools/render_project_tables.py --table launch
```

The checker is intentionally stricter than a benchmark smoke. It rejects
one-case provenance runs, local diagnostics, missing p25/median/p75 metrics,
and non-Testbox evidence even when the run is useful for CI or debugging.

## Local Diagnostic: Trusted Input Default

On 2026-07-03, after disabling untrusted DOM action fast paths by default, a
local non-Docker warm-browser `equivalent` suite diagnostic ran 5 iterations
against this worktree's release build and the real Python Playwright reference
at
`<reference-playwright-checkout>/.audit-playwright`.
This is not launch evidence; the host had unrelated browser and agent load.

| Implementation | Total mean | Cases | Artifact |
| --- | ---: | ---: | --- |
| Rustwright default trusted input | 5256.44 ms | 17 | `.benchmark-data/results/local-trusted-input-equivalent-rustwright-default-5.json` |
| Python Playwright reference | 13418.39 ms | 17 | `.benchmark-data/results/local-trusted-input-equivalent-playwright-5.json` |

Local diagnostic delta: Rustwright was 60.83% lower total mean and won 16/17
case means. The `click_button` case measured 89.61 ms for Rustwright versus
662.44 ms for Python Playwright after the fix.

The attempted local `strict` 3-iteration comparison failed before producing
timing because the current Python Playwright reference returned a timeout call
log for `check_and_uncheck` where the existing benchmark assertion expected an
`Element is not visible` message. The attempted
`RUSTWRIGHT_UNSAFE_DOM_FASTPATH=1` old-behavior proxy also failed setup on
three local runs at `Browser.new_page: timed out after 5000 ms`, so no honest
local before-fastpath timing sample is recorded here. Treat previous DOM
fast-path benchmark claims as tainted until reproduced with trusted default
input in a capped Testbox/Docker run.

## Avoid Host/Main Chrome

Do not use the host's main Chrome, an existing user profile, or an already-open
browser as benchmark evidence. Those runs inherit cache state, extensions,
profile data, background tabs, browser update timing, and local machine load.
They are useful only for debugging wrapper behavior or quick smoke checks.

If a browser-engine benchmark wrapper needs a host Chrome executable for
exploration, label the result as local diagnostic evidence. Reproduce any
performance claim through the Testbox capped Docker/sharded path before
presenting it as release-facing benchmark evidence.
