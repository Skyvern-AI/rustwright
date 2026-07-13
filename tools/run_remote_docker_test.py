#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import selectors
import time
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAX_MEMORY_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_TAILSCALE_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
REMOTE_EXIT_PREFIX = "__RUSTWRIGHT_REMOTE_EXIT__="
TAILSCALE_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/[^\s]+")
ALLOWED_MEMORY_LIMITS = {
    "1g",
    "2g",
    "3g",
    "4g",
    "5g",
    "6g",
    "7g",
    "8g",
    "1024m",
    "2048m",
    "3072m",
    "4096m",
    "5120m",
    "6144m",
    "7168m",
    "8192m",
}
REMOTE_ENV_ALLOWLIST = (
    "RUSTWRIGHT_DOCKER_IMAGE",
    "RUSTWRIGHT_DOCKER_BASE_IMAGE",
    "RUSTWRIGHT_DOCKER_LEGACY",
    "INSTALL_PUPPETEER",
    "DOCKER_BUILDKIT",
    "DOCKER_CONFIG",
    "BENCHMARK_FULL_ITERATIONS",
    "BENCHMARK_ITERATIONS",
    "BENCHMARK_DOCKER_PREFLIGHT_TIMEOUT",
    "BENCHMARK_CHROMIUM_EXECUTABLE",
    "RUSTWRIGHT_BENCH_REBUILD",
    "RUSTWRIGHT_DOCKER_REBUILD_TARGET_CACHE",
    "RUSTWRIGHT_DOCKER_REBUILD_CACHE_PREFIX",
    "RUSTWRIGHT_CDP_TRANSPORT",
    "RUSTWRIGHT_INCLUDE_PUPPETEER_BENCHMARK",
)


def memory_limit_bytes(value: str) -> int | None:
    text = value.strip().lower()
    multipliers = {
        "g": 1024**3,
        "m": 1024**2,
    }
    for suffix, multiplier in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * multiplier)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None


def valid_memory_limit(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ALLOWED_MEMORY_LIMITS:
        return True
    parsed = memory_limit_bytes(lowered)
    return parsed is not None and parsed <= MAX_MEMORY_BYTES


def parse_remote_docker_info(output: str) -> dict[str, Any]:
    if has_tailscale_auth_prompt(output):
        return {}
    lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.strip().startswith(REMOTE_EXIT_PREFIX)
    ]
    if not lines:
        return {}
    parts = lines[-1].split(maxsplit=2)
    info: dict[str, Any] = {}
    if parts:
        try:
            info["memory_bytes"] = int(parts[0])
        except ValueError:
            pass
    if len(parts) > 1:
        try:
            info["cpus"] = int(parts[1])
        except ValueError:
            pass
    if len(parts) > 2:
        info["server_version"] = parts[2]
    return info


