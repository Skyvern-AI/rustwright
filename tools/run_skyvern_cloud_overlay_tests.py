#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUSTWRIGHT_PYTHON = ROOT / "python"
DEFAULT_SOURCE = Path("~/Development/Skyvern-cloud").expanduser()
DEFAULT_TARGETS = [
    "tests/unit/test_cdp_connection_retry.py",
    "tests/unit/test_selector_retry.py",
    "tests/unit/test_upload_file_wait.py",
    "tests/unit/test_navigation_recovery.py",
]
ALIAS_ROOTS = ("playwright", "patchright", "cloakbrowser", "rustwright")
ALIAS_MODULES = [
    "playwright.async_api",
    "playwright.sync_api",
    "playwright._impl._errors",
    "patchright.async_api",
    "patchright.sync_api",
    "patchright._impl._errors",
    "cloakbrowser",
]
BACKEND_MARKER_MODULES = [
    "playwright.async_api",
    "playwright.sync_api",
    "patchright.async_api",
    "patchright.sync_api",
]
EXPECTED_BACKEND_MARKER = {
    "implementation": "rustwright",
    "package": "rustwright",
    "replacement_backend": True,
    "runtime": "rust-pyo3-extension",
    "runtime_module": "rustwright._rustwright",
    "transport": "raw-cdp",
    "cdp_first": True,
    "python_playwright_driver": False,
    "playwright_driver": "none",
}


def default_python_for_source(source: Path) -> Path:
    candidate = source / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def append_pythonpath(env: dict[str, str], paths: list[Path]) -> None:
    existing = env.get("PYTHONPATH", "")
    values = [str(path) for path in paths]
    if existing:
        values.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(values)


def tail(text: str, limit: int = 12000) -> str:
    return text[-limit:]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def missing_module_name(text: str) -> str | None:
    match = re.search(r"No module named ['\"]([^'\"]+)['\"]", text)
    if not match:
        return None
    return match.group(1)


def pytest_summary_counts(output: str) -> dict[str, int]:
    summary_line = ""
    for line in reversed(output.splitlines()):
        if re.search(r"\b\d+\s+(?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b", line) and re.search(
            r"\bin\s+\d", line
        ):
            summary_line = line
            break
    if not summary_line:
        return {}

    summary: dict[str, int] = {}
    for match in re.finditer(
        r"\b(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b",
        summary_line,
    ):
        count = int(match.group(1))
        kind = "error" if match.group(2) == "errors" else match.group(2)
        summary[kind] = count
    return summary


def classify_failure(output: str, returncode: int) -> dict[str, Any]:
    if returncode == 0:
        return {"classification": "passed"}

    missing = missing_module_name(output)
    missing_root = missing.split(".", 1)[0] if missing else None
    if missing_root in ALIAS_ROOTS:
        return {
            "classification": "alias_dependency",
            "missing_module": missing,
            "detail": f"Missing alias module {missing}",
        }
    if missing is not None:
        return {
            "classification": "environment_dependency",
            "missing_module": missing,
            "detail": f"Missing non-alias module {missing}",
        }

    alias_import_patterns = [
        r"cannot import name .* from ['\"](?:playwright|patchright|cloakbrowser|rustwright)",
        r"ImportError: .*(?:playwright|patchright|cloakbrowser|rustwright)",
        r"AttributeError: .*(?:playwright|patchright|cloakbrowser|rustwright)",
    ]
    if any(re.search(pattern, output) for pattern in alias_import_patterns):
        return {
            "classification": "alias_related",
            "detail": "Pytest output contains an alias-related import or attribute failure",
        }

    if "ImportError while loading conftest" in output:
        return {
            "classification": "collection_blocked",
            "detail": "Pytest conftest import failed before selected tests ran",
        }
    if returncode == 5:
        return {
            "classification": "no_tests_collected",
            "detail": "Pytest collected no tests for the selected targets",
        }
    return {
        "classification": "pytest_failed",
        "detail": f"Pytest exited with return code {returncode}",
    }


