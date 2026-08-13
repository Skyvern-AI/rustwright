#!/usr/bin/env python3
"""Trace-stack microbenchmark; see BENCHMARK.md for the system protocol.

This script reports microbenchmark operations only. It does not measure browser
latency, process-tree CPU, PSS, leaks, or end-to-end effects.
"""

from __future__ import annotations

import argparse
import functools
import gc
import hashlib
import inspect
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from rustwright import sync_api


SCHEMA = "rustwright.trace-stack-capture.v2"
BENCHMARK_VERSION = "2"
MAX_DEPTH = 256
Variant = str
Lane = str


def _state() -> sync_api.Tracing:
    tracing = sync_api.Tracing.__new__(sync_api.Tracing)
    tracing._sources = True
    tracing._source_file_indexes = {}
    tracing._source_files = []
    tracing._source_stacks = []
    return tracing


def _serialized(tracing: sync_api.Tracing) -> tuple[dict[str, int], list[str], list[list[Any]]]:
    return tracing._source_file_indexes, tracing._source_files, tracing._source_stacks


def _eager_reference(tracing: sync_api.Tracing, call_id: str) -> None:
    """The replaced eager inspect.stack()[2:] implementation."""
    if not tracing._sources:
        return
    try:
        stack_id = int(str(call_id).rsplit("@", 1)[1])
    except (IndexError, TypeError, ValueError):
        return
    stack_info = inspect.stack()[2:]
    frames: list[list[Any]] = []
    fallback: list[list[Any]] = []
    current_file = Path(sync_api.__file__).resolve()
    try:
        for item in stack_info:
            filename = item.filename
            if not filename:
                continue
            try:
                resolved = Path(filename).resolve()
            except OSError:
                resolved = Path(filename)
            key = str(resolved)
            index = tracing._source_file_indexes.get(key)
            if index is None:
                index = len(tracing._source_files)
                tracing._source_file_indexes[key] = index
                tracing._source_files.append(filename)
            entry = [index, int(item.lineno), 0, str(item.function or "<module>")]
            if len(fallback) < 8:
                fallback.append(entry)
            if resolved != current_file:
                parts = set(resolved.parts)
                if not ({"concurrent", "futures"}.issubset(parts) or resolved.name in {"threading.py", "_base.py"}):
                    frames.append(entry)
                    if len(frames) >= 8:
                        break
        if not frames:
            frames = fallback[:8]
        if frames:
            tracing._source_stacks.append([stack_id, frames])
    finally:
        stack_info = []


def _chain(depth: int, callback: Callable[[], None]) -> None:
    if depth:
        _chain(depth - 1, callback)
    else:
        callback()


def _full_operation(variant: Variant, depth: int, call_id: int) -> tuple[Any, ...]:
    tracing = _state()
    recorder = tracing._record_source_stack if variant == "optimized_production" else functools.partial(_eager_reference, tracing)
    _chain(depth, lambda: recorder(f"call@{call_id}"))
    return _serialized(tracing)


def _acquisition_operation(variant: Variant, depth: int) -> int:
    observed = 0

    def acquire() -> None:
        nonlocal observed
        if variant == "optimized_production":
            def visit(filename: str, lineno: Any, function: str) -> bool:
                nonlocal observed
                observed += 1
                return observed >= 8

            sync_api._walk_source_stack(sys._getframe(1), visit)
        else:
            stack_info = inspect.stack()[1:]
            try:
                observed = min(8, len(stack_info))
            finally:
                stack_info = []

    _chain(depth, acquire)
    return observed


def _control_operation(depth: int) -> None:
    _chain(depth, lambda: None)


def _operation(lane: Lane, variant: Variant, depth: int, call_id: int) -> Any:
    if lane == "full_production_helper":
        return _full_operation(variant, depth, call_id)
    if lane == "acquisition_only":
        return _acquisition_operation(variant, depth)
    return _control_operation(depth)


