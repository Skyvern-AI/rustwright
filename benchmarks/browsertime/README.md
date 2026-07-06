# Browsertime / sitespeed.io

Browsertime is for page-load and scripted user-journey timing. sitespeed.io
wraps Browsertime and adds reporting, HAR/video, and metric export.

This is a better fit than WebVoyager for repeatable page-load speed because
the runner is built around browser performance metrics and repetitions.

## Setup

```bash
python benchmarks/browsertime/run.py --setup
```

This installs `browsertime` and `sitespeed.io` under ignored
`.benchmark-data/browser-speed/npm`.

## Run

```bash
python benchmarks/browsertime/run.py --url https://example.com --iterations 5
python benchmarks/browsertime/run.py --tool sitespeed --url https://example.com --iterations 5
```

Use `--dry-run` to inspect the generated command.
