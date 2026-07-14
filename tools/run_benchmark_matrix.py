#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
import statistics
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMPLS = ["rustwright", "playwright", "typescript-playwright"]
STRICT_IMPLS = ["rustwright", "playwright"]


def output_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def default_iterations() -> int:
    return int(os.environ.get("BENCHMARK_FULL_ITERATIONS") or os.environ.get("BENCHMARK_ITERATIONS") or "10")


def extract_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char == "{":
            candidate = output[index:]
            try:
                value, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError(f"could not find benchmark JSON in output:\n{output}")


def run_impl(args: argparse.Namespace, implementation: str) -> dict[str, Any]:
    runner_lifecycle = "warm-browser" if args.lifecycle == "cold-container" else args.lifecycle
    rebuild_rustwright = implementation == "rustwright" and args.rebuild_rustwright and not args.skip_rustwright_rebuild
    env = {
        **os.environ,
        "BENCHMARK_ITERATIONS": str(args.iterations),
        "RUSTWRIGHT_BENCH_REBUILD": "1" if rebuild_rustwright else "0",
    }
    command = [
        str(ROOT / "tools" / "docker_test.sh"),
        "bench",
        "--impl",
        implementation,
        "--suite",
        args.suite,
        "--lifecycle",
        runner_lifecycle,
        "--json",
    ]
    for case_name in args.case_filters or []:
        command.extend(["--case", case_name])
    proc = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=args.timeout,
    )
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0:
        result = {
            "implementation": implementation,
            "status": "failed",
            "returncode": proc.returncode,
            "output_tail": "\n".join(combined.splitlines()[-40:]),
        }
        if is_docker_daemon_failure(combined):
            result["failure_kind"] = "docker_daemon_error"
        return result
    result = extract_json(combined)
    result["status"] = "passed"
    result["container_isolation"] = "separate_container"
    result["command"] = " ".join(command)
    result["rustwright_rebuild"] = env["RUSTWRIGHT_BENCH_REBUILD"] == "1"
    result["rustwright_rebuild_target_cache"] = env.get("RUSTWRIGHT_DOCKER_REBUILD_TARGET_CACHE") == "1"
    if result["rustwright_rebuild_target_cache"]:
        result["rustwright_rebuild_cache_prefix"] = env.get("RUSTWRIGHT_DOCKER_REBUILD_CACHE_PREFIX")
    result["matrix_lifecycle"] = args.lifecycle
    return result


def passed_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in results if item.get("status") == "passed"]


def is_docker_daemon_failure(output: str) -> bool:
    lowered = output.lower()
    return (
        "bad response from docker engine" in lowered
        or "error waiting for container" in lowered
        or "cannot connect to the docker daemon" in lowered
        or "docker info timed out" in lowered
    )


def docker_health_check(timeout: int = 10) -> dict[str, Any]:
    command = ["docker", "info"]
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        combined = output_to_text(exc.stdout) + output_to_text(exc.stderr)
        output_tail = "\n".join(combined.splitlines()[-40:])
        timeout_message = f"docker info timed out after {timeout}s"
        return {
            "status": "unhealthy",
            "command": " ".join(command),
            "returncode": None,
            "failure_kind": "docker_daemon_error",
            "output_tail": "\n".join([line for line in [output_tail, timeout_message] if line]),
        }
    except OSError as exc:
        return {
            "status": "unhealthy",
            "command": " ".join(command),
            "returncode": None,
            "failure_kind": "docker_daemon_error",
            "output_tail": str(exc),
        }
    combined = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return {
            "status": "healthy",
            "command": " ".join(command),
            "returncode": 0,
            "output_tail": "\n".join(combined.splitlines()[-20:]),
        }
    return {
        "status": "unhealthy",
        "command": " ".join(command),
        "returncode": proc.returncode,
        "failure_kind": "docker_daemon_error",
        "output_tail": "\n".join(combined.splitlines()[-40:]),
    }


def skipped_results_after_docker_preflight(
    planned_runs: list[tuple[int, str]], reason: str = "skipped_after_docker_preflight_failure"
) -> list[dict[str, Any]]:
    return [
        {
            "implementation": implementation,
            "status": "skipped",
            "reason": reason,
            "failure_kind": "docker_daemon_error",
            "repetition": repetition + 1,
        }
        for repetition, implementation in planned_runs
    ]


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def aggregate_values(values: list[float]) -> dict[str, float]:
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def aggregate_optional_values(values: list[float]) -> dict[str, float | None]:
    if values:
        return aggregate_values(values)
    return {key: None for key in ("mean", "median", "p25", "p75", "min", "max", "stdev")}


