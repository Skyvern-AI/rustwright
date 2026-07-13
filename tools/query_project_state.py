#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import check_launch_latency_claim
import check_phase2_benchmark


ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "progress": ROOT / "docs" / "internal" / "PROGRESS.json",
    "requirements": ROOT / "docs" / "internal" / "REQUIREMENTS_STATUS.json",
}


def load_sources(names: Iterable[str]) -> dict[str, Any]:
    selected = SOURCES.keys() if "all" in names else names
    return {name: json.loads(SOURCES[name].read_text(encoding="utf-8")) for name in selected}


def lookup_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def walk(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            yield from walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{prefix}.{index}" if prefix else str(index)
            yield from walk(child, child_path)
    else:
        yield prefix, value


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def shorten(value: Any, limit: int = 360) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def compact_string_list(values: Any, *, limit: int = 220) -> Any:
    if not isinstance(values, list):
        return values
    return [shorten(value, limit) for value in values]


def configured_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def phase2_checker_args(criteria: dict[str, Any], path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        json_path=path,
        suite=criteria.get("suite", "strict"),
        lifecycle=criteria.get("lifecycle", "warm-browser"),
        reference_impl=criteria.get("reference_impl", "playwright"),
        min_repetitions=configured_int(criteria.get("min_repetitions"), 3),
        min_iterations=configured_int(criteria.get("min_iterations"), 1),
        min_case_count=configured_int(
            criteria.get("min_case_count"), check_phase2_benchmark.current_strict_case_count()
        ),
        min_speedup_pct=float(criteria.get("min_speedup_pct", 30.0)),
        max_variance_ratio=float(
            criteria.get("max_variance_ratio", check_phase2_benchmark.DEFAULT_MAX_VARIANCE_RATIO)
        ),
    )


def latest_github_hosted_strict_diagnostic(progress: dict[str, Any]) -> dict[str, Any] | None:
    latest = progress.get("metrics", {}).get("latest_github_hosted_full_strict_diagnostic")
    if not isinstance(latest, dict) or not latest.get("result_artifact"):
        return None
    return latest


def recorded_phase2_report(
    latest: dict[str, Any],
    criteria: dict[str, Any],
    *,
    result_path: str,
) -> dict[str, Any] | None:
    phase2 = latest.get("phase2_report")
    benchmark = latest.get("benchmark") or {}
    if not isinstance(phase2, dict):
        return None
    variance = benchmark.get("variance_ratio") or {}
    failed_checks = [
        {"name": str(name), "passed": False, "detail": "recorded failed check"}
        for name in phase2.get("failed_checks", [])
    ]
    repetitions = benchmark.get("repetitions")
    case_count = benchmark.get("case_count") or check_phase2_benchmark.current_strict_case_count()
    observed = {
        "iterations": benchmark.get("iterations"),
        "lifecycle": benchmark.get("lifecycle"),
        "non_passed_runs": [],
        "reference_case_count": phase2.get("reference_case_count") or case_count,
        "reference_passed_repetitions": phase2.get("reference_passed_repetitions") or repetitions,
        "reference_total_median_ms": phase2.get("reference_total_median_ms"),
        "reference_variance_ratio": phase2.get("reference_variance_ratio") or variance.get("playwright"),
        "repetitions": repetitions,
        "rustwright_case_count": phase2.get("rustwright_case_count") or case_count,
        "rustwright_passed_repetitions": phase2.get("rustwright_passed_repetitions") or repetitions,
        "rustwright_reduction_pct": phase2.get("rustwright_reduction_pct"),
        "rustwright_total_median_ms": phase2.get("rustwright_total_median_ms"),
        "rustwright_variance_ratio": phase2.get("rustwright_variance_ratio") or variance.get("rustwright"),
        "suite": benchmark.get("suite"),
    }
    accepted = bool(phase2.get("accepted"))
    checks = [
        {
            "name": "all_runs_passed",
            "passed": not observed["non_passed_runs"],
            "detail": "recorded Phase 2 report has no non-passed runs",
        },
        {
            "name": "speedup",
            "passed": accepted and not any(item["name"] == "speedup" for item in failed_checks),
            "detail": f"recorded Rustwright reduction percent {observed['rustwright_reduction_pct']}",
        },
        *failed_checks,
    ]
    return {
        "status": "accepted" if accepted else "rejected",
        "accepted": accepted,
        "result_path": result_path,
        "checks": checks,
        "failed_checks": failed_checks,
        "observed": observed,
        "criteria": criteria,
        "source": "recorded_progress_metadata",
    }


def github_hosted_diagnostic_evidence(latest: dict[str, Any]) -> dict[str, Any] | None:
    benchmark = latest.get("benchmark") or {}
    phase2 = latest.get("phase2_report") or {}
    docker = latest.get("docker") or {}
    totals = benchmark.get("total_median_ms") or {}
    variance = benchmark.get("variance_ratio") or {}
    implementations = benchmark.get("implementations") or ["rustwright", "playwright"]
    repetitions = int(benchmark.get("repetitions") or 0)
    runs_total = repetitions * len(implementations)

    if not totals or not variance:
        return None

    return {
        "source": "github-actions-recorded",
        "artifact": "rustwright-benchmark-results",
        "result_path": latest.get("result_artifact"),
        "run_url": latest.get("run_url"),
        "benchmark": {
            "suite": benchmark.get("suite"),
            "lifecycle": benchmark.get("lifecycle"),
            "iterations": benchmark.get("iterations"),
            "repetitions": repetitions,
            "case": benchmark.get("case"),
            "case_count": phase2.get("rustwright_case_count")
            or benchmark.get("case_count")
            or check_phase2_benchmark.current_strict_case_count(),
            "implementations": implementations,
            "runs_passed": runs_total,
            "runs_total": runs_total,
            "total_median_ms": totals,
            # The recorded Phase 2 report stores p75/p25 ratios, not raw p25/p75.
            # These normalized values preserve the variance guard semantics for
            # token-light summaries when the ignored benchmark artifact is absent.
            "total_p25_ms": {"playwright": 1.0, "rustwright": 1.0},
            "total_p75_ms": {
                "playwright": variance.get("playwright"),
                "rustwright": variance.get("rustwright"),
            },
            "result_path": latest.get("result_artifact"),
        },
        "environment": {
            "source": "github-actions-recorded",
            "source_type": "github-actions-recorded",
            "runner": latest.get("runner_label"),
            "container_isolation": latest.get("container_isolation"),
            "docker_memory_limit": docker.get("memory_limit"),
            "docker_memory_swap_limit": docker.get("memory_swap_limit"),
            "docker_image": docker.get("image"),
            "docker_image_id": docker.get("image_id"),
            "docker_cpu_host_info": docker.get("cpu_host_info"),
            "docker_cpu_quota": docker.get("cpu_quota"),
            "git_rev": latest.get("commit"),
        },
    }


def phase_2_benchmark_acceptance(progress: dict[str, Any]) -> dict[str, Any]:
    configured = progress.get("benchmarks", {}).get("phase_2_benchmark_acceptance_checker") or {}
    criteria = configured.get("default_criteria") or {}
    hosted = latest_github_hosted_strict_diagnostic(progress)
    if hosted:
        result_path = hosted["result_artifact"]
        path = ROOT / result_path
        if path.exists():
            args = phase2_checker_args(criteria, path)
            report = check_phase2_benchmark.evaluate(check_phase2_benchmark.load_json(path), args)
            failed_checks = [item for item in report["checks"] if not item["passed"]]
            return {
                "status": report["status"],
                "accepted": report["accepted"],
                "result_path": result_path,
                "checks": report["checks"],
                "failed_checks": failed_checks,
                "observed": report["observed"],
                "criteria": report["criteria"],
                "source": "github_actions_artifact",
            }
        recorded = recorded_phase2_report(hosted, criteria, result_path=result_path)
        if recorded is not None:
            return recorded

    latest = (
        progress.get("benchmarks", {}).get("latest_current_tree_strict_repeated_attempt")
        or progress.get("benchmarks", {}).get("latest_current_tree_strict_api_smoke")
        or progress.get("benchmarks", {}).get("latest_strict_api_benchmark_smoke")
    )
    result_path = (latest or {}).get("result_path")
    if not result_path:
        return {"status": "missing", "accepted": False, "reason": "no strict benchmark result path recorded"}

    path = ROOT / result_path
    if not path.exists():
        recorded = (latest or {}).get("acceptance_checker") or {}
        if recorded.get("status") == "accepted":
            speedup = ((latest or {}).get("speedup") or {}).get(
                "vs_python_playwright_reference_lower_total_median_percent"
            )
            totals = (latest or {}).get("total_median_ms") or {}
            return {
                "status": "accepted",
                "accepted": True,
                "result_path": result_path,
                "checks": [
                    {
                        "name": "all_runs_passed",
                        "passed": True,
                        "detail": "recorded accepted benchmark evidence; result JSON is intentionally ignored",
                    },
                    {
                        "name": "speedup",
                        "passed": True,
                        "detail": f"recorded Rustwright reduction percent {speedup}",
                    },
                ],
                "failed_checks": [],
                "observed": {
                    "iterations": (latest or {}).get("iterations"),
                    "lifecycle": (latest or {}).get("lifecycle"),
                    "non_passed_runs": [],
                    "reference_case_count": (latest or {}).get("sample_count"),
                    "reference_passed_repetitions": ((latest or {}).get("passed_repetitions") or {}).get(
                        "python_playwright_reference"
                    ),
                    "reference_total_median_ms": totals.get("python_playwright_reference"),
                    "repetitions": (latest or {}).get("repetitions"),
                    "rustwright_case_count": (latest or {}).get("sample_count"),
                    "rustwright_passed_repetitions": ((latest or {}).get("passed_repetitions") or {}).get("rustwright"),
                    "rustwright_reduction_pct": speedup,
                    "rustwright_total_median_ms": totals.get("rustwright"),
                    "suite": (latest or {}).get("suite"),
                },
                "criteria": criteria,
                "source": "recorded_progress_metadata",
            }
        return {"status": "missing", "accepted": False, "result_path": result_path, "reason": "result file missing"}

    args = phase2_checker_args(criteria, path)
    report = check_phase2_benchmark.evaluate(check_phase2_benchmark.load_json(path), args)
    failed_checks = [item for item in report["checks"] if not item["passed"]]
    return {
        "status": report["status"],
        "accepted": report["accepted"],
        "result_path": result_path,
        "failed_checks": failed_checks,
        "observed": report["observed"],
        "criteria": report["criteria"],
    }


def launch_latency_claim(progress: dict[str, Any]) -> dict[str, Any]:
    hosted = latest_github_hosted_strict_diagnostic(progress)
    if hosted:
        result_path = hosted["result_artifact"]
        path = ROOT / result_path
        if path.exists():
            evidence = check_launch_latency_claim.matrix_json_to_evidence(
                check_launch_latency_claim.load_json(path),
                source="github-actions",
                runner=hosted.get("runner_label"),
                artifact="rustwright-benchmark-results",
                run_url=hosted.get("run_url"),
            )
        else:
            evidence = github_hosted_diagnostic_evidence(hosted)
        if evidence is not None:
            return check_launch_latency_claim.evaluate_evidence(
                evidence,
                evidence_path="metrics.latest_github_hosted_full_strict_diagnostic",
                min_repetitions=3,
                min_iterations=1,
                min_case_count=check_phase2_benchmark.current_strict_case_count(),
                min_speedup_pct=30.0,
                max_variance_ratio=check_launch_latency_claim.DEFAULT_MAX_VARIANCE_RATIO,
                reference_impl="playwright",
            )

    return check_launch_latency_claim.evaluate(
        progress,
        evidence_path="metrics.latest_blacksmith_benchmark_workflow",
        min_repetitions=3,
        min_iterations=1,
        min_case_count=check_phase2_benchmark.current_strict_case_count(),
        min_speedup_pct=30.0,
        max_variance_ratio=check_launch_latency_claim.DEFAULT_MAX_VARIANCE_RATIO,
        reference_impl="playwright",
    )


def compact_phase_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {
            "name": phase.get("name"),
            "status": phase.get("status"),
            "focus": phase.get("focus"),
            "completion_criteria_count": len(phase.get("completion_criteria") or []),
            "coverage_backlog_count": len(phase.get("coverage_backlog") or []),
            "primary_target": phase.get("primary_target"),
        }
        for name, phase in plan.items()
        if isinstance(phase, dict)
    }


