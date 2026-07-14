from __future__ import annotations

import functools
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable, TypeVar, cast


SAMPLE_INTERVAL_SECONDS = 0.05
PS_TIMEOUT_SECONDS = 2.0


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
            "sampling_mode": "background_thread_ps",
            "available": rss_self_kb is not None or rss_tree_kb is not None,
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
    tree = ps_process_tree(root_pid)
    return ProcessTreeRssSample(
        rss_self_kb=ps_rss_kb(root_pid),
        rss_tree_kb=sum_optional(ps_rss_kb(pid) for pid in tree),
    )


def sum_optional(values: Any) -> int | None:
    total = 0
    seen = False
    for value in values:
        if value is not None:
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


def ps_process_tree(root_pid: int) -> list[int]:
    output = run_ps(["ps", "-axo", "pid=,ppid="])
    if not output:
        return [root_pid]
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
