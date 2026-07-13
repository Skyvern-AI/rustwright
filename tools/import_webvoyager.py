#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".benchmark-data" / "manifests" / "webvoyager_tasks.json"
TIME_SENSITIVE_TERMS = {
    "booking",
    "flight",
    "flights",
    "hotel",
    "hotels",
    "today",
    "tomorrow",
    "date",
    "check-in",
    "checkout",
    "depart",
    "departure",
    "return",
}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                item = json.loads(line)
                if isinstance(item, dict):
                    yield item


def iter_records(source: Path) -> Iterable[dict[str, Any]]:
    if source.suffix.lower() == ".jsonl":
        yield from iter_jsonl(source)
        return
    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(data, dict):
        for key in ("data", "tasks", "examples"):
            value = data.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
                return
        yield data


def first_string(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def fallback_id(*values: str) -> str:
    digest = hashlib.sha1("\x1f".join(values).encode("utf-8")).hexdigest()
    return digest[:16]


def is_time_sensitive(record: dict[str, Any], site: str, instruction: str) -> bool:
    explicit = record.get("time_sensitive")
    if isinstance(explicit, bool):
        return explicit
    text = f"{site} {instruction}".lower()
    return any(term in text for term in TIME_SENSITIVE_TERMS)


def manifest_task(index: int, record: dict[str, Any]) -> dict[str, Any]:
    site = first_string(record, "web_name", "website", "site", "web")
    url = first_string(record, "web", "url", "start_url")
    instruction = first_string(record, "ques", "question", "task", "instruction", "confirmed_task")
    task_id = first_string(record, "task_id", "id", "uid") or fallback_id(site, url, instruction, str(index))
    reference_answer = first_string(record, "Final answer", "final_answer", "answer", "reference_answer")
    return {
        "task_id": task_id,
        "source": "webvoyager",
        "site": site,
        "start_url": url,
        "instruction": instruction,
        "reference_answer_available": bool(reference_answer),
        "requires_live_web": True,
        "time_sensitive": is_time_sensitive(record, site, instruction),
        "max_interactions_hint": record.get("max_iter") or record.get("max_interactions") or 15,
    }


def stratified_sample(tasks: list[dict[str, Any]], sample_size: int, seed: int) -> list[dict[str, Any]]:
    if sample_size <= 0 or sample_size >= len(tasks):
        return tasks
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        groups[str(task.get("site") or "unknown")].append(task)
    rng = random.Random(seed)
    for items in groups.values():
        rng.shuffle(items)
    sampled: list[dict[str, Any]] = []
    buckets = sorted(groups)
    while len(sampled) < sample_size and buckets:
        next_buckets = []
        for bucket in buckets:
            items = groups[bucket]
            if items and len(sampled) < sample_size:
                sampled.append(items.pop())
            if items:
                next_buckets.append(bucket)
        buckets = next_buckets
    return sorted(sampled, key=lambda item: item["task_id"])


def build_summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    sites = {task["site"] for task in tasks if task.get("site")}
    return {
        "task_count": len(tasks),
        "site_count": len(sites),
        "live_web_tasks": sum(1 for task in tasks if task["requires_live_web"]),
        "time_sensitive_tasks": sum(1 for task in tasks if task["time_sensitive"]),
        "reference_answer_tasks": sum(1 for task in tasks if task["reference_answer_available"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a WebVoyager task manifest from a local JSON/JSONL file.")
    parser.add_argument("--source", required=True, type=Path, help="Path to WebVoyager_data.jsonl, GAIA_web.jsonl, or a JSON export.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample", type=int, default=0, help="Optional site-stratified task sample size.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--exclude-time-sensitive", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = parser.parse_args()

    source = args.source.resolve()
    tasks = [manifest_task(index, record) for index, record in enumerate(iter_records(source))]
    if args.exclude_time_sensitive:
        tasks = [task for task in tasks if not task["time_sensitive"]]
    tasks = stratified_sample(tasks, args.sample, args.seed)
    manifest = {
        "schema_version": 1,
        "source": "webvoyager",
        "generated_from": str(source),
        "live_web_required": True,
        "sample": args.sample or None,
        "sample_seed": args.seed if args.sample else None,
        "time_sensitive_excluded": args.exclude_time_sensitive,
        "summary": build_summary(tasks),
        "tasks": tasks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({"output": str(args.output), **manifest["summary"]}, indent=2, sort_keys=True))
    else:
        summary = manifest["summary"]
        print(
            f"Wrote {summary['task_count']} WebVoyager task metadata rows to {args.output} "
            f"({summary['site_count']} sites, {summary['time_sensitive_tasks']} time-sensitive)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
