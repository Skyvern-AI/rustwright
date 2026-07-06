#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


DEFAULT_MIN_SPEEDUP_PCT = 30.0
DEFAULT_MIN_REPETITIONS = 3
DEFAULT_MIN_ITERATIONS = 1
DEFAULT_MAX_VARIANCE_RATIO = 1.2
ROOT = Path(__file__).resolve().parents[1]


def current_strict_case_count() -> int:
    module_path = ROOT / "benchmarks" / "automation_cases.py"
    spec = importlib.util.spec_from_file_location("rustwright_automation_cases_for_phase2_check", module_path)
    if spec is None or spec.loader is None:
        return 0
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return len(getattr(module, "BENCHMARK_STRICT_CASES", []) or [])


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition: bool, name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(condition), "detail": detail}


def implementation_runs(data: dict[str, Any], implementation: str) -> list[dict[str, Any]]:
    return [item for item in data.get("results") or [] if item.get("implementation") == implementation]


def implementation_case_count(data: dict[str, Any], implementation: str) -> int:
    return len(((data.get("aggregate") or {}).get(implementation) or {}).get("cases") or {})


def median_total(data: dict[str, Any], implementation: str) -> float | None:
    value = (((data.get("aggregate") or {}).get(implementation) or {}).get("total_mean_ms") or {}).get("median")
    return float(value) if value is not None else None


def total_metric(data: dict[str, Any], implementation: str, metric: str) -> float | None:
    value = (((data.get("aggregate") or {}).get(implementation) or {}).get("total_mean_ms") or {}).get(metric)
    return float(value) if value is not None else None


def variance_ratio(data: dict[str, Any], implementation: str) -> float | None:
    p25 = total_metric(data, implementation, "p25")
    p75 = total_metric(data, implementation, "p75")
    if p25 is None or p75 is None or p25 <= 0:
        return None
    return p75 / p25


def speedup_pct(data: dict[str, Any], baseline: str = "playwright") -> float | None:
    rustwright = median_total(data, "rustwright")
    reference = median_total(data, baseline)
    if rustwright is None or reference is None or reference <= 0:
        return None
    return (reference - rustwright) / reference * 100