def _measure(lane: Lane, variant: Variant, depth: int, captures: int) -> dict[str, Any]:
    gc.collect()
    started = time.process_time_ns()
    for call_id in range(captures):
        _operation(lane, variant, depth, call_id)
    cpu_ns = time.process_time_ns() - started

    gc.collect()
    tracemalloc.start()
    baseline_current, _ = tracemalloc.get_traced_memory()
    for call_id in range(captures):
        _operation(lane, variant, depth, call_id)
    _, peak = tracemalloc.get_traced_memory()
    gc.collect()
    retained, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "cpu_ns_per_benchmark_operation": cpu_ns / captures,
        "benchmark_operations_per_cpu_second": captures * 1_000_000_000 / cpu_ns,
        "tracemalloc_peak_bytes_above_start": max(0, peak - baseline_current),
        "tracemalloc_net_retained_bytes_after_gc": retained - baseline_current,
    }


class _Frame:
    def __init__(self, filename: str, lineno: Any, function: str, back: "_Frame | None" = None):
        self.f_code = SimpleNamespace(co_filename=filename, co_name=function)
        self.f_lineno = lineno
        self.f_back = back


def _synthetic(descriptors: list[tuple[str, Any, str]]) -> _Frame | None:
    frame = None
    for filename, lineno, function in reversed(descriptors):
        frame = _Frame(filename, lineno, function, frame)
    return frame


def _equivalence_run(tracing: sync_api.Tracing, optimized: bool) -> None:
    recorder = tracing._record_source_stack if optimized else functools.partial(_eager_reference, tracing)
    recorder("call@17")


def _real_equivalence_state(depth: int, optimized: bool) -> tuple[dict[str, int], list[str], list[list[Any]]]:
    tracing = _state()
    _chain(depth, lambda: _equivalence_run(tracing, optimized))
    return _serialized(tracing)