def alias_preflight_program() -> str:
    return f"""
from __future__ import annotations
import importlib
import json
import pathlib
import sys
import rustwright

rustwright.enable_playwright_compat()

modules = {json.dumps(ALIAS_MODULES)}
backend_marker_modules = set({json.dumps(BACKEND_MARKER_MODULES)})
expected_backend_marker = {EXPECTED_BACKEND_MARKER!r}
required_symbols = {{
    "playwright.async_api": ["async_playwright", "backend_marker", "Page", "Error", "TimeoutError"],
    "playwright.sync_api": ["sync_playwright", "backend_marker", "Page", "Error", "TimeoutError"],
    "playwright._impl._errors": ["Error", "TargetClosedError", "TimeoutError"],
    "patchright.async_api": ["async_playwright", "backend_marker", "Page", "Error", "TimeoutError"],
    "patchright.sync_api": ["sync_playwright", "backend_marker", "Page", "Error", "TimeoutError"],
    "patchright._impl._errors": ["Error", "TargetClosedError", "TimeoutError"],
    "cloakbrowser": ["launch_async", "launch_persistent_context_async"],
}}

def check_backend_marker(module, name):
    marker_fn = getattr(module, "backend_marker", None)
    if not callable(marker_fn):
        return {{
            "status": "missing",
            "failures": ["backend_marker is missing or is not callable"],
            "marker": None,
        }}
    try:
        marker = marker_fn(name)
    except Exception as exc:
        return {{
            "status": "error",
            "error": f"{{type(exc).__name__}}: {{exc}}",
            "failures": ["backend_marker raised an exception"],
            "marker": None,
        }}
    if not isinstance(marker, dict):
        return {{
            "status": "invalid",
            "failures": [f"backend_marker returned {{type(marker).__name__}}, expected dict"],
            "marker": marker,
        }}
    failures = []
    for key, expected in expected_backend_marker.items():
        actual = marker.get(key)
        if actual != expected:
            failures.append(f"{{key}}={{actual!r}}, expected {{expected!r}}")
    if marker.get("api_module") != name:
        failures.append(f"api_module={{marker.get('api_module')!r}}, expected {{name!r}}")
    expected_api_package = name.split(".", 1)[0]
    if marker.get("api_package") != expected_api_package:
        failures.append(f"api_package={{marker.get('api_package')!r}}, expected {{expected_api_package!r}}")
    return {{
        "status": "ok" if not failures else "invalid",
        "failures": failures,
        "marker": marker,
    }}

results = []
for name in modules:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        results.append({{"module": name, "status": "import_error", "error": f"{{type(exc).__name__}}: {{exc}}"}})
        continue
    path = getattr(module, "__file__", None)
    missing = [symbol for symbol in required_symbols.get(name, []) if not hasattr(module, symbol)]
    backend_marker = check_backend_marker(module, name) if name in backend_marker_modules else None
    status = "missing_symbols" if missing else "ok"
    if backend_marker is not None and backend_marker["status"] != "ok" and not missing:
        status = "invalid_backend_marker"
    results.append({{
        "module": name,
        "status": status,
        "file": str(pathlib.Path(path).resolve()) if path else None,
        "missing_symbols": missing,
        "backend_marker": backend_marker,
    }})
failed = [item for item in results if item["status"] != "ok"]
print(json.dumps({{
    "status": "ok" if not failed else "failed",
    "python": sys.executable,
    "sys_path_head": sys.path[:8],
    "modules": results,
    "failures": failed,
}}, sort_keys=True))
raise SystemExit(0 if not failed else 1)
"""


