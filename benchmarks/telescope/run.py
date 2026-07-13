#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_speed_lib import EXTERNAL_DATA, ensure_git_clone, require_file, run_and_capture


TELESCOPE_URL = "https://github.com/cloudflare/telescope.git"
TELESCOPE_DIR = EXTERNAL_DATA / "telescope"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Cloudflare Telescope from the local benchmark scaffold.")
    parser.add_argument("--setup", action="store_true", help="Clone Telescope and run npm install.")
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--browser", default="chrome")
    parser.add_argument("--width", type=int, default=1365)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--timeout-ms", type=int, default=50000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    if args.setup:
        rc = ensure_git_clone(TELESCOPE_URL, TELESCOPE_DIR, dry_run=args.dry_run)
        if rc != 0 or args.dry_run:
            return rc
        rc = run_and_capture(
            ["npm", "install"],
            benchmark_id="setup",
            name="npm-telescope",
            cwd=TELESCOPE_DIR,
            dry_run=args.dry_run,
            metadata={"destination": str(TELESCOPE_DIR)},
        )
        if rc != 0:
            return rc

    if not args.dry_run:
        require_file(TELESCOPE_DIR / "package.json", "Run `python benchmarks/telescope/run.py --setup` first.")
    command = [
        "npx",
        ".",
        "-u",
        args.url,
        "-b",
        args.browser,
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--timeout",
        str(args.timeout_ms),
    ]
    return run_and_capture(
        command,
        benchmark_id="telescope",
        name=f"{args.browser}",
        cwd=TELESCOPE_DIR,
        dry_run=args.dry_run,
        timeout=args.timeout,
        metadata={
            "url": args.url,
            "browser": args.browser,
            "width": args.width,
            "height": args.height,
            "timeout_ms": args.timeout_ms,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
