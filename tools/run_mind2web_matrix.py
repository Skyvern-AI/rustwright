#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMPLS = ["rustwright-py", "playwright", "rustwright-ts", "typescript-playwright", "typescript-puppeteer"]
LEGACY_IMPL_ALIASES = {
    "rustwright": "rustwright-py",
    "typescript-rustwright-binding": "rustwright-ts",
    "typescript-rustwright-cdp": "rustwright-ts-cdp",
}


def canonical_impl(implementation: str) -> str:
    return LEGACY_IMPL_ALIASES.get(implementation, implementation)


def default_output_path(args: argparse.Namespace) -> Path:
    percent = str(args.percentage).replace(".", "p")
    return ROOT / ".benchmark-data" / "results" / (
        f"mind2web-{percent}pct-seed{args.seed}-{args.repetitions}x{args.iterations}.json"
    )


def command_output(command: list[str]) -> str | None:
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def docker_health_check(timeout: int = 10) -> dict[str, Any]:
    try:
        proc = subprocess.run(["docker", "info"], cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
        return {
            "status": "unhealthy",
            "failure_kind": "docker_daemon_error",
            "output_tail": "\n".join(output.splitlines()[-40:]),
        }
    except OSError as exc:
        return {"status": "unhealthy", "failure_kind": "docker_daemon_error", "output_tail": str(exc)}
    combined = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return {"status": "healthy", "returncode": 0, "output_tail": "\n".join(combined.splitlines()[-20:])}
    return {
        "status": "unhealthy",
        "returncode": proc.returncode,
        "failure_kind": "docker_daemon_error",
        "output_tail": "\n".join(combined.splitlines()[-40:]),
    }


def extract_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"could not find JSON in output:\n{output}")


def run_impl(args: argparse.Namespace, implementation: str) -> dict[str, Any]:
    env = {**os.environ, "MIND2WEB_ITERATIONS": str(args.iterations)}
    command = [
        str(ROOT / "tools" / "docker_test.sh"),
        "mind2web",
        "--impl",
        implementation,
        "--manifest",
        args.manifest,
        "--percentage",
        str(args.percentage),
        "--seed",
        str(args.seed),
        "--json",
    ]
    if args.max_tasks is not None:
        command.extend(["--max-tasks", str(args.max_tasks)])
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env, timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        combined = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
        return {
            "implementation": implementation,
            "status": "failed",
            "failure_kind": "timeout",
            "timeout_s": args.timeout,
            "output_tail": "\n".join(combined.splitlines()[-40:]),
        }
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0:
        return {
            "implementation": implementation,
            "status": "failed",
            "returncode": proc.returncode,
            "output_tail": "\n".join(combined.splitlines()[-40:]),
        }
    result = extract_json(combined)
    result.setdefault("status", "passed")
    result["container_isolation"] = "separate_container"
    result["command"] = " ".join(command)
    return result


