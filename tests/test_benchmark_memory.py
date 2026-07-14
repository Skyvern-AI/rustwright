from __future__ import annotations

import time

from benchmarks import process_tree_memory
from tools.run_benchmark_matrix import aggregate_repetitions


def test_process_tree_walk_and_rss_sum(monkeypatch) -> None:
    process_table = "10 1\n11 10\n12 10\n13 11\n99 1\n"
    rss_by_pid = {10: 100, 11: 200, 12: 300, 13: 400}

    def fake_run_ps(args: list[str]) -> str | None:
        if args == ["ps", "-axo", "pid=,ppid="]:
            return process_table
        return str(rss_by_pid[int(args[-1])])

    monkeypatch.setattr(process_tree_memory, "run_ps", fake_run_ps)

    assert process_tree_memory.ps_process_tree(10) == [10, 12, 11, 13]
    assert process_tree_memory.sample_process_tree_rss(
        10
    ) == process_tree_memory.ProcessTreeRssSample(
        rss_self_kb=100,
        rss_tree_kb=1000,
    )


def test_unavailable_ps_returns_null_memory_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(process_tree_memory, "run_ps", lambda _args: None)

    sample = process_tree_memory.sample_process_tree_rss(10)

    assert sample.rss_self_kb is None
    assert sample.rss_tree_kb is None


def test_decorator_attaches_background_peak_memory(monkeypatch) -> None:
    samples = iter(
        [
            process_tree_memory.ProcessTreeRssSample(100, 500),
            process_tree_memory.ProcessTreeRssSample(125, 750),
            process_tree_memory.ProcessTreeRssSample(110, 600),
        ]
    )

    def fake_sample(_root_pid: int) -> process_tree_memory.ProcessTreeRssSample:
        return next(samples, process_tree_memory.ProcessTreeRssSample(110, 600))

    monkeypatch.setattr(process_tree_memory, "sample_process_tree_rss", fake_sample)
    monkeypatch.setattr(process_tree_memory, "SAMPLE_INTERVAL_SECONDS", 0.001)

    @process_tree_memory.attach_peak_process_tree_rss
    def benchmark() -> dict:
        time.sleep(0.01)
        return {"implementation": "example"}

    result = benchmark()

    assert result["memory"]["rss_self_kb"] == 125
    assert result["memory"]["rss_tree_kb"] == 750
    assert result["memory"]["statistic"] == "peak"
    assert result["memory"]["scope"] == "benchmark_process_and_descendants"
    assert result["memory"]["sampling_mode"] == "background_thread_ps"
    assert result["memory"]["available"] is True


def test_matrix_aggregates_memory_and_preserves_unavailable_values() -> None:
    base = {
        "implementation": "rustwright",
        "status": "passed",
        "total_mean_ms": 10.0,
        "cases": {"case": {"mean_ms": 10.0}},
    }
    aggregate = aggregate_repetitions(
        [
            {**base, "memory": {"rss_self_kb": 100, "rss_tree_kb": 500}},
            {**base, "memory": {"rss_self_kb": 120, "rss_tree_kb": None}},
        ]
    )

    memory = aggregate["rustwright"]["memory"]
    assert memory["rss_self_kb"]["median"] == 110
    assert memory["rss_tree_kb"]["median"] == 500

    unavailable = aggregate_repetitions(
        [{**base, "memory": {"rss_self_kb": None, "rss_tree_kb": None}}]
    )
    assert unavailable["rustwright"]["memory"]["rss_tree_kb"]["median"] is None
