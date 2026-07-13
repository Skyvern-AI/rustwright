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
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / ".benchmark-data/manifests/mind2web-train-100pct-action-fixtures.json"
DEFAULT_IMPLS = ["rustwright-py", "playwright", "rustwright-ts", "typescript-playwright", "typescript-puppeteer"]
EXPERIMENTAL_IMPLS = ["rustwright-ts-cdp"]
LEGACY_IMPL_ALIASES = {
    "rustwright": "rustwright-py",
    "typescript-rustwright-binding": "rustwright-ts",
    "typescript-rustwright-cdp": "rustwright-ts-cdp",
}


def canonical_impl(implementation: str) -> str:
    return LEGACY_IMPL_ALIASES.get(implementation, implementation)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    raise ValueError(f"could not find JSON in output:\n{output[-4000:]}")


def command_output(command: list[str]) -> str | None:
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def docker_available() -> bool:
    try:
        proc = subprocess.run(["docker", "info"], cwd=ROOT, text=True, capture_output=True, timeout=10)
    except Exception:
        return False
    return proc.returncode == 0


def maybe_start_docker() -> None:
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(["open", "-a", "Docker"], cwd=ROOT, text=True, capture_output=True, timeout=10)
    except Exception:
        return


def wait_for_docker(timeout_seconds: int, *, start_app: bool) -> bool:
    deadline = time.monotonic() + timeout_seconds
    if start_app:
        maybe_start_docker()
    while time.monotonic() < deadline:
        if docker_available():
            return True
        time.sleep(5)
    return docker_available()


def is_docker_infrastructure_failure(result: dict[str, Any]) -> bool:
    text = str(result.get("output_tail") or "").lower()
    if result.get("status") == "not_run":
        return True
    if result.get("failure_kind") in {"docker_daemon_unavailable", "timeout"}:
        return True
    if result.get("returncode") == 125:
        return True
    return any(
        marker in text
        for marker in (
            "cannot connect to the docker daemon",
            "docker daemon",
            "unexpected eof",
            "error waiting for container",
        )
    )


