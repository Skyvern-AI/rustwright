#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_json(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
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
    raise SystemExit(f"could not find JSON object in {path}")


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(lines)


def fmt_ms(value: float) -> str:
    return f"{value:.2f}"


def aggregate_table(data: dict[str, Any]) -> str:
    aggregate = data.get("aggregate") or {}
    rust = aggregate.get("rustwright", {}).get("total_mean_ms", {}).get("median")
    rows = []
    for implementation, item in sorted(aggregate.items()):
        total = item["total_mean_ms"]
        comparison = "baseline"
        if implementation != "rustwright" and rust is not None and total["median"] > 0:
            reduction = (float(total["median"]) - float(rust)) / float(total["median"]) * 100
            comparison = f"Rustwright lower by {reduction:.1f}%" if reduction >= 0 else f"Rustwright higher by {abs(reduction):.1f}%"
        rows.append(
            [
                implementation,
                item.get("runs", ""),
                fmt_ms(float(total["median"])),
                fmt_ms(float(total["p25"])),
                fmt_ms(float(total["p75"])),
                comparison,
            ]
        )
    return markdown_table(["Implementation", "Runs", "Median ms", "p25 ms", "p75 ms", "Comparison"], rows)


def common_case_table(data: dict[str, Any]) -> str:
    common = data.get("common_case_comparison") or {}
    totals = common.get("total_median_ms") or {}
    rust = totals.get("rustwright")
    rows = []
    for implementation, total in sorted(totals.items()):
        comparison = "baseline"
        if implementation != "rustwright" and rust is not None and float(total) > 0:
            reduction = (float(total) - float(rust)) / float(total) * 100
            comparison = f"Rustwright lower by {reduction:.1f}%" if reduction >= 0 else f"Rustwright higher by {abs(reduction):.1f}%"
        rows.append([implementation, common.get("case_count", ""), fmt_ms(float(total)), comparison])
    if not rows:
        rows.append(["", 0, "", "no common cases"])
    return markdown_table(["Implementation", "Common cases", "Common-case median total ms", "Comparison"], rows)


def win_count_table(data: dict[str, Any]) -> str:
    win_counts = (data.get("case_winners") or {}).get("win_counts") or {}
    rows = [[name, wins] for name, wins in sorted(win_counts.items(), key=lambda item: (-item[1], item[0]))]
    return markdown_table(["Implementation", "Per-case median wins"], rows)


def slow_case_table(data: dict[str, Any], *, limit: int) -> str:
    aggregate = data.get("aggregate") or {}
    rows = []
    for implementation, item in aggregate.items():
        for case_name, case in item.get("cases", {}).items():
            rows.append((float(case["median"]), implementation, case_name, case))
    rows.sort(reverse=True)
    table_rows = [
        [implementation, case_name, fmt_ms(value), fmt_ms(float(case["p25"])), fmt_ms(float(case["p75"]))]
        for value, implementation, case_name, case in rows[:limit]
    ]
    return markdown_table(["Implementation", "Case", "Median ms", "p25 ms", "p75 ms"], table_rows)


def run_status_table(data: dict[str, Any]) -> str:
    rows = []
    for item in data.get("results") or []:
        status = item.get("status", "")
        detail = item.get("reason") or item.get("failure_kind") or ""
        output_tail = item.get("output_tail") or ""
        if output_tail:
            detail = f"{detail}: {output_tail}" if detail else output_tail
        rows.append(
            [
                item.get("implementation", ""),
                item.get("repetition", ""),
                status,
                item.get("failure_kind", ""),
                detail,
            ]
        )
    if not rows:
        rows.append(["", "", "", "", "no individual run results recorded"])
    return markdown_table(["Implementation", "Repetition", "Status", "Failure kind", "Detail"], rows)


def condition_table(data: dict[str, Any]) -> str:
    metadata = data.get("metadata") or {}
    docker_preflight = metadata.get("docker_preflight") or {}
    rows = [
        ["Suite", data.get("suite")],
        ["Lifecycle", data.get("lifecycle")],
        ["Iterations", data.get("iterations")],
        ["Repetitions", data.get("repetitions")],
        ["Container isolation", data.get("container_isolation")],
        ["Docker memory", metadata.get("docker_memory_limit")],
        ["Docker swap", metadata.get("docker_memory_swap_limit")],
        ["Docker CPU quota", metadata.get("docker_cpu_quota")],
        ["Docker host CPUs", metadata.get("docker_cpu_host_info")],
        ["Docker preflight", docker_preflight.get("status")],
        ["Docker preflight failure", docker_preflight.get("failure_kind")],
        ["Docker image", metadata.get("docker_image")],
        ["Docker image id", metadata.get("docker_image_id")],
        ["Git rev", metadata.get("git_rev")],
        ["Rustwright rebuild", metadata.get("rustwright_rebuild_mode")],
    ]
    return markdown_table(["Condition", "Value"], rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render saved benchmark matrix JSON as compact Markdown tables.")
    parser.add_argument("json_path", help="Benchmark JSON path, or '-' for stdin.")
    parser.add_argument("--slow-cases", type=int, default=10, help="Number of slowest implementation/case rows to show.")
    args = parser.parse_args()

    data = load_json(args.json_path)
    sections = [
        ("Benchmark Conditions", condition_table(data)),
        ("Run Status", run_status_table(data)),
        ("Aggregate Results", aggregate_table(data)),
        ("Common-Case Results", common_case_table(data)),
        ("Per-Case Wins", win_count_table(data)),
        ("Slowest Cases", slow_case_table(data, limit=args.slow_cases)),
    ]
    for index, (title, table) in enumerate(sections):
        if index:
            print()
        print(f"## {title}")
        print()
        print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