def run_alias_preflight(python: Path, source: Path, env: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        [str(python), "-c", alias_preflight_program()],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {
            "status": "output_error",
            "stdout_tail": tail(result.stdout),
            "stderr_tail": tail(result.stderr),
        }
    report["returncode"] = result.returncode
    if result.stderr:
        report["stderr_tail"] = tail(result.stderr)
    return report


def run_pytest(
    *,
    python: Path,
    source: Path,
    env: dict[str, str],
    targets: list[str],
    pytest_args: list[str],
    timeout: int,
) -> dict[str, Any]:
    program = (
        "import sys\n"
        "import pytest\n"
        "import rustwright\n"
        "rustwright.enable_playwright_compat()\n"
        "raise SystemExit(pytest.main(sys.argv[1:]))\n"
    )
    command = [str(python), "-c", program, *pytest_args, *targets]
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=source,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        output = stdout + stderr
        return {
            "status": "timeout",
            "returncode": 124,
            "classification": "timeout",
            "command": command,
            "elapsed_ms": elapsed_ms,
            "stdout_tail": tail(stdout),
            "stderr_tail": tail(stderr),
            "combined_output_tail": tail(output),
        }
    elapsed_ms = (time.perf_counter() - started) * 1000
    output = result.stdout + result.stderr
    classification = classify_failure(output, result.returncode)
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "classification": classification["classification"],
        "failure_detail": classification.get("detail"),
        "missing_module": classification.get("missing_module"),
        "summary": pytest_summary_counts(output),
        "timed_out": timed_out,
        "command": command,
        "elapsed_ms": elapsed_ms,
        "stdout_tail": tail(result.stdout),
        "stderr_tail": tail(result.stderr),
        "combined_output_tail": tail(output),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    started_at = utc_now()
    started = time.perf_counter()
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"source directory not found: {source}")
    python = Path(args.python).expanduser() if args.python else default_python_for_source(source)
    if not python.exists():
        raise SystemExit(f"python executable not found: {python}")
    targets = args.target or list(DEFAULT_TARGETS)
    pytest_args = [] if args.no_quiet else ["-q"]
    pytest_args.extend(args.pytest_arg or [])

    env = os.environ.copy()
    append_pythonpath(env, [RUSTWRIGHT_PYTHON, ROOT, source])

    preflight = run_alias_preflight(python, source, env)
    if preflight.get("status") == "ok":
        pytest_report = run_pytest(
            python=python,
            source=source,
            env=env,
            targets=targets,
            pytest_args=pytest_args,
            timeout=max(args.timeout, 1),
        )
    else:
        pytest_report = {
            "status": "skipped",
            "classification": "alias_preflight_failed",
            "returncode": None,
            "summary": {},
        }

    status = "passed"
    if preflight.get("status") != "ok":
        status = "alias_preflight_failed"
    elif pytest_report["status"] == "passed":
        status = "passed"
    elif pytest_report["classification"] in {"alias_dependency", "alias_related"}:
        status = "alias_failed"
    elif pytest_report["classification"] == "environment_dependency":
        status = "environment_blocked"
    else:
        status = pytest_report["classification"]

    duration_seconds = round(time.perf_counter() - started, 3)
    return {
        "schema_version": 1,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": duration_seconds,
        "source_path": str(source),
        "python": str(python),
        "python_resolved": str(python.resolve()),
        "rustwright_python_path": str(RUSTWRIGHT_PYTHON.resolve()),
        "targets": targets,
        "pytest_args": pytest_args,
        "allow_environment_blockers": args.allow_environment_blockers,
        "preflight": preflight,
        "pytest": pytest_report,
    }


def exit_code_for_report(report: dict[str, Any]) -> int:
    if report["status"] == "passed":
        return 0
    if report["status"] == "environment_blocked" and report.get("allow_environment_blockers"):
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run selected Skyvern Cloud pytest targets with Rustwright alias modules first on PYTHONPATH."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Skyvern-cloud checkout path.")
    parser.add_argument("--python", help="Python executable to use; defaults to <source>/.venv/bin/python.")
    parser.add_argument("--target", action="append", help="Pytest target. May be repeated; defaults to a fake-CDP unit slice.")
    parser.add_argument("--pytest-arg", action="append", help="Extra pytest arg. May be repeated.")
    parser.add_argument("--no-quiet", action="store_true", help="Do not add -q to pytest args.")
    parser.add_argument("--timeout", type=int, default=180, help="Pytest timeout in seconds.")
    parser.add_argument(
        "--allow-environment-blockers",
        action="store_true",
        help="Exit 0 when pytest is blocked by a missing non-alias Skyvern dependency.",
    )
    parser.add_argument("--output", help="Optional JSON output path.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    report = build_report(args)
    text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return exit_code_for_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
