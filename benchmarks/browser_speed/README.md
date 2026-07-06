# Browser Speed Benchmark Scaffolds

This directory indexes the local benchmark wrappers under `benchmarks/`.

The committed files are intentionally small. Downloaded tools, cloned
third-party repos, browser traces, and benchmark results are written under
ignored `.benchmark-data/`.

## Recommended Use

Primary Rustwright speed evidence should still come from `bench-full`, because
public browser benchmarks mostly measure browser engine/config performance, not
Playwright/Rustwright API overhead.

Use these folders as a second lane:

- `crossbench/`: browser-engine/config comparisons through Speedometer,
  JetStream, and MotionMark.
- `speedometer/`: convenience wrapper for Crossbench Speedometer.
- `jetstream/`: convenience wrapper for Crossbench JetStream.
- `tachometer/`: statistically sampled in-browser microbenchmarks.
- `browsertime/`: page-load and scripted user-journey timings.
- `webpagetest/`: optional WebPageTest API submission scaffolding.
- `telescope/`: optional Cloudflare Telescope runner scaffolding.
- `basemark/`: Basemark Web community-mode notes and guarded URL helper.

## Quick Inventory

```bash
python benchmarks/browser_speed/list.py
```

Most wrappers support `--dry-run` so you can inspect the command before running
networked setup or benchmarks.