def evaluate(data: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks = []
    suite = data.get("suite")
    lifecycle = data.get("lifecycle")
    repetitions = int(data.get("repetitions") or 0)
    iterations = int(data.get("iterations") or 0)
    results = data.get("results") or []
    failed_or_skipped = [item for item in results if item.get("status") != "passed"]
    rust_runs = implementation_runs(data, "rustwright")
    reference_runs = implementation_runs(data, args.reference_impl)
    rust_passed = [item for item in rust_runs if item.get("status") == "passed"]
    reference_passed = [item for item in reference_runs if item.get("status") == "passed"]
    rust_case_count = implementation_case_count(data, "rustwright")
    reference_case_count = implementation_case_count(data, args.reference_impl)
    reduction = speedup_pct(data, args.reference_impl)
    rust_variance_ratio = variance_ratio(data, "rustwright")
    reference_variance_ratio = variance_ratio(data, args.reference_impl)

    checks.append(check(suite == args.suite, "suite", f"expected {args.suite}, got {suite}"))
    checks.append(check(lifecycle == args.lifecycle, "lifecycle", f"expected {args.lifecycle}, got {lifecycle}"))
    checks.append(
        check(
            repetitions >= args.min_repetitions,
            "repetitions",
            f"expected >= {args.min_repetitions}, got {repetitions}",
        )
    )
    checks.append(
        check(iterations >= args.min_iterations, "iterations", f"expected >= {args.min_iterations}, got {iterations}")
    )
    checks.append(
        check(
            not failed_or_skipped,
            "all_runs_passed",
            f"non-passed runs: {len(failed_or_skipped)}",
        )
    )
    checks.append(
        check(
            len(rust_passed) >= args.min_repetitions,
            "rustwright_passed_repetitions",
            f"expected >= {args.min_repetitions}, got {len(rust_passed)}",
        )
    )
    checks.append(
        check(
            len(reference_passed) >= args.min_repetitions,
            "reference_passed_repetitions",
            f"expected >= {args.min_repetitions}, got {len(reference_passed)}",
        )
    )
    checks.append(
        check(
            rust_case_count >= args.min_case_count,
            "rustwright_case_count",
            f"expected >= {args.min_case_count}, got {rust_case_count}",
        )
    )
    checks.append(
        check(
            reference_case_count >= args.min_case_count,
            "reference_case_count",
            f"expected >= {args.min_case_count}, got {reference_case_count}",
        )
    )
    checks.append(
        check(
            reduction is not None and reduction >= args.min_speedup_pct,
            "speedup",
            "not measurable"
            if reduction is None
            else f"expected Rustwright >= {args.min_speedup_pct:.1f}% lower than {args.reference_impl}, got {reduction:.1f}%",
        )
    )
    checks.append(
        check(
            rust_variance_ratio is not None and rust_variance_ratio <= args.max_variance_ratio,
            "rustwright_variance",
            "not measurable"
            if rust_variance_ratio is None
            else f"expected p75/p25 <= {args.max_variance_ratio:.2f}, got {rust_variance_ratio:.2f}",
        )
    )
    checks.append(
        check(
            reference_variance_ratio is not None and reference_variance_ratio <= args.max_variance_ratio,
            "reference_variance",
            "not measurable"
            if reference_variance_ratio is None
            else f"expected p75/p25 <= {args.max_variance_ratio:.2f}, got {reference_variance_ratio:.2f}",
        )
    )

    passed = all(item["passed"] for item in checks)
    return {
        "status": "accepted" if passed else "rejected",
        "accepted": passed,
        "path": str(args.json_path),
        "criteria": {
            "suite": args.suite,
            "lifecycle": args.lifecycle,
            "reference_impl": args.reference_impl,
            "min_repetitions": args.min_repetitions,
            "min_iterations": args.min_iterations,
            "min_case_count": args.min_case_count,
            "min_speedup_pct": args.min_speedup_pct,
            "max_variance_ratio": args.max_variance_ratio,
        },
        "observed": {
            "suite": suite,
            "lifecycle": lifecycle,
            "repetitions": repetitions,
            "iterations": iterations,
            "rustwright_passed_repetitions": len(rust_passed),
            "reference_passed_repetitions": len(reference_passed),
            "rustwright_case_count": rust_case_count,
            "reference_case_count": reference_case_count,
            "rustwright_total_median_ms": median_total(data, "rustwright"),
            "reference_total_median_ms": median_total(data, args.reference_impl),
            "rustwright_reduction_pct": reduction,
            "rustwright_variance_ratio": rust_variance_ratio,
            "reference_variance_ratio": reference_variance_ratio,
            "non_passed_runs": [
                {
                    "implementation": item.get("implementation"),
                    "repetition": item.get("repetition"),
                    "status": item.get("status"),
                    "failure_kind": item.get("failure_kind"),
                    "reason": item.get("reason"),
                }
                for item in failed_or_skipped
            ],
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether a benchmark matrix is acceptable Phase 2 evidence.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--suite", default="strict")
    parser.add_argument("--lifecycle", default="warm-browser")
    parser.add_argument("--reference-impl", default="playwright")
    parser.add_argument("--min-repetitions", type=int, default=DEFAULT_MIN_REPETITIONS)
    parser.add_argument("--min-iterations", type=int, default=DEFAULT_MIN_ITERATIONS)
    parser.add_argument("--min-case-count", type=int, default=None)
    parser.add_argument("--min-speedup-pct", type=float, default=DEFAULT_MIN_SPEEDUP_PCT)
    parser.add_argument("--max-variance-ratio", type=float, default=DEFAULT_MAX_VARIANCE_RATIO)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.min_case_count is None:
        args.min_case_count = current_strict_case_count()

    report = evaluate(load_json(args.json_path), args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"status: {report['status']}")
        for item in report["checks"]:
            mark = "PASS" if item["passed"] else "FAIL"
            print(f"{mark} {item['name']}: {item['detail']}")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
