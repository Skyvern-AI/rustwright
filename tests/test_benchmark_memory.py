from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from benchmarks import process_tree_memory
from rustwright.sync_api import Page, Request, Response, _LocalEventContextManager
from tools.run_benchmark_matrix import (
    aggregate_repetitions,
    main as benchmark_matrix_main,
    markdown_table,
    run_impl,
)


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
    # The synthetic samples intentionally omit PSS. Exercise the portable
    # RSS-only contract instead of making this fixture Linux-specific.
    monkeypatch.setattr(process_tree_memory.sys, "platform", "darwin")

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
    assert memory["pss_by_role_statistic"] == "independent_marginal_medians"

    unavailable = aggregate_repetitions(
        [{**base, "memory": {"rss_self_kb": None, "rss_tree_kb": None}}]
    )
    assert unavailable["rustwright"]["memory"]["rss_tree_kb"]["median"] is None


def test_matrix_does_not_publish_partial_repetition_aggregates() -> None:
    passed = {
        "implementation": "rustwright",
        "status": "passed",
        "total_mean_ms": 10.0,
        "memory": {"rss_self_kb": 100, "rss_tree_kb": 500},
        "cases": {"case": {"mean_ms": 10.0}},
    }
    failed = {
        "implementation": "rustwright",
        "status": "failed",
        "failure_kind": "memory_unavailable",
        "repetition": 2,
    }
    results = [
        {**passed, "repetition": 1},
        failed,
        {**passed, "repetition": 3, "total_mean_ms": 12.0},
    ]
    assert aggregate_repetitions(results, planned_repetitions=3) == {}
    diagnostic = aggregate_repetitions(results)
    assert diagnostic["rustwright"]["runs"] == 2

    rendered = markdown_table(
        {
            "implementations": ["rustwright"],
            "implementation_status": {"rustwright": "failed"},
            "aggregate": {},
            "diagnostic_aggregate": diagnostic,
            "speedups": {},
            "results": results,
        }
    )
    assert "| rustwright | failed |" in rendered
    assert "Noncanonical diagnostic aggregates" in rendered


def test_process_role_classifier_handles_exact_browser_and_non_browser_tokens() -> None:
    assert process_tree_memory.classify_process_role(
        b"/usr/bin/python3\x00-m\x00benchmarks.run_benchmarks"
    ) == "python-host"
    assert process_tree_memory.classify_process_role(
        b"/usr/bin/python3\x00--type=renderer"
    ) == "python-host"
    assert process_tree_memory.classify_process_role(
        b"/usr/bin/chrome\x00--type=renderer\x00--renderer-client-id=4"
    ) == "renderer"
    assert process_tree_memory.classify_process_role(
        b"/usr/bin/chrome\x00--type=utility\x00--utility-sub-type=network.mojom.NetworkService"
    ) == "network"
    assert process_tree_memory.classify_process_role(
        "/usr/bin/chrome --enable-features=NetworkServiceInProcess"
    ) == "browser"
    assert process_tree_memory.classify_process_role(
        "/usr/bin/chrome --type=utility --utility-sub-type=storage.mojom.StorageService"
    ) == "utility"
    assert process_tree_memory.classify_process_role(
        "/usr/bin/chrome --type=utility --utility-sub-type=fooNetworkService"
    ) == "utility"
    assert process_tree_memory.classify_process_role("/usr/bin/chrome --type=gpu-process") == "gpu"
    assert process_tree_memory.classify_process_role("/usr/bin/chrome --type=gpu") == "gpu"
    assert process_tree_memory.classify_process_role(
        "/usr/bin/chrome --type=renderer"
    ) == "renderer"
    assert process_tree_memory.classify_process_role("/usr/bin/chrome") == "browser"
    assert process_tree_memory.classify_process_role("/usr/bin/headless_shell") == "browser"
    assert process_tree_memory.classify_process_role("/usr/bin/node driver.js") == "node-driver"
    assert process_tree_memory.classify_process_role("/usr/bin/node --type=renderer") == "node-driver"
    assert process_tree_memory.classify_process_role("/usr/bin/nodejs driver.js") == "node-driver"
    assert process_tree_memory.classify_process_role("/usr/bin/helper --type=network") == "network"
    assert process_tree_memory.classify_process_role("/usr/bin/sleep 1") == "other"


