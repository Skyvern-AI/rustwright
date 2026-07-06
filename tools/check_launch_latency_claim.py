#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import check_phase2_benchmark


ROOT = Path(__file__).resolve().parents[1]
MAX_MEMORY_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_VARIANCE_RATIO = 1.2
DEFAULT_PROGRESS = ROOT / "docs" / "internal" / "PROGRESS.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition: bool, name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(condition), "detail": detail}


def dot_lookup(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def memory_limit_bytes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    multipliers = {
        "g": 1024**3,
        "gb": 1024**3,
        "m": 1024**2,
        "mb": 1024**2,
    }
    for suffix, multiplier in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * multiplier)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None


def impl_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    aliases = {
        "python_playwright_reference": "playwright",
        "rustwright-py": "rustwright",
    }
    return {aliases.get(str(item), str(item)) for item in value}


def metric_value(metrics: dict[str, Any], implementation: str) -> float | None:
    value = metrics.get(implementation)
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("median", "total_median_ms", "value"):
            if key in value:
                value = value[key]
                break
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reduction_pct(benchmark: dict[str, Any], reference_impl: str) -> float | None:
    medians = benchmark.get("total_median_ms") or {}
    rustwright = metric_value(medians, "rustwright")
    reference = metric_value(medians, reference_impl)
    if rustwright is None or reference is None or reference <= 0:
        return None
    return (reference - rustwright) / reference * 100


def variance_ratio(benchmark: dict[str, Any], implementation: str) -> float | None:
    p25 = metric_value(benchmark.get("total_p25_ms") or {}, implementation)
    p75 = metric_value(benchmark.get("total_p75_ms") or {}, implementation)
    if p25 is None or p75 is None or p25 <= 0:
        return None
    return p75 / p25


def aggregate_metric(data: dict[str, Any], metric: str) -> dict[str, float]:
    values: dict[str, float] = {}
    aggregate = data.get("aggregate") or {}
    for implementation, item in aggregate.items():
        total = (item.get("total_mean_ms") or {}).get(metric)
        if total is None:
            continue
        try:
            values[implementation] = float(total)
        except (TypeError, ValueError):
            continue
    return values


def benchmark_case_count(data: dict[str, Any], implementations: set[str]) -> int:
    aggregate = data.get("aggregate") or {}
    counts = [
        len((aggregate.get(implementation) or {}).get("cases") or {})
        for implementation in sorted(implementations)
        if implementation in aggregate
    ]
    if counts:
        return min(counts)
    common = data.get("common_case_comparison") or {}
    try:
        return int(common.get("case_count") or 0)
    except (TypeError, ValueError):
        return 0


def matrix_json_to_evidence(
    data: dict[str, Any],
    *,
    source: str,
    runner: str | None = None,
    artifact: str | None = None,
    run_url: str | None = None,
) -> dict[str, Any]:
    metadata = data.get("metadata") or {}
    implementations = impl_names(metadata.get("implementations") or list((data.get("aggregate") or {}).keys()))
    results = data.get("results") or []
    runs_passed = len([item for item in results if item.get("status") == "passed"])
    runs_total = len(results)
    result_path = data.get("result_path")
    source_label = source or "local"
    return {
        "source": source_label,
        "artifact": artifact or result_path,
        "result_path": result_path,
        "run_url": run_url,
        "benchmark": {
            "suite": data.get("suite") or metadata.get("suite"),
            "lifecycle": data.get("lifecycle") or metadata.get("lifecycle"),
            "iterations": data.get("iterations") or metadata.get("iterations"),
            "repetitions": data.get("repetitions") or metadata.get("repetitions"),
            "case": ",".join(data.get("case_filters") or metadata.get("case_filters") or []) or None,
            "case_count": benchmark_case_count(data, implementations),
            "implementations": sorted(implementations),
            "runs_passed": runs_passed,
            "runs_total": runs_total,
            "total_median_ms": aggregate_metric(data, "median"),
            "total_p25_ms": aggregate_metric(data, "p25"),
            "total_p75_ms": aggregate_metric(data, "p75"),
            "result_path": result_path,
        },
        "environment": {
            "source": source_label,
            "source_type": source_label,
            "runner": runner or metadata.get("runner") or metadata.get("machine") or source_label,
            "container_isolation": data.get("container_isolation") or metadata.get("container_isolation"),
            "docker_memory_limit": metadata.get("docker_memory_limit"),
            "docker_memory_swap_limit": metadata.get("docker_memory_swap_limit"),
            "docker_image": metadata.get("docker_image"),
            "docker_image_id": metadata.get("docker_image_id"),
            "docker_cpu_host_info": metadata.get("docker_cpu_host_info"),
            "docker_cpu_quota": metadata.get("docker_cpu_quota"),
            "git_rev": metadata.get("git_rev"),
        },
    }


