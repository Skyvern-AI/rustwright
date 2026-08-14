"""Shared statistics for the cold-start latency matrix and checker."""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from typing import Any


BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_PROTOCOL = "paired-delta-random-v1"
_BOOTSTRAP_SEED_NAMESPACE = "startup-latency"


def percentile(values: Sequence[float], probability: float) -> float:
    """Return a linear-interpolated percentile for a nonempty sequence."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("percentile probability must be between zero and one")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def distribution(values: Sequence[float]) -> dict[str, Any]:
    """Return the published distribution statistics for millisecond values."""
    if not values:
        return {
            "count": 0,
            "median_ms": None,
            "p25_ms": None,
            "p75_ms": None,
            "mad_ms": None,
        }
    median = statistics.median(values)
    return {
        "count": len(values),
        "median_ms": median,
        "p25_ms": percentile(values, 0.25),
        "p75_ms": percentile(values, 0.75),
        "mad_ms": statistics.median(abs(value - median) for value in values),
    }


def bootstrap_seed(phase: str, pair_count: int) -> str:
    """Return the declared deterministic bootstrap seed for one phase."""
    return f"{_BOOTSTRAP_SEED_NAMESPACE}:{phase}:{pair_count}"


def bootstrap_median_ci(
    values: Sequence[float],
    phase: str,
    protocol: str = BOOTSTRAP_PROTOCOL,
) -> list[float] | None:
    """Return the seeded 95% bootstrap interval for the median."""
    if protocol != BOOTSTRAP_PROTOCOL:
        raise ValueError(f"unknown bootstrap protocol: {protocol}")
    if not values:
        return None
    generator = random.Random(bootstrap_seed(phase, len(values)))
    estimates: list[float] = []
    size = len(values)
    # random() has a stable cross-version stream; randrange/_randbelow does not.
    for _ in range(BOOTSTRAP_RESAMPLES):
        estimates.append(
            statistics.median(
                values[min(int(generator.random() * size), size - 1)]
                for _ in range(size)
            )
        )
    return [percentile(estimates, 0.025), percentile(estimates, 0.975)]


def summarize_paired_ms(
    pairs: Sequence[tuple[float, float]],
    phase: str,
    *,
    bootstrap_protocol: str = BOOTSTRAP_PROTOCOL,
) -> dict[str, Any]:
    """Return all published statistics for paired before/after milliseconds."""
    before_values = [before for before, _after in pairs]
    after_values = [after for _before, after in pairs]
    deltas = [after - before for before, after in pairs]
    percentages = [
        ((after - before) / before) * 100.0
        for before, after in pairs
        if before > 0
    ]
    return {
        "before": distribution(before_values),
        "after": distribution(after_values),
        "paired": {
            "complete_pairs": len(pairs),
            "median_delta_ms": statistics.median(deltas) if deltas else None,
            "median_delta_percent": statistics.median(percentages) if percentages else None,
            "bootstrap_95_ci_ms": bootstrap_median_ci(
                deltas,
                phase,
                bootstrap_protocol,
            ),
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "bootstrap_seed": bootstrap_seed(phase, len(pairs)),
            "bootstrap_protocol": bootstrap_protocol,
        },
    }
