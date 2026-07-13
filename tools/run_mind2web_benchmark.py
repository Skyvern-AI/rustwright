#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / ".benchmark-data" / "manifests" / "mind2web_tasks.json"
DEFAULT_IMPLS = ["rustwright-py", "playwright", "rustwright-ts", "typescript-playwright", "typescript-puppeteer"]
EXPERIMENTAL_IMPLS = ["rustwright-ts-cdp"]
LEGACY_IMPL_ALIASES = {
    "rustwright": "rustwright-py",
    "typescript-rustwright-binding": "rustwright-ts",
    "typescript-rustwright-cdp": "rustwright-ts-cdp",
}


def canonical_impl(implementation: str) -> str:
    return LEGACY_IMPL_ALIASES.get(implementation, implementation)


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


def phase_timing_summary(task_runs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    phase_totals: dict[str, float] = {}
    phase_run_values: dict[str, list[float]] = {}
    measured_runs = 0
    for runs in task_runs.values():
        for run in runs:
            phase_s = run.get("phase_s")
            if not isinstance(phase_s, dict):
                continue
            measured_runs += 1
            for name, value in phase_s.items():
                try:
                    seconds = float(value)
                except (TypeError, ValueError):
                    continue
                phase_totals[name] = phase_totals.get(name, 0.0) + seconds
                phase_run_values.setdefault(name, []).append(seconds)
    total_s = sum(phase_totals.values())
    phases = {}
    for name, seconds in sorted(phase_totals.items()):
        phases[name] = {
            "total_ms": seconds * 1000,
            "share": seconds / total_s if total_s else 0.0,
            "per_task": timing_summary(phase_run_values.get(name, [])),
        }
    return {"measured_runs": measured_runs, "total_measured_ms": total_s * 1000, "phases": phases}


def benchmark_chromium_executable() -> str | None:
    for env_name in ("BENCHMARK_CHROMIUM_EXECUTABLE", "RUSTWRIGHT_CHROMIUM", "CHROME", "CHROMIUM"):
        value = os.environ.get(env_name)
        if value and Path(value).is_file():
            return value
    return None


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise SystemExit(f"{path} is not a Mind2Web manifest with a tasks array")
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
        key = str(task.get("domain") or task.get("website") or task.get("split") or "unknown")
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


def load_sync_playwright(implementation: str, reference_path: str | None) -> Callable:
    implementation = canonical_impl(implementation)
    if implementation == "rustwright-py":
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


def launch_chromium(playwright):
    options: dict[str, Any] = {"headless": True}
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


def is_replay_infrastructure_failure(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "timed out waiting for set_content domcontentloaded" in message
        or ("timed out after " in message and " ms" in message)
        or "page.set_content: execution context was destroyed" in message
        or "page.evaluate: timed out" in message
        or "execution context was destroyed" in message
    )


def close_quietly(target: Any) -> None:
    if target is None:
        return
    try:
        target.close()
    except Exception:
        return


BROWSER_EVALUATOR = r"""
(fixture) => {
  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }
  function norm(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }
  function queries(candidate) {
    const attrs = candidate || {};
    const values = [];
    function add(selector) { if (selector && !values.includes(selector)) values.push(selector); }
    const id = attrs.id || attrs["attributes.id"];
    if (id) add("#" + cssEscape(id));
    const backendNodeId = attrs.backend_node_id || attrs.backendNodeId || attrs["attributes.backend_node_id"];
    if (backendNodeId) add("[backend_node_id=" + JSON.stringify(String(backendNodeId)) + "]");
    const nodeId = attrs.node_id || attrs.nodeId || attrs["attributes.node_id"];
    if (nodeId) add("[node_id=" + JSON.stringify(String(nodeId)) + "]");
    for (const key of ["data-testid", "data-test", "data-cy", "name", "aria-label", "placeholder", "title"]) {
      const value = attrs[key] || attrs["attributes." + key];
      if (value) add("[" + key + "=" + JSON.stringify(String(value)) + "]");
    }
    const tag = (attrs.tag || attrs.tag_name || "").toLowerCase();
    if (tag && id) add(tag + "#" + cssEscape(id));
    return values;
  }
  function byCandidate(candidate) {
    for (const selector of queries(candidate)) {
      try {
        const node = document.querySelector(selector);
        if (node) return node;
      } catch (_) {}
    }
    const text = norm(candidate && (candidate.text || candidate.inner_text || candidate.value));
    if (text) {
      const lowered = text.toLowerCase();
      const nodes = Array.from(document.querySelectorAll("button,a,input,textarea,select,[role],label,[onclick],[backend_node_id],[node_id]"));
      const found = nodes.find(node => {
        const haystack = norm(node.innerText || node.textContent || node.getAttribute("aria-label") || node.getAttribute("placeholder") || node.value).toLowerCase();
        return haystack && (haystack === lowered || haystack.includes(lowered));
      });
      if (found) return found;
    }
    return null;
  }
  function target() {
    for (const candidate of fixture.candidates || []) {
      const node = byCandidate(candidate);
      if (node) return node;
    }
    return document.querySelector("button,a,input,textarea,select,[role=button],label");
  }
  function actionTarget(node) {
    if (!node) return null;
    const actionableSelector = "button,a,input,textarea,select,[role=button],label,[onclick]";
    if (node.matches && node.matches(actionableSelector)) return node;
    if (node.closest) {
      const closest = node.closest(actionableSelector);
      if (closest) return closest;
    }
    let current = node.parentElement;
    while (current) {
      if (current.matches && current.matches(actionableSelector)) return current;
      current = current.parentElement;
    }
    return node;
  }
  function fire(node, type) {
    node.dispatchEvent(new Event(type, {bubbles: true}));
  }
  function clickLikeUser(node) {
    const target = actionTarget(node);
    if (!target) return false;
    if (typeof target.click === "function") {
      target.click();
      return true;
    }
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
    }
    return true;
  }
  const node = target();
  if (!node) return {ok: false, failure_kind: "target_not_found", detail: "no candidate matched"};
  const op = String(fixture.operation || "").toUpperCase();
  const value = fixture.value || "mind2web";
  try {
    if (op.includes("TYPE") || op.includes("TEXT") || op.includes("INPUT")) {
      if ("value" in node) {
        node.focus();
        node.value = value;
        fire(node, "input");
        fire(node, "change");
      } else {
        node.textContent = value;
        fire(node, "input");
      }
    } else if (op.includes("SELECT")) {
      if (node.tagName === "SELECT") {
        const option = Array.from(node.options).find(item => item.value === value || item.textContent.trim() === value) || node.options[0];
        if (!option) return {ok: false, failure_kind: "unsupported_action", detail: "select has no options"};
        node.value = option.value;
        fire(node, "input");
        fire(node, "change");
      } else {
        if (!clickLikeUser(node)) return {ok: false, failure_kind: "unsupported_action", detail: "node cannot be clicked"};
      }
    } else if (op.includes("CHECK")) {
      if ("checked" in node && !node.checked) clickLikeUser(node);
      else if (!clickLikeUser(node)) return {ok: false, failure_kind: "unsupported_action", detail: "node cannot be clicked"};
    } else {
      if (!clickLikeUser(node)) return {ok: false, failure_kind: "unsupported_action", detail: "node cannot be clicked"};
    }
  } catch (error) {
    return {ok: false, failure_kind: "automation_failure", detail: String(error && error.message ? error.message : error)};
  }
  return {ok: true, failure_kind: "", detail: "", tag: node.tagName};
}
"""


def python_execute_fixture(
    page: Any,
    fixture: dict[str, Any],
    fixture_timeout_ms: int,
    fixture_wait_until: str,
) -> tuple[dict[str, Any], dict[str, float]]:
    set_content_started = time.perf_counter()
    page.set_content(fixture["html"], wait_until=fixture_wait_until, timeout=fixture_timeout_ms)
    set_content_s = time.perf_counter() - set_content_started
    compact_fixture = {key: value for key, value in fixture.items() if key != "html"}
    evaluate_started = time.perf_counter()
    result_json = page.evaluate(f"() => JSON.stringify(({BROWSER_EVALUATOR})({json.dumps(compact_fixture)}))")
    evaluate_s = time.perf_counter() - evaluate_started
    return json.loads(result_json), {"set_content_s": set_content_s, "evaluate_s": evaluate_s}


def task_fixtures(task: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = task.get("action_fixtures")
    if not isinstance(fixtures, list):
        return []
    return [fixture for fixture in fixtures if isinstance(fixture, dict) and fixture.get("html")]


def run_python_like(
    implementation: str,
    tasks: list[dict[str, Any]],
    iterations: int,
    reference_path: str | None,
    fixture_timeout_ms: int,
    fixture_wait_until: str,
    max_task_seconds: float | None,
    progress_every: int,
) -> dict[str, Any]:
    sync_playwright = load_sync_playwright(implementation, reference_path)
    task_runs: dict[str, list[dict[str, Any]]] = {str(task.get("task_id")): [] for task in tasks}
    browser_version = None
    recovery = {"browser_relaunches": 0, "task_retries_after_session_loss": 0, "task_retries_after_replay_timeout": 0}
    completed_task_runs = 0
    total_task_runs = len(tasks) * iterations
    with sync_playwright() as p:
        browser = launch_chromium(p)
        browser_version = getattr(browser, "version", None)
        try:
            for iteration_index in range(iterations):
                for task_index, task in enumerate(tasks, start=1):
                    task_id = str(task.get("task_id"))
                    fixtures = task_fixtures(task)
                    if not fixtures:
                        task_runs[task_id].append(
                            {"status": "skipped", "failure_kind": "no_action_fixtures", "duration_s": 0.0}
                        )
                        completed_task_runs += 1
                        if progress_every > 0 and completed_task_runs % progress_every == 0:
                            print(
                                f"[mind2web:{implementation}] {completed_task_runs}/{total_task_runs} task-runs complete "
                                f"(iteration {iteration_index + 1}/{iterations}, task {task_index}/{len(tasks)})",
                                file=sys.stderr,
                                flush=True,
                            )
                        continue
                    started = time.perf_counter()
                    status = "passed"
                    failure_kind = ""
                    detail = ""
                    completed_actions = 0
                    attempts = 0
                    replay_attempts = 0
                    phase_s = {
                        "page_create_s": 0.0,
                        "set_content_s": 0.0,
                        "evaluate_s": 0.0,
                        "page_close_s": 0.0,
                    }
                    while True:
                        page = None
                        attempt_started = time.perf_counter()
                        try:
                            page_create_started = time.perf_counter()
                            page = browser.new_page()
                            phase_s["page_create_s"] += time.perf_counter() - page_create_started
                            completed_actions = 0
                            for fixture in fixtures:
                                if max_task_seconds is not None and time.perf_counter() - attempt_started > max_task_seconds:
                                    status = "failed"
                                    failure_kind = "task_timeout"
                                    detail = f"task exceeded {max_task_seconds:.3f}s before action {completed_actions + 1}/{len(fixtures)}"
                                    break
                                result, fixture_phase_s = python_execute_fixture(page, fixture, fixture_timeout_ms, fixture_wait_until)
                                phase_s["set_content_s"] += fixture_phase_s["set_content_s"]
                                phase_s["evaluate_s"] += fixture_phase_s["evaluate_s"]
                                if not result.get("ok"):
                                    status = "failed"
                                    failure_kind = str(result.get("failure_kind") or "quality_failure")
                                    detail = str(result.get("detail") or "")
                                    break
                                completed_actions += 1
                            break
                        except Exception as exc:
                            if attempts < 1 and is_browser_session_loss(exc):
                                attempts += 1
                                recovery["task_retries_after_session_loss"] += 1
                                close_quietly(page)
                                close_quietly(browser)
                                browser = launch_chromium(p)
                                browser_version = getattr(browser, "version", None) or browser_version
                                recovery["browser_relaunches"] += 1
                                continue
                            if replay_attempts < 1 and is_replay_infrastructure_failure(exc):
                                replay_attempts += 1
                                recovery["task_retries_after_replay_timeout"] += 1
                                close_quietly(page)
                                continue
                            status = "failed"
                            failure_kind = "browser_session_lost" if is_browser_session_loss(exc) else "automation_failure"
                            detail = str(exc)
                            break
                        finally:
                            page_close_started = time.perf_counter()
                            close_quietly(page)
                            phase_s["page_close_s"] += time.perf_counter() - page_close_started
                    duration = time.perf_counter() - started
                    task_runs[task_id].append(
                        {
                            "status": status,
                            "failure_kind": failure_kind,
                            "detail": detail,
                            "duration_s": duration,
                            "completed_actions": completed_actions,
                            "action_count": len(fixtures),
                            "retries_after_session_loss": attempts,
                            "retries_after_replay_timeout": replay_attempts,
                            "phase_s": phase_s,
                        }
                    )
                    completed_task_runs += 1
                    if progress_every > 0 and completed_task_runs % progress_every == 0:
                        print(
                            f"[mind2web:{implementation}] {completed_task_runs}/{total_task_runs} task-runs complete "
                            f"(iteration {iteration_index + 1}/{iterations}, task {task_index}/{len(tasks)}, "
                            f"last={status}, actions={completed_actions}/{len(fixtures)}, {duration:.2f}s)",
                            file=sys.stderr,
                            flush=True,
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
            "fixture_timeout_ms": fixture_timeout_ms,
            "fixture_wait_until": fixture_wait_until,
            "max_task_seconds": max_task_seconds,
            "progress_every": progress_every,
            **recovery,
        },
    )


def find_typescript_playwright_reference(reference_path: str | None) -> tuple[str, str]:
    candidates = []
    if reference_path:
        candidates.append(Path(reference_path).resolve())
    candidates.append((ROOT / ".audit-playwright").resolve())
    for base in candidates:
        node = base / "playwright" / "driver" / "node"
        package = base / "playwright" / "driver" / "package"
        if node.is_file() and (package / "package.json").is_file():
            return str(node), str(package)
    node = shutil.which("node")
    if node:
        raise UnsupportedImplementation("Node exists, but bundled TypeScript Playwright package was not found")
    raise UnsupportedImplementation("Node/TypeScript Playwright reference is unavailable")


def find_node_executable(reference_path: str | None) -> str:
    try:
        node, _ = find_typescript_playwright_reference(reference_path)
        return node
    except UnsupportedImplementation:
        found = shutil.which("node")
        if found:
            return found
        raise


def find_puppeteer_package() -> str:
    for candidate in [
        Path(os.environ.get("PUPPETEER_PACKAGE_PATH", "")),
        ROOT / "node_modules" / "puppeteer-core",
        Path("/workspace/node_modules/puppeteer-core"),
        Path("/opt/puppeteer-benchmark/node_modules/puppeteer-core"),
    ]:
        if candidate and (candidate / "package.json").is_file():
            return str(candidate)
    raise UnsupportedImplementation("puppeteer-core is unavailable; rebuild Docker with INSTALL_PUPPETEER=1")


def node_task_chunks(tasks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    max_bytes = int(os.environ.get("MIND2WEB_NODE_CHUNK_BYTES", str(180 * 1024 * 1024)))
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for task in tasks:
        task_bytes = len(json.dumps(task, separators=(",", ":")).encode("utf-8"))
        if current and current_bytes + task_bytes > max_bytes:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(task)
        current_bytes += task_bytes
    if current:
        chunks.append(current)
    return chunks


def merge_node_chunk_results(implementation: str, iterations: int, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    if not chunks:
        return build_result(implementation, [], iterations, {}, {})
    tasks: dict[str, Any] = {}
    cases: dict[str, Any] = {}
    passed = failed = skipped = 0
    total_mean_ms = 0.0
    metadata = dict(chunks[0].get("metadata") or {})
    metadata["node_payload_chunks"] = len(chunks)
    for chunk in chunks:
        quality = chunk.get("quality") or {}
        passed += int(quality.get("passed_runs") or 0)
        failed += int(quality.get("failed_runs") or 0)
        skipped += int(quality.get("skipped_runs") or 0)
        tasks.update(chunk.get("tasks") or {})
        cases.update(chunk.get("cases") or {})
        total_mean_ms += float(chunk.get("total_mean_ms") or 0.0)
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
        "tasks": tasks,
        "cases": cases,
        "total_mean_ms": total_mean_ms,
    }


def run_node_adapter(
    implementation: str,
    tasks: list[dict[str, Any]],
    iterations: int,
    reference_path: str | None,
) -> dict[str, Any]:
    implementation = canonical_impl(implementation)
    if implementation == "typescript-playwright":
        node, package = find_typescript_playwright_reference(reference_path)
        env = {**os.environ, "PLAYWRIGHT_TS_PACKAGE": package}
        script = node_adapter_code("playwright")
    elif implementation == "typescript-puppeteer":
        node = find_node_executable(reference_path)
        package = find_puppeteer_package()
        executable = benchmark_chromium_executable()
        if not executable:
            raise UnsupportedImplementation("no Chromium executable was found for Puppeteer")
        env = {**os.environ, "PUPPETEER_PACKAGE_PATH": package, "PUPPETEER_EXECUTABLE_PATH": executable}
        script = node_adapter_code("puppeteer")
    elif implementation == "rustwright-ts-cdp":
        node = find_node_executable(reference_path)
        executable = benchmark_chromium_executable()
        if not executable:
            raise UnsupportedImplementation("no Chromium executable was found for TypeScript raw-CDP benchmark")
        env = {**os.environ, "RUSTWRIGHT_CDP_EXECUTABLE_PATH": executable}
        script = node_raw_cdp_adapter_code()
    elif implementation == "rustwright-ts":
        node = find_node_executable(reference_path)
        executable = benchmark_chromium_executable()
        if not executable:
            raise UnsupportedImplementation("no Chromium executable was found for TypeScript Rustwright binding benchmark")
        env = {
            **os.environ,
            "RUSTWRIGHT_TS_BINDING_PATH": str(ROOT / "tools" / "rustwright_ts_binding.js"),
            "RUSTWRIGHT_TS_EXECUTABLE_PATH": executable,
        }
        script = node_adapter_code("rustwright-binding")
    else:
        raise UnsupportedImplementation(f"{implementation} has no Node adapter")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as script_file:
        script_file.write(script)
        script_path = script_file.name
    chunk_results: list[dict[str, Any]] = []
    try:
        for chunk in node_task_chunks(tasks):
            payload = {"implementation": implementation, "tasks": chunk, "iterations": iterations}
            with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as payload_file:
                json.dump(payload, payload_file)
                payload_path = payload_file.name
            try:
                proc = subprocess.run(
                    [node, script_path, payload_path],
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=max(120, len(chunk) * iterations * 10),
                )
            finally:
                Path(payload_path).unlink(missing_ok=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr or proc.stdout)
            for line in reversed(proc.stdout.splitlines()):
                if line.startswith("MIND2WEB_JSON "):
                    chunk_results.append(json.loads(line.removeprefix("MIND2WEB_JSON ")))
                    break
            else:
                raise RuntimeError(f"{implementation} did not print Mind2Web JSON:\n{proc.stdout}")
    finally:
        Path(script_path).unlink(missing_ok=True)
    return merge_node_chunk_results(implementation, iterations, chunk_results)


def node_adapter_code(kind: str) -> str:
    if kind == "playwright":
        launcher = "const { chromium } = require(process.env.PLAYWRIGHT_TS_PACKAGE);"
        launch = "const browser = await chromium.launch({ headless: true, ...(executable ? { executablePath: executable } : {}) });"
    elif kind == "rustwright-binding":
        launcher = "const { chromium } = require(process.env.RUSTWRIGHT_TS_BINDING_PATH);"
        launch = "const browser = await chromium.launch({ executablePath: executable });"
    else:
        launcher = "const puppeteer = require(process.env.PUPPETEER_PACKAGE_PATH);"
        launch = "const browser = await puppeteer.launch({ headless: true, executablePath: process.env.PUPPETEER_EXECUTABLE_PATH, args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check'] });"
    set_content = (
        "await page.setContent(fixture.html, { waitUntil: 'domcontentloaded', timeout: 5000 });"
        if kind == "puppeteer"
        else "await page.setContent(fixture.html, { waitUntil: 'domcontentloaded', timeout: 5000 });"
    )
    new_page = "await browser.newPage();"
    close_page = "await withTimeout(page.close().catch(() => {}), 3000, 'page.close').catch(() => {});"
    return textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const {{ performance }} = require('node:perf_hooks');
        {launcher}
        const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
        const executable = process.env.BENCHMARK_CHROMIUM_EXECUTABLE || process.env.RUSTWRIGHT_CHROMIUM || process.env.CHROME || process.env.CHROMIUM || null;
        const evaluator = {BROWSER_EVALUATOR};
        function fixtures(task) {{
          return Array.isArray(task.action_fixtures) ? task.action_fixtures.filter(item => item && item.html) : [];
        }}
        function withTimeout(promise, ms, label) {{
          let timer;
          const timeout = new Promise((_, reject) => {{
            timer = setTimeout(() => reject(new Error(label + ' timed out after ' + ms + 'ms')), ms);
          }});
          return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
        }}
        function buildResult(taskRuns, browserVersion) {{
          const tasks = {{}};
          const cases = {{}};
          let passed = 0, failed = 0, skipped = 0;
          for (const task of payload.tasks) {{
            const taskId = String(task.task_id);
            const runs = taskRuns[taskId] || [];
            const durations = runs.filter(run => run.status !== 'skipped').map(run => run.duration_s);
            const statuses = runs.map(run => run.status);
            const passedRuns = statuses.filter(status => status === 'passed').length;
            const failedRuns = statuses.filter(status => status === 'failed').length;
            const skippedRuns = statuses.filter(status => status === 'skipped').length;
            passed += passedRuns; failed += failedRuns; skipped += skippedRuns;
            tasks[taskId] = {{ task_id: taskId, runs, passed_runs: passedRuns, failed_runs: failedRuns, skipped_runs: skippedRuns }};
            const ms = durations.map(value => value * 1000).sort((a, b) => a - b);
            const mean = ms.length ? ms.reduce((a, b) => a + b, 0) / ms.length : 0;
            cases[taskId] = {{
              mean_ms: mean,
              median_ms: ms.length ? ms[Math.floor(ms.length / 2)] : 0,
              p25_ms: ms.length ? ms[Math.floor((ms.length - 1) * 0.25)] : 0,
              p75_ms: ms.length ? ms[Math.floor((ms.length - 1) * 0.75)] : 0,
              min_ms: ms.length ? ms[0] : 0,
              max_ms: ms.length ? ms[ms.length - 1] : 0,
              stdev_ms: 0,
            }};
          }}
          return {{
            implementation: payload.implementation,
            iterations: payload.iterations,
            metadata: {{
              suite: 'mind2web',
              comparison_mode: 'mind2web_offline_action_replay',
              case_count: payload.tasks.length,
              browser_version: browserVersion,
              node: process.version,
              platform: process.platform,
              arch: process.arch,
              browser_executable: executable || process.env.PUPPETEER_EXECUTABLE_PATH || null,
            }},
            quality: {{
              task_count: payload.tasks.length,
              total_runs: payload.tasks.length * payload.iterations,
              passed_runs: passed,
              failed_runs: failed,
              skipped_runs: skipped,
              success_rate: passed + failed ? passed / (passed + failed) : 0,
            }},
            tasks,
            cases,
            total_mean_ms: Object.values(cases).reduce((total, item) => total + item.mean_ms, 0),
          }};
        }}
        (async () => {{
          {launch}
          const browserVersion = await browser.version();
          const taskRuns = Object.fromEntries(payload.tasks.map(task => [String(task.task_id), []]));
          try {{
            for (let iteration = 0; iteration < payload.iterations; iteration++) {{
              for (const task of payload.tasks) {{
                const taskId = String(task.task_id);
                const taskFixtures = fixtures(task);
                if (!taskFixtures.length) {{
                  taskRuns[taskId].push({{ status: 'skipped', failure_kind: 'no_action_fixtures', duration_s: 0 }});
                  continue;
                }}
                const page = {new_page}
                const started = performance.now();
                let status = 'passed', failure_kind = '', detail = '', completed_actions = 0;
                try {{
                  for (const fixture of taskFixtures) {{
                    await withTimeout((async () => {{
                      {set_content}
                    }})(), 8000, 'setContent');
                    const compactFixture = {{ ...fixture }};
                    delete compactFixture.html;
                    const result = await withTimeout(page.evaluate(evaluator, compactFixture), 3000, 'evaluate');
                    if (!result.ok) {{
                      status = 'failed';
                      failure_kind = result.failure_kind || 'quality_failure';
                      detail = result.detail || '';
                      break;
                    }}
                    completed_actions++;
                  }}
                }} catch (error) {{
                  status = 'failed';
                  failure_kind = 'automation_failure';
                  detail = String(error && error.message ? error.message : error);
                }} finally {{
                  const duration_s = (performance.now() - started) / 1000;
                  {close_page}
                  taskRuns[taskId].push({{ status, failure_kind, detail, duration_s, completed_actions, action_count: taskFixtures.length }});
                }}
              }}
            }}
          }} finally {{
            await withTimeout(browser.close().catch(() => {{}}), 5000, 'browser.close').catch(() => {{}});
          }}
          console.log('MIND2WEB_JSON ' + JSON.stringify(buildResult(taskRuns, browserVersion)));
        }})().catch(error => {{
          console.error(error && error.stack ? error.stack : error);
          process.exit(1);
        }});
        """
    )


def node_raw_cdp_adapter_code() -> str:
    evaluator = json.dumps(BROWSER_EVALUATOR)
    return textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const os = require('node:os');
        const path = require('node:path');
        const net = require('node:net');
        const {{ spawn }} = require('node:child_process');
        const {{ performance }} = require('node:perf_hooks');

        const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
        const executable = process.env.RUSTWRIGHT_CDP_EXECUTABLE_PATH;
        const evaluatorSource = {evaluator};

        function fixtures(task) {{
          return Array.isArray(task.action_fixtures) ? task.action_fixtures.filter(item => item && item.html) : [];
        }}

        function withTimeout(promise, ms, label) {{
          let timer;
          const timeout = new Promise((_, reject) => {{
            timer = setTimeout(() => reject(new Error(label + ' timed out after ' + ms + 'ms')), ms);
          }});
          return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
        }}

        function pickPort() {{
          return new Promise((resolve, reject) => {{
            const server = net.createServer();
            server.on('error', reject);
            server.listen(0, '127.0.0.1', () => {{
              const port = server.address().port;
              server.close(() => resolve(port));
            }});
          }});
        }}

        async function waitForJson(url, ms) {{
          const deadline = performance.now() + ms;
          let lastError;
          while (performance.now() < deadline) {{
            try {{
              const response = await fetch(url);
              if (response.ok) return await response.json();
              lastError = new Error('HTTP ' + response.status);
            }} catch (error) {{
              lastError = error;
            }}
            await new Promise(resolve => setTimeout(resolve, 50));
          }}
          throw lastError || new Error('timed out waiting for ' + url);
        }}

        class Cdp {{
          constructor(ws) {{
            this.ws = ws;
            this.nextId = 1;
            this.pending = new Map();
            ws.addEventListener('message', event => {{
              const message = JSON.parse(event.data);
              if (message.id && this.pending.has(message.id)) {{
                const {{ resolve, reject }} = this.pending.get(message.id);
                this.pending.delete(message.id);
                if (message.error) reject(new Error(message.error.message || JSON.stringify(message.error)));
                else resolve(message.result || {{}});
              }}
            }});
            ws.addEventListener('close', () => {{
              for (const {{ reject }} of this.pending.values()) reject(new Error('CDP websocket closed'));
              this.pending.clear();
            }});
          }}
          send(method, params = {{}}, sessionId = undefined) {{
            const id = this.nextId++;
            const message = {{ id, method, params }};
            if (sessionId) message.sessionId = sessionId;
            return new Promise((resolve, reject) => {{
              this.pending.set(id, {{ resolve, reject }});
              this.ws.send(JSON.stringify(message));
            }});
          }}
          close() {{
            try {{ this.ws.close(); }} catch (_) {{}}
          }}
        }}

        async function connect(wsUrl) {{
          const ws = new WebSocket(wsUrl);
          await new Promise((resolve, reject) => {{
            const timer = setTimeout(() => reject(new Error('websocket connect timed out')), 5000);
            ws.addEventListener('open', () => {{ clearTimeout(timer); resolve(); }}, {{ once: true }});
            ws.addEventListener('error', event => {{ clearTimeout(timer); reject(event.error || new Error('websocket error')); }}, {{ once: true }});
          }});
          return new Cdp(ws);
        }}

        async function newPage(cdp) {{
          const target = await cdp.send('Target.createTarget', {{ url: 'about:blank' }});
          const attached = await cdp.send('Target.attachToTarget', {{ targetId: target.targetId, flatten: true }});
          const sessionId = attached.sessionId;
          await cdp.send('Page.enable', {{}}, sessionId);
          await cdp.send('Runtime.enable', {{}}, sessionId);
          return {{ targetId: target.targetId, sessionId }};
        }}

        async function closePage(cdp, page) {{
          if (!page) return;
          try {{ await cdp.send('Target.closeTarget', {{ targetId: page.targetId }}); }} catch (_) {{}}
        }}

        async function setContent(cdp, page, html) {{
          const tree = await cdp.send('Page.getFrameTree', {{}}, page.sessionId);
          const frameId = tree.frameTree && tree.frameTree.frame && tree.frameTree.frame.id;
          if (!frameId) throw new Error('Page.getFrameTree did not return a main frame');
          await cdp.send('Page.setDocumentContent', {{ frameId, html }}, page.sessionId);
        }}

        async function evaluateFixture(cdp, page, fixture) {{
          const compactFixture = {{ ...fixture }};
          delete compactFixture.html;
          const expression = '(' + evaluatorSource + ')(' + JSON.stringify(compactFixture) + ')';
          const evaluated = await cdp.send('Runtime.evaluate', {{
            expression,
            awaitPromise: true,
            returnByValue: true,
            timeout: 3000,
          }}, page.sessionId);
          if (evaluated.exceptionDetails) {{
            throw new Error(evaluated.exceptionDetails.text || 'Runtime.evaluate failed');
          }}
          return evaluated.result ? evaluated.result.value : undefined;
        }}

        function buildResult(taskRuns, browserVersion) {{
          const tasks = {{}};
          const cases = {{}};
          let passed = 0, failed = 0, skipped = 0;
          for (const task of payload.tasks) {{
            const taskId = String(task.task_id);
            const runs = taskRuns[taskId] || [];
            const durations = runs.filter(run => run.status !== 'skipped').map(run => run.duration_s);
            const statuses = runs.map(run => run.status);
            const passedRuns = statuses.filter(status => status === 'passed').length;
            const failedRuns = statuses.filter(status => status === 'failed').length;
            const skippedRuns = statuses.filter(status => status === 'skipped').length;
            passed += passedRuns; failed += failedRuns; skipped += skippedRuns;
            tasks[taskId] = {{ task_id: taskId, runs, passed_runs: passedRuns, failed_runs: failedRuns, skipped_runs: skippedRuns }};
            const ms = durations.map(value => value * 1000).sort((a, b) => a - b);
            const mean = ms.length ? ms.reduce((a, b) => a + b, 0) / ms.length : 0;
            cases[taskId] = {{
              mean_ms: mean,
              median_ms: ms.length ? ms[Math.floor(ms.length / 2)] : 0,
              p25_ms: ms.length ? ms[Math.floor((ms.length - 1) * 0.25)] : 0,
              p75_ms: ms.length ? ms[Math.floor((ms.length - 1) * 0.75)] : 0,
              min_ms: ms.length ? ms[0] : 0,
              max_ms: ms.length ? ms[ms.length - 1] : 0,
              stdev_ms: 0,
            }};
          }}
          return {{
            implementation: payload.implementation,
            iterations: payload.iterations,
            metadata: {{
              suite: 'mind2web',
              comparison_mode: 'mind2web_offline_action_replay',
              adapter_kind: 'experimental_node_raw_cdp_rustwright_semantics',
              caveat: 'This is not a production TypeScript Rustwright binding; it measures Node orchestration over direct CDP.',
              case_count: payload.tasks.length,
              browser_version: browserVersion,
              node: process.version,
              platform: process.platform,
              arch: process.arch,
              browser_executable: executable,
            }},
            quality: {{
              task_count: payload.tasks.length,
              total_runs: payload.tasks.length * payload.iterations,
              passed_runs: passed,
              failed_runs: failed,
              skipped_runs: skipped,
              success_rate: passed + failed ? passed / (passed + failed) : 0,
            }},
            tasks,
            cases,
            total_mean_ms: Object.values(cases).reduce((total, item) => total + item.mean_ms, 0),
          }};
        }}

        (async () => {{
          if (typeof WebSocket !== 'function') throw new Error('Node runtime does not expose global WebSocket');
          const port = await pickPort();
          const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'rustwright-ts-cdp-'));
          const browser = spawn(executable, [
            '--remote-debugging-port=' + port,
            '--user-data-dir=' + profile,
            '--headless=new',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--no-first-run',
            '--no-default-browser-check',
            'about:blank',
          ], {{ stdio: 'ignore' }});
          const taskRuns = Object.fromEntries(payload.tasks.map(task => [String(task.task_id), []]));
          let cdp = null;
          try {{
            const version = await waitForJson('http://127.0.0.1:' + port + '/json/version', 10000);
            cdp = await connect(version.webSocketDebuggerUrl);
            const browserVersion = version.Browser || await cdp.send('Browser.getVersion').then(item => item.product || '');
            for (let iteration = 0; iteration < payload.iterations; iteration++) {{
              for (const task of payload.tasks) {{
                const taskId = String(task.task_id);
                const taskFixtures = fixtures(task);
                if (!taskFixtures.length) {{
                  taskRuns[taskId].push({{ status: 'skipped', failure_kind: 'no_action_fixtures', duration_s: 0 }});
                  continue;
                }}
                const page = await newPage(cdp);
                const started = performance.now();
                let status = 'passed', failure_kind = '', detail = '', completed_actions = 0;
                try {{
                  for (const fixture of taskFixtures) {{
                    await withTimeout(setContent(cdp, page, fixture.html), 8000, 'Page.setDocumentContent');
                    const result = await withTimeout(evaluateFixture(cdp, page, fixture), 3000, 'Runtime.evaluate');
                    if (!result || !result.ok) {{
                      status = 'failed';
                      failure_kind = result && result.failure_kind || 'quality_failure';
                      detail = result && result.detail || '';
                      break;
                    }}
                    completed_actions++;
                  }}
                }} catch (error) {{
                  status = 'failed';
                  failure_kind = 'automation_failure';
                  detail = String(error && error.message ? error.message : error);
                }} finally {{
                  const duration_s = (performance.now() - started) / 1000;
                  await closePage(cdp, page);
                  taskRuns[taskId].push({{ status, failure_kind, detail, duration_s, completed_actions, action_count: taskFixtures.length }});
                }}
              }}
            }}
            console.log('MIND2WEB_JSON ' + JSON.stringify(buildResult(taskRuns, browserVersion)));
          }} finally {{
            if (cdp) cdp.close();
            browser.kill('SIGTERM');
            try {{ fs.rmSync(profile, {{ recursive: true, force: true }}); }} catch (_) {{}}
          }}
        }})().catch(error => {{
          console.error(error && error.stack ? error.stack : error);
          process.exit(1);
        }});
        """
    )


def build_result(
    implementation: str,
    tasks: list[dict[str, Any]],
    iterations: int,
    task_runs: dict[str, list[dict[str, Any]]],
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_results = {}
    cases = {}
    passed = failed = skipped = 0
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
            "website": task.get("website"),
            "domain": task.get("domain"),
            "action_count": task.get("action_count"),
            "executable_action_count": len(task_fixtures(task)),
            "passed_runs": passed_runs,
            "failed_runs": failed_runs,
            "skipped_runs": skipped_runs,
            "runs": runs,
        }
        cases[task_id] = timing_summary(durations)
    metadata = {
        "suite": "mind2web",
        "comparison_mode": "mind2web_offline_action_replay",
        "case_count": len(tasks),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "browser_executable": benchmark_chromium_executable(),
        "container_isolation": os.environ.get("RUSTWRIGHT_CONTAINER_ISOLATION"),
        "docker_memory_limit": os.environ.get("TEST_DOCKER_MEMORY_LIMIT"),
    }
    metadata.update(extra_metadata or {})
    phase_profile = phase_timing_summary(task_runs)
    if phase_profile["measured_runs"]:
        metadata["phase_profile"] = phase_profile
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


def run_impl(args: argparse.Namespace, implementation: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    implementation = canonical_impl(implementation)
    if implementation in {"rustwright-py", "playwright"}:
        return run_python_like(
            implementation,
            tasks,
            args.iterations,
            args.reference_path,
            args.fixture_timeout_ms,
            args.fixture_wait_until,
            args.max_task_seconds,
            args.progress_every,
        )
    if implementation in {"typescript-playwright", "typescript-puppeteer", "rustwright-ts-cdp", "rustwright-ts"}:
        return run_node_adapter(implementation, tasks, args.iterations, args.reference_path)
    raise UnsupportedImplementation(f"unknown implementation: {implementation}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run imported Mind2Web action fixtures as a quality benchmark.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--impl",
        choices=[*DEFAULT_IMPLS, *EXPERIMENTAL_IMPLS, *LEGACY_IMPL_ALIASES.keys(), "all"],
        default="rustwright-py",
    )
    parser.add_argument("--percentage", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=int(os.environ.get("MIND2WEB_ITERATIONS", "1")))
    parser.add_argument(
        "--fixture-timeout-ms",
        type=int,
        default=int(os.environ.get("MIND2WEB_FIXTURE_TIMEOUT_MS", "8000")),
        help="Timeout for each offline action fixture load in Python-style adapters.",
    )
    parser.add_argument(
        "--fixture-wait-until",
        default=os.environ.get("MIND2WEB_FIXTURE_WAIT_UNTIL", "commit"),
        choices=["commit", "domcontentloaded", "load", "networkidle"],
        help="Load state for each offline action fixture in Python-style adapters. Defaults to commit for static snapshots.",
    )
    parser.add_argument(
        "--max-task-seconds",
        type=float,
        default=float(os.environ["MIND2WEB_MAX_TASK_SECONDS"]) if os.environ.get("MIND2WEB_MAX_TASK_SECONDS") else None,
        help="Optional soft timeout checked between action fixtures for Python-style adapters.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=int(os.environ.get("MIND2WEB_PROGRESS_EVERY", "0")),
        help="Print Python-style adapter progress to stderr every N task-runs. 0 disables progress.",
    )
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
    implementations = DEFAULT_IMPLS if args.impl == "all" else [canonical_impl(args.impl)]
    results = []
    for implementation in implementations:
        try:
            result = run_impl(args, implementation, tasks)
            result["status"] = "passed"
            result["selection"] = selection
        except UnsupportedImplementation as exc:
            result = {
                "implementation": implementation,
                "status": "skipped",
                "reason": str(exc),
                "selection": selection,
            }
        results.append(result)
    output: dict[str, Any]
    if args.impl == "all":
        output = {
            "implementation": "all",
            "suite": "mind2web",
            "iterations": args.iterations,
            "selection": selection,
            "results": results,
        }
    else:
        output = results[0]
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    elif args.impl == "all":
        print(f"Mind2Web {args.percentage:g}% sample, {len(tasks)} task(s), digest {selection['selection_digest']}")
        for item in results:
            if item.get("status") == "skipped":
                print(f"{item['implementation']:24s} skipped: {item['reason']}")
                continue
            quality = item["quality"]
            print(
                f"{item['implementation']:24s} success {quality['success_rate']:.1%} "
                f"({quality['passed_runs']} passed, {quality['failed_runs']} failed, {quality['skipped_runs']} skipped), "
                f"{item['total_mean_ms']:.2f} ms total mean"
            )
    else:
        item = output
        if item.get("status") == "skipped":
            print(f"{item['implementation']} skipped: {item['reason']}")
        else:
            quality = item["quality"]
            print(
                f"{item['implementation']}: success {quality['success_rate']:.1%} "
                f"across {quality['task_count']} selected Mind2Web task(s)"
            )
    failed = [item for item in results if item.get("status") == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
