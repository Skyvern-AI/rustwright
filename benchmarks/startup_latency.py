#!/usr/bin/env python3
"""Measure one Rustwright cold-start sample in one Python process."""

from __future__ import annotations

import importlib
import sys
import time


ENTRYPOINT = "python-sync"
MEASURED_PHASES = (
    "python_import",
    "manager_factory",
    "api_startup",
    "chromium_facade_first_access",
    "browser_launch",
    "first_page",
    "first_page_probe",
    "close",
)


class UsageError(ValueError):
    pass


def emit_record(record: dict[str, object]) -> None:
    import json

    print(json.dumps(record, sort_keys=True, separators=(",", ":")))


def parse_args() -> tuple[str | None, bool]:
    arguments = sys.argv[1:]
    if arguments in (["-h"], ["--help"]):
        return None, True
    if not arguments:
        return None, False
    if len(arguments) == 2 and arguments[0] == "--browser-path" and arguments[1]:
        return arguments[1], False
    if len(arguments) == 1 and arguments[0].startswith("--browser-path="):
        value = arguments[0].split("=", 1)[1]
        if value:
            return value, False
    raise UsageError("usage: startup_latency.py [--browser-path PATH]")


def package_version() -> str | None:
    from importlib import metadata

    try:
        return metadata.version("rustwright")
    except Exception:
        return None