def write_shard_manifest(source: dict[str, Any], tasks: list[dict[str, Any]], path: Path, shard_index: int) -> None:
    payload = {key: value for key, value in source.items() if key != "tasks"}
    payload["tasks"] = tasks
    payload["shard"] = {"index": shard_index, "task_count": len(tasks)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def run_shard(
    args: argparse.Namespace,
    implementation: str,
    repetition: int,
    shard_path: Path,
    shard_index: int,
    task_count: int,
) -> dict[str, Any]:
    env = {
        **os.environ,
        "MIND2WEB_ITERATIONS": str(args.iterations),
        "MIND2WEB_PROGRESS_EVERY": str(args.progress_every),
        "MIND2WEB_MAX_TASK_SECONDS": str(args.max_task_seconds),
        "MIND2WEB_FIXTURE_TIMEOUT_MS": str(args.fixture_timeout_ms),
        "MIND2WEB_FIXTURE_WAIT_UNTIL": args.fixture_wait_until,
    }
    if args.docker_memory_limit:
        env["TEST_DOCKER_MEMORY_LIMIT"] = args.docker_memory_limit
    try:
        manifest_arg = str(shard_path.resolve().relative_to(ROOT))
    except ValueError:
        manifest_arg = str(shard_path)
    command = [
        str(ROOT / "tools/docker_test.sh"),
        "mind2web",
        "--impl",
        implementation,
        "--manifest",
        manifest_arg,
        "--percentage",
        "100",
        "--seed",
        "0",
        "--json",
    ]
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env, timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        combined = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
        return {
            "status": "failed",
            "implementation": implementation,
            "repetition": repetition,
            "shard_index": shard_index,
            "task_count": task_count,
            "failure_kind": "timeout",
            "output_tail": "\n".join(combined.splitlines()[-40:]),
        }
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0:
        return {
            "status": "failed",
            "implementation": implementation,
            "repetition": repetition,
            "shard_index": shard_index,
            "task_count": task_count,
            "returncode": proc.returncode,
            "output_tail": "\n".join(combined.splitlines()[-40:]),
        }
    result = extract_json(combined)
    result["status"] = "passed"
    result["implementation"] = implementation
    result["repetition"] = repetition
    result["shard_index"] = shard_index
    result["task_count"] = task_count
    result["command"] = " ".join(command)
    return result


def shard_result_path(args: argparse.Namespace, implementation: str, repetition: int, shard_index: int) -> Path:
    safe_impl = implementation.replace("-", "_")
    return args.work_dir / "results" / f"{args.output.stem}-rep-{repetition:02d}-{safe_impl}-shard-{shard_index:03d}.json"


def read_saved_shard(args: argparse.Namespace, implementation: str, repetition: int, shard_index: int) -> dict[str, Any] | None:
    if not args.resume:
        return None
    path = shard_result_path(args, implementation, repetition, shard_index)
    if not path.is_file():
        return None
    result = load_json(path)
    if result.get("status") == "passed" and result.get("quality"):
        result["resumed_from"] = str(path)
        return result
    return None


def write_saved_shard(args: argparse.Namespace, implementation: str, repetition: int, shard_index: int, result: dict[str, Any]) -> None:
    path = shard_result_path(args, implementation, repetition, shard_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")


def merge_results(
    args: argparse.Namespace,
    implementation: str,
    repetition: int,
    source_tasks: list[dict[str, Any]],
    shard_results: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = failed = skipped = 0
    infrastructure_failed = 0
    not_run = 0
    tasks: dict[str, Any] = {}
    cases: dict[str, Any] = {}
    total_mean_ms = 0.0
    expected_shard_count = (len(source_tasks) + args.shard_size - 1) // args.shard_size
    passed_shards = [item for item in shard_results if item.get("status") == "passed" and item.get("quality")]
    failed_shards = [item for item in shard_results if item.get("status") != "passed"]
    for item in passed_shards:
        quality = item["quality"]
        passed += int(quality.get("passed_runs") or 0)
        failed += int(quality.get("failed_runs") or 0)
        skipped += int(quality.get("skipped_runs") or 0)
        tasks.update(item.get("tasks") or {})
        cases.update(item.get("cases") or {})
        total_mean_ms += float(item.get("total_mean_ms") or 0.0)
    for item in failed_shards:
        count = int(item.get("task_count") or 0) * args.iterations
        if item.get("status") == "not_run":
            not_run += count
        else:
            infrastructure_failed += count
    quality = {
        "task_count": len(source_tasks),
        "total_runs": len(source_tasks) * args.iterations,
        "passed_runs": passed,
        "failed_runs": failed,
        "skipped_runs": skipped,
        "attempted_runs": passed + failed + skipped,
        "infrastructure_failed_runs": infrastructure_failed,
        "not_run_runs": not_run,
    }
    denominator = quality["passed_runs"] + quality["failed_runs"]
    quality["success_rate"] = quality["passed_runs"] / denominator if denominator else 0.0
    complete = (
        len(passed_shards) == expected_shard_count
        and not failed_shards
        and quality["attempted_runs"] == quality["total_runs"]
        and quality["failed_runs"] == 0
        and quality["infrastructure_failed_runs"] == 0
        and quality["not_run_runs"] == 0
    )
    return {
        "implementation": implementation,
        "repetition": repetition,
        "status": "passed" if complete else "failed",
        "iterations": args.iterations,
        "metadata": {
            "suite": "mind2web",
            "comparison_mode": "mind2web_offline_action_replay_sharded",
            "case_count": len(source_tasks),
            "shard_size": args.shard_size,
            "expected_shard_count": expected_shard_count,
            "shard_count": len(shard_results),
            "passed_shards": len(passed_shards),
            "failed_shards": len(failed_shards),
            "container_isolation": "one_container_per_shard",
            "docker_memory_limit": args.docker_memory_limit or os.environ.get("TEST_DOCKER_MEMORY_LIMIT") or "8g",
            "fixture_timeout_ms": args.fixture_timeout_ms,
            "fixture_wait_until": args.fixture_wait_until,
            "max_task_seconds": args.max_task_seconds,
            "shard_attempts": args.shard_attempts,
            "docker_recovery_wait_seconds": args.docker_recovery_wait_seconds,
            "resume": args.resume,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "docker_image": os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify"),
            "docker_image_id": command_output(["docker", "image", "inspect", os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify"), "--format", "{{.Id}}"]),
            "git_rev": command_output(["git", "rev-parse", "HEAD"]),
        },
        "quality": quality,
        "tasks": tasks,
        "cases": cases,
        "total_mean_ms": total_mean_ms,
        "shards": shard_results,
    }


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
        if item.get("quality"):
            grouped.setdefault(str(item["implementation"]), []).append(item)
    aggregate = {}
    for implementation, items in grouped.items():
        aggregate[implementation] = {
            "runs": len(items),
            "passed_status_runs": sum(1 for item in items if item.get("status") == "passed"),
            "failed_status_runs": sum(1 for item in items if item.get("status") != "passed"),
            "success_rate": aggregate_values([float(item["quality"]["success_rate"]) for item in items]),
            "passed_runs": sum(int(item["quality"]["passed_runs"]) for item in items),
            "failed_runs": sum(int(item["quality"]["failed_runs"]) for item in items),
            "skipped_runs": sum(int(item["quality"]["skipped_runs"]) for item in items),
            "infrastructure_failed_runs": sum(int(item["quality"].get("infrastructure_failed_runs") or 0) for item in items),
            "not_run_runs": sum(int(item["quality"].get("not_run_runs") or 0) for item in items),
            "total_mean_ms": aggregate_values([float(item.get("total_mean_ms") or 0.0) for item in items]),
        }
    return aggregate


def matrix_result(args: argparse.Namespace, implementations: list[str], results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "suite": "mind2web",
        "status": "passed" if all(item.get("status") == "passed" for item in results) else "failed",
        "iterations": args.iterations,
        "repetitions": args.repetitions,
        "metadata": {
            "comparison_mode": "mind2web_offline_action_replay_sharded_matrix",
            "manifest": str(args.manifest),
            "implementations": implementations,
            "container_isolation": "one_container_per_shard",
            "parallelism": "sequential",
            "docker_memory_limit": args.docker_memory_limit or os.environ.get("TEST_DOCKER_MEMORY_LIMIT") or "8g",
            "fixture_timeout_ms": args.fixture_timeout_ms,
            "fixture_wait_until": args.fixture_wait_until,
            "max_task_seconds": args.max_task_seconds,
            "shard_size": args.shard_size,
            "shard_attempts": args.shard_attempts,
            "docker_recovery_wait_seconds": args.docker_recovery_wait_seconds,
            "resume": args.resume,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "docker_image": os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify"),
            "docker_image_id": command_output(["docker", "image", "inspect", os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify"), "--format", "{{.Id}}"]),
            "git_rev": command_output(["git", "rev-parse", "HEAD"]),
        },
        "results": results,
        "aggregate": aggregate_results(results),
    }


def markdown_table(result: dict[str, Any]) -> str:
    rows = [
        "| Implementation | Runs | Passed status | Success median | Passed | Failed | Infra failed | Total median ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for implementation, item in sorted(result.get("aggregate", {}).items()):
        rows.append(
            f"| {implementation} | {item['runs']} | {item['passed_status_runs']} | "
            f"{float(item['success_rate']['median']) * 100:.1f}% | {item['passed_runs']} | "
            f"{item['failed_runs']} | {item['infrastructure_failed_runs']} | "
            f"{float(item['total_mean_ms']['median']):.2f} |"
        )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a full Mind2Web benchmark as smaller Docker shards.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--impl", action="append", help="Implementation to run. Defaults to rustwright for legacy single-output mode.")
    parser.add_argument("--shard-size", type=int, default=100)
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--docker-memory-limit", default=os.environ.get("TEST_DOCKER_MEMORY_LIMIT", "6g"))
    parser.add_argument("--fixture-timeout-ms", type=int, default=int(os.environ.get("MIND2WEB_FIXTURE_TIMEOUT_MS", "8000")))
    parser.add_argument("--fixture-wait-until", default=os.environ.get("MIND2WEB_FIXTURE_WAIT_UNTIL", "commit"))
    parser.add_argument("--max-task-seconds", type=float, default=float(os.environ.get("MIND2WEB_MAX_TASK_SECONDS", "30")))
    parser.add_argument("--progress-every", type=int, default=int(os.environ.get("MIND2WEB_PROGRESS_EVERY", "25")))
    parser.add_argument("--shard-attempts", type=int, default=int(os.environ.get("MIND2WEB_SHARD_ATTEMPTS", "3")))
    parser.add_argument(
        "--docker-recovery-wait-seconds",
        type=int,
        default=int(os.environ.get("MIND2WEB_DOCKER_RECOVERY_WAIT_SECONDS", "180")),
    )
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--start-docker-on-macos", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".benchmark-data/tmp/mind2web-shards")
    parser.add_argument("--output", type=Path, default=ROOT / ".benchmark-data/results/mind2web-train-100pct-1x-rustwright-setcontent-watchdog-docker-sharded-20260530.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    manifest = load_json(args.manifest)
    tasks = [task for task in manifest.get("tasks") or [] if isinstance(task, dict)]
    if args.max_tasks is not None:
        tasks = tasks[: args.max_tasks]
    if not tasks:
        raise SystemExit(f"{args.manifest} has no tasks")
    if args.impl and "all" in args.impl:
        implementations = DEFAULT_IMPLS
    else:
        implementations = [canonical_impl(implementation) for implementation in (args.impl or ["rustwright-py"])]
    all_results = []
    shard_count = (len(tasks) + args.shard_size - 1) // args.shard_size
    for repetition in range(1, args.repetitions + 1):
        for implementation in implementations:
            shard_results = []
            for start in range(0, len(tasks), args.shard_size):
                shard_index = start // args.shard_size
                shard_tasks = tasks[start : start + args.shard_size]
                saved = read_saved_shard(args, implementation, repetition, shard_index)
                if saved:
                    shard_results.append(saved)
                    print(
                        f"[mind2web-sharded:{implementation}:rep{repetition}] reusing shard "
                        f"{shard_index + 1}/{shard_count} ({len(shard_tasks)} tasks)",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                shard_path = args.work_dir / f"{args.manifest.stem}-{implementation}-rep-{repetition:02d}-shard-{shard_index:03d}.json"
                write_shard_manifest(manifest, shard_tasks, shard_path, shard_index)
                print(
                    f"[mind2web-sharded:{implementation}:rep{repetition}] running shard "
                    f"{shard_index + 1}/{shard_count} ({len(shard_tasks)} tasks)",
                    file=sys.stderr,
                    flush=True,
                )
                result = {
                    "status": "not_run",
                    "implementation": implementation,
                    "repetition": repetition,
                    "shard_index": shard_index,
                    "task_count": len(shard_tasks),
                    "failure_kind": "docker_daemon_unavailable",
                    "output_tail": "Docker daemon unavailable before shard start",
                }
                for attempt in range(1, max(1, args.shard_attempts) + 1):
                    if not docker_available() and not wait_for_docker(args.docker_recovery_wait_seconds, start_app=args.start_docker_on_macos):
                        result = {
                            "status": "not_run",
                            "implementation": implementation,
                            "repetition": repetition,
                            "shard_index": shard_index,
                            "task_count": len(shard_tasks),
                            "attempt": attempt,
                            "failure_kind": "docker_daemon_unavailable",
                            "output_tail": "Docker daemon unavailable before shard start",
                        }
                    else:
                        result = run_shard(args, implementation, repetition, shard_path, shard_index, len(shard_tasks))
                        result["attempt"] = attempt
                    if result.get("status") == "passed":
                        break
                    if not is_docker_infrastructure_failure(result):
                        break
                    if attempt < max(1, args.shard_attempts):
                        print(
                            f"[mind2web-sharded:{implementation}:rep{repetition}] shard {shard_index + 1}/{shard_count} "
                            f"hit Docker infrastructure failure; waiting to retry attempt {attempt + 1}/{args.shard_attempts}",
                            file=sys.stderr,
                            flush=True,
                        )
                        wait_for_docker(args.docker_recovery_wait_seconds, start_app=args.start_docker_on_macos)
                shard_results.append(result)
                write_saved_shard(args, implementation, repetition, shard_index, result)
                if result.get("status") != "passed":
                    break
            all_results.append(merge_results(args, implementation, repetition, tasks, shard_results))
    legacy_single_output = len(implementations) == 1 and args.repetitions == 1
    result = all_results[0] if legacy_single_output else matrix_result(args, implementations, all_results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif not legacy_single_output:
        print(markdown_table(result))
    else:
        q = result["quality"]
        print(
            f"{implementations[0]}: success {float(q['success_rate']) * 100:.1f}% "
            f"({q['passed_runs']} passed, {q['failed_runs']} failed, {q['skipped_runs']} skipped), "
            f"shards {result['metadata']['passed_shards']}/{result['metadata']['shard_count']} passed"
        )
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
