#!/usr/bin/env python3
"""Validate benchmark runner results exactly against a manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


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


def validate_results(manifest_path: Path, results_path: Path) -> dict[str, int]:
    manifest = load_json(manifest_path, "manifest")
    if not isinstance(manifest, dict):
        raise ValidationError("manifest top-level value must be an object")
    manifest_rows = manifest.get("cases")
    manifest_ids = unique_ids(manifest_rows, "manifest.cases")

    results = load_json(results_path, "results")
    if not isinstance(results, dict):
        raise ValidationError("results top-level value must be an object")
    result_rows = results.get("results")
    result_ids = unique_ids(result_rows, "results.results")

    manifest_set = set(manifest_ids)
    result_set = set(result_ids)
    if result_set != manifest_set:
        missing = sorted(manifest_set - result_set)
        unexpected = sorted(result_set - manifest_set)
        raise ValidationError(
            f"result case IDs differ from manifest; missing={missing}, unexpected={unexpected}"
        )

    failed_ids = [
        row["id"]
        for row in result_rows
        if not isinstance(row.get("ok"), bool) or row["ok"] is not True
    ]
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
