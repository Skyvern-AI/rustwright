#!/usr/bin/env python3
"""Validate a cold-start latency matrix as Testbox evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any

from startup_latency_stats import BOOTSTRAP_PROTOCOL, summarize_paired_ms


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / ".benchmark-data" / "reports"
MEASURED_PHASES = (
    "python_import",
    "manager_factory",
    "api_startup",
    "chromium_facade_first_access",
    "browser_launch",
    "first_page",
    "first_page_probe",
    "close",
)
SUMMARY_PHASES = (*MEASURED_PHASES, "cold_process_to_first_page")
MATCHED_ENVIRONMENT_FIELDS = (
    "image_digest",
    "browser_executable",
    "browser_version",
    "python_version",
    "rust_version",
    "memory_limit",
    "memory_swap_limit",
    "cpu_quota",
    "cpu",
    "transport",
    "launcher_sha256",
    "environment_id",
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MEMORY_LIMITS_GIB = {
    "1g": 1,
    "2g": 2,
    "3g": 3,
    "4g": 4,
    "5g": 5,
    "6g": 6,
    "7g": 7,
    "8g": 8,
    "1024m": 1,
    "2048m": 2,
    "3072m": 3,
    "4096m": 4,
    "5120m": 5,
    "6144m": 6,
    "7168m": 7,
    "8192m": 8,
}


class Validation:
    def __init__(self) -> None:
        self.violations: list[dict[str, str]] = []

    def reject(self, code: str, message: str, location: str) -> None:
        self.violations.append({"code": code, "message": message, "location": location})

    def require(self, condition: bool, code: str, message: str, location: str) -> bool:
        if not condition:
            self.reject(code, message, location)
            return False
        return True


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def get_path(value: Any, *path: str) -> Any:
    current = value
    for name in path:
        if not isinstance(current, dict) or name not in current:
            return None
        current = current[name]
    return current


def contains_p95(value: Any, path: str = "artifact") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if "p95" in str(key).lower():
                matches.append(child_path)
            matches.extend(contains_p95(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(contains_p95(child, f"{path}[{index}]"))
    return matches


def validate_source(args: argparse.Namespace, validation: Validation) -> None:
    validation.require(
        args.source == "testbox",
        "source_not_testbox",
        "--source must explicitly assert testbox",
        "--source",
    )
    for name in ("runner", "run_url"):
        value = getattr(args, name)
        validation.require(
            isinstance(value, str) and bool(value.strip()),
            "missing_attestation",
            f"--{name.replace('_', '-')} must be nonempty",
            f"--{name.replace('_', '-')}",
        )


def validate_provenance(artifact: dict[str, Any], validation: Validation) -> None:
    provenance = artifact.get("provenance")
    if not validation.require(
        isinstance(provenance, dict),
        "missing_provenance",
        "provenance must be an object",
        "artifact.provenance",
    ):
        return

    for name in ("before_sha", "after_sha"):
        value = provenance.get(name)
        validation.require(
            isinstance(value, str) and bool(SHA_PATTERN.fullmatch(value)),
            "missing_provenance",
            f"{name} must be a full Git SHA",
            f"artifact.provenance.{name}",
        )

    for name in ("base_image_digest", "image_digest"):
        value = provenance.get(name)
        validation.require(
            isinstance(value, str)
            and value.startswith("sha256:")
            and bool(DIGEST_PATTERN.fullmatch(value.removeprefix("sha256:"))),
            "missing_provenance",
            f"{name} must be a sha256 image identity",
            f"artifact.provenance.{name}",
        )

    wheels = provenance.get("wheels")
    for revision in ("before", "after"):
        records = wheels.get(revision) if isinstance(wheels, dict) else None
        valid = isinstance(records, list) and len(records) == 1
        if valid:
            record = records[0]
            valid = (
                isinstance(record, dict)
                and isinstance(record.get("filename"), str)
                and bool(record["filename"])
                and isinstance(record.get("sha256"), str)
                and bool(DIGEST_PATTERN.fullmatch(record["sha256"]))
            )
        validation.require(
            valid,
            "missing_provenance",
            f"{revision} wheel filename and sha256 are required",
            f"artifact.provenance.wheels.{revision}",
        )

    required_nonempty = (
        ("measurement_image",),
        ("metadata_container_name",),
        ("browser", "executable"),
        ("browser", "version"),
        ("python_version",),
        ("rust_version",),
        ("exact_command",),
        ("start_time",),
        ("fixture_hash",),
        ("launcher_sha256",),
        ("transport",),
    )
    for path in required_nonempty:
        value = get_path(provenance, *path)
        validation.require(
            isinstance(value, str) and bool(value.strip()),
            "missing_provenance",
            f"{'.'.join(path)} is required",
            f"artifact.provenance.{'.'.join(path)}",
        )
    metadata_container_name = provenance.get("metadata_container_name")
    metadata_command = provenance.get("metadata_command")
    metadata_command_valid = (
        isinstance(metadata_command, list)
        and len(metadata_command) >= 4
        and metadata_command[:2] == ["docker", "run"]
        and all(isinstance(value, str) for value in metadata_command)
    )
    validation.require(
        metadata_command_valid,
        "missing_provenance",
        "metadata_command must record the full docker run argv list",
        "artifact.provenance.metadata_command",
    )
    validation.require(
        metadata_command_valid
        and isinstance(metadata_container_name, str)
        and any(
            value == "--name"
            and metadata_command[position + 1] == metadata_container_name
            for position, value in enumerate(metadata_command[:-1])
        ),
        "invalid_container_isolation",
        "the metadata command must assign its recorded container name",
        "artifact.provenance.metadata_command",
    )

    for name in ("fixture_hash", "launcher_sha256"):
        value = provenance.get(name)
        validation.require(
            isinstance(value, str) and bool(DIGEST_PATTERN.fullmatch(value)),
            "missing_provenance",
            f"{name} must be a sha256 digest",
            f"artifact.provenance.{name}",
        )
    validation.require(
        provenance.get("fixture_hash") == provenance.get("launcher_sha256"),
        "missing_provenance",
        "fixture_hash must identify the recorded launcher",
        "artifact.provenance.fixture_hash",
    )

    cpu = provenance.get("cpu")
    validation.require(
        isinstance(cpu, dict)
        and bool(cpu.get("model"))
        and isinstance(cpu.get("logical_count"), int)
        and cpu["logical_count"] > 0,
        "missing_provenance",
        "CPU model and logical count are required",
        "artifact.provenance.cpu",
    )
    validation.require(
        provenance.get("parallelism") == "sequential" and provenance.get("concurrency") == 1,
        "invalid_execution_model",
        "execution must be sequential with concurrency 1",
        "artifact.provenance",
    )
    validation.require(
        provenance.get("container_isolation") == "one_fresh_container_per_sample",
        "invalid_container_isolation",
        "each sample must use one fresh container",
        "artifact.provenance.container_isolation",
    )

    for name in ("memory_limit", "memory_swap_limit"):
        value = provenance.get(name)
        validation.require(
            isinstance(value, str)
            and value.lower() in MEMORY_LIMITS_GIB
            and MEMORY_LIMITS_GIB[value.lower()] <= 8,
            "invalid_resource_cap",
            f"{name} must be a recognized cap of at most 8 GiB",
            f"artifact.provenance.{name}",
        )
    validation.require(
        provenance.get("memory_limit") == provenance.get("memory_swap_limit"),
        "invalid_resource_cap",
        "memory and swap caps must match",
        "artifact.provenance",
    )

    order = provenance.get("order_sequence")
    validation.require(
        isinstance(order, list) and all(value in ("before", "after") for value in order),
        "missing_provenance",
        "the sample order sequence is required",
        "artifact.provenance.order_sequence",
    )


def expected_order(pair_count: int) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    sequence_index = 0
    for pair_id in range(1, pair_count + 1):
        revisions = ("before", "after") if pair_id % 2 else ("after", "before")
        for order_position, revision in enumerate(revisions, start=1):
            expected.append(
                {
                    "sequence_index": sequence_index,
                    "pair_id": pair_id,
                    "order_position": order_position,
                    "revision": revision,
                }
            )
            sequence_index += 1
    return expected


def validate_balanced_order(
    artifact: dict[str, Any],
    pair_count: int,
    validation: Validation,
) -> None:
    validation.require(
        artifact.get("order_scheme") == "balanced-abba",
        "unbalanced_order",
        "order_scheme must be balanced-abba",
        "artifact.order_scheme",
    )
    validation.require(
        pair_count > 0 and pair_count % 2 == 0,
        "unbalanced_order",
        "an exact balanced-abba matrix requires a positive even pair count",
        "artifact.pair_count_requested",
    )
    order = artifact.get("order_sequence")
    expected = expected_order(pair_count) if pair_count > 0 else []
    validation.require(
        order == expected,
        "unbalanced_order",
        "order_sequence must alternate AB then BA for every two matched pairs",
        "artifact.order_sequence",
    )
    provenance_order = get_path(artifact, "provenance", "order_sequence")
    expected_tokens = [item["revision"] for item in expected]
    validation.require(
        provenance_order == expected_tokens,
        "unbalanced_order",
        "provenance order_sequence must match the detailed order",
        "artifact.provenance.order_sequence",
    )


def validate_environment_record(
    environment: Any,
    validation: Validation,
    location: str,
) -> bool:
    if not validation.require(
        isinstance(environment, dict),
        "mismatched_environment",
        "sample environment must be an object",
        location,
    ):
        return False
    valid = True
    for name in MATCHED_ENVIRONMENT_FIELDS:
        value = environment.get(name)
        present = value is not None and value != "" and value != {}
        valid = validation.require(
            present,
            "mismatched_environment",
            f"matched environment field {name} is required",
            f"{location}.{name}",
        ) and valid
    return valid


def validate_launcher(
    sample: dict[str, Any],
    validation: Validation,
    location: str,
) -> bool:
    launcher = sample.get("launcher")
    if sample.get("status") != "passed":
        if isinstance(launcher, dict):
            validation.require(
                "phases" not in launcher and "derived" not in launcher,
                "failure_used_as_timing",
                "a failed sample must not contain timing phases",
                f"{location}.launcher",
            )
        return False

    if not validation.require(
        isinstance(launcher, dict) and launcher.get("status") == "ok",
        "invalid_sample",
        "a passed sample requires an ok launcher record",
        f"{location}.launcher",
    ):
        return False
    valid = validation.require(
        launcher.get("schema_version") == 1
        and launcher.get("entrypoint") == "python-sync"
        and isinstance(launcher.get("pid"), int)
        and not isinstance(launcher.get("pid"), bool)
        and launcher["pid"] > 0
        and launcher.get("clock") == "perf_counter_ns",
        "invalid_sample",
        "launcher schema, entrypoint, pid, and monotonic clock are required",
        f"{location}.launcher",
    )
    phases = launcher.get("phases")
    if not validation.require(
        isinstance(phases, list),
        "invalid_phases",
        "phases must be a list",
        f"{location}.launcher.phases",
    ):
        return False
    names = [phase.get("name") if isinstance(phase, dict) else None for phase in phases]
    valid = validation.require(
        names == list(MEASURED_PHASES),
        "non_contiguous_phases",
        "phase names and order must match the cold-start contract",
        f"{location}.launcher.phases",
    ) and valid
    previous_end: int | None = None
    first_start: int | None = None
    first_page_end: int | None = None
    for index, phase in enumerate(phases):
        phase_location = f"{location}.launcher.phases[{index}]"
        if not isinstance(phase, dict):
            validation.reject("invalid_phases", "phase must be an object", phase_location)
            valid = False
            continue
        start = phase.get("start_offset_ns")
        end = phase.get("end_offset_ns")
        duration = phase.get("duration_ns")
        numbers_valid = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (start, end, duration)
        )
        valid = validation.require(
            numbers_valid,
            "invalid_phases",
            "phase offsets and duration must be finite nonnegative integers",
            phase_location,
        ) and valid
        valid = validation.require(
            phase.get("status") == "ok",
            "invalid_phases",
            "every timing phase must have status ok",
            f"{phase_location}.status",
        ) and valid
        if not numbers_valid:
            continue
        if first_start is None:
            first_start = start
            valid = validation.require(
                start == 0,
                "non_contiguous_phases",
                "python_import must start at offset zero",
                f"{phase_location}.start_offset_ns",
            ) and valid
        if previous_end is not None:
            valid = validation.require(
                start == previous_end,
                "non_contiguous_phases",
                "each phase must start at the preceding phase endpoint",
                f"{phase_location}.start_offset_ns",
            ) and valid
        valid = validation.require(
            end >= start and duration == end - start,
            "invalid_phases",
            "phase duration must equal end minus start",
            phase_location,
        ) and valid
        previous_end = end
        if phase.get("name") == "first_page":
            first_page_end = end

    derived = get_path(launcher, "derived", "cold_process_to_first_page")
    if not validation.require(
        isinstance(derived, dict),
        "derived_total_mismatch",
        "cold_process_to_first_page is required",
        f"{location}.launcher.derived.cold_process_to_first_page",
    ):
        return False
    precision = launcher.get("clock_precision_ns", 1)
    if not isinstance(precision, int) or isinstance(precision, bool) or precision < 0:
        precision = 0
        validation.reject(
            "derived_total_mismatch",
            "clock_precision_ns must be a nonnegative integer",
            f"{location}.launcher.clock_precision_ns",
        )
        valid = False
    derived_start = derived.get("start_offset_ns")
    derived_end = derived.get("end_offset_ns")
    derived_duration = derived.get("duration_ns")
    derived_numbers = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (derived_start, derived_end, derived_duration)
    )
    valid = validation.require(
        derived_numbers,
        "derived_total_mismatch",
        "derived offsets and duration must be finite nonnegative integers",
        f"{location}.launcher.derived.cold_process_to_first_page",
    ) and valid
    if derived_numbers and first_start is not None and first_page_end is not None:
        valid = validation.require(
            abs(derived_start - first_start) <= precision
            and abs(derived_end - first_page_end) <= precision
            and abs(derived_duration - (first_page_end - first_start)) <= precision,
            "derived_total_mismatch",
            "derived total must equal the python_import-to-first_page endpoints",
            f"{location}.launcher.derived.cold_process_to_first_page",
        ) and valid
    valid = validation.require(
        derived.get("status") == "ok",
        "derived_total_mismatch",
        "derived total must have status ok",
        f"{location}.launcher.derived.cold_process_to_first_page.status",
    ) and valid
    valid = validation.require(
        launcher.get("probe")
        == {"url": "about:blank", "viewport_size": {"width": 1280, "height": 720}},
        "failed_behavior_probe",
        "the blank-page URL and viewport assertion must pass",
        f"{location}.launcher.probe",
    ) and valid
    return valid


def validate_samples(
    artifact: dict[str, Any],
    pair_count: int,
    validation: Validation,
) -> tuple[list[tuple[int, dict[str, Any], dict[str, Any]]], dict[str, int]]:
    samples = artifact.get("samples")
    if not validation.require(
        isinstance(samples, list),
        "missing_samples",
        "samples must be a list",
        "artifact.samples",
    ):
        return [], {"before": 0, "after": 0}
    validation.require(
        len(samples) == pair_count * 2,
        "missing_samples",
        "the raw artifact must retain one before and one after sample for every pair",
        "artifact.samples",
    )

    expected = expected_order(pair_count) if pair_count > 0 else []
    indexed: dict[tuple[int, str], tuple[dict[str, Any], bool]] = {}
    passed_counts = {"before": 0, "after": 0}
    top_environment = artifact.get("environment")
    metadata_container_name = get_path(
        artifact,
        "provenance",
        "metadata_container_name",
    )
    container_names: set[str] = (
        {metadata_container_name}
        if isinstance(metadata_container_name, str) and bool(metadata_container_name.strip())
        else set()
    )
    per_sample_isolation = (
        get_path(artifact, "provenance", "container_isolation")
        == "one_fresh_container_per_sample"
    )
    validate_environment_record(top_environment, validation, "artifact.environment")
    if isinstance(top_environment, dict):
        provenance_environment = {
            "image_digest": get_path(artifact, "provenance", "image_digest"),
            "browser_executable": get_path(artifact, "provenance", "browser", "executable"),
            "browser_version": get_path(artifact, "provenance", "browser", "version"),
            "python_version": get_path(artifact, "provenance", "python_version"),
            "rust_version": get_path(artifact, "provenance", "rust_version"),
            "memory_limit": get_path(artifact, "provenance", "memory_limit"),
            "memory_swap_limit": get_path(artifact, "provenance", "memory_swap_limit"),
            "cpu_quota": get_path(artifact, "provenance", "cpu_quota"),
            "cpu": get_path(artifact, "provenance", "cpu"),
            "transport": get_path(artifact, "provenance", "transport"),
            "launcher_sha256": get_path(artifact, "provenance", "launcher_sha256"),
        }
        for name, value in provenance_environment.items():
            validation.require(
                top_environment.get(name) == value,
                "mismatched_environment",
                f"declared environment {name} must match provenance",
                f"artifact.environment.{name}",
            )

    for index, sample in enumerate(samples):
        location = f"artifact.samples[{index}]"
        if not isinstance(sample, dict):
            validation.reject("invalid_sample", "sample must be an object", location)
            continue
        revision = sample.get("revision")
        pair_id = sample.get("pair_id")
        validation.require(
            revision in ("before", "after"),
            "invalid_sample",
            "sample revision must be before or after",
            f"{location}.revision",
        )
        validation.require(
            isinstance(pair_id, int) and not isinstance(pair_id, bool) and 1 <= pair_id <= pair_count,
            "invalid_sample",
            "sample pair_id is outside the declared matrix",
            f"{location}.pair_id",
        )
        validation.require(
            isinstance(sample.get("started_at"), str)
            and bool(sample["started_at"])
            and isinstance(sample.get("command"), list)
            and bool(sample["command"])
            and isinstance(sample.get("outer_process_duration_ns"), int)
            and not isinstance(sample.get("outer_process_duration_ns"), bool)
            and sample["outer_process_duration_ns"] >= 0,
            "missing_provenance",
            "each raw sample requires its command, start time, and outer duration",
            location,
        )
        container_name = sample.get("container_name")
        container_name_valid = isinstance(container_name, str) and bool(container_name.strip())
        validation.require(
            container_name_valid,
            "missing_provenance",
            "each raw sample requires a recorded container name",
            f"{location}.container_name",
        )
        if container_name_valid and per_sample_isolation:
            validation.require(
                container_name not in container_names,
                "invalid_container_isolation",
                "each fresh sample container must have a unique name",
                f"{location}.container_name",
            )
            container_names.add(container_name)
            command = sample.get("command")
            named_command = (
                isinstance(command, list)
                and any(
                    value == "--name"
                    and command[position + 1] == container_name
                    for position, value in enumerate(command[:-1])
                )
            )
            validation.require(
                named_command,
                "invalid_container_isolation",
                "the sample command must assign its recorded container name",
                f"{location}.command",
            )
        if index < len(expected):
            for name in ("sequence_index", "pair_id", "order_position", "revision"):
                validation.require(
                    sample.get(name) == expected[index][name],
                    "unbalanced_order",
                    f"sample {name} does not match the balanced order",
                    f"{location}.{name}",
                )
        validation.require(
            sample.get("status") in ("passed", "failed"),
            "invalid_sample",
            "sample status must be passed or failed",
            f"{location}.status",
        )
        if sample.get("status") == "passed":
            validation.require(
                sample.get("returncode") == 0,
                "invalid_sample",
                "a passed sample requires return code zero",
                f"{location}.returncode",
            )
        else:
            validation.require(
                sample.get("returncode") is None
                or (
                    isinstance(sample.get("returncode"), int)
                    and not isinstance(sample.get("returncode"), bool)
                    and sample["returncode"] != 0
                ),
                "invalid_sample",
                "a failed sample requires a nonzero return code or a timeout",
                f"{location}.returncode",
            )
        environment = sample.get("environment")
        environment_valid = validate_environment_record(environment, validation, f"{location}.environment")
        if environment_valid:
            validation.require(
                environment == top_environment,
                "mismatched_environment",
                "every sample must use the declared matched environment",
                f"{location}.environment",
            )
        timing_valid = validate_launcher(sample, validation, location)
        if revision in passed_counts and sample.get("status") == "passed":
            passed_counts[revision] += 1
        if isinstance(pair_id, int) and revision in ("before", "after"):
            key = (pair_id, revision)
            if key in indexed:
                validation.reject(
                    "missing_samples",
                    "duplicate sample for pair and revision",
                    location,
                )
            else:
                indexed[key] = (sample, timing_valid and environment_valid)

    complete_pair_samples: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for pair_id in range(1, pair_count + 1):
        before = indexed.get((pair_id, "before"))
        after = indexed.get((pair_id, "after"))
        if before is None or after is None:
            validation.reject(
                "missing_samples",
                "matched pair is incomplete",
                f"artifact.samples[pair_id={pair_id}]",
            )
            continue
        validation.require(
            before[0].get("environment") == after[0].get("environment"),
            "mismatched_environment",
            "before and after samples in a pair must have equal environments",
            f"artifact.samples[pair_id={pair_id}]",
        )
        if (
            before[0].get("status") == "passed"
            and after[0].get("status") == "passed"
            and before[1]
            and after[1]
        ):
            complete_pair_samples.append((pair_id, before[0], after[0]))
    return complete_pair_samples, passed_counts


def validate_reliability(
    artifact: dict[str, Any],
    pair_count: int,
    complete_pairs: int,
    passed_counts: dict[str, int],
    validation: Validation,
) -> None:
    reliability = get_path(artifact, "summary", "reliability")
    if not validation.require(
        isinstance(reliability, dict),
        "missing_success_rate",
        "summary reliability and success rates are required",
        "artifact.summary.reliability",
    ):
        return
    for revision in ("before", "after"):
        record = reliability.get(revision)
        expected_passed = passed_counts[revision]
        expected = {
            "attempted": pair_count,
            "succeeded": expected_passed,
            "failed": pair_count - expected_passed,
            "success_rate": expected_passed / pair_count if pair_count else 0.0,
        }
        validation.require(
            isinstance(record, dict)
            and record.get("attempted") == expected["attempted"]
            and record.get("succeeded") == expected["succeeded"]
            and record.get("failed") == expected["failed"]
            and is_number(record.get("success_rate"))
            and abs(record["success_rate"] - expected["success_rate"]) <= 1e-12,
            "missing_success_rate",
            f"{revision} success counts and rate must match retained samples",
            f"artifact.summary.reliability.{revision}",
        )
    matched = reliability.get("matched_pairs")
    expected_matched_rate = complete_pairs / pair_count if pair_count else 0.0
    validation.require(
        isinstance(matched, dict)
        and matched.get("attempted") == pair_count
        and matched.get("complete") == complete_pairs
        and matched.get("failed") == pair_count - complete_pairs
        and is_number(matched.get("success_rate"))
        and abs(matched["success_rate"] - expected_matched_rate) <= 1e-12,
        "missing_success_rate",
        "matched-pair success counts and rate must match retained samples",
        "artifact.summary.reliability.matched_pairs",
    )


STATISTIC_REL_TOLERANCE = 1e-9
STATISTIC_ABS_TOLERANCE = 1e-12


def phase_duration_ns(sample: dict[str, Any], phase: str) -> int | None:
    launcher = sample.get("launcher")
    if not isinstance(launcher, dict):
        return None
    if phase == "cold_process_to_first_page":
        record = get_path(launcher, "derived", phase)
    else:
        phases = launcher.get("phases")
        if not isinstance(phases, list):
            return None
        record = next(
            (
                value
                for value in phases
                if isinstance(value, dict) and value.get("name") == phase
            ),
            None,
        )
    if not isinstance(record, dict):
        return None
    duration = record.get("duration_ns")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        return None
    return duration


def statistic_matches(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int):
        return isinstance(actual, int) and not isinstance(actual, bool) and actual == expected
    if isinstance(expected, float):
        return is_number(actual) and math.isclose(
            float(actual),
            expected,
            rel_tol=STATISTIC_REL_TOLERANCE,
            abs_tol=STATISTIC_ABS_TOLERANCE,
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                statistic_matches(actual_value, expected_value)
                for actual_value, expected_value in zip(actual, expected)
            )
        )
    return actual == expected


def validate_summary(
    artifact: dict[str, Any],
    complete_pairs: list[tuple[int, dict[str, Any], dict[str, Any]]],
    validation: Validation,
) -> None:
    phases = get_path(artifact, "summary", "phases")
    if not validation.require(
        isinstance(phases, dict),
        "missing_statistics",
        "summary phases must be an object",
        "artifact.summary.phases",
    ):
        return
    validation.require(
        set(phases) == set(SUMMARY_PHASES),
        "missing_statistics",
        "summary phases must contain exactly the declared phases",
        "artifact.summary.phases",
    )
    for phase in SUMMARY_PHASES:
        location = f"artifact.summary.phases.{phase}"
        record = phases.get(phase)
        if not validation.require(
            isinstance(record, dict),
            "missing_statistics",
            f"summary for {phase} is required",
            location,
        ):
            continue
        validation.require(
            set(record) == {"before", "after", "paired"},
            "missing_statistics",
            f"summary for {phase} must contain exactly before, after, and paired",
            location,
        )

        pairs_ms: list[tuple[float, float]] = []
        for pair_id, before_sample, after_sample in complete_pairs:
            before_ns = phase_duration_ns(before_sample, phase)
            after_ns = phase_duration_ns(after_sample, phase)
            if before_ns is None or after_ns is None:
                validation.reject(
                    "statistics_mismatch",
                    "validated raw nanosecond records could not be recomputed",
                    f"artifact.samples[pair_id={pair_id}]",
                )
                continue
            pairs_ms.append(
                (float(before_ns) / 1_000_000.0, float(after_ns) / 1_000_000.0)
            )

        actual_paired = record.get("paired")
        bootstrap_protocol = (
            actual_paired.get("bootstrap_protocol")
            if isinstance(actual_paired, dict)
            else None
        )
        protocol_valid = validation.require(
            bootstrap_protocol == BOOTSTRAP_PROTOCOL,
            "unknown_bootstrap_protocol",
            f"bootstrap_protocol must be {BOOTSTRAP_PROTOCOL}",
            f"{location}.paired.bootstrap_protocol",
        )
        expected = summarize_paired_ms(
            pairs_ms,
            phase,
            bootstrap_protocol=(
                bootstrap_protocol if protocol_valid else BOOTSTRAP_PROTOCOL
            ),
        )
        for section in ("before", "after", "paired"):
            actual_section = record.get(section)
            expected_section = expected[section]
            section_location = f"{location}.{section}"
            if not validation.require(
                isinstance(actual_section, dict),
                "missing_statistics",
                f"{section} statistics must be an object",
                section_location,
            ):
                continue
            validation.require(
                set(actual_section) == set(expected_section),
                "missing_statistics",
                f"{section} must contain exactly the declared statistics",
                section_location,
            )
            for statistic, expected_value in expected_section.items():
                validation.require(
                    statistic_matches(actual_section.get(statistic), expected_value),
                    "statistics_mismatch",
                    f"{statistic} must match recomputation from raw nanosecond records",
                    f"{section_location}.{statistic}",
                )


def nonempty_argument(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must be nonempty")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail closed unless a startup-latency artifact is valid Testbox evidence."
    )
    parser.add_argument("artifact", help="Raw matrix JSON under .benchmark-data/results/.")
    parser.add_argument(
        "--source",
        required=True,
        choices=["testbox", "local"],
        help="Operator evidence-source attestation. Only attest testbox for a real Testbox run.",
    )
    parser.add_argument(
        "--runner",
        required=True,
        type=nonempty_argument,
        help="Nonempty runner label recorded in the validation report.",
    )
    parser.add_argument(
        "--run-url",
        required=True,
        type=nonempty_argument,
        help="Nonempty Testbox run reference recorded in the validation report.",
    )
    parser.add_argument(
        "--min-pairs",
        type=int,
        default=20,
        help="Required complete pairs; minimum 20.",
    )
    parser.add_argument(
        "--require-balanced-order",
        action="store_true",
        help="Compatibility flag. Balanced order is always required.",
    )
    parser.add_argument(
        "--require-matched-environment",
        action="store_true",
        help="Compatibility flag. Matched environments are always required.",
    )
    parser.add_argument("--output", help="Validation report path under .benchmark-data/reports/.")
    parser.add_argument("--json", action="store_true", help="Print the validation report as JSON.")
    return parser.parse_args()


def report_path_for(artifact_path: Path, explicit: str | None) -> Path:
    path = Path(explicit) if explicit else REPORTS_DIR / f"{artifact_path.stem}-validation.json"
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(REPORTS_DIR.resolve()):
        raise ValueError(f"--output must be under {REPORTS_DIR.resolve()}")
    return path


def main() -> int:
    args = parse_args()
    artifact_path = Path(args.artifact)
    if not artifact_path.is_absolute():
        artifact_path = ROOT / artifact_path
    artifact_path = artifact_path.resolve()
    validation = Validation()
    try:
        value = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        value = {}
        validation.reject(
            "invalid_artifact",
            f"could not read artifact: {type(exc).__name__}: {exc}",
            str(artifact_path),
        )
    artifact = value if isinstance(value, dict) else {}
    if not isinstance(value, dict):
        validation.reject("invalid_artifact", "artifact root must be an object", "artifact")

    validate_source(args, validation)
    validation.require(
        artifact.get("schema_version") == 1
        and artifact.get("kind") == "rustwright_startup_latency_matrix",
        "invalid_artifact",
        "artifact schema and kind do not match the startup matrix",
        "artifact",
    )
    pair_count_value = artifact.get("pair_count_requested")
    pair_count = pair_count_value if isinstance(pair_count_value, int) and not isinstance(pair_count_value, bool) else 0
    validation.require(
        pair_count > 0,
        "short_pair_count",
        "pair_count_requested must be positive",
        "artifact.pair_count_requested",
    )
    validation.require(
        args.min_pairs >= 20,
        "short_pair_count",
        "--min-pairs cannot lower the fail-closed floor below 20",
        "--min-pairs",
    )
    validate_provenance(artifact, validation)
    validate_balanced_order(artifact, pair_count, validation)
    complete_pair_samples, passed_counts = validate_samples(artifact, pair_count, validation)
    complete_pairs = len(complete_pair_samples)
    required_pairs = max(20, args.min_pairs)
    validation.require(
        complete_pairs >= required_pairs,
        "short_pair_count",
        f"at least {required_pairs} complete matched pairs are required; found {complete_pairs}",
        "artifact.samples",
    )
    validate_reliability(
        artifact,
        pair_count,
        complete_pairs,
        passed_counts,
        validation,
    )
    validate_summary(artifact, complete_pair_samples, validation)
    for location in contains_p95(artifact):
        validation.reject(
            "p95_forbidden",
            "p95 is forbidden for this 20-30 pair cold-start artifact",
            location,
        )

    report = {
        "schema_version": 1,
        "kind": "rustwright_startup_latency_validation",
        "created_at": utc_now(),
        "status": "passed" if not validation.violations else "failed",
        "artifact": str(artifact_path),
        "attestation": {
            "source": args.source,
            "runner": args.runner,
            "run_url": args.run_url,
        },
        "required_complete_pairs": required_pairs,
        "observed_complete_pairs": complete_pairs,
        "violations": validation.violations,
    }
    try:
        output_path = report_path_for(artifact_path, args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["report_path"] = str(output_path.relative_to(ROOT))
    except (OSError, ValueError) as exc:
        validation.reject(
            "invalid_report_path",
            f"could not write validation report: {type(exc).__name__}: {exc}",
            "--output",
        )
        report["status"] = "failed"
        report["violations"] = validation.violations

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["status"] == "passed":
        print(f"PASS: {complete_pairs} complete matched pairs")
    else:
        print(f"FAIL: {len(validation.violations)} violation(s)")
        for violation in validation.violations:
            print(f"- {violation['code']}: {violation['message']} ({violation['location']})")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
