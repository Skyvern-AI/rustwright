#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".benchmark-data" / "manifests" / "mind2web_tasks.json"


def iter_data_files(source: Path) -> Iterable[Path]:
    if source.is_file():
        yield source
        return
    for path in sorted(source.rglob("*")):
        if path.suffix.lower() in {".json", ".jsonl"}:
            yield path


def records_from_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(value, dict):
        return
    if any(key in value for key in ("annotation_id", "confirmed_task", "actions", "action_reprs")):
        yield value
        return
    for key in ("data", "tasks", "examples", "annotations"):
        child = value.get(key)
        if isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    yield item
            return
    for child in value.values():
        if isinstance(child, list) and all(isinstance(item, dict) for item in child):
            for item in child:
                yield item
            return


def iter_records(source: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    for path in iter_data_files(source):
        if path.suffix.lower() == ".jsonl":
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        item = json.loads(line)
                        if isinstance(item, dict):
                            yield path, item
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in records_from_json(data):
            yield path, item


def first_string(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def fallback_id(*values: str) -> str:
    digest = hashlib.sha1("\x1f".join(values).encode("utf-8")).hexdigest()
    return digest[:16]


def operation_type(action: dict[str, Any]) -> str:
    operation = action.get("operation")
    if isinstance(operation, dict):
        value = first_string(operation, "op", "original_op", "type", "action_type")
        if value:
            return value.upper()
    value = first_string(action, "op", "operation", "action_type", "type")
    return value.upper() if value else "UNKNOWN"


def summarize_actions(actions: Any) -> tuple[int, list[str], int, int]:
    if not isinstance(actions, list):
        return 0, [], 0, 0
    action_types = []
    html_snapshots = 0
    positive_candidates = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_types.append(operation_type(action))
        if action.get("raw_html") or action.get("cleaned_html"):
            html_snapshots += 1
        candidates = action.get("pos_candidates")
        if isinstance(candidates, list):
            positive_candidates += len(candidates)
    return len(actions), sorted(set(action_types)), html_snapshots, positive_candidates


def clean_text(value: Any, *, limit: int = 500) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit]
    return text


def candidate_attributes(candidate: dict[str, Any]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    raw_attrs = candidate.get("attributes")
    if isinstance(raw_attrs, dict):
        for key, value in raw_attrs.items():
            cleaned = clean_text(value, limit=200)
            if cleaned:
                attrs[str(key)] = cleaned
    elif isinstance(raw_attrs, str) and raw_attrs.strip():
        try:
            parsed = json.loads(raw_attrs)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                cleaned = clean_text(value, limit=200)
                if cleaned:
                    attrs[str(key)] = cleaned
    for key in (
        "tag",
        "tag_name",
        "backend_node_id",
        "node_id",
        "text",
        "inner_text",
        "aria_label",
        "placeholder",
        "name",
        "id",
    ):
        value = candidate.get(key)
        cleaned = clean_text(value, limit=200)
        if cleaned:
            attrs.setdefault(str(key), cleaned)
    return attrs


def action_fixture(action: dict[str, Any], index: int) -> dict[str, Any] | None:
    html = action.get("cleaned_html") or action.get("raw_html") or action.get("html")
    if not isinstance(html, str) or not html.strip():
        return None
    positive_candidates = action.get("pos_candidates")
    candidates = []
    if isinstance(positive_candidates, list):
        for candidate in positive_candidates[:5]:
            if isinstance(candidate, dict):
                attrs = candidate_attributes(candidate)
                if attrs:
                    candidates.append(attrs)
    operation = action.get("operation")
    value = ""
    if isinstance(operation, dict):
        value = clean_text(
            operation.get("value")
            or operation.get("text")
            or operation.get("option")
            or operation.get("answer")
            or operation.get("original_value"),
            limit=200,
        )
    if not value:
        value = clean_text(
            action.get("value")
            or action.get("text")
            or action.get("option")
            or action.get("answer")
            or action.get("action_value"),
            limit=200,
        )
    return {
        "index": index,
        "operation": operation_type(action),
        "value": value,
        "html": html,
        "candidates": candidates,
    }


def split_from_path(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    for split in ("train", "test_task", "test_website", "test_domain", "test", "dev", "validation", "val"):
        if split in parts or any(part.startswith(split) for part in parts):
            return split
    return ""


def task_identity(source_root: Path, path: Path, index: int, record: dict[str, Any]) -> dict[str, Any]:
    actions = record.get("actions")
    action_count, action_types, html_snapshots, positive_candidates = summarize_actions(actions)
    instruction = first_string(record, "confirmed_task", "task", "instruction", "ques", "question")
    website = first_string(record, "website", "web_name", "site")
    domain = first_string(record, "domain")
    subdomain = first_string(record, "subdomain")
    task_id = first_string(record, "annotation_id", "task_id", "id", "uid") or fallback_id(
        instruction,
        website,
        str(path.relative_to(source_root) if path.is_relative_to(source_root) else path),
        str(index),
    )
    return {
        "task_id": task_id,
        "source": "mind2web",
        "split": first_string(record, "split") or split_from_path(path),
        "website": website,
        "domain": domain,
        "subdomain": subdomain,
        "instruction": instruction,
        "action_count": action_count,
        "action_types": action_types,
        "html_snapshot_count": html_snapshots,
        "positive_candidate_count": positive_candidates,
        "has_raw_html": html_snapshots > 0,
        "source_file": str(path.relative_to(source_root) if path.is_relative_to(source_root) else path),
    }


def manifest_task(
    source_root: Path,
    path: Path,
    index: int,
    record: dict[str, Any],
    *,
    include_action_fixtures: bool = False,
) -> dict[str, Any]:
    actions = record.get("actions")
    task = task_identity(source_root, path, index, record)
    if include_action_fixtures:
        fixtures = []
        if isinstance(actions, list):
            for action_index, action in enumerate(actions):
                if not isinstance(action, dict):
                    continue
                fixture = action_fixture(action, action_index)
                if fixture is not None:
                    fixtures.append(fixture)
        task["action_fixtures"] = fixtures
        task["executable_action_count"] = len(fixtures)
    return task


def stratified_sample(tasks: list[dict[str, Any]], sample_size: int, seed: int, key: str) -> list[dict[str, Any]]:
    if sample_size <= 0 or sample_size >= len(tasks):
        return tasks
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        if key == "action_type":
            bucket = ",".join(task.get("action_types") or ["UNKNOWN"])
        else:
            bucket = str(task.get(key) or "unknown")
        groups[bucket].append(task)
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
    domains = {task["domain"] for task in tasks if task.get("domain")}
    websites = {task["website"] for task in tasks if task.get("website")}
    action_types = {action for task in tasks for action in task.get("action_types", [])}
    return {
        "task_count": len(tasks),
        "domain_count": len(domains),
        "website_count": len(websites),
        "action_types": sorted(action_types),
        "tasks_with_html_snapshots": sum(1 for task in tasks if task["has_raw_html"]),
    }


def selected_record_keys(
    source_root: Path,
    records: list[tuple[int, Path, dict[str, Any]]],
    sample: int,
    seed: int,
    stratify: str,
) -> set[tuple[str, str]]:
    identities = [
        task_identity(source_root, path, index, record)
        for index, path, record in records
    ]
    sampled = stratified_sample(identities, sample, seed, stratify)
    return {(str(task["source_file"]), str(task["task_id"])) for task in sampled}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a token-light Mind2Web benchmark manifest from local data.")
    parser.add_argument("--source", required=True, type=Path, help="Mind2Web JSON/JSONL file or extracted dataset directory.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample", type=int, default=0, help="Optional stratified task sample size.")
    parser.add_argument("--sample-percent", type=float, default=0.0, help="Optional stratified task sample percentage.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--stratify", choices=["domain", "website", "action_type"], default="domain")
    parser.add_argument(
        "--include-action-fixtures",
        action="store_true",
        help=(
            "Include ignored executable offline action fixtures from Mind2Web HTML snapshots. "
            "Do not commit the generated manifest."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = parser.parse_args()

    source = args.source.resolve()
    source_root = source if source.is_dir() else source.parent
    records = [(index, path, record) for index, (path, record) in enumerate(iter_records(source))]
    sample = args.sample
    if args.sample_percent:
        if args.sample_percent <= 0 or args.sample_percent > 100:
            raise SystemExit("--sample-percent must be greater than 0 and less than or equal to 100")
        sample = max(1, round(len(records) * args.sample_percent / 100))
    selected_keys = selected_record_keys(source_root, records, sample, args.seed, args.stratify) if sample else None
    tasks = []
    for index, path, record in records:
        if selected_keys is not None:
            identity = task_identity(source_root, path, index, record)
            key = (str(identity["source_file"]), str(identity["task_id"]))
            if key not in selected_keys:
                continue
        tasks.append(manifest_task(source_root, path, index, record, include_action_fixtures=args.include_action_fixtures))
    manifest = {
        "schema_version": 1,
        "source": "mind2web",
        "generated_from": str(source),
        "raw_html_included": False,
        "action_fixtures_included": args.include_action_fixtures,
        "source_task_count": len(records),
        "sample": sample or None,
        "sample_percent": args.sample_percent or None,
        "sample_seed": args.seed if sample else None,
        "stratify": args.stratify if sample else None,
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
            f"Wrote {summary['task_count']} Mind2Web task metadata rows to {args.output} "
            f"({summary['domain_count']} domains, {summary['website_count']} websites)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
