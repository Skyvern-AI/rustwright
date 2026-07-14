#!/usr/bin/env python3
"""Sample cgroup-v2 current memory and preserve the kernel peak counter."""

from __future__ import annotations

import argparse
import csv
import signal
import time
from pathlib import Path


stop_requested = False


def request_stop(_signum: int, _frame: object) -> None:
    global stop_requested
    stop_requested = True


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, request_stop)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--peak-output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument(
        "--cgroup-root", type=Path, default=Path("/sys/fs/cgroup")
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.interval <= 0:
        raise ValueError("sample interval must be positive")
    current_path = args.cgroup_root / "memory.current"
    peak_path = args.cgroup_root / "memory.peak"
    if not current_path.is_file() or not peak_path.is_file():
        raise RuntimeError("readable cgroup-v2 memory.current and memory.peak are required")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    next_sample = started
    try:
        with args.output.open("w", newline="", encoding="ascii", buffering=1) as handle:
            writer = csv.writer(handle)
            writer.writerow(("t_rel_s", "bytes"))
            while not stop_requested:
                sampled = time.monotonic()
                current_bytes = int(current_path.read_text(encoding="ascii").strip())
                writer.writerow((f"{sampled - started:.6f}", current_bytes))
                next_sample += args.interval
                remaining = next_sample - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                else:
                    next_sample = time.monotonic()
    finally:
        args.peak_output.write_text(
            peak_path.read_text(encoding="ascii").strip() + "\n",
            encoding="ascii",
        )


if __name__ == "__main__":
    main()