def speedups_from_results(results: list[dict[str, Any]]) -> dict[str, float]:
    by_name = {item["implementation"]: item for item in passed_results(results)}
    rustwright = by_name.get("rustwright")
    if not rustwright:
        return {}
    rustwright_total = float(rustwright["total_mean_ms"])
    values: dict[str, float] = {}
    for name, item in by_name.items():
        if name == "rustwright":
            continue
        total = float(item["total_mean_ms"])
        if total > 0:
            values[f"vs_{name}_reduction_pct"] = (total - rustwright_total) / total * 100
    return values


def speedups_from_aggregate(aggregate: dict[str, Any]) -> dict[str, float]:
    rustwright = aggregate.get("rustwright")
    if not rustwright:
        return {}
    rustwright_total = float(rustwright["total_mean_ms"]["median"])
    values: dict[str, float] = {}
    for name, item in aggregate.items():
        if name == "rustwright":
            continue
        total = float(item["total_mean_ms"]["median"])
        if total > 0:
            values[f"vs_{name}_median_reduction_pct"] = (total - rustwright_total) / total * 100
    return values


def case_winners_from_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    passed = passed_results(results)
    case_names = sorted({case for item in passed for case in item.get("cases", {})})
    rows = {}
    win_counts = {item["implementation"]: 0 for item in passed}
    for case_name in case_names:
        entries = []
        for item in passed:
            case = item.get("cases", {}).get(case_name)
            if case and "mean_ms" in case:
                entries.append((float(case["mean_ms"]), item["implementation"]))
        if not entries:
            continue
        entries.sort()
        winner = entries[0][1]
        win_counts[winner] += 1
        rows[case_name] = {
            "winner": winner,
            "winner_mean_ms": round(entries[0][0], 4),
            "ranked": [{"implementation": name, "mean_ms": round(value, 4)} for value, name in entries],
        }
    return {"win_counts": win_counts, "cases": rows}


def case_winners_from_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
    case_names = sorted({case for item in aggregate.values() for case in item.get("cases", {})})
    rows = {}
    win_counts = {implementation: 0 for implementation in aggregate}
    for case_name in case_names:
        entries = []
        for implementation, item in aggregate.items():
            case = item.get("cases", {}).get(case_name)
            if case and "median" in case:
                entries.append((float(case["median"]), implementation))
        if not entries:
            continue
        entries.sort()
        winner = entries[0][1]
        win_counts[winner] += 1
        rows[case_name] = {
            "winner": winner,
            "winner_median_ms": round(entries[0][0], 4),
            "ranked": [{"implementation": name, "median_ms": round(value, 4)} for value, name in entries],
        }
    return {"win_counts": win_counts, "cases": rows}


