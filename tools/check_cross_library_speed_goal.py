#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_BASELINES = ["playwright", "typescript-playwright", "typescript-puppeteer"]
DEFAULT_MIN_COMMON_CASES = 15
DEFAULT_MIN_REPETITIONS = 3
DEFAULT_MIN_ITERATIONS = 10
DEFAULT_MIN_SPEEDUP_PCT = 30.0


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition: bool, name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(condition), "detail": detail}


def passed_runs(data: dict[str, Any], implementation: str) -> list[dict[str, Any]]:
    return [
        item
        for item in data.get("results") or []
        if item.get("implementation") == implementation and item.get("status") == "passed"
    ]


def common_total(data: dict[str, Any], implementation: str) -> float | None:
    value = ((data.get("common_case_comparison") or {}).get("total_median_ms") or {}).get(implementation)
    return float(value) if value is not None else None


def common_speedup(data: dict[str, Any], baseline: str) -> float | None:
    rustwright = common_total(data, "rustwright")
    reference = common_total(data, baseline)
    if rustwright is None or reference is None or reference <= 0:
        return None
    return (reference - rustwright) / reference * 100


def evaluate(data: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    baselines = args.baseline
    required_impls = ["rustwright", *baselines]
    metadata = data.get("metadata") or {}
    common = data.get("common_case_comparison") or {}
    repetitions = int(data.get("repetitions") or 0)
    iterations = int(data.get("iterations") or 0)
    results = data.get("results") or []
    non_passed = [item for item in results if item.get("status") != "passed"]
    aggregate = data.get("aggregate") or {}

    checks.append(check(data.get("suite") == args.suite, "suite", f"expected {args.suite}, got {data.get('suite')}"))
    checks.append(
        check(
            data.get("lifecycle") == args.lifecycle,
            "lifecycle",
            f"expected {args.lifecycle}, got {data.get('lifecycle')}",
        )
    )
    checks.append(
        check(
            repetitions >= args.min_repetitions,
            "repetitions",
            f"expected >= {args.min_repetitions}, got {repetitions}",
        )
    )
    checks.append(
        check(
            iterations >= args.min_iterations,
            "iterations",
            f"expected >= {args.min_iterations}, got {iterations}",
        )
    )
    checks.append(check(not non_passed, "all_runs_passed", f"non-passed runs: {len(non_passed)}"))
    checks.append(
        check(
            data.get("container_isolation") == "one_container_per_implementation_per_repetition",
            "container_isolation",
            f"got {data.get('container_isolation')}",
        )
    )
    checks.append(
        check(
            metadata.get("docker_memory_limit") == args.docker_memory,
            "docker_memory_limit",
            f"expected {args.docker_memory}, got {metadata.get('docker_memory_limit')}",
        )
    )
    checks.append(
        check(
            metadata.get("docker_memory_swap_limit") == args.docker_memory,
            "docker_memory_swap_limit",
            f"expected {args.docker_memory}, got {metadata.get('docker_memory_swap_limit')}",
        )
    )
    checks.append(
        check(
            (metadata.get("docker_preflight") or {}).get("status") == "healthy",
            "docker_preflight",
            f"got {(metadata.get('docker_preflight') or {}).get('status')}",
        )
    )
    checks.append(
        check(
            int(common.get("case_count") or 0) >= args.min_common_cases,
            "common_case_count",
            f"expected >= {args.min_common_cases}, got {common.get('case_count') or 0}",
        )
    )

    for implementation in required_impls:
        checks.append(
            check(
                implementation in aggregate,
                f"{implementation}_aggregate",
                "present" if implementation in aggregate else "missing",
            )
        )
        runs = passed_runs(data, implementation)
        checks.append(
            check(
                len(runs) >= args.min_repetitions,
                f"{implementation}_passed_repetitions",
                f"expected >= {args.min_repetitions}, got {len(runs)}",
            )
        )
        total = common_total(data, implementation)
        checks.append(
            check(
                total is not None and total > 0,
                f"{implementation}_common_total",
                "not measurable" if total is None else f"{total:.2f} ms",
            )
        )

    speedups: dict[str, float | None] = {}
    for baseline in baselines:
        value = common_speedup(data, baseline)
        speedups[baseline] = value
        checks.append(
            check(
                value is not None and value >= args.min_speedup_pct,
                f"speedup_vs_{baseline}",
                "not measurable"
                if value is None
                else f"expected >= {args.min_speedup_pct:.1f}%, got {value:.1f}%",
            )
        )

    accepted = all(item["passed"] for item in checks)
    return {
        "status": "accepted" if accepted else "rejected",
        "accepted": accepted,
        "path": str(args.json_path),
        "criteria": {
            "suite": args.suite,
            "lifecycle": args.lifecycle,
            "baselines": baselines,
            "min_common_cases": args.min_common_cases,
            "min_repetitions": args.min_repetitions,
            "min_iterations": args.min_iterations,
            "min_speedup_pct": args.min_speedup_pct,
            "docker_memory": args.docker_memory,
        },
        "observed": {
            "suite": data.get("suite"),
            "lifecycle": data.get("lifecycle"),
            "iterations": iterations,
            "repetitions": repetitions,
            "common_case_count": common.get("case_count") or 0,
            "common_totals_ms": {
                implementation: common_total(data, implementation) for implementation in required_impls
            },
            "speedups_pct": speedups,
            "docker_preflight": metadata.get("docker_preflight"),
            "non_passed_runs": [
                {
                    "implementation": item.get("implementation"),
                    "repetition": item.get("repetition"),
                    "status": item.get("status"),
                    "failure_kind": item.get("failure_kind"),
                    "reason": item.get("reason"),
                }
                for item in non_passed
            ],
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check cross-library common-case benchmark speed evidence.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--suite", default="equivalent")
    parser.add_argument("--lifecycle", default="warm-browser")
    parser.add_argument("--baseline", action="append", default=None)
    parser.add_argument("--min-common-cases", type=int, default=DEFAULT_MIN_COMMON_CASES)
    parser.add_argument("--min-repetitions", type=int, default=DEFAULT_MIN_REPETITIONS)
    parser.add_argument("--min-iterations", type=int, default=DEFAULT_MIN_ITERATIONS)
    parser.add_argument("--min-speedup-pct", type=float, default=DEFAULT_MIN_SPEEDUP_PCT)
    parser.add_argument("--docker-memory", default="8g")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.baseline is None:
        args.baseline = DEFAULT_BASELINES.copy()

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
