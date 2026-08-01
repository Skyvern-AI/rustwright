#!/usr/bin/env python3
"""Measure wall time and peak summed RSS for a command and its descendants."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable


SAMPLE_INTERVAL_SECONDS = 0.1


def process_tree_rss_from_proc(root_pid: int) -> int:
    """Return summed VmRSS for root_pid and descendants from one /proc scan."""

    children: dict[int, list[int]] = {}
    rss_by_pid: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            # comm is parenthesized and may itself contain spaces or ')'. The
            # fields after the final ')' begin with state, then ppid.
            fields = stat[stat.rfind(")") + 2 :].split()
            ppid = int(fields[1])
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(pid)
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    rss_by_pid[pid] = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
                break
    return sum(rss_by_pid.get(pid, 0) for pid in descendants(root_pid, children))


class DarwinTaskInfo(ctypes.Structure):
    """Layout of macOS proc_taskinfo for local no-browser verification."""

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("context_switches", ctypes.c_int32),
        ("thread_count", ctypes.c_int32),
        ("running_thread_count", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


def process_tree_rss_from_darwin(root_pid: int) -> int:
    """Use libproc on macOS; release CI always takes the Linux /proc path."""

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return 0
    child_buffer = (ctypes.c_int32 * 4096)()
    stack = [root_pid]
    seen: set[int] = set()
    total_bytes = 0
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = DarwinTaskInfo()
        size = ctypes.sizeof(info)
        if libproc.proc_pidinfo(pid, 4, 0, ctypes.byref(info), size) == size:
            total_bytes += info.resident_size
        child_count = libproc.proc_listchildpids(
            pid, child_buffer, ctypes.sizeof(child_buffer)
        )
        if child_count > 0:
            stack.extend(child_buffer[: min(child_count, len(child_buffer))])
    return total_bytes // 1024


def descendants(root_pid: int, children: dict[int, list[int]]) -> Iterable[int]:
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        yield pid
        stack.extend(children.get(pid, ()))


def process_tree_rss_kb(root_pid: int) -> int:
    if Path("/proc/self/stat").is_file():
        return process_tree_rss_from_proc(root_pid)
    if sys.platform == "darwin":
        return process_tree_rss_from_darwin(root_pid)
    return 0


def write_measurement(
    output_path: Path,
    *,
    wall_seconds: float,
    peak_tree_rss_kb: int,
    exit_code: int,
    samples: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "wall_seconds": wall_seconds,
                "peak_tree_rss_kb": peak_tree_rss_kb,
                "exit_code": exit_code,
                "samples": samples,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def run(command: list[str], output_path: Path) -> int:
    started = time.perf_counter()
    try:
        process = subprocess.Popen(command)
    except OSError as error:
        elapsed = time.perf_counter() - started
        write_measurement(
            output_path,
            wall_seconds=elapsed,
            peak_tree_rss_kb=0,
            exit_code=127,
            samples=0,
        )
        print(f"measure_process_tree: {error}", file=sys.stderr)
        return 127

    rss_samples: list[int] = []
    stop_sampling = threading.Event()

    def sample_until_stopped() -> None:
        while True:
            try:
                rss_samples.append(process_tree_rss_kb(process.pid))
            except (OSError, RuntimeError, ValueError):
                rss_samples.append(0)
            if stop_sampling.wait(SAMPLE_INTERVAL_SECONDS):
                break

    sampler = threading.Thread(target=sample_until_stopped, daemon=True)
    sampler.start()
    exit_code = process.wait()
    wall_seconds = time.perf_counter() - started
    stop_sampling.set()
    sampler.join()
    peak_tree_rss_kb = max(rss_samples, default=0)
    samples = len(rss_samples)
    write_measurement(
        output_path,
        wall_seconds=wall_seconds,
        peak_tree_rss_kb=peak_tree_rss_kb,
        exit_code=exit_code,
        samples=samples,
    )
    return exit_code if exit_code >= 0 else 128 + abs(exit_code)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="measurement JSON path")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return run(args.command, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
