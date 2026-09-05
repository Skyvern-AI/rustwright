from __future__ import annotations

import functools
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

PROCESS_ROLES = (
    "browser",
    "renderer",
    "gpu",
    "utility",
    "network",
    "python-host",
    "node-driver",
    "other",
)


SAMPLE_INTERVAL_SECONDS = 0.05
PS_TIMEOUT_SECONDS = 2.0
PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class ProcessTreeRssSample:
    rss_self_kb: int | None
    rss_tree_kb: int | None
    pss_tree_kb: int | None = None
    pss_by_role_kb: dict[str, int] | None = None


class ProcessTreeRssSampler:
    """Sample peak RSS and PSS attribution for a process and its descendants."""

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
        peak_sample = max(
            (sample for sample in samples if sample.rss_tree_kb is not None),
            key=lambda sample: sample.rss_tree_kb,
            default=None,
        )
        pss_tree_kb = peak_sample.pss_tree_kb if peak_sample is not None else None
        pss_by_role_kb = (
            dict(peak_sample.pss_by_role_kb)
            if peak_sample is not None and peak_sample.pss_by_role_kb is not None
            else None
        )
        pss_required = sys.platform.startswith("linux")
        pss_available = (
            type(pss_tree_kb) is int
            and pss_tree_kb > 0
            and pss_by_role_kb is not None
            and set(pss_by_role_kb) == set(PROCESS_ROLES)
            and all(type(value) is int and value >= 0 for value in pss_by_role_kb.values())
            and sum(pss_by_role_kb.values()) == pss_tree_kb
        )
        rss_available = (
            type(rss_self_kb) is int
            and rss_self_kb > 0
            and type(rss_tree_kb) is int
            and rss_tree_kb > 0
            and rss_tree_kb >= rss_self_kb
        )
        return {
            "rss_self_kb": rss_self_kb,
            "rss_tree_kb": rss_tree_kb,
            "pss_tree_kb": pss_tree_kb,
            "pss_by_role_kb": pss_by_role_kb,
            "pss_required": pss_required,
            "pss_available": pss_available,
            "pss_statistic": "at_tree_rss_peak",
            "pss_scope": "benchmark_process_and_descendants",
            "samples_collected": len(samples),
            "sampling_interval_ms": self.sample_interval * 1000,
            "statistic": "peak",
            "scope": "benchmark_process_and_descendants",
            "sampling_mode": "background_thread_procfs_or_ps",
            "available": rss_available and (not pss_required or pss_available),
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


def _process_tree_from_table(
    root_pid: int,
    process_table: dict[int, tuple[int, int | None]],
) -> list[int]:
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
    return tree


def procfs_process_tree_rss(
    root_pid: int,
    proc_root: Path | None = None,
) -> ProcessTreeRssSample | None:
    resolved_root = PROC_ROOT if proc_root is None else proc_root
    process_table, complete = procfs_process_table(resolved_root)
    if not complete or root_pid not in process_table:
        return None
    tree = _process_tree_from_table(root_pid, process_table)
    rss_self_kb = process_table[root_pid][1]
    rss_tree_kb = sum_required(process_table[pid][1] for pid in tree)
    if not sys.platform.startswith("linux") or rss_tree_kb is None:
        return ProcessTreeRssSample(
            rss_self_kb=rss_self_kb,
            rss_tree_kb=rss_tree_kb,
        )
    pss = procfs_process_tree_pss(tree, resolved_root)
    if pss is None:
        # A process can exit between the status walk and smaps_rollup reads.
        # Re-walk once to distinguish that race from a genuinely partial read.
        process_table, complete = procfs_process_table(resolved_root)
        if complete and root_pid in process_table:
            tree = _process_tree_from_table(root_pid, process_table)
            rss_self_kb = process_table[root_pid][1]
            rss_tree_kb = sum_required(process_table[pid][1] for pid in tree)
            pss = (
                procfs_process_tree_pss(tree, resolved_root)
                if rss_tree_kb is not None
                else None
            )
    if pss is None:
        return ProcessTreeRssSample(
            rss_self_kb=rss_self_kb,
            rss_tree_kb=rss_tree_kb,
        )
    pss_tree_kb, pss_by_role_kb = pss
    return ProcessTreeRssSample(
        rss_self_kb=rss_self_kb,
        rss_tree_kb=rss_tree_kb,
        pss_tree_kb=pss_tree_kb,
        pss_by_role_kb=pss_by_role_kb,
    )


def _argv_tokens(cmdline: str | bytes | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(cmdline, bytes):
        text = cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        return text.split()
    if isinstance(cmdline, str):
        return cmdline.replace("\x00", " ").split()
    return [str(value) for value in cmdline]


def _argv_option(args: list[str], option: str) -> str | None:
    for index, token in enumerate(args):
        if token == option:
            return args[index + 1] if index + 1 < len(args) else None
        prefix = f"{option}="
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _is_chrome_executable(executable: str) -> bool:
    return executable in {
        "chrome",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "headless_shell",
        "chrome-headless-shell",
    }


def classify_process_role(cmdline: str | bytes | list[str] | tuple[str, ...]) -> str:
    """Classify a process from its executable and exact Chromium argv tokens."""

    args = _argv_tokens(cmdline)
    if not args:
        return "other"
    executable = Path(args[0]).name.lower()
    if "python" in executable:
        return "python-host"
    if executable in {"node", "nodejs"} or executable.startswith("node-") or executable.endswith("-node"):
        return "node-driver"

    process_type = (_argv_option(args, "--type") or "").lower()
    utility_subtype = (_argv_option(args, "--utility-sub-type") or "").lower()
    if utility_subtype == "network.mojom.networkservice" or process_type == "network":
        return "network"
    if process_type == "renderer":
        return "renderer"
    if process_type in {"gpu", "gpu-process"}:
        return "gpu"
    if process_type == "utility":
        return "utility"
    if _is_chrome_executable(executable) and not process_type:
        return "browser"
    return "other"


def _procfs_pss_kb(process_dir: Path) -> int | None:
    try:
        smaps_rollup = (process_dir / "smaps_rollup").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    pss_kb: int | None = None
    for line in smaps_rollup.splitlines():
        if not line.startswith("Pss:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            parsed = int(parts[1])
        except ValueError:
            return None
        if parsed < 0:
            return None
        pss_kb = parsed
        break
    return pss_kb


def procfs_process_tree_pss(
    tree: list[int],
    proc_root: Path | None = None,
) -> tuple[int, dict[str, int]] | None:
    """Read PSS for every tree process and aggregate it by process role."""

    resolved_root = PROC_ROOT if proc_root is None else proc_root
    by_role = {role: 0 for role in PROCESS_ROLES}
    total = 0
    for pid in tree:
        process_dir = resolved_root / str(pid)
        pss_kb = _procfs_pss_kb(process_dir)
        if pss_kb is None:
            return None
        try:
            cmdline = (process_dir / "cmdline").read_bytes()
        except (OSError, UnicodeError):
            cmdline = b""
        role = classify_process_role(cmdline)
        by_role[role] += pss_kb
        total += pss_kb
    return total, by_role


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
