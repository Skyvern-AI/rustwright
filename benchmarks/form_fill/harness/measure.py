#!/usr/bin/env python3
"""Coordinate workload, cgroup/PSS samplers, epochs, and optional recording."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO


SCRIPT_DIR = Path(__file__).resolve().parent
SUITE_DIR = SCRIPT_DIR.parent
OUTPUT_ROOT = Path("/output")
OUTPUT_NAME = re.compile(r"[A-Za-z0-9._-]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("playwright", "rustwright"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record", action="store_true")
    return parser.parse_args()


def start_process(command: list[str], log: IO[bytes], **kwargs: object) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def stop_process(
    process: subprocess.Popen[bytes] | None,
    stop_signal: signal.Signals = signal.SIGTERM,
    timeout: float = 10,
) -> int:
    if process is None:
        return 0
    if process.poll() is None:
        process.send_signal(stop_signal)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=5)


def wait_for_file(path: Path, process: subprocess.Popen[bytes], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        if process.poll() is not None:
            raise RuntimeError(f"process exited before creating {path.name}")
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {path.name}")


def wait_for_display(display: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    environment = {**os.environ, "DISPLAY": display}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Xvfb exited before becoming ready")
        result = subprocess.run(
            ["xdpyinfo"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.02)
    raise TimeoutError("timed out waiting for Xvfb")


def has_data_rows(path: Path) -> bool:
    return path.is_file() and len(path.read_text(encoding="ascii").splitlines()) >= 2


def validated_output(path: Path) -> Path:
    """Limit destructive cleanup to one named run under the output mount."""
    root = OUTPUT_ROOT.resolve()
    if not path.is_absolute() or not OUTPUT_NAME.fullmatch(path.name):
        raise ValueError("output must be an absolute, simply named run directory")
    if path.is_symlink() or path.parent.resolve() != root:
        raise ValueError(f"output must be a non-symlink direct child of {OUTPUT_ROOT}")
    resolved = path.resolve()
    if resolved.parent != root:
        raise ValueError(f"output resolves outside {OUTPUT_ROOT}")
    return resolved


def main() -> int:
    args = parse_args()
    output = validated_output(args.output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    log_path = output / "run.log"
    epochs: dict[str, float] = {}
    processes: list[subprocess.Popen[bytes]] = []
    cgroup_sampler: subprocess.Popen[bytes] | None = None
    stack_sampler: subprocess.Popen[bytes] | None = None
    xvfb: subprocess.Popen[bytes] | None = None
    ffmpeg: subprocess.Popen[bytes] | None = None
    workload: subprocess.Popen[bytes] | None = None
    final_code = 125

    with log_path.open("wb", buffering=0) as log:
        try:
            cgroup_sampler = start_process(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "sample_cgroup_memory.py"),
                    "--output",
                    str(output / "cgroup.csv"),
                    "--peak-output",
                    str(output / "cgroup_memory_peak_bytes.txt"),
                ],
                log,
            )
            processes.append(cgroup_sampler)
            epochs["cgroup_sampler_start"] = time.time()
            wait_for_file(output / "cgroup.csv", cgroup_sampler)

            workload_environment = {
                **os.environ,
                "BACKEND": args.backend,
                "OUT_DIR": str(output),
            }
            if args.record:
                display = ":99"
                xvfb = start_process(
                    [
                        "Xvfb",
                        display,
                        "-screen",
                        "0",
                        "520x700x24",
                        "-nolisten",
                        "tcp",
                        "-ac",
                    ],
                    log,
                )
                processes.append(xvfb)
                wait_for_display(display, xvfb)
                time.sleep(1)
                epochs["ffmpeg_start"] = time.time()
                ffmpeg = start_process(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "warning",
                        "-y",
                        "-f",
                        "x11grab",
                        "-framerate",
                        "24",
                        "-video_size",
                        "520x700",
                        "-i",
                        f"{display}.0+0,0",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-threads",
                        "1",
                        "-crf",
                        "18",
                        "-fps_mode",
                        "cfr",
                        "-r",
                        "24",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        str(output / "recording.mp4"),
                    ],
                    log,
                )
                processes.append(ffmpeg)
                time.sleep(0.2)
                if ffmpeg.poll() is not None:
                    raise RuntimeError("ffmpeg exited before the workload started")
                workload_environment.update({"DISPLAY": display, "HEADED": "1"})
            else:
                time.sleep(1)

            epochs["workload_start"] = time.time()
            workload_name = os.environ.get("BENCH_WORKLOAD") or (
                "fill_form_remote.py"
                if os.environ.get("SKYVERN_SESSION") == "1"
                else "fill_form.py"
            )
            workload = start_process(
                [sys.executable, str(SUITE_DIR / workload_name)],
                log,
                env=workload_environment,
            )
            processes.append(workload)
            epochs["stack_sampler_start"] = time.time()
            stack_sampler = start_process(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "sample_stack_memory.py"),
                    "--root-pid",
                    str(workload.pid),
                    "--output",
                    str(output / "stack_pss.csv"),
                ],
                log,
            )
            processes.append(stack_sampler)
            workload_code = workload.wait()
            epochs["workload_end"] = time.time()
            workload = None

            stack_code = stop_process(stack_sampler)
            stack_sampler = None
            if args.record:
                # ffmpeg commonly reports a non-zero status when SIGINT is the
                # requested, graceful stop. Validate the finalized file below
                # instead of treating that signal status as a workload failure.
                stop_process(ffmpeg, signal.SIGINT, timeout=20)
                epochs["ffmpeg_stop"] = time.time()
                ffmpeg = None
            time.sleep(0.2)
            cgroup_code = stop_process(cgroup_sampler)
            cgroup_sampler = None

            final_code = workload_code
            if final_code == 0 and (stack_code != 0 or cgroup_code != 0):
                final_code = 125
            required = [
                output / "timeline.json",
                output / "timings.json",
                output / "cgroup_memory_peak_bytes.txt",
            ]
            if final_code == 0 and not all(path.is_file() and path.stat().st_size for path in required):
                final_code = 125
            if final_code == 0 and not has_data_rows(output / "cgroup.csv"):
                final_code = 125
            if final_code == 0 and not has_data_rows(output / "stack_pss.csv"):
                final_code = 125
            if args.record and final_code == 0:
                recording = output / "recording.mp4"
                if not recording.is_file() or recording.stat().st_size == 0:
                    final_code = 125
        except Exception as error:
            log.write(f"measurement harness failed: {error}\n".encode("utf-8"))
            final_code = 125
        finally:
            if workload is not None:
                stop_process(workload)
            stop_process(stack_sampler)
            stop_process(ffmpeg, signal.SIGINT, timeout=20)
            stop_process(cgroup_sampler)
            stop_process(xvfb)
            for process in reversed(processes):
                stop_process(process)

    now = time.time()
    epochs.setdefault("workload_start", now)
    epochs.setdefault("stack_sampler_start", now)
    epochs.setdefault("workload_end", now)
    (output / "epochs.json").write_text(
        json.dumps(epochs, indent=2) + "\n", encoding="utf-8"
    )
    wall_time = epochs["workload_end"] - epochs["workload_start"]
    (output / "wall_time.txt").write_text(f"{wall_time:.6f}\n", encoding="ascii")
    (output / "exit_code.txt").write_text(f"{final_code}\n", encoding="ascii")
    return final_code


if __name__ == "__main__":
    raise SystemExit(main())
