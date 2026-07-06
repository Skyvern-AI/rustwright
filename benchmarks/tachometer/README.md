# Tachometer

Tachometer is useful for statistically sampled in-browser microbenchmarks. It
round-robins cases and takes enough samples to produce confidence intervals.

This measures browser-side DOM/JS/runtime behavior. It does not measure the
Rustwright API or CDP protocol overhead directly.

## Setup

```bash
python benchmarks/tachometer/run.py --setup
```

This installs Tachometer under ignored `.benchmark-data/browser-speed/npm`.

## Run

```bash
python benchmarks/tachometer/run.py --browser chrome-headless
```

Use `--dry-run` to inspect the generated command.
