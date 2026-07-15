#!/usr/bin/env python3
"""Run benchmark cases concurrently with an independent browser per run."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SUITE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SUITE_DIR))

from harness.skyvern_session import SkyvernSession  # noqa: E402


URL_PATTERN = re.compile(r"(?:https?|wss?)://[^\s\"']+", re.IGNORECASE)
OUTPUT_DIR = Path("out/suite").resolve()
backend_python: dict[str, str] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=SUITE_DIR / "cases/smoke_nav.json")
    parser.add_argument(
        "--backends", nargs="+", choices=("rustwright", "playwright"),
        default=["rustwright", "playwright"]
    )
    parser.add_argument("--reps", type=int, default=15)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("out/suite"))
    parser.add_argument(
        "--weakness", action="store_true",
        help="run one dedicated weakness-probe session per backend",
    )
    parser.add_argument(
        "--python", dest="backend_python", action="append", default=[],
        metavar="BACKEND=/ABS/PATH/TO/PYTHON",
    )
    args = parser.parse_args()
    if args.reps < 1 or args.concurrency < 1:
        parser.error("--reps and --concurrency must be positive")
    mappings = {}
    for mapping in args.backend_python:
        backend, separator, interpreter = mapping.partition("=")
        if (not separator or backend not in ("rustwright", "playwright")
                or not Path(interpreter).is_absolute()):
            parser.error("--python must be BACKEND=/absolute/path/to/python")
        mappings[backend] = interpreter
    args.backend_python = mappings
    return args


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return URL_PATTERN.sub("<redacted-address>", text)


def redact_log(path: Path, secrets: list[str]) -> None:
    if path.is_file():
        content = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(redact(content, secrets), encoding="utf-8")


def read_pss(path: Path) -> tuple[float, float]:
    with path.open(newline="", encoding="ascii") as handle:
        values = [int(row["total_bytes"]) / (1024 * 1024)
                  for row in csv.DictReader(handle)]
    if not values:
        raise ValueError("stack_pss.csv has no samples")
    return max(values), statistics.mean(values)


def p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=20, method="inclusive")[-1]


def requested_steps(case: dict[str, Any]) -> int | None:
    category = case.get("category")
    if category == "navigation":
        steps = case.get("nav_steps")
        return len(steps) if isinstance(steps, list) else None
    if category == "download":
        # Navigate, dispatch the trigger, and observe the download event.
        return 3
    return None


def run_one(case: dict[str, Any], rep: int, backend: str) -> dict[str, Any]:
    case_id, category = str(case["id"]), str(case["category"])
    run_dir = (OUTPUT_DIR / case_id / backend / f"rep{rep}").resolve()
    interpreter = backend_python.get(backend, sys.executable)
    base = {
        "case": case_id, "category": category, "backend": backend, "rep": rep,
        "interpreter": Path(interpreter).name,
    }
    secrets: list[str] = []
    returncode: int | None = None
    try:
        with SkyvernSession() as session:
            if not session.browser_address:
                raise RuntimeError("browser session returned no address")
            headers = session.cdp_headers
            secrets = [session.browser_address, *headers.values()]
            environment = {
                **os.environ,
                "BACKEND": backend,
                "CDP_URL": session.browser_address,
                "CDP_CONNECT_HEADERS": json.dumps(headers, separators=(",", ":")),
                "BENCH_WORKLOAD": (
                    "download_workload.py" if category == "download" else
                    "nav_workload.py" if category == "navigation" else "fill_form.py"
                ),
                "NAV_CASE_JSON": json.dumps(case, separators=(",", ":")),
                "DL_CASE_JSON": json.dumps(case, separators=(",", ":")),
            }
            result = subprocess.run(
                [interpreter, str(SCRIPT_DIR / "measure.py"),
                 "--backend", backend, "--output", str(run_dir)],
                cwd=SUITE_DIR,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            returncode = result.returncode

        timings = json.loads((run_dir / "timings.json").read_text(encoding="utf-8"))
        library_latency_ms = timings.get("library_latency_ms")
        if library_latency_ms is None:
            library_latency_ms = timings["action_ms"]
        library_probe = timings.get("library_probe")
        steps_requested = timings.get("steps_requested", requested_steps(case))
        steps_completed = timings.get("steps_completed")
        reached_final_step = (
            steps_requested is not None
            and steps_completed is not None
            and steps_completed == steps_requested
        )
        peak, mean = read_pss(run_dir / "stack_pss.csv")
        record = {
            **base, "ok": returncode == 0, "returncode": returncode,
            "library_latency_ms": float(library_latency_ms),
            "library_probe": library_probe,
            "weakness_probe": timings.get("weakness_probe"),
            "page_wait_ms": float(timings.get("page_wait_ms", 0)),
            "navigation_ms": float(timings["navigation_ms"]),
            "pss_peak_mb": peak, "pss_mean_mb": mean,
            "steps_requested": steps_requested,
            "steps_completed": steps_completed,
            "reached_final_step": reached_final_step,
            "equivalent_work": None,
            "soft_fail": False,
        }
        if returncode != 0:
            record["error"] = f"measure.py exited with status {returncode}"
        return record
    except Exception as error:
        return {
            **base, "ok": False, "returncode": returncode,
            "library_latency_ms": None, "library_probe": None,
            "weakness_probe": None,
            "page_wait_ms": None,
            "navigation_ms": None,
            "pss_peak_mb": None, "pss_mean_mb": None,
            "steps_requested": requested_steps(case),
            "steps_completed": None,
            "reached_final_step": False if requested_steps(case) is not None else None,
            "equivalent_work": None,
            "soft_fail": False,
            "error": redact(f"{type(error).__name__}: {error}", secrets),
        }
    finally:
        try:
            redact_log(run_dir / "run.log", secrets)
        except OSError:
            pass


def mark_equivalent_work(records: list[dict[str, Any]]) -> None:
    """Mark per-rep cross-backend step-count mismatches as soft failures."""
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for record in records:
        if record["category"] in ("navigation", "download"):
            groups.setdefault((record["case"], record["rep"]), []).append(record)

    for group in groups.values():
        comparable = [
            record for record in group if record.get("steps_completed") is not None
        ]
        if len({record["backend"] for record in comparable}) < 2:
            continue
        completed_counts = {record["steps_completed"] for record in comparable}
        equivalent = len(completed_counts) == 1
        for record in comparable:
            record["equivalent_work"] = equivalent
            if not equivalent:
                record["soft_fail"] = True
                record["soft_fail_reason"] = (
                    "backends completed different step counts for this case/rep"
                )


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mark_equivalent_work(records)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((record["case"], record["backend"]), []).append(record)
    result = []
    for (case_id, backend), group in groups.items():
        good = [record for record in group if record["ok"]]
        latencies = [record["library_latency_ms"] for record in good]
        probe_medians = [
            record["library_probe"]["library_probe_median_ms"]
            for record in good
            if record.get("library_probe") is not None
            and record["library_probe"].get("library_probe_median_ms") is not None
        ]
        page_waits = [record["page_wait_ms"] for record in good]
        navigations = [record["navigation_ms"] for record in good]
        peaks = [record["pss_peak_mb"] for record in good]
        completed_steps = [
            record["steps_completed"]
            for record in group
            if record.get("steps_completed") is not None
        ]
        equivalence = [
            record["equivalent_work"]
            for record in group
            if record.get("equivalent_work") is not None
        ]
        probe_median = statistics.median(probe_medians) if probe_medians else None
        result.append(
            {
                "case": case_id, "category": group[0]["category"],
                "backend": backend,
                "library_latency_ms_median": statistics.median(latencies) if latencies else None,
                "library_latency_ms_mean": statistics.mean(latencies) if latencies else None,
                "library_latency_ms_p95": p95(latencies) if latencies else None,
                "library_probe_median_ms": probe_median,
                "library_probe_median_ms_median": probe_median,
                "library_probe_median_ms_p95": p95(probe_medians) if probe_medians else None,
                "page_wait_ms_median": statistics.median(page_waits) if page_waits else None,
                "page_wait_ms_mean": statistics.mean(page_waits) if page_waits else None,
                "page_wait_ms_p95": p95(page_waits) if page_waits else None,
                "navigation_ms_median": statistics.median(navigations) if navigations else None,
                "navigation_ms_mean": statistics.mean(navigations) if navigations else None,
                "navigation_ms_p95": p95(navigations) if navigations else None,
                "pss_peak_mb_median": statistics.median(peaks) if peaks else None,
                "steps_requested": group[0].get("steps_requested"),
                "steps_completed_min": min(completed_steps) if completed_steps else None,
                "steps_completed_max": max(completed_steps) if completed_steps else None,
                "reached_final_step": all(
                    record.get("reached_final_step") is True for record in group
                ) if group[0]["category"] in ("navigation", "download") else None,
                "equivalent_work": all(equivalence) if equivalence else None,
                "soft_failures": sum(bool(record.get("soft_fail")) for record in group),
                "ok": len(good), "total": len(group),
            }
        )
    return result


def render_table(summaries: list[dict[str, Any]]) -> str:
    rows = ["case | category | backend | lib_latency_ms(median) | "
            "lib_latency_ms(p95) | probe_ms(median) | probe_ms(p95) | "
            "page_wait_ms(median) | page_wait_ms(p95) | "
            "pss_peak_mb(median) | equivalent_work | soft_failures | ok/total"]
    for item in summaries:
        latency = item["library_latency_ms_median"]
        latency_p95 = item["library_latency_ms_p95"]
        probe = item["library_probe_median_ms"]
        probe_p95 = item["library_probe_median_ms_p95"]
        page_wait = item["page_wait_ms_median"]
        page_wait_p95 = item["page_wait_ms_p95"]
        peak = item["pss_peak_mb_median"]
        rows.append(
            f"{item['case']} | {item['category']} | {item['backend']} | "
            f"{'-' if latency is None else f'{latency:.3f}'} | "
            f"{'-' if latency_p95 is None else f'{latency_p95:.3f}'} | "
            f"{'-' if probe is None else f'{probe:.3f}'} | "
            f"{'-' if probe_p95 is None else f'{probe_p95:.3f}'} | "
            f"{'-' if page_wait is None else f'{page_wait:.3f}'} | "
            f"{'-' if page_wait_p95 is None else f'{page_wait_p95:.3f}'} | "
            f"{'-' if peak is None else f'{peak:.3f}'} | {item['ok']}/{item['total']}"
            f" | {item['equivalent_work']} | {item['soft_failures']}"
        )
    rows.append("")
    rows.append("Note: library_latency_ms excludes page_wait.")
    return "\n".join(rows) + "\n"


def run_weakness_lane(backend: str, reps: int) -> dict[str, Any]:
    """Run weakness probes in one isolated browser session for a backend."""
    interpreter = backend_python.get(backend, sys.executable)
    output = (OUTPUT_DIR / "weakness" / f"{backend}.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    secrets: list[str] = []
    returncode: int | None = None
    try:
        with SkyvernSession() as session:
            if not session.browser_address:
                raise RuntimeError("browser session returned no address")
            headers = session.cdp_headers
            secrets = [session.browser_address, *headers.values()]
            environment = {
                **os.environ,
                "CDP_URL": session.browser_address,
                "CDP_CONNECT_HEADERS": json.dumps(headers, separators=(",", ":")),
            }
            result = subprocess.run(
                [
                    interpreter,
                    str(SUITE_DIR / "weakness_workload.py"),
                    "--backend", backend,
                    "--reps", str(reps),
                    "--output", str(output),
                ],
                cwd=SUITE_DIR,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            returncode = result.returncode

        probe = json.loads(output.read_text(encoding="utf-8"))
        record = {
            "backend": backend,
            "interpreter": Path(interpreter).name,
            "ok": returncode == 0,
            "returncode": returncode,
            **probe,
        }
        if returncode != 0:
            record["error"] = f"weakness_workload.py exited with status {returncode}"
        return record
    except Exception as error:
        return {
            "backend": backend,
            "interpreter": Path(interpreter).name,
            "ok": False,
            "returncode": returncode,
            "error": redact(f"{type(error).__name__}: {error}", secrets),
        }


def main() -> int:
    global OUTPUT_DIR, backend_python
    args = parse_args()
    backend_python = args.backend_python
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("cases file must contain a JSON list")
    OUTPUT_DIR = args.output.resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    runs = [(case, rep, backend) for case in cases
            for rep in range(1, args.reps + 1) for backend in args.backends]
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(run_one, *run) for run in runs]
        records = [future.result() for future in futures]
    summaries = summarize(records)
    weakness = (
        {backend: run_weakness_lane(backend, args.reps) for backend in args.backends}
        if args.weakness else {}
    )
    soft_failures = [record for record in records if record.get("soft_fail")]
    report = {
        "records": records, "summary": summaries,
        "failures": [record for record in records if not record["ok"]],
        "soft_failures": soft_failures,
        "weakness": weakness,
        "session_count": len(runs) + len(weakness),
        "case_session_count": len(runs),
        "weakness_session_count": len(weakness),
    }
    (OUTPUT_DIR / "suite_results.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    table = render_table(summaries)
    (OUTPUT_DIR / "suite_summary.txt").write_text(table, encoding="utf-8")
    print(table, end="")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
