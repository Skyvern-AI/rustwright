from __future__ import annotations

import functools
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar, cast


SAMPLE_INTERVAL_SECONDS = 0.05
PS_TIMEOUT_SECONDS = 2.0
PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class ProcessTreeRssSample:
    rss_self_kb: int | None
    rss_tree_kb: int | None


class ProcessTreeRssSampler:
    """Sample peak RSS for a process and its descendants outside the timing path."""

    def __init__(self, root_pid: int, sample_interval: float | None = None) -> None:
        self.root_pid = root_pid
        self.sample_interval = (
            SAMPLE_INTERVAL_SECONDS if sample_interval is None else sample_interval
        )
        self.samples: list[ProcessTreeRssSample] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Establish a best-effort baseline before entering the benchmark. This
        # is outside every per-case timer; periodic samples remain background.
        self._append(self._sample_safely())
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="benchmark-process-tree-rss-sampler",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=PS_TIMEOUT_SECONDS + 1)
        with self._lock:
            if not self.samples:
                self.samples.append(
                    ProcessTreeRssSample(rss_self_kb=None, rss_tree_kb=None)
                )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._append(self._sample_safely())
            self._stop.wait(self.sample_interval)

    def _sample_safely(self) -> ProcessTreeRssSample:
        try:
            return sample_process_tree_rss(self.root_pid)
        except Exception:
            # Memory telemetry is best effort and must never fail a benchmark.
            return ProcessTreeRssSample(rss_self_kb=None, rss_tree_kb=None)

    def _append(self, sample: ProcessTreeRssSample) -> None:
        with self._lock:
            self.samples.append(sample)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self.samples)
        rss_self_kb = max_optional(sample.rss_self_kb for sample in samples)
        rss_tree_kb = max_optional(sample.rss_tree_kb for sample in samples)
        return {
            "rss_self_kb": rss_self_kb,
            "rss_tree_kb": rss_tree_kb,
            "samples_collected": len(samples),
            "sampling_interval_ms": self.sample_interval * 1000,
            "statistic": "peak",
            "scope": "benchmark_process_and_descendants",
            "sampling_mode": "background_thread_procfs_or_ps",
            "available": rss_self_kb is not None and rss_tree_kb is not None,
        }


Result = TypeVar("Result", bound=dict[str, Any])


def attach_peak_process_tree_rss(
    function: Callable[..., Result],
) -> Callable[..., Result]:
    """Attach an always-on memory block to a benchmark result dictionary."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Result:
        sampler = ProcessTreeRssSampler(os.getpid())
        sampler.start()
        try:
            result = function(*args, **kwargs)
        finally:
            sampler.stop()
        result["memory"] = sampler.summary()
        return result

    return cast(Callable[..., Result], wrapped)


def max_optional(values: Any) -> int | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None


def sample_process_tree_rss(root_pid: int) -> ProcessTreeRssSample:
    procfs_sample = procfs_process_tree_rss(root_pid)
    if procfs_sample is not None:
        return procfs_sample
    tree = ps_process_tree(root_pid)
    rss_self_kb = ps_rss_kb(root_pid)
    return ProcessTreeRssSample(
        rss_self_kb=rss_self_kb,
        rss_tree_kb=(
            None
            if tree is None
            else sum_required(ps_rss_kb(pid) for pid in tree)
        ),
    )


def procfs_process_tree_rss(
    root_pid: int,
    proc_root: Path | None = None,
) -> ProcessTreeRssSample | None:
    process_table, complete = procfs_process_table(
        PROC_ROOT if proc_root is None else proc_root
    )
    if not complete or root_pid not in process_table:
        return None
    children: dict[int, list[int]] = {}
    for pid, (parent_pid, _) in process_table.items():
        children.setdefault(parent_pid, []).append(pid)
    tree: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in tree:
            continue
        tree.append(pid)
        stack.extend(children.get(pid, []))
    return ProcessTreeRssSample(
        rss_self_kb=process_table[root_pid][1],
        rss_tree_kb=sum_required(process_table[pid][1] for pid in tree),
    )


def procfs_process_table(
    proc_root: Path,
) -> tuple[dict[int, tuple[int, int | None]], bool]:
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return {}, False
    numeric_entries = [entry for entry in entries if entry.name.isdigit()]
    unreadable: set[str] = set()
    result: dict[int, tuple[int, int | None]] = {}
    for entry in numeric_entries:
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            unreadable.add(entry.name)
            continue
        parent_pid: int | None = None
        rss_kb: int | None = None
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    parent_pid = int(line.split()[1])
                except (IndexError, ValueError):
                    parent_pid = None
            elif line.startswith("VmRSS:"):
                try:
                    rss_kb = int(line.split()[1])
                except (IndexError, ValueError):
                    rss_kb = None
        if parent_pid is None:
            unreadable.add(entry.name)
            continue
        result[int(entry.name)] = (parent_pid, rss_kb)
    try:
        remaining = {entry.name for entry in proc_root.iterdir() if entry.name.isdigit()}
    except OSError:
        return result, False
    return result, not bool(unreadable & remaining)


def sum_required(values: Any) -> int | None:
    total = 0
    seen = False
    for value in values:
        if value is None:
            return None
        total += value
        seen = True
    return total if seen else None


def run_ps(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            args,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout


def ps_rss_kb(pid: int) -> int | None:
    output = run_ps(["ps", "-o", "rss=", "-p", str(pid)])
    if not output:
        return None
    try:
        return int(output.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def ps_process_tree(root_pid: int) -> list[int] | None:
    output = run_ps(["ps", "-axo", "pid=,ppid="])
    if not output:
        return None
    children: dict[int, list[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    result: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in result:
            continue
        result.append(pid)
        stack.extend(children.get(pid, []))
    return result
