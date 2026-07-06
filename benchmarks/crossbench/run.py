#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_speed_lib import BROWSER_SPEED_DATA, EXTERNAL_DATA, ensure_git_clone, require_file, run_and_capture


CROSSBENCH_URL = "https://chromium.googlesource.com/crossbench"
CROSSBENCH_DIR = EXTERNAL_DATA / "crossbench"
CROSSBENCH_VENV = BROWSER_SPEED_DATA / "crossbench-venv"


def crossbench_python() -> str:
    venv_python = CROSSBENCH_VENV / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return shutil.which("python3.12") or shutil.which("python3.11") or sys.executable


def setup_crossbench(*, dry_run: bool) -> int:
    rc = ensure_git_clone(CROSSBENCH_URL, CROSSBENCH_DIR, dry_run=dry_run)
    if rc != 0 or dry_run:
        return rc
    if not CROSSBENCH_VENV.exists():
        python_bin = shutil.which("python3.12") or shutil.which("python3.11")
        if not python_bin:
            raise SystemExit("Crossbench setup needs python3.12 or python3.11 on PATH.")
        rc = run_and_capture(
            [python_bin, "-m", "venv", str(CROSSBENCH_VENV)],
            benchmark_id="setup",
            name="crossbench-venv",
            dry_run=dry_run,
            metadata={"venv": str(CROSSBENCH_VENV)},
        )
        if rc != 0:
            return rc
    return run_and_capture(
        [str(CROSSBENCH_VENV / "bin" / "python"), "-m", "pip", "install", "-e", str(CROSSBENCH_DIR)],
        benchmark_id="setup",
        name="crossbench-install",
        dry_run=dry_run,
        metadata={"source": str(CROSSBENCH_DIR), "venv": str(CROSSBENCH_VENV)},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Chromium Crossbench from the local benchmark scaffold.")
    parser.add_argument("--setup", action="store_true", help="Clone Crossbench into ignored .benchmark-data/external/.")
    parser.add_argument(
        "--benchmark",
        choices=["speedometer", "jetstream", "motionmark"],
        default="speedometer",
        help="Crossbench benchmark to run.",
    )
    parser.add_argument("--browser", default="chrome-stable", help="Crossbench browser label or browser binary path.")
    parser.add_argument("--repeat", type=int, default=20, help="Crossbench repeat count.")
    parser.add_argument("--story", action="append", help="Optional Crossbench story filter. Repeat for multiple stories.")
    parser.add_argument("--probe", action="append", help="Optional Crossbench probe. Repeat for multiple probes.")
    parser.add_argument(
        "--crossbench-arg",
        action="append",
        default=[],
        help="Raw argument forwarded to Crossbench, e.g. --crossbench-arg=--env-validation=warn.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--timeout", type=int, default=None, help="Optional wrapper timeout in seconds.")
    args = parser.parse_args()

    if args.setup:
        return setup_crossbench(dry_run=args.dry_run)

    cb = CROSSBENCH_DIR / "cb.py"
    if not args.dry_run:
        require_file(cb, "Run `python benchmarks/crossbench/run.py --setup` first.")

    command = [
        crossbench_python(),
        str(cb),
        "motionmark1.3.1" if args.benchmark == "motionmark" else args.benchmark,
        f"--repeat={args.repeat}",
        f"--browser={args.browser}",
    ]
    for story in args.story or []:
        command.append(f"--story={story}")
    for probe in args.probe or []:
        command.append(f"--probe={probe}")
    command.extend(args.crossbench_arg)

    return run_and_capture(
        command,
        benchmark_id="crossbench",
        name=f"{args.benchmark}-{args.repeat}x",
        cwd=CROSSBENCH_DIR,
        dry_run=args.dry_run,
        timeout=args.timeout,
        metadata={
            "benchmark": args.benchmark,
            "browser": args.browser,
            "repeat": args.repeat,
            "story": args.story or [],
            "probe": args.probe or [],
            "crossbench_arg": args.crossbench_arg,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
