#!/usr/bin/env python3
"""Run weakness probes against one already-provisioned remote browser session."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from fill_form import make_remote_context, parse_cdp_connect_headers
from harness.weakness_probe import run_weakness_probes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("playwright", "rustwright"), required=True)
    parser.add_argument("--reps", type=int, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.reps < 1:
        parser.error("--reps must be positive")
    return args


def main() -> None:
    args = parse_args()
    cdp_url = os.environ.get("CDP_URL")
    if not cdp_url:
        raise RuntimeError("CDP_URL is required; weakness_workload.py is remote-only")

    if args.backend == "rustwright":
        from rustwright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        options: dict[str, object] = {"timeout": 120000}
        headers = parse_cdp_connect_headers(os.environ.get("CDP_CONNECT_HEADERS", ""))
        if headers:
            options["headers"] = headers
        browser = playwright.chromium.connect_over_cdp(cdp_url, **options)
        context = make_remote_context(browser)
        try:
            page = context.new_page()
            page.goto("data:text/html,<body></body>", wait_until="commit")
            results = run_weakness_probes(page, context, reps=args.reps)
        finally:
            context.close()
            browser.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
