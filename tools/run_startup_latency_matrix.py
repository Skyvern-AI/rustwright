#!/usr/bin/env python3
"""Build and run a paired Rustwright cold-start latency matrix."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from startup_latency_stats import summarize_paired_ms


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "benchmarks" / "startup_latency.py"
RESULTS_DIR = ROOT / ".benchmark-data" / "results"
REPORTS_DIR = ROOT / ".benchmark-data" / "reports"
REVISION_FETCH_TIMEOUT = 300
REVISION_UNSHALLOW_TIMEOUT = 1_200
REVISION_DEEPEN_STEPS = (256, 1_024, 4_096, 16_384)
PHASES = (
    "python_import",
    "manager_factory",
    "api_startup",
    "chromium_facade_first_access",
    "browser_launch",
    "first_page",
    "first_page_probe",
    "close",
    "cold_process_to_first_page",
)
MEMORY_LIMITS = {
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


class MatrixError(RuntimeError):
    pass


class MatrixArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise MatrixError(f"argument error: {message}")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    command: Sequence[str],
    *,
    timeout: int,
    cwd: Path = ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MatrixError(f"command timed out: {shlex.join(command)}") from exc
    if check and proc.returncode != 0:
        combined = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
        tail = "\n".join(combined[-40:])
        raise MatrixError(
            f"command failed with exit {proc.returncode}: {shlex.join(command)}\n{tail}"
        )
    return proc


def revision_exists(revision: str) -> bool:
    proc = run_command(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        timeout=30,
        check=False,
    )
    return proc.returncode == 0


def attempt_revision_fetch(
    command: Sequence[str],
    *,
    timeout: int,
    failures: list[str],
) -> None:
    try:
        proc = run_command(command, timeout=timeout, check=False)
    except MatrixError as exc:
        failures.append(str(exc))
        return
    if proc.returncode == 0:
        failures.append(f"{shlex.join(command)} completed, but the revision is still absent")
        return
    combined = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
    tail = "\n".join(combined[-10:])
    failures.append(f"{shlex.join(command)} exited {proc.returncode}\n{tail}".rstrip())


def materialize_revision(revision: str) -> None:
    if revision_exists(revision):
        return

    failures: list[str] = []
    attempt_revision_fetch(
        ["git", "fetch", "origin", revision],
        timeout=REVISION_FETCH_TIMEOUT,
        failures=failures,
    )
    if revision_exists(revision):
        return

    attempt_revision_fetch(
        ["git", "fetch", "--unshallow", "origin"],
        timeout=REVISION_UNSHALLOW_TIMEOUT,
        failures=failures,
    )
    if revision_exists(revision):
        return

    for depth in REVISION_DEEPEN_STEPS:
        attempt_revision_fetch(
            ["git", "fetch", f"--deepen={depth}", "origin"],
            timeout=REVISION_FETCH_TIMEOUT,
            failures=failures,
        )
        if revision_exists(revision):
            return

    detail = "\n\n".join(failures)
    raise MatrixError(
        f"could not resolve Git revision {revision!r} after local lookup, direct fetch, "
        f"unshallow, and bounded deepening attempts\n{detail}"
    )


def canonical_revision(revision: str) -> str:
    proc = run_command(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        timeout=30,
    )
    value = proc.stdout.strip().lower()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise MatrixError(f"revision did not resolve to a full SHA: {revision!r}")
    return value


def archive_revision(revision: str, destination: Path) -> None:
    archive_path = destination.with_suffix(".tar")
    run_command(
        ["git", "archive", "--format=tar", f"--output={archive_path}", revision],
        timeout=120,
    )
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r") as archive:
        archive.extractall(destination, filter="data")
    archive_path.unlink()


def docker_image_id(image: str, timeout: int) -> str:
    proc = run_command(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        timeout=timeout,
    )
    value = proc.stdout.strip()
    if not value.startswith("sha256:"):
        raise MatrixError(f"Docker returned an invalid image ID for {image!r}: {value!r}")
    return value


def build_dual_venv_image(
    *,
    base_image: str,
    base_image_id: str,
    before_sha: str,
    after_sha: str,
    build_timeout: int,
    memory_limit: str,
) -> tuple[str, str]:
    launcher_sha = sha256_file(LAUNCHER)
    identity = hashlib.sha256(
        f"{base_image_id}\0{before_sha}\0{after_sha}\0{launcher_sha}".encode()
    ).hexdigest()[:20]
    derived_image = f"rustwright-startup-latency:{identity}"
    setup_name = f"rustwright-startup-build-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    build_root = ROOT / ".benchmark-data" / "tmp"
    build_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="startup-build-", dir=build_root) as temporary:
        temporary_path = Path(temporary)
        before_source = temporary_path / "before"
        after_source = temporary_path / "after"
        archive_revision(before_sha, before_source)
        archive_revision(after_sha, after_source)

        build_script = """
