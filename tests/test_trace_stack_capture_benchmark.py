import importlib.util
import json
import sys
from pathlib import Path


_BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "trace_stack_capture.py"
_SPEC = importlib.util.spec_from_file_location("trace_stack_capture", _BENCHMARK_PATH)
assert _SPEC is not None and _SPEC.loader is not None
trace_stack_capture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(trace_stack_capture)


def _metrics():
    return {
        "cpu_ns_per_benchmark_operation": 1.0,
        "benchmark_operations_per_cpu_second": 1.0,
        "tracemalloc_peak_bytes_above_start": 0,
        "tracemalloc_net_retained_bytes_after_gc": 0,
    }


def test_cold_process_measures_before_equivalence(monkeypatch, capsys):
    events = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_stack_capture.py",
            "--mode",
            "cold-process",
            "--depths",
            "8",
            "--captures",
            "1",
            "--repeats",
            "1",
            "--cold-case",
            "acquisition_only/optimized_production/8",
        ],
    )
    monkeypatch.setattr(
        trace_stack_capture,
        "_measure",
        lambda *args: events.append("measure") or _metrics(),
    )
    monkeypatch.setattr(
        trace_stack_capture,
        "_validate_equivalence",
        lambda depths: events.append("equivalence") or {"equivalent": True},
    )
    monkeypatch.setattr(trace_stack_capture, "_identity", lambda: {"identity": "test"})

    trace_stack_capture.main()

    output = json.loads(capsys.readouterr().out)
    assert events == ["measure", "equivalence"]
    assert output["equivalence"] == {"equivalent": True}
    assert output["mode_note"].startswith("fresh-process first helper operation")
    assert "cold import" not in output["mode_note"].lower()


def test_steady_state_validates_before_measurement(monkeypatch, capsys):
    events = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_stack_capture.py",
            "--mode",
            "steady-state",
            "--depths",
            "8",
            "--captures",
            "1",
            "--repeats",
            "1",
        ],
    )
    monkeypatch.setattr(
        trace_stack_capture,
        "_measure",
        lambda *args: events.append("measure") or _metrics(),
    )
    monkeypatch.setattr(
        trace_stack_capture,
        "_validate_equivalence",
        lambda depths: events.append("equivalence") or {"equivalent": True},
    )
    monkeypatch.setattr(trace_stack_capture, "_identity", lambda: {"identity": "test"})

    trace_stack_capture.main()

    capsys.readouterr()
    assert events[0] == "equivalence"
    assert events.count("equivalence") == 1
