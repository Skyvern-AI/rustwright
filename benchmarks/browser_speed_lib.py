from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DATA = ROOT / ".benchmark-data"
BROWSER_SPEED_DATA = BENCHMARK_DATA / "browser-speed"
EXTERNAL_DATA = BENCHMARK_DATA / "external"
NPM_PREFIX = BROWSER_SPEED_DATA / "npm"


def shell_join(command: list[str]) -> str:
    return shlex.join([str(part) for part in command])


def repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def now_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def result_path(benchmark_id: str, name: str) -> Path:
    return BROWSER_SPEED_DATA / "results" / benchmark_id / f"{name}-{now_slug()}.json"


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_and_capture(
    command: list[str],
    *,
    benchmark_id: str,
    name: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    timeout: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    cwd = cwd or ROOT
    printable = shell_join(command)
    if dry_run:
        print(printable)
        return 0

    started = dt.datetime.now(dt.timezone.utc)
    proc = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    finished = dt.datetime.now(dt.timezone.utc)
    output_path = result_path(benchmark_id, name)
    payload = {
        "benchmark_id": benchmark_id,
        "name": name,
        "command": printable,
        "cwd": str(cwd),
        "created_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "returncode": proc.returncode,
        "status": "passed" if proc.returncode == 0 else "failed",
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "metadata": metadata or {},
    }
    write_result(output_path, payload)
    print(repo_relative(output_path))
    return proc.returncode


def npm_bin(name: str) -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    return NPM_PREFIX / "node_modules" / ".bin" / f"{name}{suffix}"


def install_npm_package(package: str, *, dry_run: bool = False) -> int:
    command = ["npm", "install", "--prefix", str(NPM_PREFIX), package]
    return run_and_capture(
        command,
        benchmark_id="setup",
        name=f"npm-{package.split('@')[0] or package}",
        dry_run=dry_run,
        metadata={"package": package, "install_prefix": str(NPM_PREFIX)},
    )


def ensure_git_clone(url: str, destination: Path, *, dry_run: bool = False) -> int:
    if destination.exists():
        print(repo_relative(destination))
        return 0
    command = ["git", "clone", "--depth", "1", url, str(destination)]
    return run_and_capture(
        command,
        benchmark_id="setup",
        name=f"git-{destination.name}",
        dry_run=dry_run,
        metadata={"url": url, "destination": str(destination)},
    )


def require_file(path: Path, setup_hint: str) -> None:
    if not path.exists():
        raise SystemExit(f"Missing {path}. {setup_hint}")


def fail(message: str) -> None:
    raise SystemExit(message)


def python() -> str:
    return sys.executable
