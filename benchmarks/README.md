# Benchmarks

See [BENCHMARK.md](../BENCHMARK.md) for the benchmark policy. In short:
authoritative Rustwright benchmark evidence should use capped, sharded Docker
runs instead of the developer machine's main Chrome or a long-lived host Chrome
process.

This directory contains two benchmark lanes:

- `run_benchmarks.py` and `automation_cases.py`: Rustwright API/equivalent
  automation speed and parity cases. This is the primary speed lane for
  Rustwright-vs-Playwright claims.
- Browser speed candidate folders: browser-engine, page-load, and external
  benchmark scaffolds used as supporting evidence.

## Browser Speed Candidates

List the available browser-speed scaffolds:

```bash
python benchmarks/browser_speed/list.py
```

The browser-speed folders are intentionally lightweight. Setup commands place
downloaded tools, cloned repos, and outputs under ignored `.benchmark-data/`.

Useful starting points:

```bash
python benchmarks/crossbench/run.py --setup
python benchmarks/speedometer/run.py --repeat 20 --browser chrome-stable --dry-run
python benchmarks/tachometer/run.py --setup
python benchmarks/browsertime/run.py --setup
```

Use `bench-full` first for automation-library speed. Use these browser-speed
candidates to check browser-engine/config, page-load, and external synthetic
performance signals.