def requested_browser_path(explicit_path: str | None) -> str | None:
    import os

    if explicit_path:
        return explicit_path
    for name in ("RUSTWRIGHT_CHROMIUM", "CHROME", "CHROMIUM"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def read_core_probe(path_value: str | None) -> dict[str, object]:
    import json
    import os
    from pathlib import Path

    if path_value is None:
        return {"status": "disabled", "records": []}
    if path_value == "-":
        return {"status": "stderr", "records": []}

    path = Path(path_value)
    if not path.is_file():
        return {"status": "absent", "records": []}

    records: list[dict[str, object]] = []
    invalid_lines = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                invalid_lines += 1
                continue
            if isinstance(value, dict) and value.get("pid") == os.getpid():
                records.append(value)
    except (OSError, UnicodeError) as exc:
        return {
            "status": "unreadable",
            "records": [],
            "error_type": type(exc).__name__,
        }

    status = "present" if records else "absent"
    result: dict[str, object] = {"status": status, "records": records}
    if invalid_lines:
        result["invalid_line_count"] = invalid_lines
    return result


def cleanup(page: object, browser: object, playwright: object) -> list[str]:
    errors: list[str] = []
    for name, value, close_name in (
        ("page", page, "close"),
        ("browser", browser, "close"),
        ("manager", playwright, "stop"),
    ):
        if value is None:
            continue
        try:
            getattr(value, close_name)()
        except Exception as exc:  # The error record must survive cleanup failures.
            errors.append(f"{name}:{type(exc).__name__}")
    return errors


def main() -> int:
    try:
        browser_path, show_help = parse_args()
    except UsageError as exc:
        import os

        emit_record(
            {
                "schema_version": 1,
                "status": "error",
                "entrypoint": ENTRYPOINT,
                "pid": os.getpid(),
                "failed_phase": "argument_parse",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "cleanup_errors": [],
                "core_timing_file": os.environ.get("RUSTWRIGHT_STARTUP_TIMING_FILE"),
            }
        )
        return 2
    if show_help:
        print(
            "Run one cold Rustwright launch and print one JSON record.\n"
            "usage: startup_latency.py [--browser-path PATH]\n"
            "Rustwright otherwise uses RUSTWRIGHT_CHROMIUM, CHROME, or CHROMIUM.\n"
            "Set RUSTWRIGHT_CDP_TRANSPORT to websocket or pipe."
        )
        return 0

    epoch_ns = 0
    phases: list[dict[str, object]] = []
    current_phase = MEASURED_PHASES[0]
    rustwright = None
    manager = None
    playwright = None
    chromium = None
    browser = None
    page = None
    probe: dict[str, object] | None = None

    def measure(name: str, operation):
        nonlocal current_phase
        current_phase = name
        start_offset_ns = phases[-1]["end_offset_ns"] if phases else 0
        value = operation()
        end_offset_ns = time.perf_counter_ns() - epoch_ns
        phases.append(
            {
                "name": name,
                "status": "ok",
                "start_offset_ns": start_offset_ns,
                "end_offset_ns": end_offset_ns,
                "duration_ns": end_offset_ns - start_offset_ns,
            }
        )
        return value

    epoch_ns = time.perf_counter_ns()
    try:
        rustwright = measure("python_import", lambda: importlib.import_module("rustwright"))
        manager = measure("manager_factory", rustwright.sync_playwright)
        playwright = measure("api_startup", manager.start)
        chromium = measure("chromium_facade_first_access", lambda: playwright.chromium)

        launch_options: dict[str, object] = {}
        if browser_path:
            launch_options["executable_path"] = browser_path
        browser = measure("browser_launch", lambda: chromium.launch(**launch_options))
        page = measure("first_page", browser.new_page)

        def assert_first_page() -> dict[str, object]:
            observed = {
                "url": page.url,
                "viewport_size": page.viewport_size,
            }
            expected = {
                "url": "about:blank",
                "viewport_size": {"width": 1280, "height": 720},
            }
            if observed != expected:
                raise AssertionError(f"blank-page probe mismatch: {observed!r}")
            return observed

        probe = measure("first_page_probe", assert_first_page)

        def close_all() -> None:
            nonlocal page, browser, playwright
            page.close()
            page = None
            browser.close()
            browser = None
            playwright.stop()
            playwright = None

        measure("close", close_all)
    except Exception as exc:
        import os
        cleanup_errors = cleanup(page, browser, playwright)
        error_record = {
            "schema_version": 1,
            "status": "error",
            "entrypoint": ENTRYPOINT,
            "pid": os.getpid(),
            "failed_phase": current_phase,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "cleanup_errors": cleanup_errors,
            "core_timing_file": os.environ.get("RUSTWRIGHT_STARTUP_TIMING_FILE"),
        }
        emit_record(error_record)
        return 1

    import os
    import platform
    for previous, current in zip(phases, phases[1:]):
        if previous["end_offset_ns"] != current["start_offset_ns"]:
            error_record = {
                "schema_version": 1,
                "status": "error",
                "entrypoint": ENTRYPOINT,
                "pid": os.getpid(),
                "failed_phase": "timing_validation",
                "error_type": "NonContiguousTimingError",
                "error_message": (
                    f"{previous['name']} ended at {previous['end_offset_ns']} ns; "
                    f"{current['name']} started at {current['start_offset_ns']} ns"
                ),
                "cleanup_errors": [],
                "core_timing_file": os.environ.get("RUSTWRIGHT_STARTUP_TIMING_FILE"),
            }
            emit_record(error_record)
            return 1

    first_page_phase = phases[MEASURED_PHASES.index("first_page")]
    total_duration_ns = first_page_phase["end_offset_ns"] - phases[0]["start_offset_ns"]

    core_timing_file = os.environ.get("RUSTWRIGHT_STARTUP_TIMING_FILE")
    result = {
        "schema_version": 1,
        "status": "ok",
        "entrypoint": ENTRYPOINT,
        "pid": os.getpid(),
        "clock": "perf_counter_ns",
        "clock_precision_ns": 1,
        "phases": phases,
        "derived": {
            "cold_process_to_first_page": {
                "status": "ok",
                "start_offset_ns": phases[0]["start_offset_ns"],
                "end_offset_ns": first_page_phase["end_offset_ns"],
                "duration_ns": total_duration_ns,
            }
        },
        "probe": probe,
        "python_version": platform.python_version(),
        "browser_version": os.environ.get("RUSTWRIGHT_BROWSER_VERSION"),
        "library_version": package_version(),
        "browser_path": requested_browser_path(browser_path),
        "transport": os.environ.get("RUSTWRIGHT_CDP_TRANSPORT") or "websocket",
        "core_timing_file": core_timing_file,
        "core_probe": read_core_probe(core_timing_file),
    }
    emit_record(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
