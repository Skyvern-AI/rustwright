#!/usr/bin/env python3
"""Aggregate release benchmark metric artifacts into a Markdown report."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


BINDING_LANGUAGE_ORDER = ("rust", "go", "java", "csharp", "ruby", "php")
BASELINE_LANGUAGES = frozenset({"go", "java", "csharp", "ruby"})
BINDING_SUITE_ORDER = ("deep", "regression")
BINDING_SUITE_TITLES = {
    "deep": "Deep workloads",
    "regression": "Regression suite",
}
IMPLEMENTATION_ORDER = (
    "rustwright",
    "playwright",
    "typescript-playwright",
    "typescript-puppeteer",
)


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def first_numeric(*values: Any) -> float | None:
    for value in values:
        parsed = numeric(value)
        if parsed is not None:
            return parsed
    return None


def markdown_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def format_number(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def format_count(value: Any) -> str:
    parsed = numeric(value)
    if parsed is None:
        return "—"
    return str(int(parsed)) if parsed.is_integer() else f"{parsed:.2f}"


def mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.fmean(present) if present else None


def metric_files(root: Path) -> list[Path]:
    # download-artifact creates <root>/<artifact-name>/ and upload-artifact may
    # either retain or strip the uploaded top-level directory. Walk every JSON
    # file and classify by filename/content instead of assuming a literal
    # parent directory named "metrics".
    return sorted(root.rglob("*.json"))


def binding_suite(path: Path) -> str:
    if path.name.endswith("-deep-metrics.json"):
        return "deep"
    # Metrics produced before the dual-manifest artifact layout had no suite
    # suffix. Preserve those as regression results.
    return "regression"


def load_metrics(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    bindings: list[dict[str, Any]] = []
    deep: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in metric_files(root):
        relative = path.relative_to(root)
        named_as_metrics = path.name.endswith("-metrics.json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            if named_as_metrics:
                problems.append(f"{relative}: {error}")
            continue
        if not isinstance(payload, dict):
            if named_as_metrics:
                problems.append(f"{relative}: top-level JSON value is not an object")
            continue
        if "lang" in payload and "impl" in payload:
            required = (
                "wall_seconds",
                "peak_tree_rss_kb",
                "exit_code",
                "samples",
                "cases",
                "failed",
            )
            missing = [key for key in required if key not in payload]
            if missing:
                problems.append(f"{relative}: binding metrics missing keys {missing}")
                continue
            wall_seconds = numeric(payload.get("wall_seconds"))
            peak_tree_rss_kb = numeric(payload.get("peak_tree_rss_kb"))
            samples = payload.get("samples")
            exit_code = payload.get("exit_code")
            cases = payload.get("cases")
            failed = payload.get("failed")
            if wall_seconds is None or wall_seconds < 0:
                problems.append(f"{relative}: wall_seconds is not a non-negative number")
                continue
            if peak_tree_rss_kb is None or peak_tree_rss_kb < 0:
                problems.append(f"{relative}: peak_tree_rss_kb is not a non-negative number")
                continue
            if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
                problems.append(f"{relative}: samples is not a positive integer")
                continue
            if isinstance(exit_code, bool) or exit_code != 0:
                problems.append(f"{relative}: measured command exit_code is not zero")
                continue
            if isinstance(cases, bool) or not isinstance(cases, int) or cases < 1:
                problems.append(f"{relative}: cases is not a positive integer")
                continue
            if isinstance(failed, bool) or failed != 0:
                problems.append(f"{relative}: failed is not zero")
                continue
            normalized = {
                **payload,
                "_source": str(relative),
                "_suite": binding_suite(path),
            }
            for key in (
                "peak_client_rss_kb",
                "peak_excluded_process_count",
                "unresolved_samples",
                "unresolved_records_total",
            ):
                if key not in payload:
                    continue
                value = payload[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    problems.append(
                        f"{relative}: {key} is not a non-negative integer; rendered as missing"
                    )
                    normalized.pop(key, None)
            bindings.append(normalized)
        elif payload.get("kind") == "python-node-deep":
            deep.append(payload)
        elif named_as_metrics:
            problems.append(f"{relative}: unrecognized metrics schema")
    return bindings, deep, problems


def ordered_languages(languages: Iterable[str]) -> list[str]:
    present = set(languages)
    known = [language for language in BINDING_LANGUAGE_ORDER if language in present]
    return known + sorted(present - set(BINDING_LANGUAGE_ORDER))


def binding_table(
    language: str,
    suite: str,
    implementations: dict[str, dict[str, Any]],
    problems: list[str],
) -> list[str]:
    lines = [
        "| Implementation | Wall seconds / speed multiple | Client-stack peak RSS MB / memory delta | Full-tree peak RSS MB | Browser processes | Unresolved scans / records | Cases passed | Failed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    ordered_impls = [name for name in ("rustwright", "playwright") if name in implementations]
    ordered_impls.extend(sorted(set(implementations) - set(ordered_impls)))
    for implementation in ordered_impls:
        metric = implementations[implementation]
        wall_seconds = numeric(metric.get("wall_seconds"))
        peak_client_rss_kb = numeric(metric.get("peak_client_rss_kb"))
        peak_client_rss_mb = (
            peak_client_rss_kb / 1024 if peak_client_rss_kb is not None else None
        )
        peak_tree_rss_kb = numeric(metric.get("peak_tree_rss_kb"))
        peak_tree_rss_mb = (
            peak_tree_rss_kb / 1024 if peak_tree_rss_kb is not None else None
        )
        unresolved_samples = metric.get("unresolved_samples")
        unresolved_records_total = metric.get("unresolved_records_total")
        if unresolved_samples is None and unresolved_records_total is None:
            unresolved = "—"
        else:
            unresolved = (
                f"{format_count(unresolved_samples)} / "
                f"{format_count(unresolved_records_total)}"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_text(implementation),
                    format_number(wall_seconds),
                    format_number(peak_client_rss_mb),
                    format_number(peak_tree_rss_mb),
                    format_count(metric.get("peak_excluded_process_count")),
                    unresolved,
                    format_count(metric.get("cases")),
                    format_count(metric.get("failed")),
                ]
            )
            + " |"
        )

    rustwright = implementations.get("rustwright")
    playwright = implementations.get("playwright")
    if rustwright is not None and playwright is not None:
        rustwright_cases = numeric(rustwright.get("cases"))
        playwright_cases = numeric(playwright.get("cases"))
        if rustwright_cases != playwright_cases:
            problems.append(
                f"{language} {BINDING_SUITE_TITLES[suite].lower()}: Rustwright and "
                "Playwright metrics have different case counts; savings row omitted"
            )
            return lines
        rustwright_wall = numeric(rustwright.get("wall_seconds"))
        playwright_wall = numeric(playwright.get("wall_seconds"))
        speed_multiple = (
            playwright_wall / rustwright_wall
            if playwright_wall is not None and rustwright_wall is not None and rustwright_wall > 0
            else None
        )
        rustwright_rss = numeric(rustwright.get("peak_client_rss_kb"))
        playwright_rss = numeric(playwright.get("peak_client_rss_kb"))
        memory_delta = (
            (playwright_rss - rustwright_rss) / playwright_rss * 100
            if playwright_rss is not None and rustwright_rss is not None and playwright_rss > 0
            else None
        )
        speed_text = "—" if speed_multiple is None else f"{speed_multiple:.2f}×"
        memory_text = "—" if memory_delta is None else f"{memory_delta:.1f}%"
        lines.append(f"| Savings | {speed_text} | {memory_text} | — | — | — | — | — |")
    return lines


def binding_section(metrics: list[dict[str, Any]], problems: list[str]) -> list[str]:
    lines = ["## Language bindings", ""]
    grouped: dict[str, dict[str, dict[str, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for metric in metrics:
        language = str(metric.get("lang", "unknown"))
        suite = str(metric.get("_suite", "regression"))
        implementation = str(metric.get("impl", "unknown"))
        if implementation in grouped[language][suite]:
            problems.append(
                f"duplicate {language}/{suite}/{implementation} metrics; kept "
                f"{grouped[language][suite][implementation]['_source']} and ignored "
                f"{metric['_source']}"
            )
            continue
        grouped[language][suite][implementation] = metric

    if not grouped:
        lines.extend(["No binding metrics were available.", ""])
        return lines

    for language in ordered_languages(grouped):
        lines.extend([f"### {markdown_text(language)}", ""])
        for suite in BINDING_SUITE_ORDER:
            implementations = grouped[language].get(suite)
            if not implementations:
                continue
            lines.extend([f"#### {BINDING_SUITE_TITLES[suite]}", ""])
            lines.extend(binding_table(language, suite, implementations, problems))
            lines.append("")
    return lines


def deep_items(payload: dict[str, Any]) -> Iterable[tuple[int, dict[str, Any]]]:
    runs = payload.get("runs")
    if isinstance(runs, list):
        candidates = runs
    else:
        candidates = [payload]
    for index, run in enumerate(candidates, start=1):
        if not isinstance(run, dict):
            continue
        repetition = run.get("repetition", index)
        try:
            repetition_number = int(repetition)
        except (TypeError, ValueError):
            repetition_number = index
        results = run.get("results")
        if run.get("implementation") == "all" and isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    yield repetition_number, item
        elif isinstance(results, list) and "implementation" not in run:
            for item in results:
                if isinstance(item, dict):
                    yield repetition_number, item
        elif "implementation" in run:
            yield repetition_number, run


def deep_measurement(repetition: int, item: dict[str, Any]) -> dict[str, Any]:
    memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    cases = item.get("cases")
    case_count = first_numeric(metadata.get("case_count"), len(cases) if isinstance(cases, dict) else None)
    total_mean_ms = first_numeric(
        item.get("total_mean_ms"),
        item.get("mean_ms"),
        item.get("total_ms"),
    )
    if total_mean_ms is None:
        wall_seconds = numeric(item.get("wall_seconds"))
        total_mean_ms = wall_seconds * 1000 if wall_seconds is not None else None
    return {
        "repetition": repetition,
        "implementation": str(item.get("implementation", "unknown")),
        "status": str(item.get("status", "ok")),
        "reason": str(item.get("reason", "")),
        "total_mean_ms": total_mean_ms,
        "rss_self_kb": first_numeric(memory.get("rss_self_kb"), item.get("rss_self_kb")),
        "rss_tree_kb": first_numeric(
            memory.get("rss_tree_kb"),
            memory.get("peak_rss_kb"),
            item.get("rss_tree_kb"),
            item.get("peak_rss_kb"),
        ),
        "case_count": case_count,
    }


def deep_section(payloads: list[dict[str, Any]]) -> list[str]:
    lines = ["## Python and Node deep benchmark", ""]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for payload in payloads:
        for repetition, item in deep_items(payload):
            measurement = deep_measurement(repetition, item)
            grouped[measurement["implementation"]].append(measurement)

    if not grouped:
        lines.extend(["No python-node-deep metrics were available.", ""])
        return lines

    lines.extend(
        [
            "| Implementation | Successful runs | Total mean ms | Peak self RSS MB | Peak tree RSS MB | Cases | Status |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    names = [name for name in IMPLEMENTATION_ORDER if name in grouped]
    names.extend(sorted(set(grouped) - set(names)))
    for implementation in names:
        measurements = grouped[implementation]
        successful = [item for item in measurements if item["status"] in {"", "ok", "passed"}]
        skipped = [item for item in measurements if item["status"] == "skipped"]
        failed = [item for item in measurements if item not in successful and item not in skipped]
        total_mean_ms = mean(item["total_mean_ms"] for item in successful)
        self_rss_mb = mean(
            item["rss_self_kb"] / 1024 if item["rss_self_kb"] is not None else None
            for item in successful
        )
        tree_rss_mb = mean(
            item["rss_tree_kb"] / 1024 if item["rss_tree_kb"] is not None else None
            for item in successful
        )
        case_count = mean(item["case_count"] for item in successful)
        if successful and not skipped and not failed:
            status = "ok"
        elif successful:
            status_parts = []
            if skipped:
                status_parts.append(f"{len(skipped)} skipped repetition(s)")
            if failed:
                status_parts.append(f"{len(failed)} failed repetition(s)")
            status = ", ".join(status_parts)
        elif failed:
            reasons = sorted({item["reason"] for item in failed if item["reason"]})
            status = "failed" + (f": {'; '.join(reasons)}" if reasons else "")
        else:
            reasons = sorted({item["reason"] for item in skipped if item["reason"]})
            status = "skipped" + (f": {'; '.join(reasons)}" if reasons else "")
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_text(implementation),
                    str(len(successful)),
                    format_number(total_mean_ms),
                    format_number(self_rss_mb),
                    format_number(tree_rss_mb),
                    format_count(case_count),
                    markdown_text(status),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def caveats_section(
    bindings: list[dict[str, Any]], deep: list[dict[str, Any]], problems: list[str]
) -> list[str]:
    regression_bindings = [
        metric for metric in bindings if metric.get("_suite") == "regression"
    ]
    rustwright_present = {
        str(metric.get("lang"))
        for metric in regression_bindings
        if metric.get("impl") == "rustwright"
    }
    baseline_present = {
        str(metric.get("lang"))
        for metric in regression_bindings
        if metric.get("impl") == "playwright"
    }
    missing_rustwright = sorted(set(BINDING_LANGUAGE_ORDER) - rustwright_present)
    missing_baselines = sorted(BASELINE_LANGUAGES - baseline_present)
    lines = ["## Caveats", ""]
    lines.append(
        "- Rust and PHP have no Playwright client baseline, so their tables show absolute Rustwright numbers only."
    )
    if missing_rustwright:
        lines.append(
            "- Rustwright binding artifacts missing at aggregation time: "
            + ", ".join(markdown_text(language) for language in missing_rustwright)
            + "."
        )
    else:
        lines.append("- No expected Rustwright binding artifact was missing at aggregation time.")
    if missing_baselines:
        lines.append(
            "- Playwright baseline directories or artifacts missing at run time are listed as skipped: "
            + ", ".join(markdown_text(language) for language in missing_baselines)
            + "."
        )
    else:
        lines.append("- No expected Playwright baseline language was skipped at run time.")
    if not deep:
        lines.append("- The Python/Node deep-benchmark artifact was missing at aggregation time.")
    lines.extend(
        [
            "- Each binding and its baseline run the regression manifest first and the deep-workload manifest second in one job against the same workflow-resolved browser executable; Python/Node deep benchmark repetitions and implementations also run sequentially.",
            "- Binding client-stack memory excludes the workflow-resolved browser executable and each matched process's entire descendant subtree from the full process tree. The Playwright client stack still includes its driver Node process because that is part of the library's cost; Rustwright is in-process.",
            "- Binding memory fields are independent peaks sampled every 100 ms. Full-tree RSS retains the command and all descendants for context, while Browser processes is the maximum simultaneous process count pruned from the client stack.",
            "- Unresolved scans / records reports bracketed process reads dropped because identity or executable resolution was not coherent. Nonzero values reduce confidence in both memory peaks. Older artifacts without these fields show — and do not imply unresolved reads.",
            "- Python/Node deep rows are unchanged and use the benchmark's process/self and process-tree peak RSS fields when available; missing values are shown as —.",
            "- These Blacksmith-runner artifacts are release regression diagnostics, not a substitute for capped Testbox evidence for launch-facing performance claims.",
        ]
    )
    if problems:
        lines.append("- Metrics warnings: " + "; ".join(markdown_text(problem) for problem in problems) + ".")
    lines.append("")
    return lines


def build_report(root: Path) -> str:
    bindings, deep, problems = load_metrics(root)
    lines = ["# Release Benchmark Report", ""]
    lines.extend(binding_section(bindings, problems))
    lines.extend(deep_section(deep))
    lines.extend(caveats_section(bindings, deep, problems))
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact_root",
        nargs="?",
        default=".",
        type=Path,
        help="Root containing downloaded artifact trees (default: current directory)",
    )
    parser.add_argument(
        "--output",
        default=Path("bench-report.md"),
        type=Path,
        help="Markdown report path (default: bench-report.md)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = build_report(args.artifact_root.resolve())
    args.output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
