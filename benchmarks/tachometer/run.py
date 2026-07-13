#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_speed_lib import install_npm_package, npm_bin, run_and_capture


HERE = Path(__file__).resolve().parent
PACKAGE = "tachometer@latest"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Tachometer browser microbenchmarks.")
    parser.add_argument("--setup", action="store_true", help="Install Tachometer under .benchmark-data/browser-speed/npm.")
    parser.add_argument("--browser", default="chrome-headless", help="Tachometer browser selector.")
    parser.add_argument("--case", action="append", help="HTML benchmark case path. Defaults to all cases/*.html.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    if args.setup:
        rc = install_npm_package(PACKAGE, dry_run=args.dry_run)
        return rc

    binary = npm_bin("tachometer")
    command = [str(binary) if binary.exists() else "npx", "--yes", PACKAGE]
    if binary.exists():
        command = [str(binary)]

    cases = [Path(item) for item in args.case] if args.case else sorted((HERE / "cases").glob("*.html"))
    if not cases:
        raise SystemExit("No Tachometer cases found.")
    command.extend(str(case) for case in cases)
    command.append(f"--browser={args.browser}")

    return run_and_capture(
        command,
        benchmark_id="tachometer",
        name=f"{args.browser}-{len(cases)}cases",
        dry_run=args.dry_run,
        timeout=args.timeout,
        metadata={"browser": args.browser, "cases": [str(case) for case in cases]},
    )


if __name__ == "__main__":
    raise SystemExit(main())
