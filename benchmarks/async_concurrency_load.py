#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


CONCURRENCY_LEVELS = (5, 25, 50, 100)
DEFAULT_VARIANTS = ("shared", "multi")
DEFAULT_MULTI_BROWSER_COUNT = 4
HEARTBEAT_INTERVAL_SECONDS = 0.01
SAMPLE_INTERVAL_SECONDS = 0.05


HTML_TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Rustwright async load case {case_id}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; }}
    main {{ width: 760px; }}
    .row {{ display: flex; gap: 12px; margin: 16px 0; }}
    button, input {{ font: inherit; padding: 8px 10px; }}
    #status {{ min-height: 24px; }}
  </style>
</head>
<body>
  <main>
    <h1>Workflow {case_id}</h1>
    <div id="ready" data-state="booting">booting</div>
    <div class="row">
      <button id="open" type="button" disabled>Open</button>
      <input id="notes" name="notes" autocomplete="off" placeholder="Notes">
    </div>
    <div id="status">idle</div>
    <div id="result">Result seed {seed}</div>
  </main>
  <script>
    const delay = {delay};
    const ready = document.querySelector("#ready");
    const openButton = document.querySelector("#open");
    const notes = document.querySelector("#notes");
    const status = document.querySelector("#status");
    const result = document.querySelector("#result");
    window.addEventListener("DOMContentLoaded", () => {{
      setTimeout(() => {{
        ready.dataset.state = "ready";
        ready.textContent = "ready";
        openButton.disabled = false;
      }}, delay);
    }});
    openButton.addEventListener("click", () => {{
      status.textContent = "clicked {case_id}";
      result.textContent = "Clicked {case_id}";
    }});
    notes.addEventListener("input", () => {{
      result.textContent = `Saved: ${{notes.value}}`;
    }});
  </script>