def test_procfs_process_tree_pss_aggregates_roles_and_rejects_partial_reads(tmp_path) -> None:
    processes = {
        10: ("/usr/bin/python3", 100),
        11: ("/usr/bin/chrome", 200),
        12: ("/usr/bin/chrome", 300),
    }
    for pid, (executable, pss_kb) in processes.items():
        process_dir = tmp_path / str(pid)
        process_dir.mkdir()
        (process_dir / "cmdline").write_bytes(
            f"{executable}\x00--type=renderer\x00".encode()
            if pid == 12
            else executable.encode()
        )
        (process_dir / "smaps_rollup").write_text(f"Pss:\t{pss_kb} kB\n", encoding="utf-8")

    total, by_role = process_tree_memory.procfs_process_tree_pss([10, 11, 12], tmp_path)
    assert total == 600
    assert by_role["python-host"] == 100
    assert by_role["browser"] == 200
    assert by_role["renderer"] == 300
    assert sum(by_role.values()) == total

    (tmp_path / "12" / "smaps_rollup").write_text("Pss:\t-1 kB\n", encoding="utf-8")
    assert process_tree_memory.procfs_process_tree_pss([10, 11, 12], tmp_path) is None


def test_pss_validity_is_fail_closed_at_tree_rss_peak(monkeypatch) -> None:
    monkeypatch.setattr(process_tree_memory.sys, "platform", "linux")
    sampler = process_tree_memory.ProcessTreeRssSampler(1)
    sampler._append(
        process_tree_memory.ProcessTreeRssSample(
            100,
            500,
            40,
            {"browser": 40},
        )
    )
    sampler._append(
        process_tree_memory.ProcessTreeRssSample(
            110,
            600,
            None,
            None,
        )
    )
    summary = sampler.summary()
    assert summary["pss_statistic"] == "at_tree_rss_peak"
    assert summary["pss_tree_kb"] is None
    assert summary["pss_available"] is False
    assert summary["available"] is False


def test_non_linux_summary_accepts_rss_with_empty_pss_schema(monkeypatch) -> None:
    monkeypatch.setattr(process_tree_memory.sys, "platform", "darwin")
    sampler = process_tree_memory.ProcessTreeRssSampler(1, sample_interval=0.05)
    sampler._append(process_tree_memory.ProcessTreeRssSample(100, 500))

    assert sampler.summary() == {
        "rss_self_kb": 100,
        "rss_tree_kb": 500,
        "pss_tree_kb": None,
        "pss_by_role_kb": None,
        "pss_required": False,
        "pss_available": False,
        "pss_statistic": "at_tree_rss_peak",
        "pss_scope": "benchmark_process_and_descendants",
        "samples_collected": 1,
        "sampling_interval_ms": 50.0,
        "statistic": "peak",
        "scope": "benchmark_process_and_descendants",
        "sampling_mode": "background_thread_procfs_or_ps",
        "available": True,
    }


def _memory_test_page() -> Page:
    page = Page.__new__(Page)
    page._navigation_responses = []
    page._network_history_lock = threading.RLock()
    page._request_log = []
    page._request_log_sequences = []
    page._next_request_log_sequence = 0
    page._request_log_keys = set()
    page._request_log_generation = 0
    page._request_history_pending_keys = set()
    page._requests_by_key = {}
    page._response_log = []
    page._response_log_sequences = []
    page._next_response_log_sequence = 0
    page._response_log_keys = set()
    page._request_log_condition = threading.Condition(page._network_history_lock)
    page._fulfilled_route_bodies = {}
    return page

def test_navigation_response_ring_caps_count_and_bytes() -> None:
    page = Page.__new__(Page)
    page._navigation_responses = []
    responses = [
        Response(url=f"https://example.test/{index}", _body_cache=b"x" * (512 * 1024))
        for index in range(20)
    ]
    for response in responses:
        page._remember_navigation_response(response)

    assert len(page._navigation_responses) == 16
    assert page._navigation_responses[0] is responses[4]
    assert responses[0] not in page._navigation_responses
    assert responses[0]._body_cache == b"x" * (512 * 1024)
    assert sum(
        len(response._body_cache)
        for response in page._navigation_responses
        if response._body_cache is not None
    ) <= 8 * 1024 * 1024