def aggregate_values(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "p25": 0.0, "p75": 0.0, "min": 0.0, "max": 0.0, "stdev": 0.0}
    ordered = sorted(values)
    def pct(percent: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * percent
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p25": pct(0.25),
        "p75": pct(0.75),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        if item.get("status") == "passed" and "quality" in item:
            grouped.setdefault(item["implementation"], []).append(item)
    aggregate = {}
    for implementation, items in grouped.items():
        aggregate[implementation] = {
            "runs": len(items),
            "success_rate": aggregate_values([float(item["quality"]["success_rate"]) for item in items]),
            "passed_runs": sum(int(item["quality"]["passed_runs"]) for item in items),
            "failed_runs": sum(int(item["quality"]["failed_runs"]) for item in items),
            "skipped_runs": sum(int(item["quality"]["skipped_runs"]) for item in items),
            "total_mean_ms": aggregate_values([float(item["total_mean_ms"]) for item in items]),
        }
    return aggregate


def matrix_metadata(args: argparse.Namespace, implementations: list[str], docker_preflight: dict[str, Any]) -> dict[str, Any]:
    image = os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify")
    healthy = docker_preflight.get("status") == "healthy"
    docker_memory_limit = os.environ.get("TEST_DOCKER_MEMORY_LIMIT", "8g")
    docker_memory_swap_limit = os.environ.get("TEST_DOCKER_MEMORY_SWAP_LIMIT", docker_memory_limit)
    return {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "workdir": str(ROOT),
        "suite": "mind2web",
        "manifest": args.manifest,
        "percentage": args.percentage,
        "seed": args.seed,
        "iterations": args.iterations,
        "repetitions": args.repetitions,
        "implementations": implementations,
        "container_isolation": "one_container_per_implementation_per_repetition",
        "parallelism": "sequential",
        "docker_memory_limit": docker_memory_limit,
        "docker_memory_swap_limit": docker_memory_swap_limit,
        "docker_cpu_quota": "unbounded_by_wrapper",
        "docker_cpu_host_info": command_output(["docker", "info", "--format", "{{.NCPU}} logical CPUs available to Docker host"]) if healthy else None,
        "docker_image": image,
        "docker_image_id": command_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"]) if healthy else None,
        "docker_image_created": command_output(["docker", "image", "inspect", image, "--format", "{{.Created}}"]) if healthy else None,
        "docker_preflight": docker_preflight,
        "git_rev": command_output(["git", "rev-parse", "HEAD"]),
        "git_status_short": command_output(["git", "status", "--short"]),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def markdown_table(result: dict[str, Any]) -> str:
    rows = [
        "| Implementation | Runs | Success Rate Median | Passed | Failed | Skipped | Total Median ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for implementation, item in result.get("aggregate", {}).items():
        rows.append(
            f"| {implementation} | {item['runs']} | {float(item['success_rate']['median']) * 100:.1f}% | "
            f"{item['passed_runs']} | {item['failed_runs']} | {item['skipped_runs']} | "
            f"{float(item['total_mean_ms']['median']):.2f} |"
        )
    for item in result["results"]:
        if item.get("status") == "skipped":
            rows.append(f"| {item['implementation']} | 0 | skipped | 0 | 0 | 0 |  |")
        elif item.get("status") == "failed":
            rows.append(f"| {item['implementation']} | 0 | failed | 0 | 1 | 0 |  |")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Mind2Web benchmark implementations in separate Docker containers.")
    parser.add_argument("--manifest", default=str(ROOT / ".benchmark-data" / "manifests" / "mind2web_tasks.json"))
    parser.add_argument("--percentage", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=int(os.environ.get("MIND2WEB_FULL_ITERATIONS", "1")))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--impl", action="append", help="Implementation to run. Defaults to all Mind2Web targets.")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("MIND2WEB_CONTAINER_TIMEOUT", "3600")))
    parser.add_argument("--docker-preflight-timeout", type=int, default=10)
    parser.add_argument("--output", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-failures", action="store_true")
    args = parser.parse_args()

    implementations = [canonical_impl(implementation) for implementation in (args.impl or list(DEFAULT_IMPLS))]
    planned = [(repetition, implementation) for repetition in range(args.repetitions) for implementation in implementations]
    docker_preflight = docker_health_check(args.docker_preflight_timeout)
    output_path = Path(args.output) if args.output else default_output_path(args)
    if not output_path.is_absolute():
        output_path = ROOT / output_path

    def current_output(results: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "suite": "mind2web",
            "iterations": args.iterations,
            "repetitions": args.repetitions,
            "metadata": matrix_metadata(args, implementations, docker_preflight),
            "results": results,
            "aggregate": aggregate_results(results),
            "result_path": str(output_path.relative_to(ROOT) if output_path.is_relative_to(ROOT) else output_path),
        }

    def write_current(results: list[dict[str, Any]]) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(current_output(results), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if docker_preflight.get("status") != "healthy":
        results = [
            {
                "implementation": implementation,
                "status": "skipped",
                "reason": "skipped_after_docker_preflight_failure",
                "failure_kind": "docker_daemon_error",
                "repetition": repetition + 1,
            }
            for repetition, implementation in planned
        ]
    else:
        results = []
        for repetition, implementation in planned:
            result = run_impl(args, implementation)
            result["repetition"] = repetition + 1
            results.append(result)
            write_current(results)
    output = current_output(results)
    write_current(results)
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(markdown_table(output))
    failed = [item for item in results if item.get("status") not in {"passed", "skipped"}]
    return 0 if args.allow_failures or not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
