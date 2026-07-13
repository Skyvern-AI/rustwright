#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_speed_lib import BROWSER_SPEED_DATA, install_npm_package, npm_bin, run_and_capture


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Browsertime or sitespeed.io from the local benchmark scaffold.")
    parser.add_argument("--setup", action="store_true", help="Install Browsertime and sitespeed.io under .benchmark-data.")
    parser.add_argument("--tool", choices=["browsertime", "sitespeed"], default="browsertime")
    parser.add_argument("--url", action="append", help="URL to test. Repeat for multiple URLs.")
    parser.add_argument("--urls-file", help="Path to newline-delimited URLs.")
    parser.add_argument("--browser", default="chrome")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    if args.setup:
        for package in ("browsertime@latest", "sitespeed.io@latest"):
            rc = install_npm_package(package, dry_run=args.dry_run)
            if rc != 0 or args.dry_run:
                return rc
        return 0

    urls = list(args.url or [])
    if args.urls_file:
        urls.extend(
            line.strip()
            for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not urls:
        urls = ["https://example.com"]

    binary_name = "sitespeed.io" if args.tool == "sitespeed" else "browsertime"
    binary = npm_bin(binary_name)
    if binary.exists():
        command = [str(binary)]
    else:
        package = "sitespeed.io@latest" if args.tool == "sitespeed" else "browsertime@latest"
        command = ["npx", "--yes", package]

    out_dir = BROWSER_SPEED_DATA / "browsertime-output"
    if args.tool == "sitespeed":
        command.extend(
            [
                "--browser",
                args.browser,
                "--browsertime.iterations",
                str(args.iterations),
                "--outputFolder",
                str(out_dir),
            ]
        )
    else:
        command.extend(["--browser", args.browser, "--iterations", str(args.iterations), "--output", str(out_dir)])
    command.extend(urls)

    return run_and_capture(
        command,
        benchmark_id="browsertime",
        name=f"{args.tool}-{args.browser}-{len(urls)}urls-{args.iterations}x",
        dry_run=args.dry_run,
        timeout=args.timeout,
        metadata={"tool": args.tool, "browser": args.browser, "iterations": args.iterations, "urls": urls},
    )


if __name__ == "__main__":
    raise SystemExit(main())
