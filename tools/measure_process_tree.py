#!/usr/bin/env python3
"""Measure wall time and peak full/client RSS for a command and descendants."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


SAMPLE_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class ProcessTreeSample:
    tree_rss_kb: int
    client_rss_kb: int
    excluded_process_count: int
    unresolved_records: int


def read_process_stat(entry: Path) -> tuple[int, int, int]:
    """Return pid, parent pid, and starttime from one /proc stat read."""

    stat = (entry / "stat").read_text(encoding="utf-8")
    closing_paren = stat.rfind(")")
    opening_paren = stat.find("(")
    if opening_paren < 1 or closing_paren < opening_paren:
        raise ValueError("malformed process stat")
    pid = int(stat[:opening_paren].strip())
    # comm is parenthesized and may itself contain spaces or ')'. The fields
    # after the final ')' begin with state, then ppid.
    fields = stat[closing_paren + 2 :].split()
    return pid, int(fields[1]), int(fields[19])


def read_process_rss_kb(entry: Path) -> int:
    """Return VmRSS from one /proc status read."""

    status = (entry / "status").read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise ValueError("process status has no VmRSS")


def classify_process_from_proc(
    entry: Path,
    excluded_realpaths: frozenset[str],
    excluded_names: frozenset[str],
) -> bool | None:
    """Return excluded/client classification, or None when it cannot be resolved."""

    try:
        executable = os.readlink(entry / "exe")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    else:
        return os.path.realpath(executable) in excluded_realpaths

    candidates: list[str] = []
    try:
        comm = (entry / "comm").read_text(encoding="utf-8").strip()
        if comm:
            candidates.append(comm)
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        pass
    try:
        cmdline = (entry / "cmdline").read_bytes().split(b"\0", 1)[0]
        if cmdline:
            candidates.append(os.fsdecode(cmdline))
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        pass

    for candidate in candidates:
        if os.path.isabs(candidate) or os.sep in candidate:
            if os.path.realpath(candidate) in excluded_realpaths:
                return True
        elif candidate in excluded_names:
            return True
    return False if candidates else None


def summarize_process_tree(
    root_pid: int,
    children: dict[int, list[int]],
    rss_by_pid: dict[int, int],
    classification_by_pid: dict[int, bool],
    unresolved_records: int,
) -> ProcessTreeSample:
    """Summarize a tree, pruning every matched root and its descendants."""

    tree_rss_kb = 0
    client_rss_kb = 0
    excluded_process_count = 0
    seen: set[int] = set()
    stack = [(root_pid, False)]
    while stack:
        pid, parent_excluded = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        classification = classification_by_pid.get(pid)
        excluded = parent_excluded or classification is True
        rss_kb = rss_by_pid.get(pid, 0)
        tree_rss_kb += rss_kb
        if excluded:
            excluded_process_count += 1
        elif classification is False:
            client_rss_kb += rss_kb
        stack.extend((child, excluded) for child in children.get(pid, ()))
    return ProcessTreeSample(
        tree_rss_kb,
        client_rss_kb,
        excluded_process_count,
        unresolved_records,
    )


def process_tree_sample_from_proc(
    root_pid: int,
    excluded_realpaths: frozenset[str],
    proc_root: Path = Path("/proc"),
) -> ProcessTreeSample:
    """Return one full/client RSS sample from coherent bracketed /proc reads."""

    children: dict[int, list[int]] = {}
    rss_by_pid: dict[int, int] = {}
    classification_by_pid: dict[int, bool] = {}
    unresolved_records = 0
    excluded_names = frozenset(os.path.basename(path) for path in excluded_realpaths)
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            observed_pid, ppid, starttime = read_process_stat(entry)
            if observed_pid != pid:
                raise ValueError("stat pid does not match proc entry")
            rss_kb = read_process_rss_kb(entry)
            classification = classify_process_from_proc(
                entry, excluded_realpaths, excluded_names
            )
            if classification is None:
                raise ValueError("process executable cannot be resolved")
            closing_pid, _, closing_starttime = read_process_stat(entry)
            if closing_pid != pid or closing_starttime != starttime:
                raise ValueError("process identity changed during scan")
        except (
            FileNotFoundError,
            PermissionError,
            OSError,
            UnicodeError,
            IndexError,
            ValueError,
        ):
            unresolved_records += 1
            continue
        children.setdefault(ppid, []).append(pid)
        rss_by_pid[pid] = rss_kb
        classification_by_pid[pid] = classification
    return summarize_process_tree(
        root_pid,
        children,
        rss_by_pid,
        classification_by_pid,
        unresolved_records,
    )


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


def process_tree_sample_from_darwin(
    root_pid: int, excluded_realpaths: frozenset[str]
) -> ProcessTreeSample:
    """Use libproc on macOS; release CI always takes the Linux /proc path."""

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return ProcessTreeSample(0, 0, 0, 0)
    child_buffer = (ctypes.c_int32 * 4096)()
    path_buffer = ctypes.create_string_buffer(4096)
    stack = [(root_pid, False)]
    seen: set[int] = set()
    tree_bytes = 0
    client_bytes = 0
    excluded_process_count = 0
    while stack:
        pid, parent_excluded = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        excluded = parent_excluded
        if not excluded and excluded_realpaths:
            path_buffer.value = b""
            if libproc.proc_pidpath(
                pid, path_buffer, ctypes.sizeof(path_buffer)
            ) > 0:
                executable = os.fsdecode(path_buffer.value)
                excluded = os.path.realpath(executable) in excluded_realpaths
        if excluded:
            excluded_process_count += 1
        info = DarwinTaskInfo()
        size = ctypes.sizeof(info)
        if libproc.proc_pidinfo(pid, 4, 0, ctypes.byref(info), size) == size:
            tree_bytes += info.resident_size
            if not excluded:
                client_bytes += info.resident_size
        child_count = libproc.proc_listchildpids(
            pid, child_buffer, ctypes.sizeof(child_buffer)
        )
        if child_count > 0:
            stack.extend(
                (child, excluded)
                for child in child_buffer[: min(child_count, len(child_buffer))]
                if child > 0
            )
    return ProcessTreeSample(
        tree_bytes // 1024,
        client_bytes // 1024,
        excluded_process_count,
        0,
    )


def process_tree_sample(
    root_pid: int,
    excluded_realpaths: frozenset[str],
) -> ProcessTreeSample:
    if Path("/proc/self/stat").is_file():
        return process_tree_sample_from_proc(root_pid, excluded_realpaths)
    if sys.platform == "darwin":
        return process_tree_sample_from_darwin(root_pid, excluded_realpaths)
    return ProcessTreeSample(0, 0, 0, 0)


def write_measurement(
    output_path: Path,
    *,
    wall_seconds: float,
    peak_tree_rss_kb: int,
    peak_client_rss_kb: int,
    peak_excluded_process_count: int,
    unresolved_samples: int,
    unresolved_records_total: int,
    exit_code: int,
    samples: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "wall_seconds": wall_seconds,
                "peak_tree_rss_kb": peak_tree_rss_kb,
                "peak_client_rss_kb": peak_client_rss_kb,
                "peak_excluded_process_count": peak_excluded_process_count,
                "unresolved_samples": unresolved_samples,
                "unresolved_records_total": unresolved_records_total,
                "exit_code": exit_code,
                "samples": samples,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def run(command: list[str], output_path: Path, exclude_exes: list[Path]) -> int:
    started = time.perf_counter()
    excluded_realpaths = frozenset(
        os.path.realpath(os.fspath(path)) for path in exclude_exes
    )
    try:
        process = subprocess.Popen(command)
    except OSError as error:
        elapsed = time.perf_counter() - started
        write_measurement(
            output_path,
            wall_seconds=elapsed,
            peak_tree_rss_kb=0,
            peak_client_rss_kb=0,
            peak_excluded_process_count=0,
            unresolved_samples=0,
            unresolved_records_total=0,
            exit_code=127,
            samples=0,
        )
        print(f"measure_process_tree: {error}", file=sys.stderr)
        return 127

    rss_samples: list[ProcessTreeSample] = []
    stop_sampling = threading.Event()

    def sample_until_stopped() -> None:
        while True:
            try:
                rss_samples.append(
                    process_tree_sample(process.pid, excluded_realpaths)
                )
            except (OSError, RuntimeError, ValueError):
                rss_samples.append(ProcessTreeSample(0, 0, 0, 1))
            if stop_sampling.wait(SAMPLE_INTERVAL_SECONDS):
                break

    sampler = threading.Thread(target=sample_until_stopped, daemon=True)
    sampler.start()
    exit_code = process.wait()
    wall_seconds = time.perf_counter() - started
    stop_sampling.set()
    sampler.join()
    peak_tree_rss_kb = max((sample.tree_rss_kb for sample in rss_samples), default=0)
    peak_client_rss_kb = max(
        (sample.client_rss_kb for sample in rss_samples), default=0
    )
    peak_excluded_process_count = max(
        (sample.excluded_process_count for sample in rss_samples), default=0
    )
    unresolved_samples = sum(sample.unresolved_records > 0 for sample in rss_samples)
    unresolved_records_total = sum(
        sample.unresolved_records for sample in rss_samples
    )
    samples = len(rss_samples)
    write_measurement(
        output_path,
        wall_seconds=wall_seconds,
        peak_tree_rss_kb=peak_tree_rss_kb,
        peak_client_rss_kb=peak_client_rss_kb,
        peak_excluded_process_count=peak_excluded_process_count,
        unresolved_samples=unresolved_samples,
        unresolved_records_total=unresolved_records_total,
        exit_code=exit_code,
        samples=samples,
    )
    return exit_code if exit_code >= 0 else 128 + abs(exit_code)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="measurement JSON path")
    parser.add_argument(
        "--exclude-exe",
        action="append",
        default=[],
        type=Path,
        help="exclude a matching executable and its descendant subtree (repeatable)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return run(args.command, args.out, args.exclude_exe)


if __name__ == "__main__":
    raise SystemExit(main())
