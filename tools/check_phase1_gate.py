#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INTERNAL_DOCS = ROOT / "docs" / "internal"
PHASE_1_ROW_COMPLETE_STATUSES = {"covered_needs_current_docker_gate", "docker_verified"}
DOCKER_VERIFIED_STATUSES = {"docker_verified", "passed"}
MAX_TEST_MEMORY_BYTES = 8 * 1024 * 1024 * 1024


def load_json(name: str) -> Any:
    return json.loads((INTERNAL_DOCS / name).read_text(encoding="utf-8"))


def check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def all_zero(values: dict[str, Any], keys: list[str]) -> bool:
    return all(values.get(key) == 0 for key in keys)


def inside_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False
    text = cgroup.read_text(encoding="utf-8", errors="ignore")
    return any(token in text for token in ("docker", "containerd", "kubepods", "libpod"))


def read_memory_limit_bytes() -> int | None:
    for path in (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        if not path.exists():
            continue
        value = path.read_text(encoding="utf-8").strip()
        if value in {"", "max"}:
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


def current_docker_gate_verified() -> tuple[bool, str]:
    if not inside_container():
        return False, "not running inside a detected container"
    limit = read_memory_limit_bytes()
    if limit is None:
        return False, "no parseable cgroup memory limit"
    if limit > MAX_TEST_MEMORY_BYTES:
        return False, f"container memory limit is above 8GB: {limit} bytes"
    return True, f"detected capped container memory limit: {limit} bytes"


def build_report(*, current_docker_run: bool = False) -> dict[str, Any]:
    requirements = load_json("REQUIREMENTS_STATUS.json")
    progress = load_json("PROGRESS.json")
    phase_1 = requirements["execution_phase_plan"]["phase_1"]
    ledger = requirements["phase_1_coverage_ledger"]
    rows = ledger["rows"]
    backlog = phase_1["coverage_backlog"]
    progress_metrics = progress["metrics"]
    shared_case_count = progress_metrics["shared_parity_cases"]["value"]
    incremental_shared = progress_metrics.get("post_full_gate_incremental_shared_parity", {})
    local_sampled = progress_metrics["latest_sampled_docker_gate"]["current_configured_selection"][
        "latest_local_validation"
    ]
    rustwright_full = progress_metrics["latest_rustwright_full_parity_run"]
    playwright_full = progress_metrics["latest_playwright_full_parity_run"]
    api_surface = progress_metrics["latest_sync_async_api_surface_audit"]
    skyvern_dependency = progress["skyvern_cloud_dependency_audit"]
    skyvern_audit = skyvern_dependency["latest_machine_audit"]
    skyvern_replacement_smoke = skyvern_dependency.get("latest_replacement_smoke") or {}
    skyvern_overlay = skyvern_dependency.get("latest_overlay_pytest") or {}
    skyvern_alias_command = skyvern_dependency.get("latest_alias_command_smoke") or {}
    skyvern_prompt_overlay = skyvern_dependency.get("latest_prompt_overlay_smoke") or {}
    prompt_module_results = skyvern_prompt_overlay.get("module_results") or []
    prompt_failure_classifications = {
        item.get("classification")
        for item in prompt_module_results
        if isinstance(item, dict) and item.get("status") != "passed"
    }
    incremental_total = int(incremental_shared.get("total") or 0)
    incremental_rustwright_passed = int(incremental_shared.get("rustwright_passed") or 0)
    incremental_playwright_passed = int(incremental_shared.get("playwright_reference_passed") or 0)
    incremental_status = str(incremental_shared.get("status") or "")
    incremental_local_passed = incremental_total == 0 or (
        incremental_status in {"local_focused_passed", "passed", "docker_verified"}
        and incremental_total >= 0
        and incremental_rustwright_passed == incremental_total
        and incremental_playwright_passed == incremental_total
    )

    row_status_counts: dict[str, int] = {}
    for row in rows:
        row_status_counts[row["status"]] = row_status_counts.get(row["status"], 0) + 1

    checks = [
        check(
            "coverage_backlog_matches_ledger",
            [row["coverage_point"] for row in rows] == backlog,
            f"{len(rows)} ledger rows for {len(backlog)} backlog entries",
        ),
        check(
            "all_phase_1_rows_have_executable_evidence",
            all(row["evidence"] and row["latest_result"] and row["remaining"] for row in rows),
            "every row has evidence, latest_result, and remaining fields",
        ),
        check(
            "all_phase_1_rows_locally_covered",
            all(row["status"] in PHASE_1_ROW_COMPLETE_STATUSES for row in rows),
            json.dumps(row_status_counts, sort_keys=True),
        ),
        check(
            "local_sampled_gate_passed",
            local_sampled["status"] == "passed"
            and local_sampled["pytest"]["passed"] > 0
            and local_sampled["rustwright_parity_sample"]["passed"] == local_sampled["rustwright_parity_sample"]["total"]
            and local_sampled["playwright_reference_parity_sample"]["passed"]
            == local_sampled["playwright_reference_parity_sample"]["total"],
            (
                f"pytest={local_sampled['pytest']['passed']} passed, "
                f"rustwright={local_sampled['rustwright_parity_sample']['passed']}/"
                f"{local_sampled['rustwright_parity_sample']['total']}, "
                f"playwright={local_sampled['playwright_reference_parity_sample']['passed']}/"
                f"{local_sampled['playwright_reference_parity_sample']['total']}"
            ),
        ),
        check(
            "full_local_rustwright_parity_recorded",
            rustwright_full["status"] == "passed"
            and rustwright_full["passed"] == rustwright_full["total"]
            and rustwright_full["total"] > 0,
            (
                f"{rustwright_full['passed']}/{rustwright_full['total']} recorded full run; "
                f"current shared count {shared_case_count}"
            ),
        ),
        check(
            "full_local_playwright_reference_parity_recorded",
            playwright_full["status"] == "passed"
            and playwright_full["passed"] == playwright_full["total"]
            and playwright_full["total"] > 0,
            (
                f"{playwright_full['passed']}/{playwright_full['total']} recorded full run; "
                f"current shared count {shared_case_count}"
            ),
        ),
        check(
            "current_shared_parity_cases_locally_covered",
            incremental_local_passed
            and rustwright_full["passed"] + incremental_rustwright_passed >= shared_case_count
            and playwright_full["passed"] + incremental_playwright_passed >= shared_case_count,
            (
                f"full={rustwright_full['passed']}/{playwright_full['passed']}, "
                f"incremental={incremental_rustwright_passed}/{incremental_playwright_passed}/"
                f"{incremental_total}, current shared count {shared_case_count}"
            ),
        ),
        check(
            "api_surface_audits_clean",
            api_surface["sync_api"]["status"] == "passed"
            and api_surface["async_api"]["status"] == "passed"
            and all_zero(api_surface["sync_api"], ["missing_classes", "missing_members", "extra_members", "signature_diffs"])
            and all_zero(
                api_surface["async_api"],
                ["missing_classes", "missing_members", "extra_members", "signature_diffs"],
            ),
            "sync and async API surface audits report 0 missing, 0 extra, and 0 signature diffs",
        ),
        check(
            "skyvern_dependency_audit_clean",
            skyvern_audit["alias_symbol_coverage"]["missing_total"] == 0
            and skyvern_audit["alias_symbol_coverage"]["import_error_total"] == 0
            and skyvern_audit["method_name_coverage"]["missing_total"] == 0
            and skyvern_audit["typed_method_coverage"]["missing_total"] == 0,
            (
                f"{skyvern_audit['scan_scope']['python_files_scanned']} Python files, "
                f"{skyvern_audit['typed_method_coverage']['typed_call_count']} typed calls, "
                f"{skyvern_audit['typed_method_coverage']['receiver_method_count']} receiver-method pairs"
            ),
        ),
        check(
            "skyvern_replacement_smoke_clean",
            skyvern_replacement_smoke.get("status") == "ok"
            and skyvern_replacement_smoke.get("audit_status") == "ok"
            and skyvern_replacement_smoke.get("alias_failures") == 0
            and skyvern_replacement_smoke.get("skyvern_module_import_mode") == "strict"
            and int(skyvern_replacement_smoke.get("skyvern_modules_failed") or 0) == 0
            and int(skyvern_replacement_smoke.get("skyvern_modules_imported") or 0) >= 9,
            (
                f"status={skyvern_replacement_smoke.get('status')}, "
                f"audit={skyvern_replacement_smoke.get('audit_status')}, "
                f"alias_failures={skyvern_replacement_smoke.get('alias_failures')}, "
                f"mode={skyvern_replacement_smoke.get('skyvern_module_import_mode')}, "
                f"skyvern_modules_imported={skyvern_replacement_smoke.get('skyvern_modules_imported')}, "
                f"skyvern_modules_failed={skyvern_replacement_smoke.get('skyvern_modules_failed')}"
            ),
        ),
        check(
            "skyvern_overlay_pytest_alias_clean",
            skyvern_overlay.get("alias_preflight_status") == "ok"
            and skyvern_overlay.get("status") == "passed"
            and skyvern_overlay.get("tests_passed", 0) > 0
            and (skyvern_overlay.get("default_conftest") or {}).get("status") == "passed"
            and (skyvern_overlay.get("noconftest") or {}).get("status") == "passed"
            and (skyvern_overlay.get("default_conftest") or {}).get("classification")
            not in {"alias_dependency", "alias_related"},
            (
                f"status={skyvern_overlay.get('status')}, "
                f"alias_preflight={skyvern_overlay.get('alias_preflight_status')}, "
                f"tests_passed={skyvern_overlay.get('tests_passed')}, "
                f"default_conftest_classification="
                f"{(skyvern_overlay.get('default_conftest') or {}).get('classification')}"
            ),
        ),
        check(
            "skyvern_alias_command_smoke_clean",
            skyvern_alias_command.get("status") == "passed"
            and skyvern_alias_command.get("alias_preflight_status") == "ok"
            and skyvern_alias_command.get("command_status") == "passed"
            and skyvern_alias_command.get("command_classification") == "passed",
            (
                f"status={skyvern_alias_command.get('status')}, "
                f"alias_preflight={skyvern_alias_command.get('alias_preflight_status')}, "
                f"command={skyvern_alias_command.get('command_status')}/"
                f"{skyvern_alias_command.get('command_classification')}"
            ),
        ),
        check(
            "skyvern_prompt_overlay_smoke_accounted",
            skyvern_prompt_overlay.get("alias_preflight_status") == "ok"
            and skyvern_prompt_overlay.get("status") == "passed"
            and int(skyvern_prompt_overlay.get("environment_blocked_modules") or 0) == 0
            and not (prompt_failure_classifications & {"alias_dependency", "alias_related"}),
            (
                f"status={skyvern_prompt_overlay.get('status')}, "
                f"alias_preflight={skyvern_prompt_overlay.get('alias_preflight_status')}, "
                f"modules={skyvern_prompt_overlay.get('module_count')}, "
                f"blocked={skyvern_prompt_overlay.get('environment_blocked_modules')}"
            ),
        ),
    ]
    recorded_docker_verified = (
        phase_1["status"] in DOCKER_VERIFIED_STATUSES
        and ledger["status"] in DOCKER_VERIFIED_STATUSES
        and ledger["docker_completion_status"] in DOCKER_VERIFIED_STATUSES
    )
    current_docker_verified = False
    current_docker_detail = "not requested"
    if current_docker_run:
        current_docker_verified, current_docker_detail = current_docker_gate_verified()
    docker_verified = recorded_docker_verified or current_docker_verified
    checks.append(
        check(
            "docker_phase_1_gate_verified",
            docker_verified,
            (
                f"phase1={phase_1['status']}, ledger={ledger['status']}, "
                f"docker={ledger['docker_completion_status']}, current_run={current_docker_detail}"
            ),
        )
    )

    local_checks = [item for item in checks if item["name"] != "docker_phase_1_gate_verified"]
    local_ready = all(item["passed"] for item in local_checks)
    complete = local_ready and docker_verified
    status = "complete" if complete else "local_ready_docker_pending" if local_ready else "incomplete"
    return {
        "status": status,
        "complete": complete,
        "local_ready": local_ready,
        "docker_verified": docker_verified,
        "recorded_docker_verified": recorded_docker_verified,
        "current_docker_verified": current_docker_verified,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Phase 1 coverage and verification gate status.")
    parser.add_argument("--require-docker", action="store_true", help="Exit nonzero unless the Docker gate is verified.")
    parser.add_argument(
        "--current-docker-run",
        action="store_true",
        help="Treat the current capped container as Docker evidence for this invocation.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    report = build_report(current_docker_run=args.current_docker_run)
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    if args.require_docker:
        return 0 if report["complete"] else 1
    return 0 if report["local_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
