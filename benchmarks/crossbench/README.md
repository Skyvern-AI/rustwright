# Crossbench

Crossbench is the preferred local runner for browser-engine/config speed
checks. It can run Speedometer, JetStream, MotionMark, and custom page flows
against specific browser binaries.

This is not an automation-library overhead benchmark. Use it to answer:

- Did our Chromium binary or launch flags make the browser itself slower?
- Does Rustwright's selected browser build behave differently from a reference
  Chrome/Chromium build on standard web benchmarks?
- Are browser-level regressions separate from Python/TypeScript wrapper costs?

## Setup

```bash
python benchmarks/crossbench/run.py --setup
```

This clones Crossbench into ignored `.benchmark-data/external/crossbench`.

## Examples

```bash
python benchmarks/crossbench/run.py --benchmark speedometer --repeat 20 --browser chrome-stable
python benchmarks/crossbench/run.py --benchmark jetstream --repeat 20 --browser /usr/local/bin/rustwright-chromium
python benchmarks/crossbench/run.py --benchmark motionmark --repeat 10 --browser chrome-stable
```

Use `--dry-run` to print the command without executing it.
