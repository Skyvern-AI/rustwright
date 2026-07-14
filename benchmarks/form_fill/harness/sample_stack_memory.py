#!/usr/bin/env python3
"""Sample PSS for a workload process and its descendant driver/browser stack."""

from __future__ import annotations

import argparse
import csv
import signal
import time
from dataclasses import dataclass
from pathlib import Path


stop_requested = False


def request_stop(_signum: int, _frame: object) -> None:
    global stop_requested
    stop_requested = True


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, request_stop)


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    name: str
    argv: tuple[str, ...]


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


def read_process(pid_dir: Path) -> Process | None:
    status_text = read_text(pid_dir / "status")
    if status_text is None:
        return None
    status = {}
    for line in status_text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    try:
        pid = int(pid_dir.name)
        ppid = int(status["PPid"])
    except (KeyError, ValueError):
        return None
    argv = tuple(
        part for part in (read_text(pid_dir / "cmdline") or "").split("\0") if part
    )
    return Process(pid=pid, ppid=ppid, name=status.get("Name", ""), argv=argv)


def read_pss_bytes(pid: int) -> int | None:
    rollup = read_text(Path("/proc") / str(pid) / "smaps_rollup")
    if rollup is None:
        return None
    for line in rollup.splitlines():
        if line.startswith("Pss:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def descendants(processes: dict[int, Process], root_pid: int) -> set[int]:
    selected = {root_pid} if root_pid in processes else set()
    changed = True
    while changed:
        changed = False
        for process in processes.values():
            if process.pid not in selected and process.ppid in selected:
                selected.add(process.pid)
                changed = True
    return selected


def direct_category(process: Process, root_pid: int) -> str | None:
    argv = " ".join(process.argv).lower()
    executable = Path(process.argv[0]).name.lower() if process.argv else ""
    token = " ".join((process.name.lower(), executable, argv))
    if process.pid == root_pid or executable.startswith("python"):
        return "python"
    if executable == "node" or executable.startswith("node-"):
        return "driver"
    if any(marker in token for marker in ("chrome", "chromium", "headless_shell")):
        return "browser"
    return None


def inherited_category(
    pid: int,
    processes: dict[int, Process],
    selected: set[int],
    categories: dict[int, str],
) -> str:
    current = processes[pid]
    seen = {pid}
    while current.ppid in selected and current.ppid not in seen:
        parent_pid = current.ppid
        if parent_pid in categories:
            return categories[parent_pid]
        seen.add(parent_pid)
        current = processes[parent_pid]
    return "python"


def scan(root_pid: int) -> tuple[int, int, int, int] | None:
    processes = {}
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        process = read_process(pid_dir)
        if process is not None:
            processes[process.pid] = process
    if root_pid not in processes:
        return None
    selected = descendants(processes, root_pid)
    categories = {
        pid: category
        for pid in selected
        if (category := direct_category(processes[pid], root_pid)) is not None
    }
    totals = {"python": 0, "driver": 0, "browser": 0}
    for pid in sorted(selected):
        pss_bytes = read_pss_bytes(pid)
        if pss_bytes is None:
            continue
        category = categories.get(pid) or inherited_category(
            pid, processes, selected, categories
        )
        categories[pid] = category
        totals[category] += pss_bytes
    return (
        sum(totals.values()),
        totals["python"],
        totals["driver"],
        totals["browser"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.root_pid <= 0 or args.interval <= 0:
        raise ValueError("root PID and interval must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    next_sample = started
    with args.output.open("w", newline="", encoding="ascii", buffering=1) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("t_rel_s", "total_bytes", "python_bytes", "driver_bytes", "browser_bytes")
        )
        while not stop_requested:
            sampled = time.monotonic()
            values = scan(args.root_pid)
            if values is None:
                break
            writer.writerow((f"{sampled - started:.6f}", *values))
            next_sample += args.interval
            remaining = next_sample - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            else:
                next_sample = time.monotonic()


if __name__ == "__main__":
    main()