def parse_pull_probe_output(output: str) -> dict[str, Any]:
    for line in reversed([line.strip() for line in output.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("image"):
            return value
    return {}


def normalize_docker_args(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        args = args[1:]
    return args or ["sampled"]


def remote_env_assignments(env: dict[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    return [f"{name}={shlex.quote(source[name])}" for name in REMOTE_ENV_ALLOWLIST if source.get(name)]


def build_remote_command(workdir: str, memory_limit: str, docker_args: list[str], env: dict[str, str] | None = None) -> str:
    pieces = [
        "cd",
        shlex.quote(workdir),
        "&&",
        f"TEST_DOCKER_MEMORY_LIMIT={shlex.quote(memory_limit)}",
        *remote_env_assignments(env),
        "tools/docker_test.sh",
        *[shlex.quote(arg) for arg in docker_args],
    ]
    return " ".join(pieces)


def build_remote_docker_info_command() -> str:
    return "docker info --format '{{.MemTotal}} {{.NCPU}} {{.ServerVersion}}'"


def build_remote_pull_probe_command(image: str, timeout: int) -> str:
    payload = {
        "image": image,
        "timeout": max(timeout, 1),
    }
    return f"""python3 - <<'PY'
import json
import subprocess

payload = {json.dumps(payload)}
try:
    proc = subprocess.run(
        ["docker", "pull", payload["image"]],
        text=True,
        capture_output=True,
        timeout=payload["timeout"],
    )
except subprocess.TimeoutExpired as exc:
    output = (exc.stdout or "") + (exc.stderr or "")
    print(json.dumps({{"status": "timeout", "image": payload["image"], "output_tail": output[-2000:]}}))
    raise SystemExit(124)

output = proc.stdout + proc.stderr
print(json.dumps({{
    "status": "passed" if proc.returncode == 0 else "failed",
    "image": payload["image"],
    "returncode": proc.returncode,
    "output_tail": output[-2000:],
}}))
raise SystemExit(proc.returncode)
PY"""


def find_tailscale_binary(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    tailscale = shutil.which("tailscale")
    if tailscale:
        return tailscale
    if Path(DEFAULT_TAILSCALE_BIN).is_file():
        return DEFAULT_TAILSCALE_BIN
    return None


def remote_exec_command(transport: str, host: str | None, remote_command: str, tailscale_bin: str | None) -> list[str]:
    remote_command = wrap_remote_command(remote_command)
    if transport == "tailscale-ssh":
        return [tailscale_bin or "tailscale", "ssh", host or "<missing-host>", remote_command]
    return ["ssh", host or "<missing-host>", remote_command]


def wrap_remote_command(remote_command: str) -> str:
    script = (
        f"{remote_command}\n"
        "__rustwright_remote_rc=$?\n"
        f"printf '\\n{REMOTE_EXIT_PREFIX}%s\\n' \"$__rustwright_remote_rc\"\n"
        'exit "$__rustwright_remote_rc"'
    )
    return f"sh -lc {shlex.quote(script)}"


def output_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def tail_lines(text: str, limit: int = 40) -> str:
    return "\n".join(text.splitlines()[-limit:])


def tailscale_auth_url(text: str) -> str | None:
    match = TAILSCALE_AUTH_URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,)")


def has_tailscale_auth_prompt(text: str) -> bool:
    return "Tailscale SSH requires an additional check" in text and tailscale_auth_url(text) is not None


def auth_prompt_from_command_result(result: dict[str, Any]) -> str | None:
    if result.get("auth_url"):
        return str(result["auth_url"])
    output = output_to_text(result.get("output_tail") or "")
    return tailscale_auth_url(output) if has_tailscale_auth_prompt(output) else None


def terminate_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def read_available(stream: Any) -> str:
    chunks: list[str] = []
    while stream is not None:
        try:
            data = os.read(stream.fileno(), 4096)
        except BlockingIOError:
            break
        except OSError:
            break
        if not data:
            break
        chunks.append(output_to_text(data))
    return "".join(chunks)


def read_process_output(proc: subprocess.Popen[Any], timeout: int) -> tuple[str, str, int | None]:
    selector = selectors.DefaultSelector()
    streams = [stream for stream in (proc.stdout, proc.stderr) if stream is not None]
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)

        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            returncode = proc.poll()
            if returncode is not None:
                for stream in streams:
                    chunks.append(read_available(stream))
                return "".join(chunks), "", returncode

            combined = "".join(chunks)
            if has_tailscale_auth_prompt(combined):
                terminate_process(proc)
                return combined, "tailscale_ssh_web_auth_required", None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process(proc)
                return combined, f"timed out after {timeout}s", None

            events = selector.select(timeout=min(0.2, remaining))
            for key, _ in events:
                text = read_available(key.fileobj)
                if text:
                    chunks.append(text)
                else:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
    finally:
        selector.close()


def run_command(command: list[str], timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        combined, reason, returncode = read_process_output(proc, timeout)
    except OSError as exc:
        return {"status": "failed", "returncode": None, "output_tail": str(exc), "reason": "os_error"}
    if reason:
        report = {
            "status": "failed",
            "returncode": returncode,
            "output_tail": tail_lines(combined, 20),
            "reason": reason,
        }
        auth_url = tailscale_auth_url(combined)
        if auth_url:
            report["auth_url"] = auth_url
        return report
    if returncode is None:
        return {
            "status": "failed",
            "returncode": None,
            "output_tail": tail_lines(combined, 20),
            "reason": "command exited without return code",
        }
    for line in reversed(combined.splitlines()):
        if line.startswith(REMOTE_EXIT_PREFIX):
            try:
                returncode = int(line.removeprefix(REMOTE_EXIT_PREFIX))
            except ValueError:
                pass
            break
    return {
        "status": "passed" if returncode == 0 else "failed",
        "returncode": returncode,
        "output_tail": tail_lines(combined),
    }


def availability(
    host: str | None,
    skip_tailscale_check: bool,
    timeout: int,
    tailscale_bin: str | None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if not host:
        checks.append({"name": "remote_host_configured", "passed": False, "detail": "set --host or RUSTWRIGHT_REMOTE_HOST"})
        return {"ready": False, "checks": checks}
    checks.append({"name": "remote_host_configured", "passed": True, "detail": host})

    if skip_tailscale_check:
        checks.append({"name": "tailscale_status", "passed": True, "detail": "skipped by --skip-tailscale-check"})
        return {"ready": True, "checks": checks}

    if not tailscale_bin:
        checks.append(
            {
                "name": "tailscale_binary",
                "passed": False,
                "detail": f"tailscale not found on PATH or at {DEFAULT_TAILSCALE_BIN}",
            }
        )
        return {"ready": False, "checks": checks}
    checks.append({"name": "tailscale_binary", "passed": True, "detail": tailscale_bin})

    status = run_command([tailscale_bin, "status"], timeout)
    checks.append(
        {
            "name": "tailscale_status",
            "passed": status["status"] == "passed",
            "detail": status.get("output_tail") or status.get("reason") or "",
        }
    )
    return {"ready": all(item["passed"] for item in checks), "checks": checks}


def print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"status: {report['status']}")
    for check in report.get("checks", []):
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"{mark} {check['name']}: {check['detail']}")
    print("ssh command:")
    print(" ".join(shlex.quote(part) for part in report["ssh_command"]))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run tools/docker_test.sh on a remote Tailscale-accessible host, usually the Mac mini."
    )
    parser.add_argument("--host", default=os.environ.get("RUSTWRIGHT_REMOTE_HOST"))
    parser.add_argument("--workdir", default=os.environ.get("RUSTWRIGHT_REMOTE_WORKDIR", str(ROOT)))
    parser.add_argument("--memory-limit", default=os.environ.get("TEST_DOCKER_MEMORY_LIMIT", "8g"))
    parser.add_argument(
        "--transport",
        choices=("ssh", "tailscale-ssh"),
        default=os.environ.get("RUSTWRIGHT_REMOTE_TRANSPORT", "ssh"),
        help="Remote command transport. Use tailscale-ssh for Tailscale SSH hosts such as the Mac mini.",
    )
    parser.add_argument(
        "--tailscale-bin",
        default=os.environ.get("RUSTWRIGHT_TAILSCALE_BIN"),
        help="Path to the tailscale CLI. Defaults to PATH lookup, then the macOS app bundle.",
    )
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--tailscale-timeout", type=int, default=10)
    parser.add_argument("--skip-tailscale-check", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the SSH command without checking or running remote commands.")
    parser.add_argument("--check-only", action="store_true", help="Check local Tailscale readiness without running SSH.")
    parser.add_argument(
        "--remote-docker-check",
        action="store_true",
        help="During --check-only, also run docker info on the remote host and verify the memory cap fits that Docker VM.",
    )
    parser.add_argument(
        "--remote-pull-check",
        action="append",
        default=[],
        metavar="IMAGE",
        help="During --check-only, also test whether the remote Docker daemon can pull IMAGE within --remote-pull-timeout.",
    )
    parser.add_argument("--remote-pull-timeout", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("docker_args", nargs=argparse.REMAINDER, help="Arguments after -- are passed to tools/docker_test.sh.")
    args = parser.parse_args()

    docker_args = normalize_docker_args(args.docker_args)
    tailscale_bin = find_tailscale_binary(args.tailscale_bin)
    if not valid_memory_limit(args.memory_limit):
        report = {
            "status": "rejected",
            "checks": [
                {
                    "name": "memory_limit",
                    "passed": False,
                    "detail": f"TEST_DOCKER_MEMORY_LIMIT must be 8g or lower, got {args.memory_limit}",
                }
            ],
            "ssh_command": [],
        }
        print_report(report, args.json)
        return 2

    remote_command = build_remote_command(args.workdir, args.memory_limit, docker_args)
    ssh_command = remote_exec_command(args.transport, args.host, remote_command, tailscale_bin)
    base_report: dict[str, Any] = {
        "host": args.host,
        "workdir": args.workdir,
        "docker_args": docker_args,
        "memory_limit": args.memory_limit,
        "transport": args.transport,
        "tailscale_bin": tailscale_bin,
        "remote_command": remote_command,
        "ssh_command": ssh_command,
    }

    if args.dry_run:
        report = {
            **base_report,
            "status": "dry_run",
            "checks": [{"name": "dry_run", "passed": True, "detail": "remote command was not executed"}],
        }
        print_report(report, args.json)
        return 0

    ready = availability(args.host, args.skip_tailscale_check, args.tailscale_timeout, tailscale_bin)
    if args.check_only or not ready["ready"]:
        checks = list(ready["checks"])
        remote_docker_info: dict[str, Any] = {}
        auth_failure_url: str | None = None
        if args.check_only and ready["ready"] and args.remote_docker_check:
            docker_command = build_remote_docker_info_command()
            docker_check = run_command(
                remote_exec_command(args.transport, args.host, docker_command, tailscale_bin),
                args.tailscale_timeout,
            )
            docker_auth_url = auth_prompt_from_command_result(docker_check)
            if docker_auth_url:
                auth_failure_url = docker_auth_url
            else:
                remote_docker_info = parse_remote_docker_info(docker_check.get("output_tail", ""))
            checks.append(
                {
                    "name": "remote_docker_info",
                    "passed": docker_check["status"] == "passed",
                    "detail": docker_check.get("output_tail") or docker_check.get("reason") or "",
                }
            )
            remote_memory = remote_docker_info.get("memory_bytes")
            requested_memory = memory_limit_bytes(args.memory_limit)
            if isinstance(remote_memory, int) and isinstance(requested_memory, int):
                checks.append(
                    {
                        "name": "memory_limit_within_remote_docker_host",
                        "passed": requested_memory <= remote_memory,
                        "detail": f"requested={requested_memory} remote={remote_memory}",
                    }
                )
        if args.check_only and ready["ready"] and args.remote_pull_check:
            for image in args.remote_pull_check:
                pull_command = " ".join(
                    [
                        *remote_env_assignments(),
                        build_remote_pull_probe_command(image, args.remote_pull_timeout),
                    ]
                )
                pull_check = run_command(
                    remote_exec_command(args.transport, args.host, pull_command, tailscale_bin),
                    args.remote_pull_timeout + args.tailscale_timeout + 5,
                )
                pull_auth_url = auth_prompt_from_command_result(pull_check)
                if pull_auth_url:
                    auth_failure_url = pull_auth_url
                pull_report = {} if pull_auth_url else parse_pull_probe_output(pull_check.get("output_tail", ""))
                pull_passed = pull_check["status"] == "passed" and pull_report.get("status") == "passed"
                checks.append(
                    {
                        "name": f"remote_docker_pull:{image}",
                        "passed": pull_passed,
                        "detail": pull_check.get("output_tail") or pull_check.get("reason") or "",
                    }
                )
        report = {
            **base_report,
            "status": "ready" if all(item["passed"] for item in checks) else "unavailable",
            "checks": checks,
            "remote_docker_info": remote_docker_info,
        }
        if auth_failure_url:
            report["reason"] = "tailscale_ssh_web_auth_required"
            report["auth_url"] = auth_failure_url
        print_report(report, args.json)
        return 0 if all(item["passed"] for item in checks) else 1

    run = run_command(ssh_command, args.timeout)
    report = {
        **base_report,
        "status": run["status"],
        "checks": ready["checks"],
        "returncode": run["returncode"],
        "output_tail": run["output_tail"],
    }
    if run.get("reason"):
        report["reason"] = run["reason"]
    if run.get("auth_url"):
        report["auth_url"] = run["auth_url"]
    print_report(report, args.json)
    return 0 if run["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
