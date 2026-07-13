#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MotionMark through the local Crossbench wrapper.")
    parser.add_argument("--browser", default="chrome-stable")
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--crossbench-arg", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    command = [
        sys.executable,
        str(ROOT / "benchmarks" / "crossbench" / "run.py"),
        "--benchmark",
        "motionmark",
        "--browser",
        args.browser,
        "--repeat",
        str(args.repeat),
    ]
    if args.dry_run:
        command.append("--dry-run")
    for item in args.crossbench_arg:
        command.append(f"--crossbench-arg={item}")
    if args.timeout is not None:
        command.extend(["--timeout", str(args.timeout)])
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
