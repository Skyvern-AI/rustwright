#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import query_project_state


ROOT = Path(__file__).resolve().parents[1]
INTERNAL_DOCS = ROOT / "docs" / "internal"
REFERENCE_IMPLEMENTATIONS = (
    ("python_playwright_reference", "Python Playwright reference", "vs_python_playwright_reference"),
    ("typescript_playwright_reference", "TypeScript Playwright", "vs_typescript_playwright_reference"),
    ("typescript_puppeteer_reference", "TypeScript Puppeteer", "vs_typescript_puppeteer_reference"),
)


def ms(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.2f}"


def comparison(percent: float | None) -> str:
    if percent is None:
        return "not measured"
    if percent >= 0:
        return f"Rustwright lower by {percent:.1f}%"
    return f"Rustwright higher by {abs(percent):.1f}%"


def comparison_for(speedup: dict[str, Any], prefix: str, metric: str) -> str:
    return comparison(speedup.get(f"{prefix}_lower_{metric}_percent"))


def load_json(name: str) -> Any:
    return json.loads((INTERNAL_DOCS / name).read_text(encoding="utf-8"))


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


def requirements_matrix() -> str:
    data = load_json("REQUIREMENTS_STATUS.json")
    rows = []
    for item in sorted(data["feature_coverage_matrix"], key=lambda row: row["rice"], reverse=True):
        rows.append(
            [
                item["feature_area"],
                item["usage_frequency"],
                item["chromium_sync"],
                item["chromium_async"],
                item["firefox"],
                item["webkit"],
                item["rice"],
            ]
        )
    return markdown_table(
        ["Feature area", "Frequency", "Chromium sync", "Chromium async", "Firefox", "WebKit", "RICE"],
        rows,
    )


def benchmark_table() -> str:
    progress = load_json("PROGRESS.json")
    latest = (
        progress["benchmarks"].get("latest_repeated_bench_full_matrix")
        or progress["benchmarks"].get("latest_bench_full_baseline")
        or progress["benchmarks"].get("latest_broadened_docker_suite")
        or progress["benchmarks"]["latest_broadened_local_suite"]
    )

    if "total_median_ms" in latest:
        medians = latest["total_median_ms"]
        p25s = latest.get("total_p25_ms", {})
        p75s = latest.get("total_p75_ms", {})
        speedup = latest["speedup"]

        rows = [
            [
                "Rustwright",
                latest["repetitions"],
                latest["iterations"],
                ms(medians.get("rustwright")),
                ms(p25s.get("rustwright")),
                ms(p75s.get("rustwright")),
                "baseline",
            ],
        ]
        always_show = {"python_playwright_reference", "typescript_playwright_reference"}
        for key, label, speedup_prefix in REFERENCE_IMPLEMENTATIONS:
            if key not in medians and key not in always_show:
                continue
            rows.append(
                [
                    label,
                    latest["repetitions"],
                    latest["iterations"],
                    ms(medians.get(key)),
                    ms(p25s.get(key)),
                    ms(p75s.get(key)),
                    comparison_for(speedup, speedup_prefix, "total_median"),
                ]
            )
        result_table = markdown_table(
            ["Implementation", "Repetitions", "Iterations", "Median ms", "p25 ms", "p75 ms", "Comparison"],
            rows,
        )
        condition_rows = [
            ["Suite", latest.get("suite", "not recorded")],
            ["Lifecycle", latest.get("lifecycle", "not recorded")],
            ["Container isolation", latest.get("container_isolation", "not recorded")],
            ["Resource model", latest.get("resource_model", "not recorded")],
            ["Failure policy", latest.get("failure_policy", "not recorded")],
            ["Measured failures", ", ".join(latest.get("measured_failures") or []) or "none recorded"],
            ["Result path", latest.get("result_path", "not recorded")],
        ]
        return result_table + "\n\n" + markdown_table(["Condition", "Value"], condition_rows)

    latest = (
        progress["benchmarks"].get("latest_bench_full_baseline")
        or progress["benchmarks"].get("latest_broadened_docker_suite")
        or progress["benchmarks"]["latest_broadened_local_suite"]
    )
    means = latest["total_mean_ms"]
    speedup = latest["speedup"]

    rows = [
        ["Rustwright", latest["sample_count"], latest["iterations"], ms(means["rustwright"]), "baseline"],
        [
            "Python Playwright reference",
            latest["sample_count"],
            latest["iterations"],
            ms(means["python_playwright_reference"]),
            comparison_for(speedup, "vs_python_playwright_reference", "total_mean"),
        ],
    ]
    for key, label, speedup_prefix in REFERENCE_IMPLEMENTATIONS[1:3]:
        if key not in means:
            if key == "typescript_playwright_reference":
                rows.append(
                    [
                        label,
                        "n/a",
                        "n/a",
                        "not measured",
                        str(speedup.get(key, "not measured")).replace("_", " "),
                    ]
                )
            continue
        rows.append(
            [
                label,
                latest["sample_count"],
                latest["iterations"],
                ms(means[key]),
                comparison_for(speedup, speedup_prefix, "total_mean"),
            ]
        )
    result_table = markdown_table(
        ["Implementation", "Samples", "Iterations", "Total mean ms", "Comparison"],
        rows,
    )
    condition_rows = [
        ["Container isolation", latest.get("container_isolation", "not recorded")],
        ["Resource model", latest.get("resource_model", "not recorded")],
        ["Failure policy", latest.get("failure_policy", "not recorded")],
        ["Measured failures", ", ".join(latest.get("measured_failures") or []) or "none recorded"],
    ]
    return result_table + "\n\n" + markdown_table(["Condition", "Value"], condition_rows)


