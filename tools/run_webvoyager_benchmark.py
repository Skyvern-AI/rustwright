#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import os
import platform
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / ".benchmark-data" / "manifests" / "webvoyager_tasks.json"
DEFAULT_IMPLS = ["rustwright", "playwright"]
DEFAULT_NETWORK_WARMUP_URL = os.environ.get("WEBVOYAGER_NETWORK_WARMUP_URL", "https://example.com/")


class UnsupportedImplementation(RuntimeError):
    pass


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def timing_summary(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": statistics.mean(values) * 1000 if values else 0.0,
        "median_ms": statistics.median(values) * 1000 if values else 0.0,
        "p25_ms": percentile(values, 0.25) * 1000 if values else 0.0,
        "p75_ms": percentile(values, 0.75) * 1000 if values else 0.0,
        "min_ms": min(values) * 1000 if values else 0.0,
        "max_ms": max(values) * 1000 if values else 0.0,
        "stdev_ms": statistics.stdev(values) * 1000 if len(values) > 1 else 0.0,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise SystemExit(f"{path} is not a WebVoyager manifest with a tasks array")
    return data


def selected_tasks(manifest: dict[str, Any], percentage: float, seed: int, max_tasks: int | None) -> list[dict[str, Any]]:
    tasks = [task for task in manifest["tasks"] if isinstance(task, dict)]
    if not tasks:
        return []
    if percentage <= 0 or percentage > 100:
        raise SystemExit("--percentage must be greater than 0 and less than or equal to 100")
    sample_size = max(1, round(len(tasks) * percentage / 100))
    if max_tasks is not None:
        sample_size = min(sample_size, max_tasks)
    if sample_size >= len(tasks):
        return sorted(tasks, key=lambda item: str(item.get("task_id", "")))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        key = str(task.get("site") or task.get("domain") or "unknown")
        grouped.setdefault(key, []).append(task)
    rng = random.Random(seed)
    for group in grouped.values():
        rng.shuffle(group)
    sampled: list[dict[str, Any]] = []
    buckets = sorted(grouped)
    while len(sampled) < sample_size and buckets:
        next_buckets = []
        for bucket in buckets:
            group = grouped[bucket]
            if group and len(sampled) < sample_size:
                sampled.append(group.pop())
            if group:
                next_buckets.append(bucket)
        buckets = next_buckets
    return sorted(sampled, key=lambda item: str(item.get("task_id", "")))


def sample_digest(tasks: list[dict[str, Any]]) -> str:
    ids = [str(task.get("task_id", "")) for task in tasks]
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]


def benchmark_chromium_executable() -> str | None:
    for env_name in ("BENCHMARK_CHROMIUM_EXECUTABLE", "RUSTWRIGHT_CHROMIUM", "CHROME", "CHROMIUM"):
        value = os.environ.get(env_name)
        if value and Path(value).is_file():
            return value
    return None


def load_sync_playwright(implementation: str, reference_path: str | None) -> Callable:
    if implementation == "rustwright":
        from rustwright.sync_api import sync_playwright

        return sync_playwright
    if implementation == "playwright":
        if reference_path:
            path = str(Path(reference_path).resolve())
            if path not in sys.path:
                sys.path.insert(0, path)
        for name in list(sys.modules):
            if name == "playwright" or name.startswith("playwright."):
                del sys.modules[name]
        module = importlib.import_module("playwright.sync_api")
        module_path = Path(getattr(module, "__file__", "")).resolve()
        if ROOT / "python" in module_path.parents or "rustwright" in str(module_path):
            raise UnsupportedImplementation("real Python Playwright is unavailable; local alias is shadowing it")
        return module.sync_playwright
    raise UnsupportedImplementation(f"{implementation} is not a Python Playwright-style implementation")


def launch_chromium(playwright: Any) -> Any:
    options: dict[str, Any] = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    executable = benchmark_chromium_executable()
    if executable:
        options["executable_path"] = executable
    try:
        return playwright.chromium.launch(**options)
    except Exception as exc:
        message = str(exc)
        if "mach_port_rendezvous" not in message and "bootstrap_check_in" not in message:
            raise
        return playwright.chromium.launch(**{**options, "args": ["--single-process"]})


def is_browser_session_loss(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "cdp websocket is closed" in message
        or "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "context has been closed" in message
        or "timed out after 5000 ms" in message
    )


def close_quietly(target: Any) -> None:
    if target is None:
        return
    try:
        target.close()
    except Exception:
        return


def task_url(task: dict[str, Any]) -> str:
    value = task.get("start_url") or task.get("url") or task.get("web")
    return str(value or "")


