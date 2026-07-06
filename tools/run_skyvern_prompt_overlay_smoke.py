#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from run_skyvern_alias_command import (  # noqa: E402
    RUSTWRIGHT_PYTHON,
    append_pythonpath,
    classify_failure,
    default_python_for_source,
    run_alias_preflight,
    tail,
)


DEFAULT_SOURCE = Path("~/Development/Skyvern-cloud").expanduser()
DEFAULT_MODULES = [
    "prompt_evaluation.extract_action.scripts.extract_action",
    "prompt_evaluation.check_user_goal.scripts.check_user_goal",
    "prompt_evaluation.parse_input_or_select.scripts.parse_input_or_select",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def import_program(module: str) -> str:
    return f"""
from __future__ import annotations
import importlib
import json
import pathlib
import rustwright

rustwright.enable_playwright_compat()

module = importlib.import_module({module!r})
path = getattr(module, "__file__", None)
print(json.dumps({{
    "module": {module!r},
    "file": str(pathlib.Path(path).resolve()) if path else None,
}}, sort_keys=True))
"""


def run_module_import(module: str, python: Path, source: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    command = [str(python), "-c", import_program(module)]
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
        return {
            "module": module,
            "status": "failed",
            "classification": "timeout",
            "returncode": 124,
            "elapsed_ms": elapsed_ms,
            "stdout_tail": tail(stdout),
            "stderr_tail": tail(stderr),
            "combined_output_tail": tail(stdout + stderr),
        }

    elapsed_ms = (time.perf_counter() - started) * 1000
    output = result.stdout + result.stderr
    classification = classify_failure(output, result.returncode)
    imported: dict[str, Any] | None = None
    if result.returncode == 0:
        try:
            imported = json.loads(result.stdout)
        except json.JSONDecodeError:
            imported = None
    return {
        "module": module,
        "status": "passed" if result.returncode == 0 else "failed",
        "classification": classification["classification"],
        "failure_detail": classification.get("detail"),
        "missing_module": classification.get("missing_module"),
        "returncode": result.returncode,
        "elapsed_ms": elapsed_ms,
        "imported": imported,
        "stdout_tail": tail(result.stdout),
        "stderr_tail": tail(result.stderr),
        "combined_output_tail": tail(output),
    }


def derive_status(preflight: dict[str, Any], modules: list[dict[str, Any]]) -> str:
    if preflight.get("status") != "ok":
        return "alias_preflight_failed"
    classifications = {item.get("classification") for item in modules if item.get("status") != "passed"}
    if not classifications:
        return "passed"
    if classifications & {"alias_dependency", "alias_related"}:
        return "alias_failed"
    if classifications <= {"environment_dependency"}:
        return "environment_blocked"
    if classifications <= {"timeout"}:
        return "timeout"
    return "failed"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    started_at = utc_now()
    started = time.perf_counter()
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"source directory not found: {source}")
    python = Path(args.python).expanduser() if args.python else default_python_for_source(source)
    if not python.exists():
        raise SystemExit(f"python executable not found: {python}")
    modules = args.module or list(DEFAULT_MODULES)

    env = os.environ.copy()
    append_pythonpath(env, [RUSTWRIGHT_PYTHON, ROOT, source])
    preflight = run_alias_preflight(python, source, env)
    if preflight.get("status") == "ok":
        module_results = [
            run_module_import(module, python, source, env, max(args.timeout, 1))
            for module in modules
        ]
    else:
        module_results = [
            {
                "module": module,
                "status": "skipped",
                "classification": "alias_preflight_failed",
                "returncode": None,
            }
            for module in modules
        ]
    status = derive_status(preflight, module_results)
    return {
        "schema_version": 1,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "source_path": str(source),
        "python": str(python),
        "python_resolved": str(python.resolve()),
        "rustwright_python_path": str(RUSTWRIGHT_PYTHON.resolve()),
        "allow_environment_blockers": args.allow_environment_blockers,
        "modules": modules,
        "preflight": preflight,
        "module_results": module_results,
    }


def exit_code_for_report(report: dict[str, Any]) -> int:
    if report["status"] == "passed":
        return 0
    if report["status"] == "environment_blocked" and report.get("allow_environment_blockers"):
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import Skyvern prompt-evaluation entrypoints under the Rustwright alias overlay."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Skyvern-cloud checkout path.")
    parser.add_argument("--python", help="Python executable to use; defaults to <source>/.venv/bin/python.")
    parser.add_argument("--module", action="append", help="Prompt-evaluation module to import. May be repeated.")
    parser.add_argument("--timeout", type=int, default=180, help="Per-module import timeout in seconds.")
    parser.add_argument(
        "--allow-environment-blockers",
        action="store_true",
        help="Exit 0 when prompt modules are blocked only by missing non-alias Skyvern dependencies.",
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
