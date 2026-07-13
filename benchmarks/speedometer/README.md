# Speedometer

Speedometer measures web application responsiveness with simulated user
interactions. In this repo it is a browser-engine/config benchmark, not a
Rustwright API-overhead benchmark.

The wrapper delegates to `benchmarks/crossbench/run.py`.

## Setup

```bash
python benchmarks/crossbench/run.py --setup
```

## Run

```bash
python benchmarks/speedometer/run.py --repeat 20 --browser chrome-stable
python benchmarks/speedometer/run.py --repeat 20 --browser /usr/local/bin/rustwright-chromium
```
