#!/usr/bin/env python3
"""Validate benchmark runner results exactly against a manifest."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


MAX_REPEAT = 1000
ITERATION_ERROR = re.compile(r"^iteration ([1-9][0-9]*): ")


class ValidationError(ValueError):
    pass


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValidationError(f"{label} file does not exist: {path}") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"{label} is not valid JSON: {path}: {error}") from error


def unique_ids(rows: Any, label: str) -> list[str]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError(f"{label} must be a non-empty list")
    identifiers: list[str] = []
    duplicates: set[str] = set()
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValidationError(f"{label}[{index}] must be an object")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError(f"{label}[{index}].id must be a non-empty string")
        if identifier in seen:
            duplicates.add(identifier)
        seen.add(identifier)
        identifiers.append(identifier)
    if duplicates:
        raise ValidationError(f"{label} contains duplicate IDs: {sorted(duplicates)}")
    return identifiers


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ValidationError("manifest top-level value must be an object")
    version = manifest.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValidationError("manifest.version must equal 1")

    rows = manifest.get("cases")
    unique_ids(rows, "manifest.cases")
    contracts: list[dict[str, Any]] = []
    for index, case in enumerate(rows):
        label = f"manifest.cases[{index}] ({case['id']!r})"
        repeat = case.get("repeat", 1)
        if (
            isinstance(repeat, bool)
            or not isinstance(repeat, int)
            or not 1 <= repeat <= MAX_REPEAT
        ):
            raise ValidationError(
                f"{label}.repeat must be an integer between 1 and {MAX_REPEAT}"
            )
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValidationError(f"{label}.steps must be a non-empty list")
        capture_names: set[str] = set()
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValidationError(f"{label}.steps[{step_index}] must be an object")
            if "capture" in step:
                capture = step["capture"]
                if not isinstance(capture, str) or not capture:
                    raise ValidationError(
                        f"{label}.steps[{step_index}].capture must be a non-empty string"
                    )
                capture_names.add(capture)
        if repeat > 1 and not any(
            step.get("op") == "goto" for step in steps
        ):
            raise ValidationError(f"{label} has repeat {repeat} but no goto step")
        contracts.append(
            {"id": case["id"], "repeat": repeat, "captures": capture_names}
        )
    return contracts


def validate_result_row(row: dict[str, Any], contract: dict[str, Any]) -> None:
    case_id = contract["id"]
    label = f"result case {case_id!r}"
    required_keys = {"id", "ok", "captures", "ms"}
    allowed_keys = required_keys | {"error"}
    missing_keys = sorted(required_keys - row.keys())
    if missing_keys:
        raise ValidationError(f"{label} is missing required key(s): {missing_keys}")
    unexpected_keys = sorted(row.keys() - allowed_keys)
    if unexpected_keys:
        raise ValidationError(f"{label} has unexpected key(s): {unexpected_keys}")

    ok = row["ok"]
    if not isinstance(ok, bool):
        raise ValidationError(f"{label}.ok must be a bool")

    if "error" in row:
        if ok:
            raise ValidationError(f"{label}.error is only allowed when ok is false")
        if not isinstance(row["error"], str):
            raise ValidationError(f"{label}.error must be a string when present")

    captures = row["captures"]
    if not isinstance(captures, dict):
        raise ValidationError(f"{label}.captures must be an object")
    expected_captures = contract["captures"]
    actual_captures = set(captures)
    if actual_captures != expected_captures:
        missing = sorted(expected_captures - actual_captures)
        unexpected = sorted(actual_captures - expected_captures)
        raise ValidationError(
            f"{label}.captures keys differ from the manifest; "
            f"missing={missing}, unexpected={unexpected}"
        )

    milliseconds = row["ms"]
    if (
        isinstance(milliseconds, bool)
        or not isinstance(milliseconds, (int, float))
        or (isinstance(milliseconds, float) and not math.isfinite(milliseconds))
        or milliseconds < 0
    ):
        raise ValidationError(f"{label}.ms must be a non-negative number")

    repeat = contract["repeat"]
    if not ok and repeat > 1:
        error = row.get("error")
        if not isinstance(error, str):
            raise ValidationError(
                f"{label}.error must be present when ok is false and repeat is {repeat}"
            )
        match = ITERATION_ERROR.match(error)
        if match is None:
            raise ValidationError(
                f"{label}.error must start with 'iteration <N>: ' for repeat {repeat}"
            )
        iteration = int(match.group(1))
        if not 1 <= iteration <= repeat:
            raise ValidationError(
                f"{label}.error iteration {iteration} is outside the range 1..{repeat}"
            )


def validate_results(manifest_path: Path, results_path: Path) -> dict[str, int]:
    manifest = load_json(manifest_path, "manifest")
    contracts = validate_manifest(manifest)
    manifest_ids = [contract["id"] for contract in contracts]

    results = load_json(results_path, "results")
    if not isinstance(results, dict):
        raise ValidationError("results top-level value must be an object")
    result_rows = results.get("results")
    result_ids = unique_ids(result_rows, "results.results")

    for index in range(max(len(manifest_ids), len(result_ids))):
        if index >= len(result_ids):
            expected = manifest_ids[index]
            raise ValidationError(
                f"result case order is missing manifest case {expected!r} at index {index}"
            )
        if index >= len(manifest_ids):
            unexpected = result_ids[index]
            raise ValidationError(
                f"result case order has unexpected case {unexpected!r} at index {index}"
            )
        expected = manifest_ids[index]
        actual = result_ids[index]
        if actual != expected:
            raise ValidationError(
                f"result case order mismatch at index {index}: "
                f"expected {expected!r}, got {actual!r}"
            )

    for row, contract in zip(result_rows, contracts):
        validate_result_row(row, contract)

    failed_ids = [row["id"] for row in result_rows if not row["ok"]]
    if failed_ids:
        raise ValidationError(f"result cases are not ok:true: {failed_ids}")

    return {"cases": len(result_rows), "failed": 0}


def measurement_payload(path: Path) -> dict[str, int | float]:
    payload = load_json(path, "measurement")
    if not isinstance(payload, dict):
        raise ValidationError("measurement top-level value must be an object")

    wall_seconds = payload.get("wall_seconds")
    peak_tree_rss_kb = payload.get("peak_tree_rss_kb")
    exit_code = payload.get("exit_code")
    samples = payload.get("samples")
    unresolved_samples = payload.get("unresolved_samples")
    unresolved_records_total = payload.get("unresolved_records_total")
    if (
        isinstance(wall_seconds, bool)
        or not isinstance(wall_seconds, (int, float))
        or wall_seconds < 0
    ):
        raise ValidationError("measurement.wall_seconds must be a non-negative number")
    if (
        isinstance(peak_tree_rss_kb, bool)
        or not isinstance(peak_tree_rss_kb, int)
        or peak_tree_rss_kb < 0
    ):
        raise ValidationError("measurement.peak_tree_rss_kb must be a non-negative integer")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
        raise ValidationError(f"measurement.exit_code must be zero, got {exit_code!r}")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
        raise ValidationError("measurement.samples must be a positive integer")
    for key, value in (
        ("unresolved_samples", unresolved_samples),
        ("unresolved_records_total", unresolved_records_total),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"measurement.{key} must be a non-negative integer")
    return {
        "wall_seconds": float(wall_seconds),
        "peak_tree_rss_kb": peak_tree_rss_kb,
        "exit_code": exit_code,
        "samples": samples,
        "unresolved_samples": unresolved_samples,
        "unresolved_records_total": unresolved_records_total,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--measurement", type=Path)
    parser.add_argument("--metrics-out", type=Path)
    parser.add_argument("--lang")
    parser.add_argument("--impl", dest="implementation")
    args = parser.parse_args(argv)
    metric_arguments = (args.measurement, args.metrics_out, args.lang, args.implementation)
    if any(value is not None for value in metric_arguments) and not all(
        value is not None for value in metric_arguments
    ):
        parser.error(
            "--measurement, --metrics-out, --lang, and --impl must be supplied together"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = validate_results(args.manifest, args.results)
        if args.measurement is not None:
            metrics = {
                "lang": args.lang,
                "impl": args.implementation,
                **measurement_payload(args.measurement),
                **summary,
            }
            write_json(args.metrics_out, metrics)
    except ValidationError as error:
        print(f"validate_binding_results: {error}", file=sys.stderr)
        return 1
    print(f"validated {summary['cases']} result case(s); failures=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
