# JetStream

JetStream measures JavaScript and WebAssembly compute-heavy workloads. It is
useful for detecting browser-engine/config regressions, but it does not measure
Rustwright protocol or Python/TypeScript wrapper overhead.

The wrapper delegates to `benchmarks/crossbench/run.py`.

## Setup

```bash
python benchmarks/crossbench/run.py --setup
```

## Run

```bash
python benchmarks/jetstream/run.py --repeat 20 --browser chrome-stable
python benchmarks/jetstream/run.py --repeat 20 --browser /usr/local/bin/rustwright-chromium
```