def test_navigation_response_eviction_releases_page_owned_graph_without_clearing_body() -> None:
    page = _memory_test_page()
    body_size = 512 * 1024
    held_response = None
    held_request = None
    for index in range(25):
        request_id = f"request-{index}"
        url = f"https://example.test/{index}"
        body = (f"body-{index}".encode() * body_size)[:body_size]
        request = Request(
            url=url,
            method="GET",
            resource_type="document",
            request_id=request_id,
            _page=page,
        )
        response = Response(
            url=url,
            status=200,
            request_id=request_id,
            request=request,
            _page=page,
            _body_cache=body,
        )
        page._fulfilled_route_bodies[request_id] = body
        page._remember_navigation_response(response)
        if index == 0:
            held_response = response
            held_request = request

    page_owned_responses = {
        id(response): response
        for response in [*page._navigation_responses, *page._response_log]
    }
    page_owned_bodies = {
        id(response._body_cache): response._body_cache
        for response in page_owned_responses.values()
        if response._body_cache is not None
    }
    page_owned_bodies.update(
        {id(body): body for body in page._fulfilled_route_bodies.values()}
    )

    assert len(page._navigation_responses) == 16
    assert sum(len(body) for body in page_owned_bodies.values()) <= 8 * 1024 * 1024
    assert held_response is not None
    assert held_request is not None
    assert held_response not in page._navigation_responses
    assert held_response not in page._response_log
    assert held_request not in page._request_log
    assert all(key[0] != "request-0" for key in page._requests_by_key)
    assert "request-0" not in page._fulfilled_route_bodies
    assert held_response.body() == (b"body-0" * body_size)[:body_size]

def test_fulfilled_route_body_staging_is_bounded_without_response_caches() -> None:
    page = _memory_test_page()
    body_size = 1024 * 1024
    for index in range(100):
        body = bytes([index]) * body_size
        page._remember_fulfilled_route_body(f"fulfilled-{index}", body)

    page_owned_bodies = page._page_owned_bodies_locked()
    assert sum(len(body) for body in page_owned_bodies.values()) <= 8 * 1024 * 1024
    assert len(page._fulfilled_route_bodies) <= 8
    assert "fulfilled-0" not in page._fulfilled_route_bodies
    assert "fulfilled-99" in page._fulfilled_route_bodies


def test_fulfilled_route_body_moves_into_recorded_response() -> None:
    page = _memory_test_page()
    body = b"fulfilled-body"
    response = Response(
        url="https://example.test/fulfilled",
        status=200,
        request_id="fulfilled-request",
        _page=page,
    )
    page._remember_fulfilled_route_body("fulfilled-request", body)
    page._remember_navigation_response(response)

    assert response._body_cache is body
    assert "fulfilled-request" not in page._fulfilled_route_bodies


def test_network_history_pruning_serializes_event_pump_append() -> None:
    page = _memory_test_page()
    body_size = 512 * 1024
    for index in range(16):
        page._remember_navigation_response(
            Response(
                url=f"https://example.test/{index}",
                status=200,
                request_id=f"request-{index}",
                _page=page,
                _body_cache=b"x" * body_size,
            )
        )

    snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    append_started = threading.Event()
    append_done = threading.Event()
    original_snapshot = page._page_owned_bodies_locked

    def blocked_snapshot() -> dict[int, bytes]:
        if not snapshot_started.is_set():
            snapshot_started.set()
            assert release_snapshot.wait(timeout=1.0)
        return original_snapshot()

    page._page_owned_bodies_locked = blocked_snapshot

    prune_thread = threading.Thread(target=page._prune_navigation_responses)
    prune_thread.start()
    assert snapshot_started.wait(timeout=1.0)

    appended = Response(
        url="https://example.test/concurrent",
        status=200,
        request_id="request-concurrent",
        _page=page,
        _body_cache=b"concurrent",
    )

    def append_response() -> None:
        append_started.set()
        page._remember_navigation_response(appended)
        append_done.set()

    append_thread = threading.Thread(target=append_response)
    append_thread.start()
    assert append_started.wait(timeout=1.0)
    assert not append_done.wait(timeout=0.05)
    release_snapshot.set()
    prune_thread.join(timeout=1.0)
    append_thread.join(timeout=1.0)

    assert not prune_thread.is_alive()
    assert not append_thread.is_alive()
    assert appended in page._navigation_responses
    assert appended in page._response_log


