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
REQUIRED_ALIAS_SYMBOLS = {
    "playwright.async_api": [
        "Error",
        "TimeoutError",
        "async_playwright",
        "Browser",
        "BrowserContext",
        "Frame",
        "Locator",
        "Page",
        "Route",
    ],
    "playwright.sync_api": [
        "Error",
        "TimeoutError",
        "sync_playwright",
        "Browser",
        "BrowserContext",
        "Frame",
        "Locator",
        "Page",
        "Route",
    ],
    "playwright._impl._errors": ["Error", "TargetClosedError", "TimeoutError"],
    "patchright.async_api": ["Error", "TimeoutError", "async_playwright", "Page"],
    "patchright.sync_api": ["Error", "TimeoutError", "sync_playwright", "Page"],
    "patchright._impl._errors": ["Error", "TargetClosedError", "TimeoutError"],
    "cloakbrowser": ["ensure_binary", "launch_async", "launch_persistent_context_async"],
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


def classify_failure(output: str, returncode: int) -> dict[str, Any]:
    if returncode == 0:
        return {"classification": "passed"}
    if returncode == 124:
        return {"classification": "timeout", "detail": "Command timed out"}

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
            "detail": "Command output contains an alias-related import or attribute failure",
        }

    return {
        "classification": "command_failed",
        "detail": f"Command exited with return code {returncode}",
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
required_symbols = {json.dumps(REQUIRED_ALIAS_SYMBOLS, sort_keys=True)}
results = []
for name in modules:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        results.append({{"module": name, "status": "import_error", "error": f"{{type(exc).__name__}}: {{exc}}"}})
        continue
    path = getattr(module, "__file__", None)
    missing = [symbol for symbol in required_symbols.get(name, []) if not hasattr(module, symbol)]
    results.append({{
        "module": name,
        "status": "missing_symbols" if missing else "ok",
        "file": str(pathlib.Path(path).resolve()) if path else None,
        "missing_symbols": missing,
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


def normalized_command(raw: list[str]) -> list[str]:
    command = list(raw)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("command is required after --")
    return command


def run_command(command: list[str], source: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
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
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        output = stdout + stderr
        return {
            "status": "timeout",
            "returncode": 124,
            "classification": "timeout",
            "failure_detail": "Command timed out",
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
    command = normalized_command(args.command)

    env = os.environ.copy()
    append_pythonpath(env, [RUSTWRIGHT_PYTHON, ROOT, source])

    if args.no_preflight:
        preflight = {"status": "skipped"}
    else:
        preflight = run_alias_preflight(python, source, env)

    if preflight.get("status") == "ok" or args.no_preflight:
        command_result = run_command(command, source, env, max(args.timeout, 1))
    else:
        command_result = {
            "status": "skipped",
            "returncode": None,
            "classification": "alias_preflight_failed",
        }

    status = "passed"
    if preflight.get("status") not in {"ok", "skipped"}:
        status = "alias_preflight_failed"
    elif command_result["status"] == "passed":
        status = "passed"
    elif command_result["classification"] in {"alias_dependency", "alias_related"}:
        status = "alias_failed"
    elif command_result["classification"] == "environment_dependency":
        status = "environment_blocked"
    else:
        status = command_result["classification"]

    return {
        "schema_version": 1,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "source_path": str(source),
        "working_directory": str(source),
        "python": str(python),
        "python_resolved": str(python.resolve()),
        "rustwright_python_path": str(RUSTWRIGHT_PYTHON.resolve()),
        "command": command,
        "timeout_seconds": max(args.timeout, 1),
        "allow_environment_blockers": args.allow_environment_blockers,
        "no_preflight": args.no_preflight,
        "preflight": preflight,
        "command_result": command_result,
    }


def exit_code_for_report(report: dict[str, Any]) -> int:
    if report["status"] == "passed":
        return 0
    if report["status"] == "environment_blocked" and report.get("allow_environment_blockers"):
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run any command with Rustwright Playwright/Patchright/Cloakbrowser aliases first on PYTHONPATH."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Skyvern-cloud checkout path.")
    parser.add_argument("--python", help="Python executable for alias preflight; defaults to <source>/.venv/bin/python.")
    parser.add_argument("--timeout", type=int, default=180, help="Command timeout in seconds.")
    parser.add_argument("--no-preflight", action="store_true", help="Skip alias module preflight.")
    parser.add_argument(
        "--allow-environment-blockers",
        action="store_true",
        help="Exit 0 when the command is blocked by a missing non-alias Skyvern dependency.",
    )
    parser.add_argument("--output", help="Optional JSON output path.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --.")
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