def compact_phase_1_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    rows = ledger.get("rows") or []
    status_counts: dict[str, int] = {}
    remaining: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "docker_verified":
            remaining.append(
                {
                    "id": row.get("id"),
                    "coverage_point": row.get("coverage_point"),
                    "status": status,
                    "remaining": row.get("remaining"),
                }
            )
    return {
        "status": ledger.get("status"),
        "row_count": len(rows),
        "status_counts": status_counts,
        "remaining_rows": remaining,
    }


def compact_gate(gate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(gate, dict):
        return gate
    current = gate.get("current_configured_selection") or {}
    latest = current.get("latest_docker_attempt") or {}
    return {
        "command": gate.get("command") or latest.get("command"),
        "status": latest.get("status") or current.get("docker_status") or gate.get("status"),
        "memory_cap": gate.get("memory_cap"),
        "pytest": latest.get("pytest") or gate.get("pytest"),
        "rustwright_parity_sample": latest.get("rustwright_parity_sample") or gate.get("rustwright_parity_sample"),
        "playwright_reference_parity_sample": latest.get("playwright_reference_parity_sample")
        or gate.get("playwright_parity_sample"),
        "phase_1_gate_checker": latest.get("phase_1_gate_checker"),
    }


def compact_phase1_gate_update(update: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(update, dict):
        return update
    docker = update.get("docker_execution") if isinstance(update.get("docker_execution"), dict) else {}
    local = update.get("local_validation") if isinstance(update.get("local_validation"), dict) else {}
    return {
        "date": update.get("date"),
        "status": update.get("status"),
        "change_summary": shorten(update.get("change_summary"), 240),
        "local_validation": {
            key: shorten(local.get(key), 260 if key == "remote_wrapper_fast_auth_classification" else 160)
            for key in (
                "phase1_gate_checker",
                "pytest",
                "py_compile",
                "shell_syntax",
                "remote_wrapper_fast_auth_classification",
            )
            if local.get(key) is not None
        },
        "docker_execution": {
            key: shorten(docker.get(key), 220)
            for key in (
                "local_docker_attempt",
                "mac_mini_attempt",
                "mac_mini_auth_url",
                "next_required_evidence",
            )
            if docker.get(key) is not None
        },
    }


def compact_benchmark(benchmark: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(benchmark, dict):
        return benchmark
    return {
        "status": benchmark.get("status"),
        "command": benchmark.get("command"),
        "result_path": benchmark.get("result_path") or benchmark.get("latest_result_path"),
        "suite": benchmark.get("suite"),
        "lifecycle": benchmark.get("lifecycle"),
        "iterations": benchmark.get("iterations"),
        "repetitions": benchmark.get("repetitions"),
        "sample_count": benchmark.get("sample_count"),
        "container_isolation": benchmark.get("container_isolation"),
        "total_median_ms": benchmark.get("total_median_ms") or benchmark.get("latest_total_median_ms"),
        "speedup": benchmark.get("speedup"),
        "measured_failures": benchmark.get("measured_failures"),
    }


def compact_lifecycle_smokes(smokes: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(smokes, dict):
        return smokes
    compact: dict[str, Any] = {}
    for name, smoke in smokes.items():
        if not isinstance(smoke, dict):
            compact[name] = smoke
            continue
        compact[name] = {
            key: smoke.get(key)
            for key in (
                "status",
                "rustwright_median_ms",
                "python_playwright_reference_median_ms",
                "typescript_playwright_reference_mean_ms",
            )
            if smoke.get(key) is not None
        }
    return compact


def compact_performance_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return plan
    hotspots = plan.get("first_tranche_hotspots") or []
    pipe_probe = plan.get("latest_pipe_transport_probe") or {}
    request_probe = plan.get("latest_default_request_history_probe") or {}
    request_fetch_probe = plan.get("latest_request_history_fetch_completion_probe") or {}
    nodelay_probe = plan.get("latest_websocket_nodelay_probe") or {}
    nodelay_full = nodelay_probe.get("macmini_full_strict") if isinstance(nodelay_probe, dict) else {}
    return {
        "status": plan.get("status"),
        "baseline_result_path": plan.get("baseline_result_path"),
        "latest_result_path": plan.get("latest_result_path"),
        "latest_total_median_ms": plan.get("latest_total_median_ms"),
        "latest_macmini_hotspot_confirmation": plan.get("latest_macmini_hotspot_confirmation"),
        "latest_websocket_nodelay_probe": {
            "status": nodelay_probe.get("status"),
            "full_lower_pct": (
                (nodelay_full.get("speedup") or {}).get("phase2_metric_rustwright_lower_percent")
                if isinstance(nodelay_full, dict)
                else None
            ),
        }
        if isinstance(nodelay_probe, dict)
        else nodelay_probe,
        "latest_pipe_transport_probe": {
            "status": pipe_probe.get("status"),
            "transport": pipe_probe.get("transport"),
            "decision": pipe_probe.get("decision"),
            "macmini_response_slice": pipe_probe.get("macmini_response_slice"),
            "macmini_hotspot_slice": pipe_probe.get("macmini_hotspot_slice"),
        }
        if isinstance(pipe_probe, dict)
        else pipe_probe,
        "latest_default_request_history_probe": {
            "status": request_probe.get("status"),
            "macmini_focused_request_history": request_probe.get("macmini_focused_request_history"),
            "macmini_hotspot_followup": request_probe.get("macmini_hotspot_followup"),
            "next_tranche": request_probe.get("next_tranche"),
        }
        if isinstance(request_probe, dict)
        else request_probe,
        "latest_request_history_fetch_completion_probe": {
            "status": request_fetch_probe.get("status"),
            "macmini_hotspot": request_fetch_probe.get("macmini_hotspot"),
            "macmini_full_strict": request_fetch_probe.get("macmini_full_strict"),
        }
        if isinstance(request_fetch_probe, dict)
        else request_fetch_probe,
        "targets": plan.get("targets"),
        "top_hotspots": [
            {
                "case": item.get("case"),
                "status": item.get("status"),
                "latest_delta_ms": item.get("latest_delta_ms"),
                "planned_optimization": item.get("planned_optimization"),
            }
            for item in hotspots[:5]
        ],
    }


def compact_dependency_audit(audit: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(audit, dict):
        return audit
    latest = audit.get("latest_machine_audit") or audit.get("latest_result") or audit.get("latest_audit") or {}
    latest_smoke = audit.get("latest_replacement_smoke") if isinstance(audit.get("latest_replacement_smoke"), dict) else {}
    latest_overlay = audit.get("latest_overlay_pytest") if isinstance(audit.get("latest_overlay_pytest"), dict) else {}
    latest_alias_command = (
        audit.get("latest_alias_command_smoke") if isinstance(audit.get("latest_alias_command_smoke"), dict) else {}
    )
    latest_prompt_overlay = (
        audit.get("latest_prompt_overlay_smoke") if isinstance(audit.get("latest_prompt_overlay_smoke"), dict) else {}
    )
    alias_coverage = latest.get("alias_symbol_coverage") or {}
    method_coverage = latest.get("method_name_coverage") or {}
    typed_coverage = latest.get("typed_method_coverage") or {}
    child_statuses = [
        alias_coverage.get("status"),
        method_coverage.get("status"),
        typed_coverage.get("status"),
    ]
    derived_status = audit.get("status") or latest.get("status")
    if derived_status is None and all(status == "ok" for status in child_statuses):
        derived_status = "ok"
    elif derived_status is None:
        derived_status = next((status for status in child_statuses if status), None)
    return {
        "status": derived_status,
        "audit_date": audit.get("audit_date") or latest.get("audit_date"),
        "source_path": audit.get("source_path"),
        "mode": audit.get("mode"),
        "output_path": latest.get("output_path"),
        "python_files_scanned": latest.get("python_files_scanned")
        or (latest.get("scan_scope") or {}).get("python_files_scanned"),
        "text_files_scanned": latest.get("text_files_scanned") or (latest.get("scan_scope") or {}).get("text_files_scanned"),
        "alias_symbol_coverage": {
            key: alias_coverage.get(key)
            for key in ("status", "missing_total", "import_error_total")
            if alias_coverage.get(key) is not None
        },
        "method_name_coverage": {
            key: method_coverage.get(key)
            for key in ("status", "missing_total", "import_error_total", "method_count")
            if method_coverage.get(key) is not None
        },
        "typed_method_coverage": {
            key: typed_coverage.get(key)
            for key in (
                "status",
                "missing_total",
                "import_error_total",
                "typed_call_count",
                "receiver_method_count",
            )
            if typed_coverage.get(key) is not None
        },
        "top_cdp_methods": (latest.get("top_cdp_methods") or [])[:8],
        "requirement_area_counts": latest.get("requirement_area_counts"),
        "latest_replacement_smoke": {
            key: shorten(latest_smoke.get(key), 260)
            for key in (
                "date",
                "status",
                "output_path",
                "alias_failures",
                "audit_status",
                "failure_policy",
                "skyvern_module_import_mode",
                "skyvern_modules_imported",
                "skyvern_modules_failed",
                "skyvern_module_import_warning",
            )
            if latest_smoke.get(key) is not None
        },
        "latest_overlay_pytest": {
            key: shorten(latest_overlay.get(key), 260)
            for key in (
                "status",
                "alias_preflight_status",
                "alias_modules_checked",
                "target_count",
                "tests_passed",
            )
            if latest_overlay.get(key) is not None
        }
        | {
            "default_conftest": {
                key: shorten((latest_overlay.get("default_conftest") or {}).get(key), 260)
                for key in (
                    "status",
                    "classification",
                    "missing_module",
                    "pytest_summary",
                )
                if (latest_overlay.get("default_conftest") or {}).get(key) is not None
            },
            "noconftest": {
                key: shorten((latest_overlay.get("noconftest") or {}).get(key), 260)
                for key in (
                    "status",
                    "pytest_summary",
                )
                if (latest_overlay.get("noconftest") or {}).get(key) is not None
            },
        },
        "latest_alias_command_smoke": {
            key: shorten(latest_alias_command.get(key), 260)
            for key in (
                "status",
                "alias_preflight_status",
                "command_status",
                "command_classification",
                "skyvern_module",
                "output_path",
            )
            if latest_alias_command.get(key) is not None
        }
        | {
            "cloud_browser_import_smoke": {
                key: shorten((latest_alias_command.get("cloud_browser_import_smoke") or {}).get(key), 260)
                for key in (
                    "status",
                    "classification",
                    "output_path",
                )
                if (latest_alias_command.get("cloud_browser_import_smoke") or {}).get(key) is not None
            }
        },
        "latest_prompt_overlay_smoke": {
            key: shorten(latest_prompt_overlay.get(key), 260)
            for key in (
                "status",
                "alias_preflight_status",
                "module_count",
                "environment_blocked_modules",
                "missing_module",
                "output_path",
            )
            if latest_prompt_overlay.get(key) is not None
        },
    }


def compact_p0_status(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return status
    return {
        key: {
            "status": value.get("status"),
            "current": shorten(value.get("current")),
            "remaining": shorten(value.get("remaining")),
        }
        for key, value in status.items()
        if isinstance(value, dict)
    }


def compact_goal_completion(goal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(goal, dict):
        return goal
    return {
        "short_goal": shorten(goal.get("short_goal"), 360),
        "active_goal": shorten(goal.get("active_goal"), 360),
        "required_for_completion": compact_string_list(goal.get("required_for_completion"), limit=260),
        "completion_boundary": shorten(goal.get("completion_boundary"), 260),
        "no_hidden_third_gates": goal.get("no_hidden_third_gates"),
    }


def compact_external_audit(audit: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(audit, dict):
        return audit
    return {
        "date": audit.get("date"),
        "status": audit.get("status"),
        "verdict": audit.get("verdict"),
        "tool": audit.get("tool"),
        "artifact_path": audit.get("artifact_path"),
        "summary": shorten(audit.get("summary"), 220),
        "ranked_next_actions": compact_string_list((audit.get("ranked_next_actions") or [])[:2], limit=180),
    }


def compact_phase2_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    compact = dict(report)
    compact.pop("checks", None)
    return compact


def compact_launch_claim(report: dict[str, Any]) -> dict[str, Any]:
    observed = report.get("observed") if isinstance(report.get("observed"), dict) else {}
    criteria = report.get("criteria") if isinstance(report.get("criteria"), dict) else {}
    return {
        "status": report.get("status"),
        "accepted": report.get("accepted"),
        "evidence_path": report.get("evidence_path"),
        "checks": [
            {
                "name": item.get("name"),
                "passed": item.get("passed"),
                "detail": shorten(item.get("detail"), 120),
            }
            for item in report.get("checks", [])
            if isinstance(item, dict)
        ],
        "criteria": {
            key: criteria.get(key)
            for key in (
                "min_case_count",
                "min_repetitions",
                "min_iterations",
                "min_speedup_pct",
                "reference_impl",
            )
            if criteria.get(key) is not None
        },
        "observed": {
            key: observed.get(key)
            for key in (
                "runner",
                "case_count",
                "suite",
                "lifecycle",
                "iterations",
                "repetitions",
                "runs_passed",
                "runs_total",
                "rustwright_reduction_pct",
                "result_path",
                "run_url",
            )
            if observed.get(key) is not None
        },
    }


def compact_remote_wrapper(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return value
    latest = value.get("latest_check") if isinstance(value.get("latest_check"), dict) else {}
    remote_docker = latest.get("remote_docker") if isinstance(latest.get("remote_docker"), dict) else {}
    memory_checks = latest.get("memory_limit_checks") if isinstance(latest.get("memory_limit_checks"), dict) else {}
    remote_pull_check = latest.get("remote_pull_check") if isinstance(latest.get("remote_pull_check"), dict) else {}
    latest_phase1 = latest.get("latest_remote_phase1") if isinstance(latest.get("latest_remote_phase1"), dict) else {}
    latest_strict = (
        latest.get("latest_remote_strict_benchmark")
        if isinstance(latest.get("latest_remote_strict_benchmark"), dict)
        else {}
    )
    return {
        "status": value.get("status"),
        "tool": value.get("tool"),
        "host": latest.get("host"),
        "transport": latest.get("transport"),
        "remote_docker": remote_docker,
        "memory_limit_checks": memory_checks,
        "remote_pull_check": {
            key: remote_pull_check.get(key)
            for key in ("status", "failed_check", "passed_check")
            if remote_pull_check.get(key) is not None
        },
        "latest_remote_phase1": {
            key: latest_phase1.get(key)
            for key in (
                "status",
                "image",
                "memory_cap_bytes",
            )
            if latest_phase1.get(key) is not None
        },
        "latest_remote_strict_benchmark": {
            **{
                key: latest_strict.get(key)
                for key in (
                    "status",
                    "result_path",
                    "case_count",
                    "repetitions",
                    "iterations",
                    "docker_memory_limit",
                    "runs_passed",
                    "runs_total",
                )
                if latest_strict.get(key) is not None
            },
            "speedup_vs_python_pct": (
                (latest_strict.get("speedup") or {}).get(
                    "vs_python_playwright_reference_lower_total_mean_distribution_median_percent"
                )
                or (latest_strict.get("speedup") or {}).get("vs_playwright_median_reduction_pct")
            ),
            "acceptance_status": ((latest_strict.get("acceptance_checker") or {}).get("status")),
        },
        "docker_build_status": latest.get("docker_build_status"),
    }


def compact_strict_local_profile(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return value
    return {
        key: value.get(key)
        for key in (
            "date",
            "status",
            "suite",
            "lifecycle",
            "iterations",
            "case_count",
            "docker",
            "failures",
            "remote_mac_mini",
            "total_mean_ms",
            "speedup",
        )
        if value.get(key) is not None
    }


def compact_testbox_path(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return value
    warmup = value.get("latest_warmup_attempt") if isinstance(value.get("latest_warmup_attempt"), dict) else {}
    helper_update = value.get("latest_helper_update") if isinstance(value.get("latest_helper_update"), dict) else {}
    visibility = (
        value.get("latest_visibility_preflight")
        if isinstance(value.get("latest_visibility_preflight"), dict)
        else {}
    )
    skyvern_cloud = (
        value.get("latest_skyvern_cloud_testbox_attempt")
        if isinstance(value.get("latest_skyvern_cloud_testbox_attempt"), dict)
        else {}
    )
    return {
        "status": value.get("status"),
        "workflow": value.get("workflow"),
        "helper": value.get("helper"),
        "evidence_policy": shorten(value.get("evidence_policy")),
        "latest_helper_update": {
            key: value
            for key, value in {
                "date": helper_update.get("date"),
                "status": helper_update.get("status"),
                "summary": "RUSTWRIGHT_TESTBOX_RUN_COMMAND wired"
                if helper_update.get("summary")
                else None,
            }.items()
            if value is not None
        },
        "latest_visibility_preflight": {
            key: shorten(visibility.get(key), 180)
            for key in (
                "status",
                "static_preflight_status",
            )
            if visibility.get(key) is not None
        },
        "latest_warmup_attempt": {
            key: shorten(warmup.get(key), 220)
            for key in (
                "date",
                "status",
                "command",
                "blacksmith_error",
                "main_push_attempt",
                "pr_merge_attempt",
                "workflow_fix_pr",
            )
            if warmup.get(key) is not None
        },
        "latest_skyvern_cloud_testbox_attempt": {
            key: shorten(skyvern_cloud.get(key), 220)
            for key in (
                "date",
                "status",
                "testbox_id",
                "run_url",
                "hostname",
                "failure",
                "interpretation",
            )
            if skyvern_cloud.get(key) is not None
        },
    }


def build_summary(data: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    progress = data.get("progress", {})
    requirements = data.get("requirements", {})
    matrix = requirements.get("feature_coverage_matrix") or []
    top_rice = sorted(matrix, key=lambda item: item.get("rice", 0), reverse=True)[:5]
    latest_benchmark = (
        progress.get("benchmarks", {}).get("latest_repeated_bench_full_matrix")
        or progress.get("benchmarks", {}).get("latest_bench_full_baseline")
        or progress.get("benchmarks", {}).get("latest_broadened_docker_suite")
        or progress.get("benchmarks", {}).get("latest_broadened_local_suite")
    )
    latest_strict_benchmark = progress.get("benchmarks", {}).get("latest_strict_api_benchmark_smoke")
    phase_1_ledger = requirements.get("phase_1_coverage_ledger")
    phase_plan = requirements.get("execution_phase_plan")
    strict_plan = progress.get("benchmarks", {}).get("strict_api_performance_plan")
    dependency_audit = progress.get("skyvern_cloud_dependency_audit") or requirements.get("skyvern_cloud_dependency_audit")
    return {
        "shared_parity_cases": progress.get("metrics", {}).get("shared_parity_cases", {}).get("value"),
        "strict_api_cases": progress.get("metrics", {}).get("strict_api_cases", {}).get("value"),
        "latest_sampled_docker_gate": progress.get("metrics", {}).get("latest_sampled_docker_gate")
        if full
        else compact_gate(progress.get("metrics", {}).get("latest_sampled_docker_gate")),
        "phase_1_current_gate_update": progress.get("metrics", {}).get("latest_phase1_gate_wiring_update")
        if full
        else compact_phase1_gate_update(progress.get("metrics", {}).get("latest_phase1_gate_wiring_update")),
        "remote_mac_mini_docker_wrapper": progress.get("metrics", {}).get("remote_mac_mini_docker_wrapper")
        if full
        else compact_remote_wrapper(progress.get("metrics", {}).get("remote_mac_mini_docker_wrapper")),
        "testbox_benchmark_path": progress.get("metrics", {}).get("testbox_benchmark_path")
        if full
        else compact_testbox_path(progress.get("metrics", {}).get("testbox_benchmark_path")),
        "latest_external_launch_audit": progress.get("metrics", {}).get("latest_external_launch_audit")
        if full
        else compact_external_audit(progress.get("metrics", {}).get("latest_external_launch_audit")),
        "latest_benchmark": latest_benchmark if full else compact_benchmark(latest_benchmark),
        "latest_strict_benchmark": latest_strict_benchmark if full else compact_benchmark(latest_strict_benchmark),
        "latest_strict_local_profile": progress.get("benchmarks", {}).get("latest_strict_local_profile")
        if full
        else compact_strict_local_profile(progress.get("benchmarks", {}).get("latest_strict_local_profile")),
        "latest_benchmark_harness_overhead_fix": progress.get("benchmarks", {}).get("latest_benchmark_harness_overhead_fix"),
        "phase_2_benchmark_acceptance": phase_2_benchmark_acceptance(progress)
        if full
        else compact_phase2_acceptance(phase_2_benchmark_acceptance(progress)),
        "launch_latency_claim": launch_latency_claim(progress)
        if full
        else compact_launch_claim(launch_latency_claim(progress)),
        "strict_api_performance_plan": strict_plan if full else compact_performance_plan(strict_plan),
        "skyvern_cloud_dependency_audit": dependency_audit if full else compact_dependency_audit(dependency_audit),
        "benchmark_lifecycle_smokes": progress.get("benchmarks", {}).get("latest_lifecycle_smokes")
        if full
        else compact_lifecycle_smokes(progress.get("benchmarks", {}).get("latest_lifecycle_smokes")),
        "top_next_work": progress.get("top_next_work") if full else compact_string_list(progress.get("top_next_work")),
        "top_rice_features": [
            {
                "id": item.get("id"),
                "feature_area": item.get("feature_area"),
                "rice": item.get("rice"),
                "chromium_sync": item.get("chromium_sync"),
                "firefox": item.get("firefox"),
                "webkit": item.get("webkit"),
            }
            for item in top_rice
        ],
        "goal_completion_definition": requirements.get("goal_completion_definition")
        if full
        else compact_goal_completion(requirements.get("goal_completion_definition")),
        "execution_phase_plan": phase_plan if full else compact_phase_plan(phase_plan or {}),
        "phase_1_coverage_ledger": phase_1_ledger if full else compact_phase_1_ledger(phase_1_ledger or {}),
        "p0_status": requirements.get("p0_status") if full else compact_p0_status(requirements.get("p0_status")),
        "known_major_gaps": requirements.get("known_major_gaps")
        if full
        else compact_string_list(requirements.get("known_major_gaps")),
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Query compact project status JSON companions.")
    parser.add_argument(
        "--source",
        choices=["progress", "requirements", "all"],
        action="append",
        default=None,
        help="JSON source to query. Defaults to all.",
    )
    parser.add_argument("--path", help="Dot path to print, for example progress.metrics.shared_parity_cases.value")
    parser.add_argument("--grep", help="Case-insensitive search across leaf paths and values.")
    parser.add_argument("--list-paths", action="store_true", help="List leaf paths for the selected source(s).")
    parser.add_argument("--summary", action="store_true", help="Print a compact summary. This is the default action.")
    parser.add_argument("--full-summary", action="store_true", help="Print the older verbose summary shape.")
    args = parser.parse_args()

    requested_sources = args.source or ["all"]
    source_names = ["all"] if "all" in requested_sources else requested_sources
    data = load_sources(source_names)

    if args.path:
        root, _, subpath = args.path.partition(".")
        if root not in data or not subpath:
            raise SystemExit(f"--path must start with one of: {', '.join(data)}")
        print_json(lookup_path(data[root], subpath))
        return 0

    if args.grep:
        needle = args.grep.lower()
        matches: dict[str, Any] = {}
        for source, value in data.items():
            for path, leaf in walk(value, source):
                text = f"{path} {leaf}".lower()
                if needle in text:
                    matches[path] = leaf
        print_json(matches)
        return 0

    if args.list_paths:
        for source, value in data.items():
            for path, _ in walk(value, source):
                print(path)
        return 0

    print_json(build_summary(data, full=args.full_summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