def evaluate(
    progress: dict[str, Any],
    *,
    evidence_path: str,
    min_repetitions: int,
    min_iterations: int,
    min_case_count: int,
    min_speedup_pct: float,
    max_variance_ratio: float,
    reference_impl: str,
) -> dict[str, Any]:
    evidence = dot_lookup(progress, evidence_path)
    if not isinstance(evidence, dict):
        return {
            "status": "missing",
            "accepted": False,
            "evidence_path": evidence_path,
            "checks": [check(False, "evidence_present", f"no object found at {evidence_path}")],
            "observed": {},
            "criteria": {
                "min_repetitions": min_repetitions,
                "min_iterations": min_iterations,
            "min_case_count": min_case_count,
            "min_speedup_pct": min_speedup_pct,
            "max_variance_ratio": max_variance_ratio,
            "reference_impl": reference_impl,
        },
        }
    return evaluate_evidence(
        evidence,
        evidence_path=evidence_path,
        min_repetitions=min_repetitions,
        min_iterations=min_iterations,
        min_case_count=min_case_count,
        min_speedup_pct=min_speedup_pct,
        max_variance_ratio=max_variance_ratio,
        reference_impl=reference_impl,
    )


def evaluate_evidence(
    evidence: dict[str, Any],
    *,
    evidence_path: str,
    min_repetitions: int,
    min_iterations: int,
    min_case_count: int,
    min_speedup_pct: float,
    max_variance_ratio: float,
    reference_impl: str,
) -> dict[str, Any]:
    benchmark = evidence.get("benchmark") or {}
    environment = evidence.get("environment") or {}
    runner = str(environment.get("runner") or "")
    command = str(evidence.get("command") or "")
    workflow = str(evidence.get("workflow") or "")
    source_text = " ".join(
        [
            runner,
            command,
            workflow,
            str(evidence.get("source") or ""),
            str(environment.get("source") or ""),
            str(environment.get("source_type") or ""),
        ]
    ).lower()
    memory_bytes = memory_limit_bytes(environment.get("docker_memory_limit"))
    swap_bytes = memory_limit_bytes(environment.get("docker_memory_swap_limit"))
    implementations = impl_names(benchmark.get("implementations"))
    repetitions = int(benchmark.get("repetitions") or 0)
    iterations = int(benchmark.get("iterations") or 0)
    case_count = int(benchmark.get("case_count") or benchmark.get("sample_count") or 0)
    selected_case = benchmark.get("case")
    runs_passed = benchmark.get("runs_passed")
    runs_total = benchmark.get("runs_total")
    median_metrics = benchmark.get("total_median_ms") or {}
    p25_metrics = benchmark.get("total_p25_ms") or {}
    p75_metrics = benchmark.get("total_p75_ms") or {}
    reduction = reduction_pct(benchmark, reference_impl)
    rust_variance_ratio = variance_ratio(benchmark, "rustwright")
    reference_variance_ratio = variance_ratio(benchmark, reference_impl)

    checks = [
        check("testbox" in source_text, "testbox_backed", f"runner/command/workflow: {runner or command or workflow or 'missing'}"),
        check(
            environment.get("container_isolation") in {
                "one_container_per_implementation_per_repetition",
                "one_container_per_implementation_per_repetition_or_shard",
                "one_container_per_shard",
            },
            "container_isolation",
            f"got {environment.get('container_isolation')}",
        ),
        check(
            memory_bytes is not None and memory_bytes <= MAX_MEMORY_BYTES,
            "docker_memory_cap",
            f"got {environment.get('docker_memory_limit')}",
        ),
        check(
            swap_bytes is not None and swap_bytes <= MAX_MEMORY_BYTES,
            "docker_swap_cap",
            f"got {environment.get('docker_memory_swap_limit')}",
        ),
        check(benchmark.get("suite") == "strict", "strict_suite", f"got {benchmark.get('suite')}"),
        check(benchmark.get("lifecycle") == "warm-browser", "warm_browser_lifecycle", f"got {benchmark.get('lifecycle')}"),
        check(repetitions >= min_repetitions, "minimum_repetitions", f"expected >= {min_repetitions}, got {repetitions}"),
        check(iterations >= min_iterations, "minimum_iterations", f"expected >= {min_iterations}, got {iterations}"),
        check(case_count >= min_case_count and not selected_case, "full_case_scope", f"case={selected_case!r}, case_count={case_count}"),
        check(
            {"rustwright", reference_impl}.issubset(implementations),
            "primary_implementations",
            f"implementations={sorted(implementations)}",
        ),
        check(
            isinstance(runs_passed, int) and isinstance(runs_total, int) and runs_passed == runs_total and runs_total > 0,
            "all_runs_passed",
            f"runs_passed={runs_passed}, runs_total={runs_total}",
        ),
        check(
            bool(median_metrics) and bool(p25_metrics) and bool(p75_metrics),
            "distribution_stats",
            "requires total_median_ms, total_p25_ms, and total_p75_ms",
        ),
        check(
            reduction is not None and reduction >= min_speedup_pct,
            "latency_win",
            "not measurable from total_median_ms"
            if reduction is None
            else f"expected Rustwright >= {min_speedup_pct:.1f}% lower than {reference_impl}, got {reduction:.1f}%",
        ),
        check(
            rust_variance_ratio is not None and rust_variance_ratio <= max_variance_ratio,
            "rustwright_variance",
            "not measurable"
            if rust_variance_ratio is None
            else f"expected p75/p25 <= {max_variance_ratio:.2f}, got {rust_variance_ratio:.2f}",
        ),
        check(
            reference_variance_ratio is not None and reference_variance_ratio <= max_variance_ratio,
            "reference_variance",
            "not measurable"
            if reference_variance_ratio is None
            else f"expected p75/p25 <= {max_variance_ratio:.2f}, got {reference_variance_ratio:.2f}",
        ),
        check(bool(evidence.get("run_url") or evidence.get("result_path") or benchmark.get("result_path")), "artifact_link", "requires run_url or result_path"),
        check(bool(evidence.get("artifact")), "artifact_name", f"got {evidence.get('artifact')}"),
        check(bool(environment.get("docker_image_id")), "docker_image_id", f"got {environment.get('docker_image_id')}"),
        check(bool(environment.get("git_rev")), "git_revision", f"got {environment.get('git_rev')}"),
        check(bool(environment.get("docker_cpu_host_info") or environment.get("docker_cpu_quota")), "cpu_metadata", "requires CPU host info or quota"),
    ]

    accepted = all(item["passed"] for item in checks)
    return {
        "status": "accepted" if accepted else "rejected",
        "accepted": accepted,
        "evidence_path": evidence_path,
        "checks": checks,
        "criteria": {
            "min_repetitions": min_repetitions,
            "min_iterations": min_iterations,
            "min_case_count": min_case_count,
            "min_speedup_pct": min_speedup_pct,
            "max_variance_ratio": max_variance_ratio,
            "reference_impl": reference_impl,
            "max_memory_bytes": MAX_MEMORY_BYTES,
        },
        "observed": {
            "runner": runner,
            "suite": benchmark.get("suite"),
            "case": selected_case,
            "case_count": case_count,
            "lifecycle": benchmark.get("lifecycle"),
            "repetitions": repetitions,
            "iterations": iterations,
            "implementations": sorted(implementations),
            "runs_passed": runs_passed,
            "runs_total": runs_total,
            "docker_memory_limit": environment.get("docker_memory_limit"),
            "docker_memory_swap_limit": environment.get("docker_memory_swap_limit"),
            "container_isolation": environment.get("container_isolation"),
            "rustwright_reduction_pct": reduction,
            "rustwright_variance_ratio": rust_variance_ratio,
            "reference_variance_ratio": reference_variance_ratio,
            "result_path": benchmark.get("result_path") or evidence.get("result_path"),
            "run_url": evidence.get("run_url"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether recorded benchmark evidence is launch-ready latency evidence.")
    parser.add_argument(
        "--progress",
        type=Path,
        default=DEFAULT_PROGRESS,
        help="Progress metadata JSON; defaults to the internal progress file when present.",
    )
    parser.add_argument("--evidence-path", default="metrics.latest_blacksmith_benchmark_workflow")
    parser.add_argument(
        "--benchmark-json",
        type=Path,
        help="Validate a saved tools/run_benchmark_matrix.py JSON artifact directly instead of reading progress metadata.",
    )
    parser.add_argument(
        "--source",
        default="local",
        help="Execution source for --benchmark-json. Use 'testbox' only for artifacts actually produced in a Blacksmith Testbox.",
    )
    parser.add_argument("--runner", help="Runner label for --benchmark-json, for example blacksmith-testbox.")
    parser.add_argument("--artifact", help="Artifact name for --benchmark-json.")
    parser.add_argument("--run-url", help="Run URL for --benchmark-json.")
    parser.add_argument("--min-repetitions", type=int, default=3)
    parser.add_argument("--min-iterations", type=int, default=1)
    parser.add_argument("--min-case-count", type=int, default=None)
    parser.add_argument("--min-speedup-pct", type=float, default=30.0)
    parser.add_argument("--max-variance-ratio", type=float, default=DEFAULT_MAX_VARIANCE_RATIO)
    parser.add_argument("--reference-impl", default="playwright")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    min_case_count = args.min_case_count
    if min_case_count is None:
        min_case_count = check_phase2_benchmark.current_strict_case_count()

    if args.benchmark_json:
        evidence = matrix_json_to_evidence(
            load_json(args.benchmark_json),
            source=args.source,
            runner=args.runner,
            artifact=args.artifact,
            run_url=args.run_url,
        )
        report = evaluate_evidence(
            evidence,
            evidence_path=str(args.benchmark_json),
            min_repetitions=args.min_repetitions,
            min_iterations=args.min_iterations,
            min_case_count=min_case_count,
            min_speedup_pct=args.min_speedup_pct,
            max_variance_ratio=args.max_variance_ratio,
            reference_impl=args.reference_impl,
        )
    elif args.progress.exists():
        report = evaluate(
            load_json(args.progress),
            evidence_path=args.evidence_path,
            min_repetitions=args.min_repetitions,
            min_iterations=args.min_iterations,
            min_case_count=min_case_count,
            min_speedup_pct=args.min_speedup_pct,
            max_variance_ratio=args.max_variance_ratio,
            reference_impl=args.reference_impl,
        )
    else:
        report = {
            "status": "unavailable",
            "accepted": False,
            "checks": [
                check(
                    False,
                    "progress_metadata_available",
                    f"progress metadata not found at {args.progress}; pass --benchmark-json to validate a public artifact",
                )
            ],
            "observed": {"progress": str(args.progress)},
            "criteria": {},
        }
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