def aggregate_repetitions(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in passed_results(results):
        grouped.setdefault(item["implementation"], []).append(item)
    aggregate = {}
    for implementation, items in grouped.items():
        totals = [float(item["total_mean_ms"]) for item in items]
        case_names = sorted({name for item in items for name in item.get("cases", {})})
        memory_fields = ("rss_self_kb", "rss_tree_kb")
        memory = {
            field: aggregate_optional_values(
                [
                    float(item["memory"][field])
                    for item in items
                    if item.get("memory", {}).get(field) is not None
                ]
            )
            for field in memory_fields
        }
        aggregate[implementation] = {
            "runs": len(items),
            "total_mean_ms": aggregate_values(totals),
            "memory": memory,
            "cases": {
                name: aggregate_values([float(item["cases"][name]["mean_ms"]) for item in items if name in item.get("cases", {})])
                for name in case_names
            },
        }
    return aggregate


def common_case_names(aggregate: dict[str, Any]) -> list[str]:
    case_sets = [set(item.get("cases", {})) for item in aggregate.values() if item.get("cases")]
    if not case_sets:
        return []
    return sorted(set.intersection(*case_sets))


def common_case_comparison(aggregate: dict[str, Any]) -> dict[str, Any]:
    names = common_case_names(aggregate)
    if not names:
        return {
            "case_count": 0,
            "case_names": [],
            "total_median_ms": {},
            "speedups": {},
        }
    totals = {}
    for implementation, item in aggregate.items():
        total = 0.0
        for name in names:
            case = item.get("cases", {}).get(name)
            if case is None:
                break
            total += float(case["median"])
        else:
            totals[implementation] = total

    rustwright_total = totals.get("rustwright")
    speedups = {}
    if rustwright_total is not None:
        for implementation, total in totals.items():
            if implementation == "rustwright" or total <= 0:
                continue
            speedups[f"vs_{implementation}_median_reduction_pct"] = (total - rustwright_total) / total * 100

    return {
        "case_count": len(names),
        "case_names": names,
        "total_median_ms": totals,
        "speedups": speedups,
    }


def command_output(command: list[str]) -> str | None:
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def matrix_metadata(
    args: argparse.Namespace, implementations: list[str], docker_preflight: dict[str, Any] | None = None
) -> dict[str, Any]:
    image = os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify")
    docker_is_healthy = docker_preflight is None or docker_preflight.get("status") == "healthy"
    image_id = command_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"]) if docker_is_healthy else None
    image_created = (
        command_output(["docker", "image", "inspect", image, "--format", "{{.Created}}"]) if docker_is_healthy else None
    )
    git_rev = command_output(["git", "rev-parse", "HEAD"])
    git_status = command_output(["git", "status", "--short"])
    cpu_quota = (
        command_output(["docker", "info", "--format", "{{.NCPU}} logical CPUs available to Docker host"])
        if docker_is_healthy
        else None
    )
    return {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "workdir": str(ROOT),
        "suite": args.suite,
        "lifecycle": args.lifecycle,
        "iterations": args.iterations,
        "repetitions": args.repetitions,
        "case_filters": args.case_filters or [],
        "implementations": implementations,
        "container_isolation": "one_container_per_implementation_per_repetition",
        "parallelism": "sequential",
        "docker_memory_limit": os.environ.get("TEST_DOCKER_MEMORY_LIMIT", "8g"),
        "docker_memory_swap_limit": os.environ.get("TEST_DOCKER_MEMORY_LIMIT", "8g"),
        "docker_cpu_quota": "unbounded_by_wrapper",
        "docker_cpu_host_info": cpu_quota,
        "rustwright_rebuild_mode": "release_wheel_each_rustwright_repetition" if args.rebuild_rustwright and not args.skip_rustwright_rebuild else "reuse_image_extension",
        "rustwright_cdp_transport": os.environ.get("RUSTWRIGHT_CDP_TRANSPORT") or "websocket",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "docker_image": image,
        "docker_image_id": image_id,
        "docker_image_created": image_created,
        "docker_preflight": docker_preflight,
        "git_rev": git_rev,
        "git_status_short": git_status,
    }


def markdown_table(result: dict[str, Any]) -> str:
    rows = [
        "| Implementation | Status | Total median ms | Comparison |",
        "| --- | --- | ---: | --- |",
    ]
    speedup = result["speedups"]
    rows_source = result.get("aggregate", {}).items() if result.get("aggregate") else []
    if rows_source:
        for name, item in rows_source:
            total = item["total_mean_ms"]["median"]
            p25 = item["total_mean_ms"]["p25"]
            p75 = item["total_mean_ms"]["p75"]
            comparison = "baseline"
            rustwright = result["aggregate"].get("rustwright")
            if name != "rustwright" and rustwright:
                baseline = float(total)
                rust = float(rustwright["total_mean_ms"]["median"])
                if baseline > 0:
                    reduction = (baseline - rust) / baseline * 100
                    comparison = f"Rustwright lower by {reduction:.1f}%" if reduction >= 0 else f"Rustwright higher by {abs(reduction):.1f}%"
            rows.append(f"| {name} | passed | {float(total):.2f} (p25 {float(p25):.2f}, p75 {float(p75):.2f}) | {comparison} |")
        return "\n".join(rows)
    for item in result["results"]:
        name = item["implementation"]
        if item.get("status") != "passed":
            rows.append(f"| {name} | failed |  | see JSON output |")
            continue
        comparison = "baseline"
        if name != "rustwright":
            value = speedup.get(f"vs_{name}_reduction_pct")
            if value is not None:
                comparison = f"Rustwright lower by {value:.1f}%" if value >= 0 else f"Rustwright higher by {abs(value):.1f}%"
        rows.append(f"| {name} | passed | {float(item['total_mean_ms']):.2f} | {comparison} |")
    return "\n".join(rows)


def default_result_path(args: argparse.Namespace) -> Path:
    case_suffix = ""
    if args.case_filters:
        safe_names = []
        for name in args.case_filters:
            safe_names.append("".join(char if char.isalnum() or char in "-_" else "-" for char in name))
        case_suffix = "-cases-" + "+".join(safe_names)
    return ROOT / ".benchmark-data" / "results" / (
        f"bench-full-{args.suite}-{args.lifecycle}{case_suffix}-{args.repetitions}x{args.iterations}.json"
    )


def write_result(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run benchmark implementations in separate Docker containers through tools/docker_test.sh."
    )
    parser.add_argument("--iterations", type=int, default=default_iterations())
    parser.add_argument("--repetitions", type=int, default=1, help="Repeat the full implementation matrix this many times.")
    parser.add_argument("--impl", action="append", help="Implementation to run. Defaults to the standard matrix.")
    parser.add_argument("--include-puppeteer", action="store_true", help="Add typescript-puppeteer to the default matrix.")
    parser.add_argument("--suite", choices=["equivalent", "strict"], default="equivalent")
    parser.add_argument(
        "--case",
        action="append",
        dest="case_filters",
        help="Forward a named Python benchmark case to tools/docker_test.sh bench. Repeat for multiple cases.",
    )
    parser.add_argument(
        "--lifecycle",
        choices=["warm-browser", "warm-page", "cold-browser", "cold-container"],
        default="warm-browser",
    )
    parser.add_argument(
        "--rebuild-rustwright",
        action="store_true",
        help="Build and install a Rustwright release wheel inside each Rustwright benchmark container.",
    )
    parser.add_argument(
        "--skip-rustwright-rebuild",
        action="store_true",
        help="Deprecated compatibility flag; Rustwright rebuilds are skipped by default unless --rebuild-rustwright is set.",
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--docker-preflight-timeout",
        type=int,
        default=int(os.environ.get("BENCHMARK_DOCKER_PREFLIGHT_TIMEOUT", "10")),
        help="Seconds to wait for docker info before reporting the matrix as skipped.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the benchmark matrix JSON to this path. Defaults to .benchmark-data/results/bench-full-<suite>-<lifecycle>-<repetitions>x<iterations>.json.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a Markdown table.")
    parser.add_argument("--allow-failures", action="store_true", help="Exit 0 even if an implementation fails.")
    args = parser.parse_args()

    implementations = args.impl or (list(STRICT_IMPLS) if args.suite == "strict" else list(DEFAULT_IMPLS))
    if args.include_puppeteer and args.suite == "equivalent" and "typescript-puppeteer" not in implementations:
        implementations.append("typescript-puppeteer")
    if args.suite == "strict":
        unsupported = [implementation for implementation in implementations if implementation not in STRICT_IMPLS]
        if unsupported:
            raise SystemExit(f"--suite strict only supports: {', '.join(STRICT_IMPLS)}; got {', '.join(unsupported)}")
    planned_runs = [(repetition, implementation) for repetition in range(args.repetitions) for implementation in implementations]
    docker_preflight = docker_health_check(args.docker_preflight_timeout)
    if docker_preflight.get("status") != "healthy":
        results = skipped_results_after_docker_preflight(planned_runs)
    else:
        results = []
        stop_remaining_reason: str | None = None
        for repetition, implementation in planned_runs:
            if stop_remaining_reason is not None:
                results.append(
                    {
                        "implementation": implementation,
                        "status": "skipped",
                        "reason": stop_remaining_reason,
                        "failure_kind": "docker_daemon_error",
                        "repetition": repetition + 1,
                    }
                )
                continue
            result = run_impl(args, implementation)
            result["repetition"] = repetition + 1
            results.append(result)
            if result.get("failure_kind") == "docker_daemon_error":
                stop_remaining_reason = "skipped_after_docker_daemon_error"
    aggregate = aggregate_repetitions(results)
    result = {
        "iterations": args.iterations,
        "repetitions": args.repetitions,
        "suite": args.suite,
        "lifecycle": args.lifecycle,
        "case_filters": args.case_filters or [],
        "container_isolation": "one_container_per_implementation_per_repetition",
        "metadata": matrix_metadata(args, implementations, docker_preflight),
        "results": results,
        "rustwright_rebuild_target_cache": os.environ.get("RUSTWRIGHT_DOCKER_REBUILD_TARGET_CACHE") == "1",
        "aggregate": aggregate,
        "common_case_comparison": common_case_comparison(aggregate),
        "case_winners": case_winners_from_aggregate(aggregate) if aggregate else case_winners_from_results(results),
        "speedups": speedups_from_aggregate(aggregate) if aggregate else speedups_from_results(results),
    }
    output_path = Path(args.output) if args.output else default_result_path(args)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    try:
        result["result_path"] = str(output_path.relative_to(ROOT))
    except ValueError:
        result["result_path"] = str(output_path)
    write_result(result, output_path)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(markdown_table(result))

    failed = [item for item in results if item.get("status") != "passed"]
    return 0 if args.allow_failures or not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
