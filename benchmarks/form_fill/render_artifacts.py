#!/usr/bin/env python3
"""Render local-diagnostic form-fill memory charts and synchronized videos."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt


FPS = 24
MIB = 1024 * 1024
COLORS = {"playwright": "#2a78d6", "rustwright": "#1baf7a"}


@dataclass(frozen=True)
class Run:
    key: str
    name: str
    color: str
    path: Path
    epochs: dict[str, float]
    timeline: dict[str, Any]
    timings: dict[str, Any]
    stack_t: list[float]
    stack_mib: list[float]
    cgroup_t: list[float]
    cgroup_mib: list[float]
    cgroup_peak_mib: float

    def relative_times(self, sample_times: list[float], epoch_key: str) -> list[float]:
        offset = self.epochs[epoch_key] - self.epochs["workload_start"]
        return [value + offset for value in sample_times]

    def phase_ranges(self, kinds: set[str], origin_epoch: float) -> list[tuple[float, float]]:
        script_start = float(self.timeline["script_start_epoch"])
        ranges = []
        for interval in self.timeline["intervals"]:
            if interval["kind"] in kinds:
                ranges.append(
                    (
                        script_start + float(interval["t0_s"]) - origin_epoch,
                        script_start + float(interval["t1_s"]) - origin_epoch,
                    )
                )
        return ranges


def read_csv(path: Path, time_column: str, value_column: str) -> tuple[list[float], list[float]]:
    with path.open(newline="", encoding="ascii") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) < 2:
        raise ValueError(f"{path} needs at least two samples")
    times = [float(row[time_column]) for row in rows]
    values = [int(row[value_column]) / MIB for row in rows]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError(f"{path} sample times must increase")
    return times, values


def load_run(key: str, path: Path) -> Run:
    stack_t, stack_mib = read_csv(path / "stack_pss.csv", "t_rel_s", "total_bytes")
    cgroup_t, cgroup_mib = read_csv(path / "cgroup.csv", "t_rel_s", "bytes")
    return Run(
        key=key,
        name="Playwright" if key == "playwright" else "Rustwright",
        color=COLORS[key],
        path=path,
        epochs={
            name: float(value)
            for name, value in json.loads(
                (path / "epochs.json").read_text(encoding="utf-8")
            ).items()
        },
        timeline=json.loads((path / "timeline.json").read_text(encoding="utf-8")),
        timings=json.loads((path / "timings.json").read_text(encoding="utf-8")),
        stack_t=stack_t,
        stack_mib=stack_mib,
        cgroup_t=cgroup_t,
        cgroup_mib=cgroup_mib,
        cgroup_peak_mib=int(
            (path / "cgroup_memory_peak_bytes.txt").read_text(encoding="ascii")
        )
        / MIB,
    )


def add_phase_bands(axis: Any, run: Run, origin_epoch: float) -> None:
    for start, end in run.phase_ranges({"nav"}, origin_epoch):
        axis.axvspan(start, end, color=run.color, alpha=0.08, linewidth=0)
    for start, end in run.phase_ranges({"pause"}, origin_epoch):
        axis.axvspan(start, end, color="#777777", alpha=0.035, linewidth=0)


def style_axis(axis: Any, ylabel: str) -> None:
    axis.set_facecolor("#fcfcfb")
    axis.grid(axis="y", color="#e1e0d9", linewidth=0.7)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    axis.set_ylabel(ylabel)


def render_comparison(runs: list[Run], output_dir: Path) -> None:
    figure, axes = plt.subplots(2, 1, figsize=(16, 9), dpi=100, sharex=True)
    figure.patch.set_facecolor("#fcfcfb")
    figure.suptitle("Form-fill memory profile — local diagnostic", fontsize=23, weight="bold")
    axes[0].set_title("Workload stack proportional set size (Python + driver + browser)")
    axes[1].set_title("Whole capped-container cgroup memory")
    for axis, ylabel in zip(axes, ("Stack PSS (MiB)", "Cgroup memory (MiB)")):
        style_axis(axis, ylabel)

    for run in runs:
        stack_x = run.relative_times(run.stack_t, "stack_sampler_start")
        cgroup_x = run.relative_times(run.cgroup_t, "cgroup_sampler_start")
        axes[0].plot(stack_x, run.stack_mib, color=run.color, label=run.name, linewidth=1.7)
        axes[1].plot(cgroup_x, run.cgroup_mib, color=run.color, label=run.name, linewidth=1.7)
        add_phase_bands(axes[0], run, run.epochs["workload_start"])
        add_phase_bands(axes[1], run, run.epochs["workload_start"])
    axes[0].legend(frameon=False)
    axes[1].legend(frameon=False)
    axes[1].set_xlabel("Seconds from workload start")
    figure.text(
        0.5,
        0.012,
        "Colored shading: page/browser time · gray shading: scripted choreography pauses",
        ha="center",
        fontsize=9,
        color="#555555",
    )
    figure.tight_layout(rect=(0, 0.04, 1, 0.94))
    figure.savefig(output_dir / "comparison.png", facecolor=figure.get_facecolor())
    plt.close(figure)


def video_metadata(path: Path) -> tuple[float, int]:
    payload = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames,r_frame_rate:format=duration",
                "-of",
                "json",
                str(path),
            ],
            text=True,
        )
    )
    duration = float(payload["format"]["duration"])
    stream = payload["streams"][0]
    numerator, denominator = map(int, stream["r_frame_rate"].split("/"))
    rate = numerator / denominator
    if abs(rate - FPS) > 0.01:
        raise ValueError(f"{path} is {rate} fps; expected {FPS}")
    frames_value = stream.get("nb_frames")
    frames = int(frames_value) if frames_value not in (None, "N/A") else round(duration * FPS)
    return duration, frames


def render_memory_video(run: Run, output_dir: Path, shared_max: float) -> None:
    recording = run.path / "recording.mp4"
    duration, frames = video_metadata(recording)
    origin = run.epochs["ffmpeg_start"]
    x = [
        value + run.epochs["stack_sampler_start"] - origin
        for value in run.stack_t
    ]
    figure, axis = plt.subplots(figsize=(5.2, 7), dpi=100)
    figure.patch.set_facecolor("#fcfcfb")
    style_axis(axis, "Stack PSS (MiB)")
    axis.set_xlim(0, duration)
    axis.set_ylim(0, shared_max)
    axis.set_xlabel("Seconds from recording start")
    axis.set_title(f"{run.name} form-fill stack memory", weight="bold")
    add_phase_bands(axis, run, origin)
    (line,) = axis.plot([], [], color=run.color, linewidth=1.8)
    (head,) = axis.plot([], [], "o", color=run.color, markersize=6)
    label = axis.annotate("", xy=(0, 0), xytext=(8, 8), textcoords="offset points")

    output = output_dir / f"{run.key}_memory.mp4"
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            "520x700",
            "-r",
            str(FPS),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-frames:v",
            str(frames),
            str(output),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None:
        raise RuntimeError("ffmpeg stdin was not created")
    try:
        for frame in range(frames):
            current = frame / FPS
            count = max(1, bisect.bisect_right(x, current))
            visible_x = x[:count]
            visible_y = run.stack_mib[:count]
            line.set_data(visible_x, visible_y)
            head.set_data([visible_x[-1]], [visible_y[-1]])
            label.xy = (visible_x[-1], visible_y[-1])
            label.set_text(f"{visible_y[-1]:.1f} MiB")
            figure.canvas.draw()
            process.stdin.write(figure.canvas.buffer_rgba())
    finally:
        process.stdin.close()
    error = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    plt.close(figure)
    if return_code:
        raise RuntimeError(f"ffmpeg failed for {run.name}: {error}")


def write_stats(runs: list[Run], output_dir: Path) -> None:
    payload = {
        "classification": "example run, demo-grade local diagnostic",
        "runs": {
            run.key: {
                "wall_time_s": round(
                    run.epochs["workload_end"] - run.epochs["workload_start"], 6
                ),
                "sampled_stack_pss_peak_mib": round(max(run.stack_mib), 6),
                "kernel_cgroup_peak_mib": round(run.cgroup_peak_mib, 6),
                "fields_filled": run.timings["fields_filled"],
            }
            for run in runs
        },
    }
    (output_dir / "stats.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--playwright-run", type=Path, required=True)
    parser.add_argument("--rustwright-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = [
        load_run("playwright", args.playwright_run.resolve()),
        load_run("rustwright", args.rustwright_run.resolve()),
    ]
    render_comparison(runs, output_dir)
    write_stats(runs, output_dir)
    shared_max = max(max(run.stack_mib) for run in runs) * 1.1
    for run in runs:
        render_memory_video(run, output_dir, shared_max)


if __name__ == "__main__":
    main()
