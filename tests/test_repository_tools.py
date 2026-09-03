from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]


def test_benchmark_runner_rejects_abbreviated_impl_override():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "run_benchmarks.py"),
            "--impl",
            "rustwright",
            "--im=all",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --im=all" in result.stderr


def test_launch_latency_claim_checker_rejects_smoke_only_blacksmith_run(tmp_path):
    benchmark_json = tmp_path / "launch-smoke.json"
    write_synthetic_launch_benchmark(benchmark_json)
    data = json.loads(benchmark_json.read_text(encoding="utf-8"))
    data["repetitions"] = 1
    data["results"] = data["results"][:2]
    for implementation in ("rustwright", "playwright"):
        data["aggregate"][implementation]["runs"] = 1
        data["aggregate"][implementation]["cases"] = {
            "strict_case_0": {"median": 1.0}
        }
    data["aggregate"]["rustwright"]["total_mean_ms"] = {"median": 300.0}
    data["aggregate"]["playwright"]["total_mean_ms"] = {"median": 200.0}
    benchmark_json.write_text(json.dumps(data), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_launch_latency_claim.py"),
            "--benchmark-json",
            str(benchmark_json),
            "--source",
            "github-actions",
            "--runner",
            "blacksmith-2vcpu-ubuntu-2404",
            "--artifact",
            "smoke-artifact",
            "--run-url",
            "https://github.com/Skyvern-AI/rustwright/actions/runs/1",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)
    checks = {item["name"]: item for item in report["checks"]}

    assert result.returncode == 1
    assert report["status"] == "rejected"
    assert report["accepted"] is False
    assert checks["testbox_backed"]["passed"] is False
    assert checks["docker_memory_cap"]["passed"] is True
    assert checks["docker_swap_cap"]["passed"] is True
    assert checks["artifact_link"]["passed"] is True
    assert checks["minimum_repetitions"]["passed"] is False
    assert checks["full_case_scope"]["passed"] is False
    assert checks["distribution_stats"]["passed"] is False
    assert checks["latency_win"]["passed"] is False