def test_navigation_response_body_cache_prunes_post_hoc() -> None:
    page = _memory_test_page()
    body_size = 512 * 1024
    responses = []
    for index in range(16):
        response = Response(
            url=f"https://example.test/{index}",
            status=200,
            request_id=f"request-{index}",
            _page=page,
            _body_cache=b"x" * body_size,
        )
        responses.append(response)
        page._remember_navigation_response(response)

    late = Response(
        url="https://example.test/late",
        status=200,
        request_id="request-late",
        _page=page,
    )
    page._remember_navigation_response(late)
    late_body = b"late" * (body_size // 4)
    page._default_timeout = 1_000.0
    page._core = SimpleNamespace(
        response_body=lambda _request_id, _timeout: json.dumps(
            {
                "body": base64.b64encode(late_body).decode("ascii"),
                "base64Encoded": True,
            }
        )
    )

    assert late.body() == late_body
    assert responses[0] not in page._navigation_responses
    assert responses[0] not in page._response_log
    assert responses[0].body() == b"x" * body_size
    assert sum(
        len(response._body_cache)
        for response in page._navigation_responses
        if response._body_cache is not None
    ) <= 8 * 1024 * 1024


def test_response_waiter_cursor_survives_pruning_during_handler_registration() -> None:
    page = _memory_test_page()
    body_size = 512 * 1024
    for index in range(16):
        page._remember_navigation_response(
            Response(
                url=f"https://example.test/{index}",
                status=200,
                request_id=f"request-{index}",
                _page=page,
                _body_cache=b"x" * body_size,
            )
        )

    class Context:
        _default_timeout = 1_000.0

        def __init__(self) -> None:
            self._pages = [page]
            self._handlers = {}

        def on(self, event: str, handler) -> None:
            if event == "response":
                page._remember_navigation_response(
                    Response(
                        url="https://example.test/matching",
                        status=200,
                        request_id="request-matching",
                        _page=page,
                        _body_cache=b"matching",
                    )
                )
            self._handlers[event] = handler

        def remove_listener(self, event: str, handler) -> None:
            if self._handlers.get(event) is handler:
                self._handlers.pop(event, None)

    target = Context()
    with _LocalEventContextManager(
        target,
        "response",
        lambda response: response.url.endswith("/matching"),
        timeout=1_000.0,
    ) as waiter:
        pass

    assert waiter.value.url == "https://example.test/matching"


@pytest.mark.parametrize("repetitions", ["0", "-1"])
def test_benchmark_matrix_rejects_non_positive_repetitions_at_parse_time(
    monkeypatch, repetitions: str
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_benchmark_matrix.py", "--repetitions", repetitions],
    )
    with pytest.raises(SystemExit) as error:
        benchmark_matrix_main()
    assert error.value.code == 2

def test_matrix_rejects_invalid_or_partial_pss(monkeypatch) -> None:
    completed = argparse.Namespace(
        returncode=0,
        stdout=json.dumps(
            {
                "implementation": "rustwright",
                "memory": {
                    "available": True,
                    "rss_self_kb": 100,
                    "rss_tree_kb": 500,
                    "pss_required": True,
                    "pss_available": True,
                    "pss_tree_kb": 10,
                    "pss_by_role_kb": {"browser": 10},
                },
            }
        ),
        stderr="",
    )
    monkeypatch.setattr("tools.run_benchmark_matrix.subprocess.run", lambda *_args, **_kwargs: completed)
    args = argparse.Namespace(
        lifecycle="warm-browser",
        rebuild_rustwright=False,
        skip_rustwright_rebuild=False,
        iterations=1,
        suite="strict",
        case_filters=[],
        timeout=10,
    )
    result = run_impl(args, "rustwright")
    assert result["status"] == "failed"
    assert result["failure_kind"] == "memory_unavailable"
