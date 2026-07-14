# Speed and memory benchmark

`bench-full` records timing and peak resident memory in the same JSON result for
every implementation run. Memory collection is always on; no additional flag is
required.

Each passed item in `results` contains a `memory` block with:

- `rss_self_kb`: peak RSS of the benchmark's Python process. For Rustwright,
  this includes the in-process Rust client. For `playwright-python`, it includes
  the Python client but not its Node driver.
- `rss_tree_kb`: peak summed RSS of that Python process and all descendants,
  including the driver and Chromium process tree.
- sampling provenance, interval, availability, and sample count.

The sampler uses the same method as `benchmarks/async_concurrency_load.py`: it
walks parent/child PIDs with `ps`, reads RSS in KiB, samples every 50 ms, and
retains the independent peak for the root and whole tree. It takes one baseline
before entering the implementation run, then samples on a daemon background
thread; case timers continue to wrap only the operation under test, so `ps`
calls are not executed on the latency hot path. If `ps`, the tree walk, or an
individual RSS read is unavailable, the corresponding value is `null` and the
benchmark continues.

Whole-tree RSS is normally Chromium-dominated. Report `rss_tree_kb` for the
actual end-to-end process cost and `rss_self_kb` alongside it as the available
library-host portion. The latter is not perfectly symmetric:
`playwright-python` puts additional client logic in its Node child, while
Rustwright keeps its client in the measured Python process. The harness does not
attempt to classify driver children separately from Chromium, because command
names and process layouts vary by browser build and platform.

For repeated runs, the raw peak remains in every `results` item and `aggregate`
contains distribution summaries for both RSS fields. Benchmark output belongs
under the ignored `.benchmark-data/` directory; do not commit raw result JSON,
terminal logs, or generated reports.

## Exact Testbox dispatch

From the repository root, this command warms a Blacksmith Testbox, builds the
benchmark image, runs only Rustwright and `playwright-python`, downloads the
ignored JSON artifact, and records strict-suite speed plus memory together:

```bash
RUSTWRIGHT_TESTBOX_DOWNLOAD_RESULTS=1 tools/run_benchmark_testbox.sh -- 'set -euo pipefail; mkdir -p .benchmark-data/results; timestamp="$(date -u +%Y%m%dT%H%M%SZ)"; BENCHMARK_FULL_ITERATIONS=10 TEST_DOCKER_MEMORY_LIMIT=8g RUSTWRIGHT_DOCKER_IMAGE=rustwright-verify-testbox tools/docker_test.sh bench-full --impl rustwright --impl playwright --suite strict --lifecycle warm-browser --repetitions 3 --output ".benchmark-data/results/bench-full-strict-speed-memory-${timestamp}.json" --json'
```

For a local Docker preflight using an already-built image, run:

```bash
BENCHMARK_FULL_ITERATIONS=10 TEST_DOCKER_MEMORY_LIMIT=8g tools/docker_test.sh bench-full --impl rustwright --impl playwright --suite strict --lifecycle warm-browser --repetitions 3 --output .benchmark-data/results/bench-full-strict-speed-memory.json --json
```