def write_synthetic_launch_benchmark(path: Path) -> None:
    cases = {
        f"strict_case_{index}": {"median": 1.0, "p25": 0.9, "p75": 1.1}
        for index in range(78)
    }
    path.write_text(
        json.dumps(
            {
                "suite": "strict",
                "lifecycle": "warm-browser",
                "iterations": 1,
                "repetitions": 3,
                "case_filters": [],
                "container_isolation": "one_container_per_implementation_per_repetition",
                "result_path": ".benchmark-data/results/synthetic-launch.json",
                "metadata": {
                    "implementations": ["rustwright", "playwright"],
                    "container_isolation": "one_container_per_implementation_per_repetition",
                    "docker_memory_limit": "8g",
                    "docker_memory_swap_limit": "8g",
                    "docker_image_id": "sha256:synthetic",
                    "docker_cpu_host_info": "8 logical CPUs available to Docker host",
                    "git_rev": "abc123",
                },
                "results": [
                    {"implementation": implementation, "status": "passed", "repetition": repetition}
                    for repetition in range(1, 4)
                    for implementation in ["rustwright", "playwright"]
                ],
                "aggregate": {
                    "rustwright": {
                        "runs": 3,
                        "total_mean_ms": {"median": 100.0, "p25": 95.0, "p75": 105.0},
                        "cases": cases,
                    },
                    "playwright": {
                        "runs": 3,
                        "total_mean_ms": {"median": 200.0, "p25": 190.0, "p75": 210.0},
                        "cases": cases,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_launch_latency_claim_checker_accepts_full_testbox_benchmark_json(tmp_path):
    benchmark_json = tmp_path / "launch-benchmark.json"
    write_synthetic_launch_benchmark(benchmark_json)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_launch_latency_claim.py"),
            "--benchmark-json",
            str(benchmark_json),
            "--source",
            "testbox",
            "--runner",
            "blacksmith-testbox",
            "--artifact",
            "synthetic-launch-benchmark",
            "--run-url",
            "https://github.com/Skyvern-AI/rustwright/actions/runs/1",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "accepted"
    assert report["accepted"] is True
    assert report["observed"]["case_count"] == 78
    assert report["observed"]["rustwright_reduction_pct"] == 50.0
    assert all(check["passed"] for check in report["checks"])


def test_launch_latency_claim_checker_rejects_benchmark_json_without_testbox_source(tmp_path):
    benchmark_json = tmp_path / "launch-benchmark.json"
    write_synthetic_launch_benchmark(benchmark_json)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_launch_latency_claim.py"),
            "--benchmark-json",
            str(benchmark_json),
            "--artifact",
            "synthetic-launch-benchmark",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)
    checks = {item["name"]: item for item in report["checks"]}

    assert result.returncode == 1
    assert report["status"] == "rejected"
    assert checks["testbox_backed"]["passed"] is False
    assert checks["latency_win"]["passed"] is True


def test_benchmark_artifact_checker_writes_phase2_and_launch_reports(tmp_path):
    benchmark_json = tmp_path / "launch-benchmark.json"
    reports_dir = tmp_path / "reports"
    write_synthetic_launch_benchmark(benchmark_json)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_benchmark_artifacts.py"),
            "--pattern",
            str(benchmark_json),
            "--output-dir",
            str(reports_dir),
            "--source",
            "testbox",
            "--runner",
            "blacksmith-testbox",
            "--artifact",
            "synthetic-launch-benchmark",
            "--run-url",
            "https://github.com/Skyvern-AI/rustwright/actions/runs/1",
            "--enforce-phase2",
            "--enforce-launch",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["result_count"] == 1
    assert report["phase2_failures"] == 0
    assert report["launch_failures"] == 0
    assert report["checks"][0]["phase2"]["status"] == "accepted"
    assert report["checks"][0]["launch"]["status"] == "accepted"
    assert (reports_dir / "phase2-launch-benchmark.json").exists()
    assert (reports_dir / "launch-launch-benchmark.json").exists()


def test_benchmark_artifact_checker_keeps_launch_rejections_nonfatal_without_enforcement(tmp_path):
    benchmark_json = tmp_path / "launch-benchmark.json"
    reports_dir = tmp_path / "reports"
    write_synthetic_launch_benchmark(benchmark_json)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_benchmark_artifacts.py"),
            "--pattern",
            str(benchmark_json),
            "--output-dir",
            str(reports_dir),
            "--source",
            "github-actions",
            "--runner",
            "blacksmith-4vcpu-ubuntu-2404",
            "--artifact",
            "synthetic-launch-benchmark",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["phase2_failures"] == 0
    assert report["launch_failures"] == 1
    assert report["checks"][0]["phase2"]["status"] == "accepted"
    assert report["checks"][0]["launch"]["status"] == "rejected"



def test_remote_docker_test_dry_run_builds_capped_tailscale_ssh_command():
    env = os.environ.copy()
    env.pop("TEST_DOCKER_MEMORY_LIMIT", None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_remote_docker_test.py"),
            "--host",
            "mac-mini.tailnet.example",
            "--workdir",
            "/tmp/rustwright-checkout",
            "--dry-run",
            "--json",
            "--",
            "sampled",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "dry_run"
    assert report["host"] == "mac-mini.tailnet.example"
    assert report["memory_limit"] == "8g"
    assert report["docker_args"] == ["sampled"]
    assert report["remote_command"] == (
        "cd /tmp/rustwright-checkout && "
        "TEST_DOCKER_MEMORY_LIMIT=8g tools/docker_test.sh sampled"
    )
    assert report["ssh_command"][:2] == ["ssh", "mac-mini.tailnet.example"]
    assert report["remote_command"] in report["ssh_command"][2]
    assert "__RUSTWRIGHT_REMOTE_EXIT__=" in report["ssh_command"][2]


def test_remote_docker_test_dry_run_supports_tailscale_ssh_transport():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_remote_docker_test.py"),
            "--host",
            "builder@mac-mini.example",
            "--workdir",
            "/tmp/rustwright-checkout",
            "--transport",
            "tailscale-ssh",
            "--tailscale-bin",
            "/bin/echo",
            "--dry-run",
            "--json",
            "--",
            "sampled",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "dry_run"
    assert report["transport"] == "tailscale-ssh"
    assert report["tailscale_bin"] == "/bin/echo"
    assert report["ssh_command"][:3] == ["/bin/echo", "ssh", "builder@mac-mini.example"]
    assert report["remote_command"] in report["ssh_command"][3]
    assert "__RUSTWRIGHT_REMOTE_EXIT__=" in report["ssh_command"][3]


def test_remote_docker_test_classifies_tailscale_web_auth_prompt_quickly():
    sys.path.insert(0, str(ROOT / "tools"))
    from run_remote_docker_test import run_command

    start = time.monotonic()
    result = run_command(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                "print('# Tailscale SSH requires an additional check.')\n"
                "print('# To authenticate, visit: https://login.tailscale.com/a/testauth')\n"
                "sys.stdout.flush()\n"
                "time.sleep(60)\n"
            ),
        ],
        timeout=30,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 5
    assert result["status"] == "failed"
    assert result["returncode"] is None
    assert result["reason"] == "tailscale_ssh_web_auth_required"
    assert result["auth_url"] == "https://login.tailscale.com/a/testauth"
    assert "Tailscale SSH requires an additional check" in result["output_tail"]


def test_remote_docker_check_only_promotes_tailscale_web_auth_prompt(tmp_path):
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\n"
        "printf '# Tailscale SSH requires an additional check.\\n'\n"
        "printf '# To authenticate, visit: https://login.tailscale.com/a/checkonlyauth\\n'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_remote_docker_test.py"),
            "--host",
            "builder@mac-mini.example",
            "--transport",
            "ssh",
            "--skip-tailscale-check",
            "--check-only",
            "--remote-docker-check",
            "--memory-limit",
            "7g",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "unavailable"
    assert report["reason"] == "tailscale_ssh_web_auth_required"
    assert report["auth_url"] == "https://login.tailscale.com/a/checkonlyauth"
    assert report["remote_docker_info"] == {}
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["remote_docker_info"]["passed"] is False
    assert "Tailscale SSH requires an additional check" in checks["remote_docker_info"]["detail"]


def test_remote_docker_test_rejects_memory_caps_above_8gb():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_remote_docker_test.py"),
            "--host",
            "mac-mini.tailnet.example",
            "--memory-limit",
            "9g",
            "--dry-run",
            "--json",
            "--",
            "sampled",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 2
    assert report["status"] == "rejected"
    assert report["checks"][0]["name"] == "memory_limit"
    assert report["checks"][0]["passed"] is False


def test_docker_test_build_command_handles_non_legacy_empty_dockerfile_args(tmp_path):
    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "DOCKER": str(fake_docker),
            "TEST_DOCKER_MEMORY_LIMIT": "7g",
            "RUSTWRIGHT_DOCKER_IMAGE": "rustwright-test-build-command",
        }
    )

    result = subprocess.run(
        [str(ROOT / "tools" / "docker_test.sh"), "build"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
    )
    args = result.stdout.splitlines()

    assert result.returncode == 0
    assert args[:5] == ["build", "--memory", "7g", "--memory-swap", "7g"]
    assert "-t" in args
    assert "rustwright-test-build-command" in args
    assert args[-1] == "."


def test_remote_docker_info_parser_reads_host_capacity():
    sys.path.insert(0, str(ROOT / "tools"))
    from run_remote_docker_test import (
        build_remote_command,
        build_remote_pull_probe_command,
        memory_limit_bytes,
        parse_pull_probe_output,
        parse_remote_docker_info,
    )

    info = parse_remote_docker_info("8321712128 10 29.4.3\n\n__RUSTWRIGHT_REMOTE_EXIT__=0\n")
    auth_prompt_info = parse_remote_docker_info(
        "# Tailscale SSH requires an additional check.\n"
        "# To authenticate, visit: https://login.tailscale.com/a/testauth\n"
    )

    assert info == {"memory_bytes": 8321712128, "cpus": 10, "server_version": "29.4.3"}
    assert auth_prompt_info == {}
    assert memory_limit_bytes("8g") > info["memory_bytes"]
    assert memory_limit_bytes("7g") < info["memory_bytes"]
    pull_probe = build_remote_pull_probe_command("hello-world:latest", 30)
    assert '"image": "hello-world:latest"' in pull_probe
    assert '"timeout": 30' in pull_probe
    assert "subprocess.TimeoutExpired" in pull_probe
    pull_report = parse_pull_probe_output('noise\n{"status": "timeout", "image": "hello-world:latest"}\n')
    assert pull_report == {"status": "timeout", "image": "hello-world:latest"}
    remote_command = build_remote_command(
        "/repo path",
        "7g",
        ["build", "."],
        {
            "RUSTWRIGHT_DOCKER_IMAGE": "rustwright-verify-macmini",
            "RUSTWRIGHT_DOCKER_BASE_IMAGE": "local/hermes-agent:latest",
            "RUSTWRIGHT_DOCKER_LEGACY": "1",
            "DOCKER_BUILDKIT": "0",
            "DOCKER_CONFIG": "/tmp/rustwright-docker-config",
            "BENCHMARK_FULL_ITERATIONS": "1",
        },
    )
    assert remote_command == (
        "cd '/repo path' && TEST_DOCKER_MEMORY_LIMIT=7g "
        "RUSTWRIGHT_DOCKER_IMAGE=rustwright-verify-macmini "
        "RUSTWRIGHT_DOCKER_BASE_IMAGE=local/hermes-agent:latest "
        "RUSTWRIGHT_DOCKER_LEGACY=1 "
        "DOCKER_BUILDKIT=0 DOCKER_CONFIG=/tmp/rustwright-docker-config "
        "BENCHMARK_FULL_ITERATIONS=1 tools/docker_test.sh build ."
    )


def test_remote_docker_test_check_only_requires_host():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_remote_docker_test.py"),
            "--check-only",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={key: value for key, value in os.environ.items() if key != "RUSTWRIGHT_REMOTE_HOST"},
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "unavailable"
    assert report["checks"][0]["name"] == "remote_host_configured"
    assert report["checks"][0]["passed"] is False




def test_benchmark_testbox_helper_classifies_blacksmith_visibility_404(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "$*" = "remote get-url origin" ]; then
  echo "git@github.com:Skyvern-AI/rustwright.git"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    blacksmith = bin_dir / "blacksmith"
    blacksmith.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  echo "blacksmith version 0.4.41"
  exit 0
fi
echo "Error: Could not fetch .github/workflows/benchmark-testbox.yml at ref main: HTTP request returned status code 404:" >&2
exit 1
""",
        encoding="utf-8",
    )
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "workflow" ]; then
  echo "Rustwright Benchmark Testbox	active	286588901"
  exit 0
fi
if [ "${1:-}" = "api" ] && [ "${2:-}" = "orgs/Skyvern-AI/installations" ]; then
  echo "app_slug=blacksmith-sh id=136569713 repository_selection=selected contents=write workflows=write updated_at=2026-06-18T10:02:32.000-04:00"
  exit 0
fi
if [ "${1:-}" = "api" ]; then
  echo ".github/workflows/benchmark-testbox.yml sha=abc123"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    blacksmith.chmod(0o755)
    gh.chmod(0o755)

    result = subprocess.run(
        [str(ROOT / "tools" / "run_benchmark_testbox.sh")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RUSTWRIGHT_TESTBOX_JOB": "benchmark",
        },
    )

    assert result.returncode == 1
    assert "blacksmith_repo_visibility_blocked" in result.stderr
    assert "Blacksmith could not read the workflow from GitHub" in result.stderr
    assert ".github/workflows/benchmark-testbox.yml sha=abc123" in result.stderr
    assert "repository_selection=selected" in result.stderr


def test_check_testbox_visibility_reports_blacksmith_repo_visibility_block(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "$*" = "remote get-url origin" ]; then
  echo "git@github.com:Skyvern-AI/rustwright.git"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "api" ] && [ "${2:-}" = "orgs/Skyvern-AI/installations" ]; then
  cat <<'JSON'
{"installations":[{"id":136569713,"app_slug":"blacksmith-sh","repository_selection":"selected","permissions":{"contents":"write","workflows":"write"},"updated_at":"2026-06-18T10:02:32.000-04:00"}]}
JSON
  exit 0
fi
if [ "${1:-}" = "api" ]; then
  echo ".github/workflows/benchmark-testbox.yml sha=abc123"
  exit 0
fi
if [ "${1:-}" = "workflow" ]; then
  echo "Rustwright Benchmark Testbox	active	286588901"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    blacksmith = bin_dir / "blacksmith"
    blacksmith.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  echo "blacksmith version 0.4.41"
  exit 0
fi
if [ "${1:-}" = "auth" ]; then
  echo "Authenticated organizations:" >&2
  echo "  * Skyvern-AI (current)" >&2
  exit 0
fi
if [ "${1:-}" = "testbox" ] && [ "${2:-}" = "warmup" ]; then
  echo "Could not fetch .github/workflows/benchmark-testbox.yml at ref main: HTTP request returned status code 404:" >&2
  exit 1
fi
exit 1
""",
        encoding="utf-8",
    )
    for executable in (git, gh, blacksmith):
        executable.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_testbox_visibility.py"),
            "--probe-warmup",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "blacksmith_repo_visibility_blocked"
    assert report["github"]["contents"]["returncode"] == 0
    assert report["github"]["blacksmith_app_installation"]["installed"] is True
    assert report["github"]["blacksmith_app_installation"]["repository_selection"] == "selected"
    assert report["diagnosis"]["probable_root_cause"] == "blacksmith_github_app_selected_repo_access"
    assert report["blacksmith"]["auth_status"]["returncode"] == 0
    assert report["warmup"]["returncode"] == 1


def test_native_extension_abi_checker_passes_for_current_worktree():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_native_extension.py")],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": "python:."},
    )
    assert "ABI check passed" in result.stdout