def phase_2_acceptance_table() -> str:
    progress = load_json("PROGRESS.json")
    report = query_project_state.phase_2_benchmark_acceptance(progress)
    observed = report["observed"]
    summary_rows = [
        ["Status", report["status"]],
        ["Accepted", report["accepted"]],
        ["Result path", report.get("result_path")],
        ["Source", report.get("source")],
        ["Rustwright median ms", observed.get("rustwright_total_median_ms")],
        ["Reference median ms", observed.get("reference_total_median_ms")],
        ["Rustwright reduction percent", observed.get("rustwright_reduction_pct")],
        ["Non-passed runs", len(observed.get("non_passed_runs") or [])],
    ]
    check_rows = [[item["name"], item["passed"], item["detail"]] for item in report.get("checks", [])]
    return markdown_table(["Field", "Value"], summary_rows) + "\n\n" + markdown_table(["Check", "Passed", "Detail"], check_rows)


def launch_latency_claim_table() -> str:
    progress = load_json("PROGRESS.json")
    report = query_project_state.launch_latency_claim(progress)
    observed = report["observed"]
    summary_rows = [
        ["Status", report["status"]],
        ["Accepted", report["accepted"]],
        ["Evidence path", report["evidence_path"]],
        ["Runner", observed.get("runner")],
        ["Suite", observed.get("suite")],
        ["Case", observed.get("case")],
        ["Case count", observed.get("case_count")],
        ["Repetitions", observed.get("repetitions")],
        ["Iterations", observed.get("iterations")],
        ["Rustwright reduction percent", observed.get("rustwright_reduction_pct")],
        ["Result path", observed.get("result_path")],
        ["Run URL", observed.get("run_url")],
    ]
    check_rows = [[item["name"], item["passed"], item["detail"]] for item in report["checks"]]
    return markdown_table(["Field", "Value"], summary_rows) + "\n\n" + markdown_table(["Check", "Passed", "Detail"], check_rows)


def sampled_gate_table() -> str:
    progress = load_json("PROGRESS.json")
    gate = progress["metrics"]["latest_sampled_docker_gate"]
    rows = [
        ["pytest", gate["pytest"]["passed"], "", gate["pytest"]["deselected"]],
        ["Rustwright parity sample", gate["rustwright_parity_sample"]["passed"], gate["rustwright_parity_sample"]["total"], ""],
        ["Playwright parity sample", gate["playwright_parity_sample"]["passed"], gate["playwright_parity_sample"]["total"], ""],
    ]
    return markdown_table(["Gate", "Passed", "Total", "Deselected"], rows)


def phase_1_coverage_table() -> str:
    requirements = load_json("REQUIREMENTS_STATUS.json")
    ledger = requirements["phase_1_coverage_ledger"]
    rows = []
    for item in ledger["rows"]:
        rows.append(
            [
                item["coverage_point"],
                item["status"],
                item["evidence_type"],
                item["latest_result"],
                item["remaining"],
            ]
        )
    return markdown_table(["Coverage point", "Status", "Evidence", "Latest result", "Remaining"], rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render project status JSON as Markdown tables.")
    parser.add_argument(
        "--table",
        choices=["all", "requirements", "benchmarks", "launch", "sampled", "phase1"],
        default="all",
        help="Table to render. Defaults to all.",
    )
    args = parser.parse_args()

    sections: list[tuple[str, str]] = []
    if args.table in {"all", "requirements"}:
        sections.append(("Feature Coverage And RICE", requirements_matrix()))
    if args.table in {"all", "benchmarks"}:
        sections.append(("Benchmark Snapshot", benchmark_table()))
        sections.append(("Phase 2 Benchmark Acceptance", phase_2_acceptance_table()))
    if args.table in {"all", "benchmarks", "launch"}:
        sections.append(("Launch Latency Claim", launch_latency_claim_table()))
    if args.table in {"all", "sampled"}:
        sections.append(("Latest Docker Sampled Gate", sampled_gate_table()))
    if args.table in {"all", "phase1"}:
        sections.append(("Phase 1 Coverage Ledger", phase_1_coverage_table()))

    for index, (title, table) in enumerate(sections):
        if index:
            print()
        print(f"## {title}")
        print()
        print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
