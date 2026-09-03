from __future__ import annotations

import argparse
import json
import time

from benchmarks import process_tree_memory
from tools.run_benchmark_matrix import aggregate_repetitions, run_impl


def test_process_tree_walk_and_rss_sum(monkeypatch) -> None:
    process_table = "10 1\n11 10\n12 10\n13 11\n99 1\n"
    rss_by_pid = {10: 100, 11: 200, 12: 300, 13: 400}

    def fake_run_ps(args: list[str]) -> str | None:
        if args == ["ps", "-axo", "pid=,ppid="]:
            return process_table
        return str(rss_by_pid[int(args[-1])])

    monkeypatch.setattr(process_tree_memory, "run_ps", fake_run_ps)
    monkeypatch.setattr(
        process_tree_memory, "procfs_process_tree_rss", lambda _root_pid: None
    )

    assert process_tree_memory.ps_process_tree(10) == [10, 12, 11, 13]
    assert process_tree_memory.sample_process_tree_rss(
        10
    ) == process_tree_memory.ProcessTreeRssSample(
        rss_self_kb=100,
        rss_tree_kb=1000,
    )


def test_procfs_process_tree_walk_and_rss_sum(tmp_path) -> None:
    def write_status(pid: int, parent_pid: int, rss_kb: int | None) -> None:
        process_dir = tmp_path / str(pid)
        process_dir.mkdir(exist_ok=True)
        rss_line = "" if rss_kb is None else f"VmRSS:\t{rss_kb} kB\n"
        (process_dir / "status").write_text(
            f"Name:\tprocess-{pid}\nPPid:\t{parent_pid}\n{rss_line}",
            encoding="utf-8",
        )

    write_status(10, 1, 100)
    write_status(11, 10, 200)
    write_status(12, 10, 300)
    write_status(13, 11, 400)
    write_status(99, 1, 500)

    assert process_tree_memory.procfs_process_tree_rss(
        10, tmp_path
    ) == process_tree_memory.ProcessTreeRssSample(
        rss_self_kb=100,
        rss_tree_kb=1000,
    )

    write_status(13, 11, None)
    assert process_tree_memory.procfs_process_tree_rss(
        10, tmp_path
    ) == process_tree_memory.ProcessTreeRssSample(
        rss_self_kb=100,
        rss_tree_kb=None,
    )

    unreadable = tmp_path / "77"
    unreadable.mkdir()
    assert process_tree_memory.procfs_process_tree_rss(10, tmp_path) is None


def test_unavailable_ps_returns_null_memory_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(process_tree_memory, "run_ps", lambda _args: None)
    monkeypatch.setattr(
        process_tree_memory, "procfs_process_tree_rss", lambda _root_pid: None
    )

    sample = process_tree_memory.sample_process_tree_rss(10)

    assert sample.rss_self_kb is None
    assert sample.rss_tree_kb is None


def test_failed_ps_tree_walk_does_not_report_self_as_tree(monkeypatch) -> None:
    def fake_run_ps(args: list[str]) -> str | None:
        if args == ["ps", "-axo", "pid=,ppid="]:
            return None
        return "100"

    monkeypatch.setattr(process_tree_memory, "run_ps", fake_run_ps)
    monkeypatch.setattr(
        process_tree_memory, "procfs_process_tree_rss", lambda _root_pid: None
    )

    assert process_tree_memory.sample_process_tree_rss(
        10
    ) == process_tree_memory.ProcessTreeRssSample(
        rss_self_kb=100,
        rss_tree_kb=None,
    )


def test_matrix_rejects_latency_result_without_memory(monkeypatch) -> None:
    completed = argparse.Namespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(
        "tools.run_benchmark_matrix.subprocess.run",
        lambda *_args, **_kwargs: completed,
    )
    args = argparse.Namespace(
        lifecycle="warm-browser",
        rebuild_rustwright=False,
        skip_rustwright_rebuild=False,
        iterations=1,
        suite="strict",
        case_filters=[],
        timeout=10,
    )

    invalid_memory_blocks = [
        {"available": False},
        {"available": True},
        {"available": True, "rss_self_kb": 100, "rss_tree_kb": None},
        {"available": True, "rss_self_kb": 100, "rss_tree_kb": True},
        {"available": True, "rss_self_kb": 100, "rss_tree_kb": -1},
        {"available": True, "rss_self_kb": 500, "rss_tree_kb": 100},
    ]
    for memory in invalid_memory_blocks:
        completed.stdout = json.dumps(
            {
                "implementation": "rustwright",
                "memory": memory,
            }
        )
        result = run_impl(args, "rustwright")

        assert result["status"] == "failed"
        assert result["failure_kind"] == "memory_unavailable"
    completed.stdout = json.dumps(
        {
            "implementation": "rustwright",
            "memory": {"available": True, "rss_self_kb": 100, "rss_tree_kb": 500},
        }
    )
    result = run_impl(args, "rustwright")

    assert result["status"] == "passed"
    assert result["container_isolation"] == "separate_container"
    assert result["memory"]["rss_tree_kb"] == 500


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
    assert result["memory"]["sampling_mode"] == "background_thread_procfs_or_ps"
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
