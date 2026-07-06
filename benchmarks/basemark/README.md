# Basemark Web

Basemark Web is a browser/system benchmark covering JavaScript, DOM/CSS, and
graphics-heavy workloads.

This folder intentionally does not auto-run the benchmark. Basemark's community
mode has usage terms, so the wrapper requires an explicit environment opt-in.

## Print Community URL

```bash
python benchmarks/basemark/run.py
```

## Guarded Launch Helper

```bash
BASEMARK_ALLOW_COMMUNITY_MODE=1 python benchmarks/basemark/run.py --print-command
```

Use Basemark as supporting browser-engine/system evidence only. It does not
measure Rustwright API overhead.
