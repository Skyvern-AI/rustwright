# Cloudflare Telescope

Telescope is a cross-browser performance testing agent that can collect timing
metrics, HAR files, screenshots, and videos.

Use it as an exploratory page-performance candidate. It is newer than
Browsertime/sitespeed, so keep it supporting evidence until we have stable local
experience with it.

## Setup

```bash
python benchmarks/telescope/run.py --setup
```

This clones Telescope into ignored `.benchmark-data/external/telescope` and
runs `npm install` in that clone.

## Run

```bash
python benchmarks/telescope/run.py --url https://example.com --browser chrome
```