def test_benchmark_matrix_reports_common_case_totals_for_mixed_impls():
    sys.path.insert(0, str(ROOT / "tools"))
    from run_benchmark_matrix import common_case_comparison

    aggregate = {
        "rustwright": {
            "cases": {
                "a": {"median": 10.0},
                "b": {"median": 20.0},
                "rust_only": {"median": 100.0},
            }
        },
        "playwright": {
            "cases": {
                "a": {"median": 15.0},
                "b": {"median": 25.0},
                "python_only": {"median": 100.0},
            }
        },
        "typescript-puppeteer": {
            "cases": {
                "a": {"median": 12.0},
                "b": {"median": 18.0},
            }
        },
    }

    comparison = common_case_comparison(aggregate)

    assert comparison["case_names"] == ["a", "b"]
    assert comparison["case_count"] == 2
    assert comparison["total_median_ms"] == {
        "rustwright": 30.0,
        "playwright": 40.0,
        "typescript-puppeteer": 30.0,
    }
    assert comparison["speedups"]["vs_playwright_median_reduction_pct"] == 25.0
    assert comparison["speedups"]["vs_typescript-puppeteer_median_reduction_pct"] == 0.0


def test_benchmark_matrix_prefers_full_iterations_env(monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import run_benchmark_matrix

    monkeypatch.setenv("BENCHMARK_ITERATIONS", "20")
    monkeypatch.setenv("BENCHMARK_FULL_ITERATIONS", "3")
    assert run_benchmark_matrix.default_iterations() == 3

    monkeypatch.delenv("BENCHMARK_FULL_ITERATIONS")
    assert run_benchmark_matrix.default_iterations() == 20


def test_benchmark_matrix_metadata_uses_configured_docker_memory(monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    from run_benchmark_matrix import matrix_metadata

    monkeypatch.setenv("TEST_DOCKER_MEMORY_LIMIT", "7g")
    args = argparse.Namespace(
        suite="strict",
        lifecycle="warm-browser",
        iterations=1,
        repetitions=1,
        case_filters=[],
        rebuild_rustwright=False,
        skip_rustwright_rebuild=False,
    )
    metadata = matrix_metadata(args, ["rustwright"], docker_preflight={"status": "unhealthy"})

    assert metadata["docker_memory_limit"] == "7g"
    assert metadata["docker_memory_swap_limit"] == "7g"


def test_benchmark_matrix_classifies_docker_daemon_failures():
    sys.path.insert(0, str(ROOT / "tools"))
    from run_benchmark_matrix import is_docker_daemon_failure

    assert is_docker_daemon_failure("docker: Error response from daemon: Bad response from Docker engine.")
    assert is_docker_daemon_failure('error waiting for container: invalid character "c" looking for beginning of value')
    assert is_docker_daemon_failure("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    assert is_docker_daemon_failure("docker info timed out after 10s")
    assert not is_docker_daemon_failure("benchmark assertion failed")


def test_benchmark_matrix_marks_all_runs_skipped_after_docker_preflight_failure():
    sys.path.insert(0, str(ROOT / "tools"))
    from run_benchmark_matrix import skipped_results_after_docker_preflight

    skipped = skipped_results_after_docker_preflight(
        [
            (0, "rustwright"),
            (0, "playwright"),
            (1, "rustwright"),
        ]
    )

    assert skipped == [
        {
            "implementation": "rustwright",
            "status": "skipped",
            "reason": "skipped_after_docker_preflight_failure",
            "failure_kind": "docker_daemon_error",
            "repetition": 1,
        },
        {
            "implementation": "playwright",
            "status": "skipped",
            "reason": "skipped_after_docker_preflight_failure",
            "failure_kind": "docker_daemon_error",
            "repetition": 1,
        },
        {
            "implementation": "rustwright",
            "status": "skipped",
            "reason": "skipped_after_docker_preflight_failure",
            "failure_kind": "docker_daemon_error",
            "repetition": 2,
        },
    ]


def test_render_benchmark_matrix_shows_skipped_run_status(tmp_path):
    result_path = tmp_path / "benchmark.json"
    result_path.write_text(
        json.dumps(
            {
                "suite": "strict",
                "lifecycle": "warm-browser",
                "iterations": 1,
                "repetitions": 1,
                "container_isolation": "one_container_per_implementation_per_repetition",
                "metadata": {
                    "docker_preflight": {
                        "status": "unhealthy",
                        "failure_kind": "docker_daemon_error",
                    }
                },
                "results": [
                    {
                        "implementation": "rustwright",
                        "repetition": 1,
                        "status": "skipped",
                        "failure_kind": "docker_daemon_error",
                        "reason": "skipped_after_docker_preflight_failure",
                    }
                ],
                "aggregate": {},
                "common_case_comparison": {},
                "case_winners": {"win_counts": {}, "cases": {}},
                "speedups": {},
            }
        ),
        encoding="utf-8",
    )

    rendered = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "render_benchmark_matrix.py"), str(result_path), "--slow-cases", "1"],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert "## Run Status" in rendered.stdout
    assert "rustwright" in rendered.stdout
    assert "skipped_after_docker_preflight_failure" in rendered.stdout
    assert "Docker preflight" in rendered.stdout
    assert "unhealthy" in rendered.stdout


def test_phase_2_benchmark_checker_rejects_failed_or_low_sample_runs(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import check_phase2_benchmark

    def args(path: Path):
        return argparse.Namespace(
            json_path=path,
            suite="strict",
            lifecycle="warm-browser",
            reference_impl="playwright",
            min_repetitions=3,
            min_iterations=1,
            min_case_count=2,
            min_speedup_pct=30.0,
            max_variance_ratio=1.2,
        )

    low_sample = {
        "suite": "strict",
        "lifecycle": "warm-browser",
        "iterations": 1,
        "repetitions": 1,
        "results": [
            {"implementation": "rustwright", "status": "passed", "repetition": 1},
            {"implementation": "playwright", "status": "passed", "repetition": 1},
        ],
        "aggregate": {
            "rustwright": {
                "total_mean_ms": {"median": 50.0},
                "cases": {"a": {}, "b": {}},
            },
            "playwright": {
                "total_mean_ms": {"median": 100.0},
                "cases": {"a": {}, "b": {}},
            },
        },
    }
    low_sample_path = tmp_path / "low-sample.json"
    low_sample_path.write_text(json.dumps(low_sample), encoding="utf-8")

    low_sample_report = check_phase2_benchmark.evaluate(low_sample, args(low_sample_path))

    assert low_sample_report["status"] == "rejected"
    assert any(check["name"] == "repetitions" and not check["passed"] for check in low_sample_report["checks"])

    failed = {
        **low_sample,
        "repetitions": 3,
        "results": [
            {"implementation": "rustwright", "status": "passed", "repetition": 1},
            {"implementation": "playwright", "status": "failed", "repetition": 1, "failure_kind": "docker_daemon_error"},
        ],
    }
    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps(failed), encoding="utf-8")

    failed_report = check_phase2_benchmark.evaluate(failed, args(failed_path))

    assert failed_report["status"] == "rejected"
    assert any(check["name"] == "all_runs_passed" and not check["passed"] for check in failed_report["checks"])


def test_phase_2_benchmark_checker_accepts_repeated_fast_strict_matrix(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import check_phase2_benchmark

    data = {
        "suite": "strict",
        "lifecycle": "warm-browser",
        "iterations": 2,
        "repetitions": 3,
        "results": [
            {"implementation": "rustwright", "status": "passed", "repetition": 1},
            {"implementation": "playwright", "status": "passed", "repetition": 1},
            {"implementation": "rustwright", "status": "passed", "repetition": 2},
            {"implementation": "playwright", "status": "passed", "repetition": 2},
            {"implementation": "rustwright", "status": "passed", "repetition": 3},
            {"implementation": "playwright", "status": "passed", "repetition": 3},
        ],
        "aggregate": {
            "rustwright": {
                "total_mean_ms": {"median": 60.0, "p25": 58.0, "p75": 62.0},
                "cases": {"a": {}, "b": {}},
            },
            "playwright": {
                "total_mean_ms": {"median": 100.0, "p25": 95.0, "p75": 105.0},
                "cases": {"a": {}, "b": {}},
            },
        },
    }
    path = tmp_path / "accepted.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    args = argparse.Namespace(
        json_path=path,
        suite="strict",
        lifecycle="warm-browser",
        reference_impl="playwright",
        min_repetitions=3,
        min_iterations=1,
        min_case_count=2,
        min_speedup_pct=30.0,
        max_variance_ratio=1.2,
    )

    report = check_phase2_benchmark.evaluate(data, args)

    assert report["status"] == "accepted"
    assert report["observed"]["rustwright_reduction_pct"] == 40.0
    assert report["observed"]["reference_variance_ratio"] < 1.2

    high_variance = json.loads(json.dumps(data))
    high_variance["aggregate"]["playwright"]["total_mean_ms"] = {"median": 100.0, "p25": 70.0, "p75": 110.0}
    high_variance_report = check_phase2_benchmark.evaluate(high_variance, args)

    assert high_variance_report["status"] == "rejected"
    assert any(
        check["name"] == "reference_variance" and not check["passed"]
        for check in high_variance_report["checks"]
    )


def test_phase_2_benchmark_checker_derives_current_strict_case_count():
    sys.path.insert(0, str(ROOT / "tools"))
    import check_phase2_benchmark

    assert check_phase2_benchmark.current_strict_case_count() == 78
