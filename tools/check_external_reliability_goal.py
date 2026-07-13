#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIND2WEB_LOCAL = (
    ROOT / ".benchmark-data/results/mind2web-train-100pct-1x-rustwright-setcontent-watchdog-local-20260530.json"
)
DEFAULT_MIND2WEB_DOCKER = (
    ROOT / ".benchmark-data/results/mind2web-train-100pct-1x-rustwright-setcontent-watchdog-docker-sharded-20260530.json"
)
DEFAULT_WEBVOYAGER = (
    ROOT / ".benchmark-data/results/webvoyager-real-nosensitive-100pct-1x-rustwright-devshm-cold-browser-20260530.json"
)


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"could not find JSON object in {path}")


def result_for_impl(data: dict[str, Any], implementation: str) -> dict[str, Any] | None:
    if data.get("implementation") == implementation:
        return data
    for item in data.get("results") or []:
        if item.get("implementation") == implementation:
            return item
    return None


def quality(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    return result.get("quality") or {}


def metadata(data: dict[str, Any] | None, result: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = {}
    if data:
        merged.update(data.get("metadata") or {})
    if result:
        merged.update(result.get("metadata") or {})
    return merged


def check(condition: bool, name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(condition), "detail": detail}


def memory_limit_bytes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        if text.endswith("g"):
            return int(float(text[:-1]) * 1024 * 1024 * 1024)
        if text.endswith("m"):
            return int(float(text[:-1]) * 1024 * 1024)
        return int(text)
    except ValueError:
        return None


def memory_at_or_below_8gb(value: Any) -> bool:
    parsed = memory_limit_bytes(value)
    return parsed is not None and parsed <= 8 * 1024 * 1024 * 1024


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    mind2web_local_data = load_json(args.mind2web_local)
    mind2web_docker_data = load_json(args.mind2web_docker)
    webvoyager_data = load_json(args.webvoyager)

    mind2web_local = result_for_impl(mind2web_local_data or {}, "rustwright")
    mind2web_docker = result_for_impl(mind2web_docker_data or {}, "rustwright")
    webvoyager = result_for_impl(webvoyager_data or {}, "rustwright")

    mind2web_local_q = quality(mind2web_local)
    mind2web_docker_q = quality(mind2web_docker)
    webvoyager_q = quality(webvoyager)
    mind2web_local_meta = metadata(mind2web_local_data, mind2web_local)
    mind2web_docker_meta = metadata(mind2web_docker_data, mind2web_docker)
    webvoyager_meta = metadata(webvoyager_data, webvoyager)

    checks = [
        check(mind2web_local_data is not None, "mind2web_local_file", str(args.mind2web_local)),
        check(
            int(mind2web_local_q.get("task_count") or 0) >= args.mind2web_min_tasks,
            "mind2web_local_task_count",
            f"expected >= {args.mind2web_min_tasks}, got {mind2web_local_q.get('task_count')}",
        ),
        check(
            int(mind2web_local_q.get("failed_runs") or 0) == 0,
            "mind2web_local_failures",
            f"failed_runs={mind2web_local_q.get('failed_runs')}",
        ),
        check(
            float(mind2web_local_q.get("success_rate") or 0.0) >= args.min_success_rate,
            "mind2web_local_success_rate",
            f"success_rate={mind2web_local_q.get('success_rate')}",
        ),
        check(
            mind2web_local_meta.get("max_task_seconds") is not None,
            "mind2web_local_watchdog",
            f"max_task_seconds={mind2web_local_meta.get('max_task_seconds')}",
        ),
        check(webvoyager_data is not None, "webvoyager_file", str(args.webvoyager)),
        check(
            int(webvoyager_q.get("task_count") or 0) >= args.webvoyager_min_tasks,
            "webvoyager_task_count",
            f"expected >= {args.webvoyager_min_tasks}, got {webvoyager_q.get('task_count')}",
        ),
        check(
            int(webvoyager_q.get("failed_runs") or 0) == 0,
            "webvoyager_failures",
            f"failed_runs={webvoyager_q.get('failed_runs')}",
        ),
        check(
            float(webvoyager_q.get("success_rate") or 0.0) >= args.min_success_rate,
            "webvoyager_success_rate",
            f"success_rate={webvoyager_q.get('success_rate')}",
        ),
        check(
            webvoyager_meta.get("container_isolation") == "one_container_per_implementation_per_repetition",
            "webvoyager_docker_isolation",
            f"container_isolation={webvoyager_meta.get('container_isolation')}",
        ),
        check(
            memory_at_or_below_8gb(webvoyager_meta.get("docker_memory_limit")),
            "webvoyager_docker_memory",
            f"docker_memory_limit={webvoyager_meta.get('docker_memory_limit')}",
        ),
    ]

    docker_checks = [
        check(mind2web_docker_data is not None, "mind2web_docker_file", str(args.mind2web_docker)),
        check(
            mind2web_docker_data is not None and mind2web_docker.get("status") == "passed",
            "mind2web_docker_status",
            f"status={mind2web_docker.get('status') if mind2web_docker else None}",
        ),
        check(
            mind2web_docker_data is not None and int(mind2web_docker_q.get("task_count") or 0) >= args.mind2web_min_tasks,
            "mind2web_docker_task_count",
            f"expected >= {args.mind2web_min_tasks}, got {mind2web_docker_q.get('task_count')}",
        ),
        check(
            mind2web_docker_data is not None
            and int(mind2web_docker_q.get("attempted_runs") or 0) >= args.mind2web_min_tasks,
            "mind2web_docker_attempted_runs",
            f"expected >= {args.mind2web_min_tasks}, got {mind2web_docker_q.get('attempted_runs')}",
        ),
        check(
            mind2web_docker_data is not None and int(mind2web_docker_q.get("failed_runs") or 0) == 0,
            "mind2web_docker_failures",
            f"failed_runs={mind2web_docker_q.get('failed_runs')}",
        ),
        check(
            mind2web_docker_data is not None
            and mind2web_docker_q.get("infrastructure_failed_runs") is not None
            and int(mind2web_docker_q.get("infrastructure_failed_runs") or 0) == 0,
            "mind2web_docker_infrastructure_failures",
            f"infrastructure_failed_runs={mind2web_docker_q.get('infrastructure_failed_runs')}",
        ),
        check(
            mind2web_docker_data is not None
            and mind2web_docker_q.get("not_run_runs") is not None
            and int(mind2web_docker_q.get("not_run_runs") or 0) == 0,
            "mind2web_docker_not_run",
            f"not_run_runs={mind2web_docker_q.get('not_run_runs')}",
        ),
        check(
            mind2web_docker_data is not None
            and float(mind2web_docker_q.get("success_rate") or 0.0) >= args.min_success_rate,
            "mind2web_docker_success_rate",
            f"success_rate={mind2web_docker_q.get('success_rate')}",
        ),
        check(
            mind2web_docker_data is not None
            and mind2web_docker_meta.get("container_isolation")
            in {"separate_container", "one_container_per_implementation_per_repetition", "one_container_per_shard"},
            "mind2web_docker_isolation",
            f"container_isolation={mind2web_docker_meta.get('container_isolation')}",
        ),
        check(
            mind2web_docker_data is not None
            and memory_at_or_below_8gb(mind2web_docker_meta.get("docker_memory_limit")),
            "mind2web_docker_memory",
            f"docker_memory_limit={mind2web_docker_meta.get('docker_memory_limit')}",
        ),
    ]

    accepted_without_docker = all(item["passed"] for item in checks)
    accepted = accepted_without_docker and all(item["passed"] for item in docker_checks)
    return {
        "status": "accepted" if accepted else "pending",
        "accepted": accepted,
        "accepted_without_mind2web_docker": accepted_without_docker,
        "criteria": {
            "mind2web_min_tasks": args.mind2web_min_tasks,
            "webvoyager_min_tasks": args.webvoyager_min_tasks,
            "min_success_rate": args.min_success_rate,
        },
        "paths": {
            "mind2web_local": str(args.mind2web_local),
            "mind2web_docker": str(args.mind2web_docker),
            "webvoyager": str(args.webvoyager),
        },
        "observed": {
            "mind2web_local": mind2web_local_q,
            "mind2web_local_metadata": {
                "max_task_seconds": mind2web_local_meta.get("max_task_seconds"),
                "browser_relaunches": mind2web_local_meta.get("browser_relaunches"),
                "task_retries_after_session_loss": mind2web_local_meta.get("task_retries_after_session_loss"),
            },
            "mind2web_docker": mind2web_docker_q,
            "mind2web_docker_metadata": {
                "container_isolation": mind2web_docker_meta.get("container_isolation"),
                "docker_memory_limit": mind2web_docker_meta.get("docker_memory_limit"),
                "passed_shards": mind2web_docker_meta.get("passed_shards"),
                "failed_shards": mind2web_docker_meta.get("failed_shards"),
            },
            "webvoyager": webvoyager_q,
            "webvoyager_metadata": {
                "container_isolation": webvoyager_meta.get("container_isolation"),
                "docker_memory_limit": webvoyager_meta.get("docker_memory_limit"),
                "browser_lifecycle": webvoyager_meta.get("browser_lifecycle"),
                "network_warmup_url": webvoyager_meta.get("network_warmup_url"),
            },
        },
        "checks": checks + docker_checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Mind2Web/WebVoyager Rustwright reliability evidence.")
    parser.add_argument("--mind2web-local", type=Path, default=DEFAULT_MIND2WEB_LOCAL)
    parser.add_argument("--mind2web-docker", type=Path, default=DEFAULT_MIND2WEB_DOCKER)
    parser.add_argument("--webvoyager", type=Path, default=DEFAULT_WEBVOYAGER)
    parser.add_argument("--mind2web-min-tasks", type=int, default=1009)
    parser.add_argument("--webvoyager-min-tasks", type=int, default=512)
    parser.add_argument("--min-success-rate", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = evaluate(args)
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
