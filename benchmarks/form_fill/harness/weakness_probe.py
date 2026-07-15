"""Measure targeted Rustwright weak spots on an existing browser session.

NOTE (connect_breakdown): A true connect breakdown for R1 needs CDP-traffic
instrumentation that is not available at the Playwright API level. Capturing
the individual connection phases, protocol commands, and wire timings is a
follow-up rather than part of these already-connected page/context probes.

rustwright query_selector_all is ~O(n) with a large per-handle constant (~270ms/element observed on a remote session) vs Playwright's batched ~1ms/element — measured via qsa_ms_per_handle.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from typing import Any


_PROPERTY_BATCH_READS = 100
HANDLES_N = int(os.environ.get("WP_HANDLES_N", "15"))
COUNT_N = int(os.environ.get("WP_COUNT_N", "1000"))


def _measure(
    reps: int, operation: Callable[[], Any], *, operations_per_rep: int = 1
) -> dict[str, float]:
    """Measure an operation, optionally normalizing each sample per API read."""
    samples_ms: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        operation()
        elapsed_ms = (time.perf_counter() - started) * 1_000
        samples_ms.append(elapsed_ms / operations_per_rep)

    if not samples_ms:
        raise ValueError("reps must be at least 1")

    p95 = (
        samples_ms[0]
        if len(samples_ms) == 1
        else statistics.quantiles(samples_ms, n=100, method="inclusive")[94]
    )
    return {
        "median": round(statistics.median(samples_ms), 3),
        "p95": round(p95, 3),
    }


def _property_batch(page: Any) -> None:
    """Alternate remote and local property reads in one sequential batch."""
    for index in range(_PROPERTY_BATCH_READS):
        if index % 2 == 0:
            page.title()
        else:
            _ = page.url


def run_weakness_probes(page, context, reps=15) -> dict:
    """Run R3/R4 probes against an already-connected page and context."""
    results: dict[str, Any] = {"reps": reps}

    try:
        results["object_eval_small_ms"] = _measure(
            reps,
            lambda: page.evaluate(
                """() => ({
                    id: 7,
                    name: 'weakness-probe',
                    enabled: true,
                    score: 12.5,
                    tags: ['r3', 'small'],
                    nested: {left: 1, right: {value: 'x'}},
                    counts: [1, 2, 3, 4],
                    metadata: {source: 'benchmark', version: 1},
                    empty: null,
                    flags: {ready: true, cached: false}
                })"""
            ),
        )
    except Exception:
        results["object_eval_small_ms"] = None

    try:
        results["object_eval_large_ms"] = _measure(
            reps,
            lambda: page.evaluate(
                """() => ({
                    kind: 'large',
                    items: Array.from({length: 5000}, (_, i) => ({
                        index: i,
                        label: 'item-' + i,
                        active: i % 2 === 0
                    }))
                })"""
            ),
        )
    except Exception:
        results["object_eval_large_ms"] = None

    try:
        small_median = results["object_eval_small_ms"]["median"]
        large_median = results["object_eval_large_ms"]["median"]
        results["object_eval_large_to_small_ratio"] = round(
            large_median / small_median, 3
        )
    except (KeyError, TypeError, ZeroDivisionError):
        results["object_eval_large_to_small_ratio"] = None

    try:
        page.evaluate(
            f"""() => {{
                document.querySelectorAll('.wp-item').forEach((e) => e.remove());
                const c=document.createElement('div');
                for(let i=0;i<{HANDLES_N};i++){{
                    const s=document.createElement('span');
                    s.className='wp-item';
                    s.textContent='x'+i;
                    c.appendChild(s);
                }}
                document.body.appendChild(c);
            }}"""
        )
        qsa_handles = _measure(
            reps, lambda: page.query_selector_all(".wp-item")
        )
        results["qsa_handles_ms"] = qsa_handles
        results["qsa_ms_per_handle"] = round(
            qsa_handles["median"] / HANDLES_N, 3
        )
    except Exception:
        results["qsa_handles_ms"] = None
        results["qsa_ms_per_handle"] = None

    try:
        page.evaluate(
            f"""() => {{
                document.querySelectorAll('.wp-item2').forEach((e) => e.remove());
                const c=document.createElement('div');
                for(let i=0;i<{COUNT_N};i++){{
                    const s=document.createElement('span');
                    s.className='wp-item2';
                    s.textContent='x'+i;
                    c.appendChild(s);
                }}
                document.body.appendChild(c);
            }}"""
        )
        results["locator_count_ms"] = _measure(
            reps, lambda: page.locator(".wp-item2").count()
        )
    except Exception:
        results["locator_count_ms"] = None

    try:
        results["property_batch_ms"] = _measure(
            reps,
            lambda: _property_batch(page),
            operations_per_rep=_PROPERTY_BATCH_READS,
        )
    except Exception:
        results["property_batch_ms"] = None

    return results