set -eu
mkdir -p /opt/startup-wheels/before /opt/startup-wheels/after /opt/startup-harness
mv /tmp/startup_latency.py /opt/startup-harness/startup_latency.py
CARGO_TARGET_DIR=/tmp/startup-target-before python -m pip wheel --no-cache-dir --no-build-isolation --no-deps --wheel-dir /opt/startup-wheels/before /inputs/before
CARGO_TARGET_DIR=/tmp/startup-target-after python -m pip wheel --no-cache-dir --no-build-isolation --no-deps --wheel-dir /opt/startup-wheels/after /inputs/after
python -m venv /opt/startup-before
python -m venv /opt/startup-after
/opt/startup-before/bin/python -m pip install --no-index --no-deps /opt/startup-wheels/before/*.whl
/opt/startup-after/bin/python -m pip install --no-index --no-deps /opt/startup-wheels/after/*.whl
rm -rf /tmp/startup-target-before /tmp/startup-target-after
""".strip()
        create_command = [
            "docker",
            "create",
            "--name",
            setup_name,
            f"--memory={memory_limit}",
            f"--memory-swap={memory_limit}",
            "--volume",
            f"{before_source.resolve()}:/inputs/before:ro",
            "--volume",
            f"{after_source.resolve()}:/inputs/after:ro",
            "--entrypoint",
            "/bin/sh",
            base_image,
            "-c",
            build_script,
        ]
        try:
            run_command(create_command, timeout=120)
            run_command(
                ["docker", "cp", str(LAUNCHER), f"{setup_name}:/tmp/startup_latency.py"],
                timeout=120,
            )
            run_command(["docker", "start", "--attach", setup_name], timeout=build_timeout)
            run_command(["docker", "commit", setup_name, derived_image], timeout=300)
        finally:
            best_effort_remove_container(setup_name)

    return derived_image, docker_image_id(derived_image, 120)


def parse_json_output(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def query_image_environment(
    image: str,
    memory_limit: str,
    timeout: int,
) -> dict[str, Any]:
    query = r'''
import glob
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys


def output(command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=30).strip()
    except Exception:
        return None


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def package_version(python):
    return output([python, "-c", "import importlib.metadata; print(importlib.metadata.version('rustwright'))"])

cpu_model = None
try:
    with open("/proc/cpuinfo", encoding="utf-8") as handle:
        for line in handle:
            if line.lower().startswith(("model name", "hardware")) and ":" in line:
                cpu_model = line.split(":", 1)[1].strip()
                break
except OSError:
    pass
cpu_model = cpu_model or platform.processor() or platform.machine()
browser = os.environ.get("RUSTWRIGHT_CHROMIUM") or os.environ.get("CHROME") or os.environ.get("CHROMIUM")
wheels = {}
for revision in ("before", "after"):
    matches = glob.glob(f"/opt/startup-wheels/{revision}/*.whl")
    wheels[revision] = [
        {"filename": os.path.basename(path), "sha256": digest(path)}
        for path in sorted(matches)
    ]
print(json.dumps({
    "browser_executable": browser,
    "browser_version": output([browser, "--version"]) if browser else None,
    "python_version": platform.python_version(),
    "python_build": platform.python_build(),
    "rust_version": output(["rustc", "--version"]),
    "platform": platform.platform(),
    "machine": platform.machine(),
    "cpu": {"model": cpu_model, "logical_count": os.cpu_count()},
    "library_versions": {
        "before": package_version("/opt/startup-before/bin/python"),
        "after": package_version("/opt/startup-after/bin/python"),
    },
    "wheels": wheels,
}, sort_keys=True))
'''.strip()
    metadata_container_name = (
        f"rustwright-startup-metadata-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    command = [
        "docker",
        "run",
        "--name",
        metadata_container_name,
        f"--memory={memory_limit}",
        f"--memory-swap={memory_limit}",
        "--entrypoint",
        "/usr/local/bin/python",
        image,
        "-c",
        query,
    ]
    try:
        proc = run_command(command, timeout=timeout)
    finally:
        best_effort_remove_container(metadata_container_name)
    value = parse_json_output(proc.stdout)
    if value is None:
        raise MatrixError("could not parse environment metadata from the measurement image")
    required = ("browser_executable", "browser_version", "python_version", "rust_version", "cpu")
    missing = [name for name in required if not value.get(name)]
    if missing:
        raise MatrixError(f"measurement image metadata is missing: {', '.join(missing)}")
    for revision in ("before", "after"):
        wheels = value.get("wheels", {}).get(revision)
        if not isinstance(wheels, list) or len(wheels) != 1:
            raise MatrixError(f"expected one {revision} wheel in the measurement image")
    value["metadata_container_name"] = metadata_container_name
    value["metadata_command"] = command
    return value


def balanced_abba_plan(pair_count: int) -> list[dict[str, Any]]:
    if pair_count < 1:
        raise MatrixError("--pairs must be positive")
    if pair_count % 2:
        raise MatrixError("--pairs must be even for exact balanced-abba order")
    plan: list[dict[str, Any]] = []
    sequence_index = 0
    for pair_id in range(1, pair_count + 1):
        order = ("before", "after") if pair_id % 2 else ("after", "before")
        for order_position, revision in enumerate(order, start=1):
            plan.append(
                {
                    "sequence_index": sequence_index,
                    "pair_id": pair_id,
                    "order_position": order_position,
                    "revision": revision,
                }
            )
            sequence_index += 1
    return plan


def sample_command(
    *,
    image: str,
    memory_limit: str,
    revision: str,
    core_path: str,
    browser_version: str,
    transport: str,
    container_name: str,
    use_existing_container: bool,
) -> list[str]:
    python = f"/opt/startup-{revision}/bin/python"
    launcher = "/opt/startup-harness/startup_latency.py"
    environment_args = [
        "--env",
        f"RUSTWRIGHT_STARTUP_TIMING_FILE={core_path}",
        "--env",
        f"RUSTWRIGHT_BROWSER_VERSION={browser_version}",
        "--env",
        f"RUSTWRIGHT_CDP_TRANSPORT={transport}",
    ]
    if use_existing_container:
        return [
            "docker",
            "exec",
            *environment_args,
            container_name,
            python,
            launcher,
        ]
    return [
        "docker",
        "run",
        "--name",
        container_name,
        f"--memory={memory_limit}",
        f"--memory-swap={memory_limit}",
        *environment_args,
        "--entrypoint",
        python,
        image,
        launcher,
    ]


def run_sample(
    *,
    item: dict[str, Any],
    image: str,
    memory_limit: str,
    browser_version: str,
    transport: str,
    environment: dict[str, Any],
    timeout: int,
    block_container_name: str | None,
) -> dict[str, Any]:
    core_path = f"/tmp/rustwright-startup-{item['pair_id']}-{item['sequence_index']}.jsonl"
    use_existing_container = block_container_name is not None
    container_name = block_container_name or (
        f"rustwright-startup-sample-{item['sequence_index']}-{os.getpid()}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    command = sample_command(
        image=image,
        memory_limit=memory_limit,
        revision=item["revision"],
        core_path=core_path,
        browser_version=browser_version,
        transport=transport,
        container_name=container_name,
        use_existing_container=use_existing_container,
    )
    started_at = utc_now()
    outer_start_ns = time.perf_counter_ns()
    try:
        try:
            proc = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            returncode: int | None = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
            timeout_error = False
        except subprocess.TimeoutExpired as exc:
            returncode = None
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            timeout_error = True
        outer_duration_ns = time.perf_counter_ns() - outer_start_ns
    finally:
        if not use_existing_container:
            best_effort_remove_container(container_name)
    launcher = parse_json_output(stdout)
    passed = (
        not timeout_error
        and returncode == 0
        and isinstance(launcher, dict)
        and launcher.get("status") == "ok"
    )
    sample: dict[str, Any] = {
        **item,
        "status": "passed" if passed else "failed",
        "started_at": started_at,
        "outer_process_duration_ns": outer_duration_ns,
        "returncode": returncode,
        "command": command,
        "container_name": container_name,
        "environment": environment,
        "launcher": launcher,
    }
    if not passed:
        sample["failure"] = {
            "timed_out": timeout_error,
            "stderr_tail": "\n".join(stderr.splitlines()[-40:]),
            "stdout_tail": "\n".join(stdout.splitlines()[-40:]),
        }
    return sample


def start_revision_block_containers(
    image: str,
    memory_limit: str,
) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for revision in ("before", "after"):
            name = f"rustwright-startup-{revision}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            command = [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                f"--memory={memory_limit}",
                f"--memory-swap={memory_limit}",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                "while true; do sleep 3600; done",
            ]
            names[revision] = name
            run_command(command, timeout=120)
    except BaseException:
        stop_revision_block_containers(names)
        raise
    return names


def best_effort_remove_container(name: str) -> None:
    try:
        run_command(["docker", "rm", "-f", name], timeout=120, check=False)
    except Exception:
        pass


def stop_revision_block_containers(names: dict[str, str]) -> None:
    for name in names.values():
        best_effort_remove_container(name)


def phase_duration_ms(sample: dict[str, Any], phase: str) -> float | None:
    if sample.get("status") != "passed":
        return None
    launcher = sample.get("launcher")
    if not isinstance(launcher, dict):
        return None
    if phase == "cold_process_to_first_page":
        record = launcher.get("derived", {}).get(phase)
    else:
        records = launcher.get("phases")
        if not isinstance(records, list):
            return None
        record = next(
            (value for value in records if isinstance(value, dict) and value.get("name") == phase),
            None,
        )
    if not isinstance(record, dict):
        return None
    duration_ns = record.get("duration_ns")
    if not isinstance(duration_ns, (int, float)) or isinstance(duration_ns, bool):
        return None
    if not math.isfinite(float(duration_ns)) or duration_ns < 0:
        return None
    return float(duration_ns) / 1_000_000.0


def summarize(samples: list[dict[str, Any]], pair_count: int) -> dict[str, Any]:
    by_pair: dict[int, dict[str, dict[str, Any]]] = {}
    for sample in samples:
        by_pair.setdefault(sample["pair_id"], {})[sample["revision"]] = sample

    phase_summary: dict[str, Any] = {}
    complete_pair_ids = [
        pair_id
        for pair_id, pair in sorted(by_pair.items())
        if pair.get("before", {}).get("status") == "passed"
        and pair.get("after", {}).get("status") == "passed"
    ]
    for phase in PHASES:
        pairs_ms: list[tuple[float, float]] = []
        for pair_id in complete_pair_ids:
            pair = by_pair[pair_id]
            before = phase_duration_ms(pair["before"], phase)
            after = phase_duration_ms(pair["after"], phase)
            if before is not None and after is not None:
                pairs_ms.append((before, after))
        phase_summary[phase] = summarize_paired_ms(pairs_ms, phase)

    reliability: dict[str, Any] = {}
    for revision in ("before", "after"):
        selected = [sample for sample in samples if sample["revision"] == revision]
        succeeded = sum(sample.get("status") == "passed" for sample in selected)
        attempted = len(selected)
        reliability[revision] = {
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": attempted - succeeded,
            "success_rate": succeeded / attempted if attempted else 0.0,
        }
    reliability["matched_pairs"] = {
        "attempted": pair_count,
        "complete": len(complete_pair_ids),
        "failed": pair_count - len(complete_pair_ids),
        "success_rate": len(complete_pair_ids) / pair_count if pair_count else 0.0,
    }
    return {"phases": phase_summary, "reliability": reliability}


def ensure_under(path: Path, directory: Path, label: str) -> Path:
    resolved = path if path.is_absolute() else ROOT / path
    resolved = resolved.resolve()
    directory = directory.resolve()
    if not resolved.is_relative_to(directory):
        raise MatrixError(f"{label} must be under {directory}")
    return resolved


def default_output_path() -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return RESULTS_DIR / f"startup-latency-{timestamp}.json"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = MatrixArgumentParser(
        description=(
            "Build two Rustwright revisions in one Docker image and run a paired cold-start matrix."
        ),
        epilog=(
            "The default per-sample-container mode gives the strongest isolation: every sample gets "
            "a fresh capped container, Python process, and browser. Use revision-block-container only "
            "when the image cannot support per-sample containers. That fallback still starts a fresh "
            "Python process and browser for each sample, but container filesystem and kernel state "
            "persist for one before or after revision block. The checker rejects fallback artifacts "
            "as publication evidence."
        ),
    )
    parser.add_argument("--before-rev", required=True, help="Baseline Git revision.")
    parser.add_argument("--after-rev", required=True, help="Candidate Git revision.")
    parser.add_argument("--pairs", type=int, default=30, help="Matched pair count. Must be even; default 30.")
    parser.add_argument("--order", choices=["balanced-abba"], default="balanced-abba")
    parser.add_argument(
        "--output",
        help="Raw JSON path under .benchmark-data/results/. A timestamped path is the default.",
    )
    parser.add_argument(
        "--image",
        default=os.environ.get("RUSTWRIGHT_DOCKER_IMAGE", "rustwright-verify-testbox"),
        help="Prepared base image. Defaults to RUSTWRIGHT_DOCKER_IMAGE or rustwright-verify-testbox.",
    )
    parser.add_argument(
        "--isolation",
        choices=["per-sample-container", "revision-block-container"],
        default="per-sample-container",
        help="Container isolation mode. Use revision-block-container only as a diagnostic fallback.",
    )
    parser.add_argument(
        "--memory-limit",
        default=os.environ.get("TEST_DOCKER_MEMORY_LIMIT", "8g").lower(),
        help="Docker memory and swap cap. Must be 8 GiB or less; default 8g.",
    )
    parser.add_argument("--sample-timeout", type=int, default=180)
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--json", action="store_true", help="Print the summary report as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.memory_limit not in MEMORY_LIMITS or MEMORY_LIMITS[args.memory_limit] > 8:
        raise MatrixError("--memory-limit must be 8 GiB or less in whole-GiB units")
    if args.sample_timeout <= 0 or args.build_timeout <= 0:
        raise MatrixError("timeouts must be positive")
    transport = os.environ.get("RUSTWRIGHT_CDP_TRANSPORT") or "websocket"
    if transport not in {"websocket", "pipe"}:
        raise MatrixError("RUSTWRIGHT_CDP_TRANSPORT must be websocket or pipe")

    started_at = utc_now()
    exact_command = shlex.join([sys.executable, *sys.argv])
    materialize_revision(args.before_rev)
    materialize_revision(args.after_rev)
    before_sha = canonical_revision(args.before_rev)
    after_sha = canonical_revision(args.after_rev)
    plan = balanced_abba_plan(args.pairs)
    base_image_id = docker_image_id(args.image, 120)
    derived_image, image_digest = build_dual_venv_image(
        base_image=args.image,
        base_image_id=base_image_id,
        before_sha=before_sha,
        memory_limit=args.memory_limit,
        after_sha=after_sha,
        build_timeout=args.build_timeout,
    )
    image_metadata = query_image_environment(derived_image, args.memory_limit, args.sample_timeout)
    launcher_sha = sha256_file(LAUNCHER)
    environment = {
        "image_digest": image_digest,
        "browser_executable": image_metadata["browser_executable"],
        "browser_version": image_metadata["browser_version"],
        "python_version": image_metadata["python_version"],
        "rust_version": image_metadata["rust_version"],
        "memory_limit": args.memory_limit,
        "memory_swap_limit": args.memory_limit,
        "cpu_quota": "unbounded_by_runner",
        "cpu": image_metadata["cpu"],
        "transport": transport,
        "launcher_sha256": launcher_sha,
    }
    environment_id = hashlib.sha256(
        json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    environment["environment_id"] = environment_id

    samples: list[dict[str, Any]] = []
    block_containers: dict[str, str] = {}
    try:
        if args.isolation == "revision-block-container":
            block_containers = start_revision_block_containers(derived_image, args.memory_limit)
        for item in plan:
            samples.append(
                run_sample(
                    item=item,
                    image=derived_image,
                    memory_limit=args.memory_limit,
                    browser_version=image_metadata["browser_version"],
                    transport=transport,
                    environment=environment,
                    timeout=args.sample_timeout,
                    block_container_name=block_containers.get(item["revision"]),
                )
            )
    finally:
        stop_revision_block_containers(block_containers)

    order_sequence = [item["revision"] for item in plan]
    provenance = {
        "before_sha": before_sha,
        "after_sha": after_sha,
        "base_image": args.image,
        "base_image_digest": base_image_id,
        "measurement_image": derived_image,
        "image_digest": image_digest,
        "wheels": image_metadata["wheels"],
        "metadata_container_name": image_metadata["metadata_container_name"],
        "metadata_command": image_metadata["metadata_command"],
        "browser": {
            "executable": image_metadata["browser_executable"],
            "version": image_metadata["browser_version"],
        },
        "python_version": image_metadata["python_version"],
        "python_build": image_metadata["python_build"],
        "rust_version": image_metadata["rust_version"],
        "library_versions": image_metadata["library_versions"],
        "memory_limit": args.memory_limit,
        "memory_swap_limit": args.memory_limit,
        "cpu_quota": "unbounded_by_runner",
        "cpu": image_metadata["cpu"],
        "platform": image_metadata["platform"],
        "machine": image_metadata["machine"],
        "exact_command": exact_command,
        "start_time": started_at,
        "order_sequence": order_sequence,
        "parallelism": "sequential",
        "concurrency": 1,
        "container_isolation": (
            "one_fresh_container_per_sample"
            if args.isolation == "per-sample-container"
            else "one_persistent_container_per_revision_block"
        ),
        "fixture_hash": launcher_sha,
        "launcher_sha256": launcher_sha,
        "transport": transport,
    }
    summary = summarize(samples, args.pairs)
    output_path = ensure_under(
        Path(args.output) if args.output else default_output_path(),
        RESULTS_DIR,
        "--output",
    )
    report_path = ensure_under(
        REPORTS_DIR / f"{output_path.stem}-summary.json",
        REPORTS_DIR,
        "summary output",
    )
    artifact = {
        "schema_version": 1,
        "kind": "rustwright_startup_latency_matrix",
        "created_at": utc_now(),
        "pair_count_requested": args.pairs,
        "order_scheme": args.order,
        "isolation_mode": args.isolation,
        "provenance": provenance,
        "environment": environment,
        "order_sequence": plan,
        "samples": samples,
        "summary": summary,
        "result_path": str(output_path.relative_to(ROOT)),
        "report_path": str(report_path.relative_to(ROOT)),
    }
    report = {
        "schema_version": 1,
        "kind": "rustwright_startup_latency_summary",
        "created_at": artifact["created_at"],
        "result_path": artifact["result_path"],
        "provenance": provenance,
        "summary": summary,
    }
    write_json(output_path, artifact)
    write_json(report_path, report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        reliability = summary["reliability"]["matched_pairs"]
        print(f"Raw result: {artifact['result_path']}")
        print(f"Summary: {artifact['report_path']}")
        print(
            f"Complete pairs: {reliability['complete']}/{reliability['attempted']} "
            f"({reliability['success_rate']:.1%})"
        )
    failures = [sample for sample in samples if sample.get("status") != "passed"]
    return 3 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MatrixError as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error_message": str(exc)}))
        raise SystemExit(1)