def is_navigation_timeout(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "timeout" in message or "timed out" in message


def is_retryable_navigation_result(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "failed"
        and result.get("failure_kind") == "navigation_failure"
        and ("timeout" in str(result.get("detail", "")).lower() or "timed out" in str(result.get("detail", "")).lower())
    )


def page_evidence(page: Any) -> dict[str, Any]:
    script = """() => JSON.stringify({
  title: document.title || '',
  url: location.href,
  bodyTextLength: document.body ? (document.body.innerText || document.body.textContent || '').trim().length : 0
})"""
    if hasattr(page, "_evaluate_handle_with_timeout"):
        handle = page._evaluate_handle_with_timeout(script, timeout_ms=1000, method="Page.evaluate")
        try:
            evidence = handle.json_value()
        finally:
            try:
                handle.dispose()
            except Exception:
                pass
    else:
        evidence = page.evaluate(script)
    return json.loads(evidence)


def classify_loaded_page(
    parsed: dict[str, Any],
    response_status: int | None,
    response_url: str,
    *,
    warning_kind: str = "",
    warning_detail: str = "",
) -> dict[str, Any]:
    body_length = int(parsed.get("bodyTextLength") or 0)
    title = str(parsed.get("title") or "")
    if response_status is not None and int(response_status) >= 500:
        return {
            "status": "failed",
            "failure_kind": "server_error",
            "detail": f"HTTP {response_status}",
            "response_status": response_status,
            "response_url": response_url,
            "title": title,
            "body_text_length": body_length,
        }
    if not title and body_length == 0:
        return {
            "status": "failed",
            "failure_kind": "empty_page",
            "detail": "page loaded without title or body text",
            "response_status": response_status,
            "response_url": response_url,
            "title": title,
            "body_text_length": body_length,
        }
    return {
        "status": "passed",
        "failure_kind": "",
        "detail": "",
        "response_status": response_status,
        "response_url": response_url or str(parsed.get("url") or ""),
        "title": title,
        "body_text_length": body_length,
        "navigation_warning": warning_kind,
        "navigation_warning_detail": warning_detail,
    }


def run_task(page: Any, task: dict[str, Any], navigation_timeout: float) -> dict[str, Any]:
    url = task_url(task)
    if not url:
        return {"status": "skipped", "failure_kind": "missing_start_url", "detail": "task has no start_url"}
    response_status = None
    response_url = ""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout)
        if response is not None:
            response_status = getattr(response, "status", None)
            response_url = getattr(response, "url", "") or ""
        parsed = page_evidence(page)
    except Exception as exc:
        if is_navigation_timeout(exc):
            try:
                parsed = page_evidence(page)
                return classify_loaded_page(
                    parsed,
                    response_status,
                    response_url,
                    warning_kind="domcontentloaded_timeout_with_page_evidence",
                    warning_detail=str(exc),
                )
            except Exception:
                pass
        return {
            "status": "failed",
            "failure_kind": "navigation_failure",
            "detail": str(exc),
            "response_status": response_status,
            "response_url": response_url,
        }
    return classify_loaded_page(parsed, response_status, response_url)


def warm_browser_network(browser: Any, url: str, navigation_timeout: float) -> dict[str, Any]:
    if not url:
        return {"enabled": False, "status": "skipped", "url": ""}
    page = None
    started = time.perf_counter()
    try:
        page = browser.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout)
        evidence = page_evidence(page)
        return {
            "enabled": True,
            "status": "passed",
            "url": url,
            "duration_s": time.perf_counter() - started,
            "response_status": getattr(response, "status", None) if response is not None else None,
            "response_url": getattr(response, "url", "") if response is not None else str(evidence.get("url") or ""),
            "title": str(evidence.get("title") or ""),
            "body_text_length": int(evidence.get("bodyTextLength") or 0),
        }
    except Exception as exc:
        return {
            "enabled": True,
            "status": "failed",
            "url": url,
            "duration_s": time.perf_counter() - started,
            "failure_kind": "network_warmup_failure",
            "detail": str(exc),
        }
    finally:
        close_quietly(page)


