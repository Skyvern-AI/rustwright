#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATTERNS = [
    ".benchmark-data/results/bench-full-strict-*.json",
    ".benchmark-data/results/defensible-speed-strict-*.json",
]


def run_json(command: list[str]) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        report = {
            "status": "output_error",
            "accepted": False,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    return proc.returncode, report


def status_from_report(report: dict[str, Any]) -> str:
    status = report.get("status")
    if isinstance(status, str):
        return status
    if report.get("accepted") is True:
        return "accepted"
    return "rejected"


def discover_results(patterns: list[str]) -> list[Path]:
    results: set[Path] = set()
    for pattern in patterns:
        for item in glob.glob(str(ROOT / pattern)):
            path = Path(item)
            if path.is_file():
                results.add(path)
    return sorted(results)


def report_path(output_dir: Path, kind: str, result: Path) -> Path:
    return output_dir / f"{kind}-{result.stem}.json"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def check_result(args: argparse.Namespace, result: Path) -> dict[str, Any]:
    phase2_command = [
        sys.executable,
        str(ROOT / "tools" / "check_phase2_benchmark.py"),
        str(result),
        "--json",
    ]
    phase2_rc, phase2 = run_json(phase2_command)
    phase2_output = report_path(args.output_dir, "phase2", result)
    write_json(phase2_output, phase2)

    launch_command = [
        sys.executable,
        str(ROOT / "tools" / "check_launch_latency_claim.py"),
        "--benchmark-json",
        str(result),
        "--source",
        args.source,
        "--artifact",
        args.artifact,
        "--json",
    ]
    if args.runner:
        launch_command.extend(["--runner", args.runner])
    if args.run_url:
        launch_command.extend(["--run-url", args.run_url])
    launch_rc, launch = run_json(launch_command)
    launch_output = report_path(args.output_dir, "launch", result)
    write_json(launch_output, launch)

    return {
        "result_path": display_path(result),
        "phase2": {
            "returncode": phase2_rc,
            "status": status_from_report(phase2),
            "accepted": bool(phase2.get("accepted")),
            "report_path": display_path(phase2_output),
        },
        "launch": {
            "returncode": launch_rc,
            "status": status_from_report(launch),
            "accepted": bool(launch.get("accepted")),
            "report_path": display_path(launch_output),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Phase 2 and launch-claim reports for strict benchmark JSON artifacts.")
    parser.add_argument("--pattern", action="append", help="Glob pattern relative to the repo root. May be repeated.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".benchmark-data" / "reports")
    parser.add_argument("--source", default="local", help="Source label for launch claim reports; use testbox only for real Testbox runs.")
    parser.add_argument("--runner", help="Runner label for launch claim reports.")
    parser.add_argument("--artifact", default="rustwright-benchmark-results", help="Artifact name for launch claim reports.")
    parser.add_argument("--run-url", help="Workflow or Testbox run URL for launch claim reports.")
    parser.add_argument("--enforce-phase2", action="store_true", help="Exit non-zero when any Phase 2 report rejects.")
    parser.add_argument("--enforce-launch", action="store_true", help="Exit non-zero when any launch report rejects.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    patterns = args.pattern or DEFAULT_PATTERNS
    results = discover_results(patterns)
    checks = [check_result(args, result) for result in results]
    phase2_failures = [item for item in checks if not item["phase2"]["accepted"]]
    launch_failures = [item for item in checks if not item["launch"]["accepted"]]
    summary = {
        "status": "missing" if not results else "checked",
        "source": args.source,
        "patterns": patterns,
        "result_count": len(results),
        "phase2_failures": len(phase2_failures),
        "launch_failures": len(launch_failures),
        "checks": checks,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"checked {len(results)} strict benchmark artifact(s)")
        for item in checks:
            print(
                f"{item['result_path']}: "
                f"phase2={item['phase2']['status']} launch={item['launch']['status']}"
            )

    if not results and (args.enforce_phase2 or args.enforce_launch):
        return 1
    if args.enforce_phase2 and phase2_failures:
        return 1
    if args.enforce_launch and launch_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
