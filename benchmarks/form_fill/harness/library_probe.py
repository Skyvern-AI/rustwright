"""Measure page-library overhead without page-render waits."""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any


def _measure(reps: int, operation: Callable[[], Any]) -> dict[str, float]:
    samples_ms: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        operation()
        samples_ms.append((time.perf_counter() - started) * 1_000)

    measured_ms = samples_ms[1:]
    if not measured_ms:
        raise ValueError("reps must be at least 2")

    p95 = (
        measured_ms[0]
        if len(measured_ms) == 1
        else statistics.quantiles(measured_ms, n=100, method="inclusive")[94]
    )
    return {
        "median": round(statistics.median(measured_ms), 3),
        "p95": round(p95, 3),
    }


def run_library_probes(page: Any, reps: int = 25) -> dict[str, Any]:
    """Run render-free probes against an already-connected page."""
    results: dict[str, Any] = {"reps": reps}

    try:
        results["evaluate_rtt_ms"] = _measure(reps, lambda: page.evaluate("1"))
    except Exception:
        results["evaluate_rtt_ms"] = None

    try:
        results["property_read_ms"] = _measure(
            reps, lambda: page.evaluate("document.title")
        )
    except Exception:
        results["property_read_ms"] = None

    try:
        page.evaluate(
            """() => {
                let b = document.getElementById('__rw_probe_btn__');
                if (!b) {
                    b = document.createElement('button');
                    b.id = '__rw_probe_btn__';
                    b.textContent = 'probe';
                    b.style.cssText = 'position:fixed;top:8px;left:8px;z-index:2147483647;width:60px;height:24px';
                    document.body.appendChild(b);
                }
                return true;
            }"""
        )
        results["stable_click_ms"] = _measure(
            reps, lambda: page.click("#__rw_probe_btn__", timeout=5000)
        )
    except Exception:
        results["stable_click_ms"] = None

    probe_results = (
        results["evaluate_rtt_ms"],
        results["property_read_ms"],
        results["stable_click_ms"],
    )
    results["library_probe_median_ms"] = (
        round(sum(probe["median"] for probe in probe_results), 3)
        if all(probe is not None for probe in probe_results)
        else None
    )
    return results


if __name__ == "__main__":
    print("This module is imported by benchmark workloads, not run directly.")