</body>
</html>
"""


class LoadHandler(BaseHTTPRequestHandler):
    server_version = "RustwrightAsyncLoad/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_text("ok")
            return
        if not parsed.path.startswith("/case/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            case_id = int(parsed.path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        query = parse_qs(parsed.query)
        seed = int(query.get("seed", ["0"])[0])
        delay = 5 + ((case_id * 17 + seed) % 30)
        html = HTML_TEMPLATE.format(case_id=case_id, seed=seed, delay=delay)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def local_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), LoadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="async-load-http")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@dataclass
class OpRecorder:
    timings: dict[str, list[float]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, name: str, elapsed_ms: float) -> None:
        async with self._lock:
            self.timings.setdefault(name, []).append(elapsed_ms)

    async def record_error(self, task_id: int, error: BaseException) -> None:
        async with self._lock:
            self.errors.append(
                {
                    "task_id": task_id,
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )


async def timed(recorder: OpRecorder, name: str, fn: Callable[[], Any]) -> Any:
    start = time.perf_counter()
    try:
        return await fn()
    finally:
        await recorder.record(name, (time.perf_counter() - start) * 1000)


@dataclass
class Heartbeat:
    max_lag_ms: float = 0.0
    samples: list[float] = field(default_factory=list)

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            expected = loop.time() + HEARTBEAT_INTERVAL_SECONDS
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            now = loop.time()
            lag_ms = max(0.0, (now - expected) * 1000)
            self.samples.append(lag_ms)
            self.max_lag_ms = max(self.max_lag_ms, lag_ms)


@dataclass
class ResourceSample:
    active_threads: int
    ps_threads_self: int | None
    ps_threads_tree: int | None
    rss_self_kb: int | None
    rss_tree_kb: int | None


class ResourceSampler:
    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self.samples: list[ResourceSample] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="async-load-resource-sampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._append(sample_resources(self.root_pid))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._append(sample_resources(self.root_pid))
            self._stop.wait(SAMPLE_INTERVAL_SECONDS)

    def _append(self, sample: ResourceSample) -> None:
        with self._lock:
            self.samples.append(sample)

    def peaks(self) -> dict[str, int | None]:
        with self._lock:
            samples = list(self.samples)
        return {
            "active_threads": max((sample.active_threads for sample in samples), default=threading.active_count()),
            "ps_threads_self": max_optional(sample.ps_threads_self for sample in samples),
            "ps_threads_tree": max_optional(sample.ps_threads_tree for sample in samples),
            "rss_self_kb": max_optional(sample.rss_self_kb for sample in samples),
            "rss_tree_kb": max_optional(sample.rss_tree_kb for sample in samples),
        }

    def sample_count(self) -> int:
        with self._lock:
            return len(self.samples)


def max_optional(values: Any) -> int | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None


def sample_resources(root_pid: int) -> ResourceSample:
    tree = ps_process_tree(root_pid)
    return ResourceSample(
        active_threads=threading.active_count(),
        ps_threads_self=ps_thread_count(root_pid),
        ps_threads_tree=sum_optional(ps_thread_count(pid) for pid in tree),
        rss_self_kb=ps_rss_kb(root_pid),
        rss_tree_kb=sum_optional(ps_rss_kb(pid) for pid in tree),
    )


def sum_optional(values: Any) -> int | None:
    total = 0
    seen = False
    for value in values:
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def run_ps(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def ps_rss_kb(pid: int) -> int | None:
    output = run_ps(["ps", "-o", "rss=", "-p", str(pid)])
    if not output:
        return None
    try:
        return int(output.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def ps_thread_count(pid: int) -> int | None:
    if sys.platform.startswith("linux"):
        output = run_ps(["ps", "-o", "nlwp=", "-p", str(pid)])
        if output:
            try:
                return int(output.strip().splitlines()[0])
            except (IndexError, ValueError):
                return None
    output = run_ps(["ps", "-M", "-p", str(pid)])
    if not output:
        return None
    lines = [line for line in output.splitlines() if line.strip()]
    return max(len(lines) - 1, 0) if lines else None


def ps_process_tree(root_pid: int) -> list[int]:
    output = run_ps(["ps", "-axo", "pid=,ppid="])
    if not output:
        return [root_pid]
    children: dict[int, list[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    result: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in result:
            continue
        result.append(pid)
        stack.extend(children.get(pid, []))
    return result


async def configure_rustwright_executor(workers: int | None) -> None:
    if workers is None:
        return
    import rustwright.async_api as rw_async

    configure = getattr(rw_async, "configure_async_executor", None)
    if configure is None:
        raise RuntimeError("rustwright.async_api.configure_async_executor is not available in this build")
    configure(max_workers=workers, thread_name_prefix="rustwright-async-load")


def rustwright_executor_info(impl: str) -> dict[str, Any] | None:
    if impl != "rustwright":
        return None
    try:
        import rustwright.async_api as rw_async
    except Exception:
        return None
    info = getattr(rw_async, "async_executor_info", None)
    if info is None:
        return None
    return info()


async def import_async_stack(impl: str, rustwright_workers: int | None) -> Any:
    if impl == "rustwright":
        await configure_rustwright_executor(rustwright_workers)
        from rustwright.async_api import async_playwright

        return async_playwright
    if impl == "playwright":
        from playwright.async_api import async_playwright

        return async_playwright
    raise ValueError(f"unknown impl: {impl}")


async def launch_browsers(async_playwright: Any, impl: str, count: int) -> tuple[Any, list[Any]]:
    manager = async_playwright()
    pw = await manager.start()
    launch_options: dict[str, Any] = {"headless": True}
    executable = os.environ.get("BENCHMARK_CHROMIUM_EXECUTABLE") or os.environ.get("RUSTWRIGHT_CHROMIUM")
    if executable:
        if impl == "playwright":
            launch_options["executable_path"] = executable
        else:
            launch_options["executable_path"] = executable
    browsers = [await pw.chromium.launch(**launch_options) for _ in range(count)]
    return manager, browsers


async def close_stack(manager: Any, browsers: list[Any]) -> None:
    for browser in browsers:
        try:
            await browser.close()
        except Exception:
            pass
    stop = getattr(manager, "stop", None)
    if stop is not None:
        await stop()


async def run_task(task_id: int, base_url: str, browser: Any, recorder: OpRecorder, seed: int) -> str:
    context = None
    page = None
    value = f"task-{task_id}-seed-{seed}"
    task_start = time.perf_counter()
    try:
        context = await timed(recorder, "new_context", lambda: browser.new_context())
        page = await timed(recorder, "new_page", lambda: context.new_page())
        await timed(recorder, "goto", lambda: page.goto(f"{base_url}/case/{task_id}?seed={seed}", wait_until="domcontentloaded"))
        await timed(recorder, "wait_for_selector", lambda: page.wait_for_selector("#ready[data-state='ready']", timeout=10_000))
        await timed(recorder, "click", lambda: page.click("#open", timeout=10_000))
        await timed(recorder, "fill", lambda: page.fill("#notes", value, timeout=10_000))
        text = await timed(recorder, "inner_text", lambda: page.inner_text("#result", timeout=10_000))
        await timed(recorder, "screenshot", lambda: page.screenshot(full_page=False))
        if value not in text:
            raise AssertionError(f"unexpected result text: {text!r}")
        return text
    finally:
        if page is not None:
            try:
                await timed(recorder, "page_close", lambda: page.close())
            except Exception:
                pass
        if context is not None:
            try:
                await timed(recorder, "context_close", lambda: context.close())
            except Exception:
                pass
        await recorder.record("task_total", (time.perf_counter() - task_start) * 1000)


async def run_scenario(args: argparse.Namespace, base_url: str, concurrency: int, variant: str) -> dict[str, Any]:
    rustwright_workers = args.rustwright_executor_workers if args.impl == "rustwright" else None
    async_playwright = await import_async_stack(args.impl, rustwright_workers)
    browser_count = 1 if variant == "shared" else min(args.multi_browser_count, concurrency)
    manager, browsers = await launch_browsers(async_playwright, args.impl, browser_count)
    recorder = OpRecorder()
    heartbeat = Heartbeat()
    resource_sampler = ResourceSampler(os.getpid())
    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat.run(stop))
    resource_sampler.start()
    rng = random.Random(args.seed + concurrency * 100 + browser_count)
    start = time.perf_counter()
    status = "passed"
    traceback_timer = threading.Timer(
        args.scenario_timeout,
        dump_traceback,
        args=(args.traceback_dir, args.impl, variant, concurrency),
    )
    traceback_timer.daemon = True
    traceback_timer.start()
    try:
        tasks = [
            asyncio.create_task(
                run_task(
                    task_id,
                    base_url,
                    browsers[task_id % len(browsers)],
                    recorder,
                    rng.randrange(1_000_000),
                )
            )
            for task_id in range(concurrency)
        ]
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=args.scenario_timeout)
        for task_id, result in enumerate(results):
            if isinstance(result, BaseException):
                status = "failed"
                await recorder.record_error(task_id, result)
    except asyncio.TimeoutError as exc:
        status = "timeout"
        await recorder.record_error(-1, exc)
        dump_traceback(args.traceback_dir, args.impl, variant, concurrency)
    finally:
        elapsed = time.perf_counter() - start
        traceback_timer.cancel()
        stop.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        resource_sampler.stop()
        await close_stack(manager, browsers)

    return {
        "impl": args.impl,
        "variant": variant,
        "concurrency": concurrency,
        "browser_count": browser_count,
        "status": status,
        "total_seconds": elapsed,
        "throughput_tasks_per_second": concurrency / elapsed if elapsed > 0 else None,
        "ops": summarize_ops(recorder.timings),
        "errors": recorder.errors,
        "event_loop_lag_ms": summarize_values(heartbeat.samples),
        "resource_peaks": resource_sampler.peaks(),
        "samples_collected": resource_sampler.sample_count(),
        "rustwright_async_executor": rustwright_executor_info(args.impl),
    }


def dump_traceback(traceback_dir: str | None, impl: str, variant: str, concurrency: int) -> None:
    if not traceback_dir:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        return
    directory = Path(traceback_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"traceback-{impl}-{variant}-n{concurrency}.txt"
    with path.open("w", encoding="utf-8") as handle:
        faulthandler.dump_traceback(file=handle, all_threads=True)


def summarize_ops(timings: dict[str, list[float]]) -> dict[str, Any]:
    return {name: summarize_values(values) for name, values in sorted(timings.items())}


def summarize_values(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    sorted_values = sorted(values)
    return {
        "count": len(sorted_values),
        "min": sorted_values[0],
        "p50": percentile(sorted_values, 50),
        "p95": percentile(sorted_values, 95),
        "p99": percentile(sorted_values, 99),
        "max": sorted_values[-1],
        "mean": statistics.fmean(sorted_values),
    }


def percentile(sorted_values: list[float], percentile_value: float) -> float:
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (percentile_value / 100)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[int(rank)]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (rank - lower)


def build_metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "pid": os.getpid(),
        "command": sys.argv,
        "impl": args.impl,
        "seed": args.seed,
        "scenario_timeout": args.scenario_timeout,
        "rustwright_executor_workers": args.rustwright_executor_workers,
        "benchmark_chromium_executable": os.environ.get("BENCHMARK_CHROMIUM_EXECUTABLE"),
        "rustwright_chromium": os.environ.get("RUSTWRIGHT_CHROMIUM"),
        "default_threadpool_expected_max_workers": min(32, (os.cpu_count() or 1) + 4),
    }


def write_results(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    with local_server() as base_url:
        payload: dict[str, Any] = {
            "metadata": build_metadata(args),
            "server": {"base_url": base_url},
            "results": [],
        }
        output = Path(args.output)
        for variant in args.variants:
            if variant not in DEFAULT_VARIANTS:
                raise ValueError(f"unknown variant {variant!r}; expected one of {DEFAULT_VARIANTS}")
            for concurrency in args.concurrency:
                print(f"running impl={args.impl} variant={variant} concurrency={concurrency}", flush=True)
                result = await run_scenario(args, base_url, concurrency, variant)
                payload["results"].append(result)
                write_results(output, payload)
                print(
                    f"done status={result['status']} seconds={result['total_seconds']:.2f} "
                    f"throughput={result['throughput_tasks_per_second']:.2f}/s",
                    flush=True,
                )
        return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure Rustwright async wrapper concurrency against Python Playwright async.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python benchmarks/async_concurrency_load.py --impl rustwright --concurrency 5 --variants shared --output results/rw.json
              python benchmarks/async_concurrency_load.py --impl playwright --concurrency 5 25 50 100 --output results/pw.json
              python benchmarks/async_concurrency_load.py --impl rustwright --rustwright-executor-workers 100 --output results/rw-fixed.json
            """
        ),
    )
    parser.add_argument("--impl", choices=("rustwright", "playwright"), required=True)
    parser.add_argument("--concurrency", nargs="+", type=int, default=list(CONCURRENCY_LEVELS))
    parser.add_argument("--variants", nargs="+", default=list(DEFAULT_VARIANTS), choices=DEFAULT_VARIANTS)
    parser.add_argument("--multi-browser-count", type=int, default=DEFAULT_MULTI_BROWSER_COUNT)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scenario-timeout", type=float, default=240.0)
    parser.add_argument("--rustwright-executor-workers", type=int, default=None)
    parser.add_argument("--traceback-dir", default=None)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.multi_browser_count < 1:
        raise SystemExit("--multi-browser-count must be >= 1")
    if any(value < 1 for value in args.concurrency):
        raise SystemExit("--concurrency values must be >= 1")
    output = Path(args.output)
    with tempfile.TemporaryDirectory(prefix="rw-async-load-"):
        payload = asyncio.run(async_main(args))
    write_results(output, payload)
    print(f"wrote {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