def _validate_equivalence(depths: list[int]) -> dict[str, Any]:
    real_cases = []
    for depth in depths:
        old = _real_equivalence_state(depth, False)
        new = _real_equivalence_state(depth, True)
        if old != new:
            raise RuntimeError(f"complete serialized equivalence failed for real depth {depth}")
        real_cases.append({"depth": depth, "equivalent": True})

    adversarial = {
        "filtering_and_original_name": [
            (str(Path(sync_api.__file__).resolve()), 1, "internal"),
            ("folder/../user.py", 2, "first"),
            ("user.py", 3, "second"),
            ("threading.py", 4, "thread"),
            ("module.py", 5, ""),
        ],
        "cutoff": [(f"frame-{index}.py", index, f"f{index}") for index in range(12)],
        "all_filtered_fallback": [(str(Path(sync_api.__file__).resolve()), index, "internal") for index in range(10)],
        "empty_filename": [("", 1, "ignored"), ("user.py", 2, "user")],
    }
    original_getframe = sys._getframe
    original_stack = inspect.stack
    checked = []
    try:
        for name, descriptors in adversarial.items():
            frame = _synthetic(descriptors)
            sys._getframe = lambda requested_depth, frame=frame: frame  # type: ignore[assignment]
            items = [SimpleNamespace(filename="skip.py", lineno=0, function="skip")] * 2
            items += [SimpleNamespace(filename=f, lineno=line, function=fn) for f, line, fn in descriptors]
            inspect.stack = lambda items=items: items  # type: ignore[assignment]
            old = _state()
            new = _state()
            _equivalence_run(old, False)
            _equivalence_run(new, True)
            if _serialized(old) != _serialized(new):
                raise RuntimeError(f"complete serialized equivalence failed for adversarial case {name}")
            checked.append(name)
        for malformed in (None, "", "call", "call@", "call@bad"):
            old = _state()
            new = _state()
            _eager_reference(old, malformed)  # type: ignore[arg-type]
            new._record_source_stack(malformed)  # type: ignore[arg-type]
            if _serialized(old) != _serialized(new):
                raise RuntimeError(f"malformed call-id equivalence failed: {malformed!r}")
    finally:
        sys._getframe = original_getframe  # type: ignore[assignment]
        inspect.stack = original_stack  # type: ignore[assignment]
    return {"real_cases": real_cases, "adversarial_cases": checked, "malformed_call_ids": 5}


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _depths(value: str) -> list[int]:
    if not value or any(not token.strip() for token in value.split(",")):
        raise argparse.ArgumentTypeError("depths must be a non-empty comma-separated integer list")
    try:
        parsed = [int(token) for token in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("depths must contain only integers") from error
    if any(depth < 0 or depth > MAX_DEPTH for depth in parsed):
        raise argparse.ArgumentTypeError(f"depths must be between 0 and {MAX_DEPTH}")
    if len(set(parsed)) != len(parsed):
        raise argparse.ArgumentTypeError("depths must be unique")
    return parsed


def _identity() -> dict[str, str]:
    implementation = inspect.getsource(sync_api.Tracing._record_source_stack) + inspect.getsource(sync_api._walk_source_stack)
    benchmark = Path(__file__).read_bytes()
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "benchmark_sha256": hashlib.sha256(benchmark).hexdigest(),
        "implementation": "rustwright.sync_api.Tracing._record_source_stack+_walk_source_stack",
        "implementation_sha256": hashlib.sha256(implementation.encode()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depths", type=_depths, default=_depths("8,16,24"))
    parser.add_argument("--captures", type=_positive, default=2_000)
    parser.add_argument("--repeats", type=_positive, default=7)
    parser.add_argument("--mode", choices=("steady-state", "cold-process"), default="steady-state")
    parser.add_argument("--cold-case", help="single lane/variant/depth case required in cold-process mode")
    args = parser.parse_args()
    if args.mode == "cold-process" and (args.repeats != 1 or args.captures != 1):
        parser.error("cold-process mode requires --repeats 1 --captures 1")
    if args.mode == "steady-state" and args.cold_case:
        parser.error("--cold-case is only valid with --mode cold-process")

    variants = ("old_eager_reference", "optimized_production")
    combos = [(lane, variant, depth) for depth in args.depths for lane in ("acquisition_only", "full_production_helper") for variant in variants]
    combos += [("no_capture_control", "control", depth) for depth in args.depths]
    if args.mode == "cold-process":
        if not args.cold_case:
            parser.error("cold-process mode requires --cold-case lane/variant/depth")
        matches = [combo for combo in combos if "/".join(map(str, combo)) == args.cold_case]
        if not matches:
            parser.error("--cold-case must name a lane/variant/depth present in --depths")
        combos = matches
    # A cold-process sample must reach its selected helper before the
    # equivalence gate exercises either implementation. Steady-state samples
    # intentionally retain validation-before-measurement semantics.
    equivalence = None if args.mode == "cold-process" else _validate_equivalence(args.depths)
    samples = []
    order_metadata = []
    for repeat in range(args.repeats):
        rotated = combos[repeat % len(combos) :] + combos[: repeat % len(combos)]
        order = list(reversed(rotated)) if repeat % 2 else rotated
        order_metadata.append({"repeat": repeat, "order": ["/".join(map(str, item)) for item in order]})
        for position, (lane, variant, depth) in enumerate(order):
            samples.append({
                "repeat": repeat,
                "order_position": position,
                "lane": lane,
                "variant": variant,
                "depth": depth,
                **_measure(lane, variant, depth, args.captures),
            })

    if args.mode == "cold-process":
        equivalence = _validate_equivalence(args.depths)

    summaries = []
    for lane, variant, depth in combos:
        matching = [sample for sample in samples if (sample["lane"], sample["variant"], sample["depth"]) == (lane, variant, depth)]
        summaries.append({
            "lane": lane,
            "variant": variant,
            "depth": depth,
            "sample_count": len(matching),
            "median_cpu_ns_per_benchmark_operation": statistics.median(
                sample["cpu_ns_per_benchmark_operation"] for sample in matching
            ),
        })

    print(json.dumps({
        "schema": SCHEMA,
        "identity": _identity(),
        "python": sys.version,
        "mode": args.mode,
        "mode_note": (
            "fresh-process first helper operation is the CPU-timed sample; "
            "tracemalloc sampling and fail-closed equivalence validation follow in the same process"
            if args.mode == "cold-process"
            else "counterbalanced warmed-process samples"
        ),
        "captures_per_sample": args.captures,
        "repeats": args.repeats,
        "depths": args.depths,
        "equivalence": equivalence,
        "order_metadata": order_metadata,
        "samples": samples,
        "summaries": summaries,
        "metric_scope": "microbenchmark operations; not browser latency, process-tree CPU, PSS, leaks, or end-to-end effects",
    }, sort_keys=True))


if __name__ == "__main__":
    main()