def build_result(
    implementation: str,
    tasks: list[dict[str, Any]],
    iterations: int,
    task_runs: dict[str, list[dict[str, Any]]],
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    passed = 0
    failed = 0
    skipped = 0
    task_results = {}
    cases = {}
    for task in tasks:
        task_id = str(task.get("task_id"))
        runs = task_runs.get(task_id, [])
        passed_runs = sum(1 for run in runs if run["status"] == "passed")
        failed_runs = sum(1 for run in runs if run["status"] == "failed")
        skipped_runs = sum(1 for run in runs if run["status"] == "skipped")
        passed += passed_runs
        failed += failed_runs
        skipped += skipped_runs
        durations = [float(run["duration_s"]) for run in runs if run["status"] != "skipped"]
        task_results[task_id] = {
            "task_id": task_id,
            "site": task.get("site"),
            "start_url": task_url(task),
            "instruction": task.get("instruction"),
            "passed_runs": passed_runs,
            "failed_runs": failed_runs,
            "skipped_runs": skipped_runs,
            "runs": runs,
        }
        cases[task_id] = timing_summary(durations)
    metadata = {
        "suite": "webvoyager",
        "comparison_mode": "webvoyager_navigation_reliability",
        "case_count": len(tasks),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "browser_executable": benchmark_chromium_executable(),
    }
    metadata.update(extra_metadata or {})
    return {
        "implementation": implementation,
        "iterations": iterations,
        "metadata": metadata,
        "quality": {
            "task_count": len(tasks),
            "total_runs": len(tasks) * iterations,
            "passed_runs": passed,
            "failed_runs": failed,
            "skipped_runs": skipped,
            "success_rate": passed / (passed + failed) if passed + failed else 0.0,
        },
        "tasks": task_results,
        "cases": cases,
        "total_mean_ms": sum(case["mean_ms"] for case in cases.values()),
    }


def run_task_with_browser(
    playwright: Any,
    task: dict[str, Any],
    navigation_timeout: float,
    network_warmup_url: str,
    network_warmup_timeout: float,
) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    browser = launch_chromium(playwright)
    browser_version = getattr(browser, "version", None)
    network_warmup = warm_browser_network(browser, network_warmup_url, network_warmup_timeout)
    page = None
    try:
        page = browser.new_page()
        return run_task(page, task, navigation_timeout), browser_version, network_warmup
    finally:
        close_quietly(page)
        close_quietly(browser)


def run_python_like(
    implementation: str,
    tasks: list[dict[str, Any]],
    iterations: int,
    reference_path: str | None,
    navigation_timeout: float,
    network_warmup_url: str,
    network_warmup_timeout: float,
    browser_lifecycle: str,
    task_retries: int,
) -> dict[str, Any]:
    sync_playwright = load_sync_playwright(implementation, reference_path)
    task_runs: dict[str, list[dict[str, Any]]] = {str(task.get("task_id")): [] for task in tasks}
    browser_version = None
    recovery = {
        "browser_relaunches": 0,
        "task_retries_after_session_loss": 0,
        "task_retries_after_navigation_timeout": 0,
    }
    network_warmup: dict[str, Any] = {"enabled": False, "status": "skipped", "url": ""}
    network_warmups: list[dict[str, Any]] = []
    with sync_playwright() as p:
        if browser_lifecycle == "cold-browser":
            for _ in range(iterations):
                for task in tasks:
                    task_id = str(task.get("task_id"))
                    started = time.perf_counter()
                    session_retries = 0
                    navigation_retries = 0
                    while True:
                        try:
                            task_result, task_browser_version, task_network_warmup = run_task_with_browser(
                                p,
                                task,
                                navigation_timeout,
                                network_warmup_url,
                                network_warmup_timeout,
                            )
                            browser_version = task_browser_version or browser_version
                            network_warmup = task_network_warmup
                            network_warmups.append(task_network_warmup)
                            if navigation_retries < task_retries and is_retryable_navigation_result(task_result):
                                navigation_retries += 1
                                recovery["task_retries_after_navigation_timeout"] += 1
                                recovery["browser_relaunches"] += 1
                                continue
                            break
                        except Exception as exc:
                            if session_retries < 1 and is_browser_session_loss(exc):
                                session_retries += 1
                                recovery["task_retries_after_session_loss"] += 1
                                recovery["browser_relaunches"] += 1
                                continue
                            task_result = {
                                "status": "failed",
                                "failure_kind": "browser_session_lost" if is_browser_session_loss(exc) else "automation_failure",
                                "detail": str(exc),
                            }
                            break
                    duration = time.perf_counter() - started
                    task_runs[task_id].append(
                        {
                            **task_result,
                            "duration_s": duration,
                            "retries_after_session_loss": session_retries,
                            "retries_after_navigation_timeout": navigation_retries,
                        }
                    )
        else:
            browser = launch_chromium(p)
            browser_version = getattr(browser, "version", None)
            network_warmup = warm_browser_network(browser, network_warmup_url, network_warmup_timeout)
            network_warmups.append(network_warmup)
            try:
                for _ in range(iterations):
                    for task in tasks:
                        task_id = str(task.get("task_id"))
                        started = time.perf_counter()
                        session_retries = 0
                        navigation_retries = 0
                        while True:
                            page = None
                            try:
                                page = browser.new_page()
                                task_result = run_task(page, task, navigation_timeout)
                                if navigation_retries < task_retries and is_retryable_navigation_result(task_result):
                                    navigation_retries += 1
                                    recovery["task_retries_after_navigation_timeout"] += 1
                                    close_quietly(page)
                                    close_quietly(browser)
                                    browser = launch_chromium(p)
                                    browser_version = getattr(browser, "version", None) or browser_version
                                    network_warmups.append(
                                        warm_browser_network(browser, network_warmup_url, network_warmup_timeout)
                                    )
                                    recovery["browser_relaunches"] += 1
                                    continue
                                break
                            except Exception as exc:
                                if session_retries < 1 and is_browser_session_loss(exc):
                                    session_retries += 1
                                    recovery["task_retries_after_session_loss"] += 1
                                    close_quietly(page)
                                    close_quietly(browser)
                                    browser = launch_chromium(p)
                                    browser_version = getattr(browser, "version", None) or browser_version
                                    network_warmups.append(
                                        warm_browser_network(browser, network_warmup_url, network_warmup_timeout)
                                    )
                                    recovery["browser_relaunches"] += 1
                                    continue
                                task_result = {
                                    "status": "failed",
                                    "failure_kind": "browser_session_lost" if is_browser_session_loss(exc) else "automation_failure",
                                    "detail": str(exc),
                                }
                                break
                            finally:
                                close_quietly(page)
                        duration = time.perf_counter() - started
                        task_runs[task_id].append(
                            {
                                **task_result,
                                "duration_s": duration,
                                "retries_after_session_loss": session_retries,
                                "retries_after_navigation_timeout": navigation_retries,
                            }
                        )
            finally:
                close_quietly(browser)
    return build_result(
        implementation,
        tasks,
        iterations,
        task_runs,
        {
            "browser_version": browser_version,
            "browser_lifecycle": browser_lifecycle,
            "network_warmup": network_warmup,
            "network_warmup_count": len(network_warmups),
            "network_warmup_failures": sum(1 for item in network_warmups if item.get("status") == "failed"),
            **recovery,
        },
    )


def run_impl(args: argparse.Namespace, implementation: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    if implementation in {"rustwright", "playwright"}:
        return run_python_like(
            implementation,
            tasks,
            args.iterations,
            args.reference_path,
            args.navigation_timeout,
            args.network_warmup_url,
            args.network_warmup_timeout,
            args.browser_lifecycle,
            args.task_retries,
        )
    raise UnsupportedImplementation(f"{implementation} is not yet supported by the WebVoyager runner")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run imported WebVoyager tasks as a navigation reliability benchmark.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--impl", choices=[*DEFAULT_IMPLS, "all"], default="rustwright")
    parser.add_argument("--percentage", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=int(os.environ.get("WEBVOYAGER_ITERATIONS", "1")))
    parser.add_argument("--navigation-timeout", type=float, default=float(os.environ.get("WEBVOYAGER_NAVIGATION_TIMEOUT", "15000")))
    parser.add_argument("--network-warmup-url", default=DEFAULT_NETWORK_WARMUP_URL)
    parser.add_argument("--network-warmup-timeout", type=float, default=float(os.environ.get("WEBVOYAGER_NETWORK_WARMUP_TIMEOUT", "10000")))
    parser.add_argument("--browser-lifecycle", choices=["warm-browser", "cold-browser"], default=os.environ.get("WEBVOYAGER_BROWSER_LIFECYCLE", "cold-browser"))
    parser.add_argument("--task-retries", type=int, default=int(os.environ.get("WEBVOYAGER_TASK_RETRIES", "1")))
    parser.add_argument("--reference-path", default=str(ROOT / ".audit-playwright"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    tasks = selected_tasks(manifest, args.percentage, args.seed, args.max_tasks)
    selection = {
        "manifest": str(args.manifest),
        "manifest_task_count": len(manifest.get("tasks", [])),
        "percentage": args.percentage,
        "seed": args.seed,
        "max_tasks": args.max_tasks,
        "selected_task_count": len(tasks),
        "selected_task_ids": [str(task.get("task_id")) for task in tasks],
        "selection_digest": sample_digest(tasks),
    }
    implementations = DEFAULT_IMPLS if args.impl == "all" else [args.impl]
    results = []
    for implementation in implementations:
        try:
            result = run_impl(args, implementation, tasks)
            result["selection"] = selection
            result["status"] = "passed"
        except UnsupportedImplementation as exc:
            result = {"implementation": implementation, "status": "skipped", "reason": str(exc), "selection": selection}
        results.append(result)
    output = {"suite": "webvoyager", "comparison_mode": "webvoyager_navigation_reliability", "results": results}
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        for result in results:
            quality = result.get("quality") or {}
            print(
                f"{result['implementation']}: {result.get('status')} "
                f"{quality.get('passed_runs', 0)}/{quality.get('total_runs', 0)}"
            )
    failed = [item for item in results if item.get("status") == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
