from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Callable

from automation_cases import BENCHMARK_CASES as EQUIVALENT_CASES
from automation_cases import BENCHMARK_STRICT_CASES
from process_tree_memory import attach_peak_process_tree_rss

ROOT = Path(__file__).resolve().parents[1]
STRICT_IMPLS = {"rustwright", "playwright"}
EQUIVALENT_IMPLS = {"rustwright", "playwright", "typescript-playwright", "typescript-puppeteer"}


class PlaywrightReferenceUnavailable(RuntimeError):
    pass


class TypeScriptPlaywrightUnavailable(RuntimeError):
    pass


class TypeScriptPuppeteerUnavailable(RuntimeError):
    pass


def load_sync_playwright(implementation: str, reference_path: str | None = None) -> Callable:
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
        try:
            module = importlib.import_module("playwright.sync_api")
        except ImportError as exc:
            raise PlaywrightReferenceUnavailable(
                "Could not import a real Playwright reference package. "
                "Pass --reference-path .audit-playwright or install Playwright."
            ) from exc
        module_path = Path(getattr(module, "__file__", "")).resolve()
        if ROOT / "python" in module_path.parents or "rustwright" in str(module_path):
            raise PlaywrightReferenceUnavailable(
                "The local drop-in playwright alias is shadowing real Playwright. "
                "Pass --reference-path .audit-playwright or run outside the editable repo environment."
            )
        return module.sync_playwright

    raise ValueError(f"unknown implementation: {implementation}")


def find_typescript_playwright_reference(reference_path: str | None = None) -> tuple[str, str]:
    candidates: list[Path] = []
    if reference_path:
        candidates.append(Path(reference_path).resolve())
    candidates.append(Path(os.environ.get("PLAYWRIGHT_REFERENCE_PATH", "")).resolve())
    candidates.append((ROOT / ".audit-playwright").resolve())

    for base in candidates:
        if not str(base):
            continue
        node = base / "playwright" / "driver" / "node"
        package = base / "playwright" / "driver" / "package"
        if node.is_file() and (package / "package.json").is_file():
            return str(node), str(package)

    raise TypeScriptPlaywrightUnavailable(
        "Could not find the bundled Node Playwright reference. "
        "Pass --reference-path pointing at a Python Playwright install that contains playwright/driver/node."
    )


def find_node_executable(reference_path: str | None = None) -> str:
    try:
        node, _ = find_typescript_playwright_reference(reference_path)
        return node
    except TypeScriptPlaywrightUnavailable:
        found = shutil.which("node")
        if found:
            return found
    raise TypeScriptPuppeteerUnavailable(
        "Could not find a Node executable. Install Node or pass --reference-path pointing at a Playwright "
        "reference install that contains playwright/driver/node."
    )


def find_puppeteer_package() -> str:
    candidates = []
    explicit = os.environ.get("PUPPETEER_PACKAGE_PATH")
    if explicit:
        candidates.append(Path(explicit).resolve())
    candidates.extend(
        [
            ROOT / "node_modules" / "puppeteer-core",
            Path("/workspace/node_modules/puppeteer-core"),
            Path("/opt/puppeteer-benchmark/node_modules/puppeteer-core"),
        ]
    )
    for package in candidates:
        if (package / "package.json").is_file():
            return str(package)
    raise TypeScriptPuppeteerUnavailable(
        "Could not find puppeteer-core. Rebuild the Docker image with INSTALL_PUPPETEER=1 "
        "or set PUPPETEER_PACKAGE_PATH to a puppeteer-core package directory."
    )


def benchmark_chromium_executable() -> str | None:
    for env_name in ("BENCHMARK_CHROMIUM_EXECUTABLE", "RUSTWRIGHT_CHROMIUM", "CHROME", "CHROMIUM"):
        value = os.environ.get(env_name)
        if value and Path(value).is_file():
            return value
    return None


def launch_chromium(playwright):
    launch_options: dict[str, object] = {"headless": True}
    executable = benchmark_chromium_executable()
    if executable:
        launch_options["executable_path"] = executable
    for attempt in range(2):
        try:
            return playwright.chromium.launch(**launch_options)
        except Exception as exc:
            message = str(exc)
            transient_launch_crash = (
                "Target page, context or browser has been closed" in message
                or "process did exit" in message
                or "SIGSEGV" in message
            )
            if transient_launch_crash and attempt == 0:
                continue
            if (
                "mach_port_rendezvous" not in message
                and "bootstrap_check_in" not in message
                and "sandbox_parameters_mac" not in message
            ):
                raise
            return playwright.chromium.launch(**{**launch_options, "args": ["--single-process"]})
    raise RuntimeError("unreachable chromium launch retry state")


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
        "mean_ms": statistics.mean(values) * 1000,
        "median_ms": statistics.median(values) * 1000,
        "p25_ms": percentile(values, 0.25) * 1000,
        "p75_ms": percentile(values, 0.75) * 1000,
        "min_ms": min(values) * 1000,
        "max_ms": max(values) * 1000,
        "stdev_ms": statistics.stdev(values) * 1000 if len(values) > 1 else 0.0,
    }


def select_cases(suite: str) -> list[Callable]:
    if suite == "equivalent":
        return list(EQUIVALENT_CASES)
    if suite == "strict":
        return list(BENCHMARK_STRICT_CASES)
    raise ValueError(f"unknown benchmark suite: {suite}")


def filter_cases(cases: list[Callable], requested: list[str] | None) -> list[Callable]:
    if not requested:
        return cases
    by_name = {case.__name__: case for case in cases}
    selected = []
    missing = []
    for name in requested:
        case = by_name.get(name)
        if case is None:
            missing.append(name)
        else:
            selected.append(case)
    if missing:
        raise SystemExit(f"unknown benchmark case(s): {', '.join(sorted(missing))}")
    return selected


def comparison_mode(suite: str, implementation: str) -> str:
    if suite == "strict":
        return "strict_playwright_api"
    if implementation in {"rustwright", "playwright", "typescript-playwright"}:
        return "playwright_api_equivalent_cases"
    return "lower_level_equivalent_workflows"


def benchmark_metadata(implementation: str, suite: str, lifecycle: str, cases: list[Callable]) -> dict[str, object]:
    return {
        "suite": suite,
        "lifecycle": lifecycle,
        "case_count": len(cases),
        "comparison_mode": comparison_mode(suite, implementation),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": sys.executable,
        "rustwright_chromium": os.environ.get("RUSTWRIGHT_CHROMIUM"),
        "rustwright_cdp_transport": os.environ.get("RUSTWRIGHT_CDP_TRANSPORT") or "websocket",
        "browser_executable": benchmark_chromium_executable(),
        "playwright_browsers_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        "rustwright_browsers_path": os.environ.get("RUSTWRIGHT_BROWSERS_PATH"),
    }


def run_once(sync_playwright: Callable, cases: list[Callable]) -> dict[str, float]:
    timings = {}
    with sync_playwright() as p:
        browser = launch_chromium(p)
        try:
            for case in cases:
                page = browser.new_page()
                started = time.perf_counter()
                case(page)
                timings[case.__name__] = time.perf_counter() - started
                page.close()
        finally:
            browser.close()
    return timings


def find_chromium_executable() -> str:
    explicit = benchmark_chromium_executable()
    if explicit:
        return explicit
    home = Path.home()
    cache = home / "Library/Caches/ms-playwright"
    candidates = []
    if cache.is_dir():
        for path in sorted(cache.iterdir(), key=lambda item: item.name, reverse=True):
            if path.name.startswith("chromium_headless_shell"):
                candidates.append(path / "chrome-headless-shell-mac-arm64/chrome-headless-shell")
                candidates.append(path / "chromium_headless_shell-mac/headless_shell")
            if path.name.startswith("chromium-"):
                candidates.append(
                    path / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
                )
    candidates.extend(
        [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Could not find a Chromium executable for the Puppeteer comparison")


@attach_peak_process_tree_rss
def run_typescript_playwright(
    iterations: int,
    reference_path: str | None = None,
    *,
    suite: str = "equivalent",
    lifecycle: str = "warm-browser",
    case_filters: list[str] | None = None,
) -> dict:
    if suite != "equivalent":
        raise TypeScriptPlaywrightUnavailable("TypeScript Playwright currently supports the equivalent benchmark suite")
    if lifecycle not in {"warm-browser", "warm-page", "cold-browser", "cold-container"}:
        raise TypeScriptPlaywrightUnavailable("TypeScript Playwright does not support the requested lifecycle")
    node, package = find_typescript_playwright_reference(reference_path)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as script:
        script.write(typescript_playwright_code(iterations, lifecycle=lifecycle, case_filters=case_filters))
        script_path = script.name
    try:
        proc = subprocess.run(
            [node, script_path],
            text=True,
            capture_output=True,
            env={**os.environ, "PLAYWRIGHT_TS_PACKAGE": package},
            timeout=max(120, iterations * 60),
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("BENCHMARK_JSON "):
            return json.loads(line.removeprefix("BENCHMARK_JSON "))
    raise RuntimeError(f"TypeScript Playwright did not print benchmark JSON:\n{proc.stdout}")


@attach_peak_process_tree_rss
def run_typescript_puppeteer(
    iterations: int,
    reference_path: str | None = None,
    *,
    suite: str = "equivalent",
    lifecycle: str = "warm-browser",
    case_filters: list[str] | None = None,
) -> dict:
    if suite != "equivalent":
        raise TypeScriptPuppeteerUnavailable("TypeScript Puppeteer only supports the equivalent workflow benchmark suite")
    if lifecycle not in {"warm-browser", "warm-page", "cold-browser", "cold-container"}:
        raise TypeScriptPuppeteerUnavailable("TypeScript Puppeteer does not support the requested lifecycle")
    node = find_node_executable(reference_path)
    package = find_puppeteer_package()
    executable = find_chromium_executable()
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as script:
        script.write(typescript_puppeteer_code(iterations, lifecycle=lifecycle, case_filters=case_filters))
        script_path = script.name
    try:
        proc = subprocess.run(
            [node, script_path],
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PUPPETEER_PACKAGE_PATH": package,
                "PUPPETEER_EXECUTABLE_PATH": executable,
            },
            timeout=max(120, iterations * 60),
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("BENCHMARK_JSON "):
            return json.loads(line.removeprefix("BENCHMARK_JSON "))
    raise RuntimeError(f"TypeScript Puppeteer did not print benchmark JSON:\n{proc.stdout}")


@attach_peak_process_tree_rss
def run_playwright_like(
    implementation: str,
    iterations: int,
    reference_path: str | None = None,
    *,
    suite: str = "equivalent",
    lifecycle: str = "warm-browser",
    case_filters: list[str] | None = None,
) -> dict:
    sync_playwright = load_sync_playwright(implementation, reference_path=reference_path)
    cases = filter_cases(select_cases(suite), case_filters)
    samples: list[dict[str, float]] = []
    browser_version = None
    effective_lifecycle = "warm-browser" if lifecycle == "cold-container" else lifecycle
    with sync_playwright() as p:
        if effective_lifecycle == "warm-browser":
            browser = launch_chromium(p)
            browser_version = getattr(browser, "version", None)
            try:
                for _ in range(iterations):
                    timings = {}
                    for case in cases:
                        page = browser.new_page()
                        started = time.perf_counter()
                        try:
                            case(page)
                            timings[case.__name__] = time.perf_counter() - started
                        finally:
                            page.close()
                    samples.append(timings)
            finally:
                browser.close()
        elif effective_lifecycle == "warm-page":
            browser = launch_chromium(p)
            browser_version = getattr(browser, "version", None)
            try:
                for _ in range(iterations):
                    timings = {}
                    page = browser.new_page()
                    try:
                        for case in cases:
                            started = time.perf_counter()
                            case(page)
                            timings[case.__name__] = time.perf_counter() - started
                    finally:
                        page.close()
                    samples.append(timings)
            finally:
                browser.close()
        elif effective_lifecycle == "cold-browser":
            for _ in range(iterations):
                timings = {}
                for case in cases:
                    started = time.perf_counter()
                    browser = launch_chromium(p)
                    browser_version = browser_version or getattr(browser, "version", None)
                    try:
                        page = browser.new_page()
                        try:
                            case(page)
                            timings[case.__name__] = time.perf_counter() - started
                        finally:
                            page.close()
                    finally:
                        browser.close()
                samples.append(timings)
        else:
            raise ValueError(f"unknown lifecycle: {lifecycle}")
    case_names = [case.__name__ for case in cases]
    result = {
        "implementation": implementation,
        "iterations": iterations,
        "metadata": {
            **benchmark_metadata(implementation, suite, lifecycle, cases),
            "browser_version": browser_version,
        },
        "cases": {
            name: timing_summary([sample[name] for sample in samples])
            for name in case_names
        },
    }
    result["total_mean_ms"] = sum(value["mean_ms"] for value in result["cases"].values())
    return result


def speedup_report(results: list[dict]) -> dict[str, float]:
    by_name = {result["implementation"]: result for result in results if result.get("status") != "skipped"}
    rustwright = by_name.get("rustwright")
    if rustwright is None:
        return {}
    report: dict[str, float] = {}
    rustwright_total = float(rustwright["total_mean_ms"])
    for baseline in ("playwright", "typescript-playwright", "typescript-puppeteer"):
        if baseline not in by_name:
            continue
        baseline_total = float(by_name[baseline]["total_mean_ms"])
        if baseline_total <= 0:
            continue
        report[f"vs_{baseline}_reduction_pct"] = (baseline_total - rustwright_total) / baseline_total * 100
    return report


def include_puppeteer_in_all() -> bool:
    return os.environ.get("RUSTWRIGHT_INCLUDE_PUPPETEER_BENCHMARK") == "1"


def typescript_stats_footer(implementation: str, iterations: int, suite: str, lifecycle: str) -> str:
    return textwrap.dedent(
        f"""
          const result = {{
            implementation: {json.dumps(implementation)},
            iterations: {iterations},
            metadata: {{
              suite: {json.dumps(suite)},
              lifecycle: {json.dumps(lifecycle)},
              case_count: cases.length,
              comparison_mode: {json.dumps(comparison_mode(suite, implementation))},
              node: process.version,
              platform: process.platform,
              arch: process.arch,
              browser_version: browserVersion,
              playwright_ts_package: process.env.PLAYWRIGHT_TS_PACKAGE || null,
              puppeteer_package_path: process.env.PUPPETEER_PACKAGE_PATH || null,
              browser_executable: process.env.PUPPETEER_EXECUTABLE_PATH || process.env.BENCHMARK_CHROMIUM_EXECUTABLE || process.env.RUSTWRIGHT_CHROMIUM || process.env.CHROME || process.env.CHROMIUM || null,
            }},
            cases: Object.fromEntries(cases.map(([name]) => {{
              const values = samples.map(sample => sample[name] * 1000).sort((a, b) => a - b);
              const mean = values.reduce((total, value) => total + value, 0) / values.length;
              const midpoint = Math.floor(values.length / 2);
              const median = values.length % 2 ? values[midpoint] : (values[midpoint - 1] + values[midpoint]) / 2;
              const variance = values.length > 1 ? values.reduce((total, value) => total + Math.pow(value - mean, 2), 0) / (values.length - 1) : 0;
              return [name, {{
                mean_ms: mean,
                median_ms: median,
                p25_ms: values[Math.floor((values.length - 1) * 0.25)],
                p75_ms: values[Math.floor((values.length - 1) * 0.75)],
                min_ms: values[0],
                max_ms: values[values.length - 1],
                stdev_ms: Math.sqrt(variance),
              }}];
            }})),
          }};
          result.total_mean_ms = Object.values(result.cases).reduce((total, value) => total + value.mean_ms, 0);
          console.log('BENCHMARK_JSON ' + JSON.stringify(result));
        """
    )


def typescript_playwright_code(
    iterations: int,
    *,
    lifecycle: str = "warm-browser",
    case_filters: list[str] | None = None,
) -> str:
    requested_cases = json.dumps(case_filters or [])
    return textwrap.dedent(
        f"""
        const {{ chromium }} = require(process.env.PLAYWRIGHT_TS_PACKAGE);
        const {{ performance }} = require('node:perf_hooks');
        const benchmarkChromiumExecutable = process.env.BENCHMARK_CHROMIUM_EXECUTABLE || process.env.RUSTWRIGHT_CHROMIUM || process.env.CHROME || process.env.CHROMIUM || null;

        function dataUrl(html) {{
          return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
        }}

        function assert(condition, message = 'assertion failed') {{
          if (!condition) throw new Error(message);
        }}

        async function expectError(callback, ...substrings) {{
          try {{
            await callback();
          }} catch (error) {{
            const text = String(error && error.stack ? error.stack : error);
            for (const substring of substrings) assert(text.includes(substring), `expected error to include ${{substring}}, got ${{text}}`);
            return;
          }}
          throw new Error(`expected error containing ${{substrings.join(', ')}}`);
        }}

        async function expectErrorAny(callback, ...substrings) {{
          try {{
            await callback();
          }} catch (error) {{
            const text = String(error && error.stack ? error.stack : error);
            assert(substrings.some(substring => text.includes(substring)), `expected error to include one of ${{substrings.join(', ')}}, got ${{text}}`);
            return;
          }}
          throw new Error(`expected error containing one of ${{substrings.join(', ')}}`);
        }}

        async function launchChromium() {{
          const launchOptions = {{ headless: true }};
          if (benchmarkChromiumExecutable) launchOptions.executablePath = benchmarkChromiumExecutable;
          try {{
            return await chromium.launch(launchOptions);
          }} catch (error) {{
            const message = String(error && error.message ? error.message : error);
            if (
              !message.includes('mach_port_rendezvous') &&
              !message.includes('bootstrap_check_in') &&
              !message.includes('sandbox_parameters_mac')
            ) throw error;
            return await chromium.launch({{ ...launchOptions, args: ['--single-process'] }});
          }}
        }}

        async function goto_and_title(page) {{
          await page.goto(dataUrl('<title>Bench</title><main>ready</main>'));
          assert(await page.title() === 'Bench');
        }}

        async function set_content_and_read_text(page) {{
          await page.setContent('<section><h1>Dashboard</h1><p id="status">Ready</p></section>');
          assert(await page.textContent('#status') === 'Ready');
        }}

        async function evaluate_json(page) {{
          await page.setContent('<div></div>');
          const value = await page.evaluate(() => ({{ sum: 1 + 2, ok: true }}));
          assert(value.sum === 3 && value.ok === true);
          assert(Number.isNaN(await page.evaluate(() => Number.NaN)));
          assert(await page.evaluate(() => Infinity) === Infinity);
          assert(await page.evaluate(() => -Infinity) === -Infinity);
          assert(Object.is(await page.evaluate(() => -0), -0));
          const values = await page.evaluate(() => ({{ nan: Number.NaN, inf: Infinity, negInf: -Infinity, negZero: -0, nested: [Number.NaN, -0] }}));
          assert(Number.isNaN(values.nan));
          assert(values.inf === Infinity);
          assert(values.negInf === -Infinity);
          assert(Object.is(values.negZero, -0));
          assert(Number.isNaN(values.nested[0]));
          assert(Object.is(values.nested[1], -0));
          const serialized = await page.evaluate(() => ({{
            date: new Date('2020-01-02T03:04:05.678Z'),
            regex: /abc/gi,
            url: new URL('https://example.com/a?b=1'),
            big: 42n,
            error: new TypeError('boom'),
            symbol: Symbol('ignored'),
            array: [new Date('2021-02-03T04:05:06Z'), /x/m, -3n, new Error('nested')]
          }}));
          assert(serialized.date instanceof Date && serialized.date.toISOString() === '2020-01-02T03:04:05.678Z');
          assert(serialized.regex instanceof RegExp && serialized.regex.source === 'abc' && serialized.regex.flags === 'gi');
          assert(serialized.url instanceof URL && serialized.url.href === 'https://example.com/a?b=1');
          assert(serialized.big === 42n);
          assert(serialized.symbol === undefined);
          assert(serialized.error instanceof Error && serialized.error.name === 'TypeError' && serialized.error.message === 'boom');
          assert(serialized.array[0] instanceof Date && serialized.array[0].toISOString() === '2021-02-03T04:05:06.000Z');
          assert(serialized.array[1] instanceof RegExp && serialized.array[1].source === 'x' && serialized.array[1].flags === 'm');
          assert(serialized.array[2] === -3n);
          assert(serialized.array[3] instanceof Error && serialized.array[3].message === 'nested');
        }}

        async function click_button(page) {{
          await page.setContent('<button id="go" onclick="document.body.dataset.clicked=\\'yes\\'">Go</button>');
          await page.locator('#go').click({{ trial: true }});
          await page.click('#go', {{ trial: true }});
          await page.locator('#go').dblclick({{ trial: true }});
          assert(await page.evaluate(() => document.body.dataset.clicked || null) === null);
          await page.click('#go');
          assert(await page.evaluate(() => document.body.dataset.clicked) === 'yes');
        }}

        async function fill_input(page) {{
          await page.setContent(`
            <input id='email'>
            <input id='hidden-email' style='display:none' value='hidden'>
            <input id='disabled-email' disabled value='disabled'>
            <input id='readonly-email' readonly value='readonly'>
            <input id='checkbox-email' type='checkbox' value='old'>
            <input id='number-code' type='number'>
            <input id='date-code' type='date'>
            <select id='plan'><option value='basic'>Basic</option><option value='pro' selected>Pro</option></select>
            <button id='button-email' value='button-value'>Button</button>
            <div id='editable-email' contenteditable>editable</div>
            <div id='plain-email'>plain</div>
          `);
          await page.fill('#email', 'user@example.com');
          assert(await page.evaluate(() => document.querySelector('#email').value) === 'user@example.com');
          await expectError(() => page.locator('#plain-email').fill('normal plain', {{ timeout: 500 }}), 'not an <input>', 'role allowing');
          await expectError(() => page.locator('#plan').fill('normal select', {{ timeout: 500 }}), '[contenteditable] element');
          await expectError(() => page.locator('#checkbox-email').fill('checked', {{ timeout: 500 }}), 'Input of type "checkbox"');
          await expectError(() => page.locator('#checkbox-email').clear({{ timeout: 500 }}), 'Input of type "checkbox"');
          await expectError(() => page.locator('#number-code').fill('abc', {{ timeout: 500 }}), 'Cannot type text into input[type=number]');
          await expectErrorAny(() => page.locator('#date-code').fill('1', {{ timeout: 500 }}), 'Malformed value', 'Timeout 500ms exceeded');
          assert(await page.locator('#plan').inputValue() === 'pro');
          await expectError(() => page.locator('#plain-email').inputValue({{ timeout: 500 }}), 'Node is not an <input>');
          await expectError(() => page.locator('#button-email').inputValue({{ timeout: 500 }}), 'Node is not an <input>');
          await page.locator('#email').clear({{ force: true }});
          assert(await page.evaluate(() => document.querySelector('#email').value) === '');
          await page.locator('#email').fill('forced@example.com', {{ force: true }});
          assert(await page.evaluate(() => document.querySelector('#email').value) === 'forced@example.com');
          await page.locator('#hidden-email').fill('forced-hidden', {{ force: true }});
          assert(await page.evaluate(() => document.querySelector('#hidden-email').value) === 'hidden');
          await page.fill('#hidden-email', 'page-forced-hidden', {{ force: true }});
          assert(await page.evaluate(() => document.querySelector('#hidden-email').value) === 'hidden');
          await page.locator('#disabled-email').fill('forced-disabled', {{ force: true }});
          assert(await page.evaluate(() => document.querySelector('#disabled-email').value) === 'disabled');
          await page.locator('#readonly-email').fill('forced-readonly', {{ force: true }});
          assert(await page.evaluate(() => document.querySelector('#readonly-email').value) === 'readonly');
          await page.locator('#editable-email').fill('forced editable', {{ force: true }});
          assert(await page.textContent('#editable-email') === 'forced editable');
          await expectError(() => page.locator('#plain-email').fill('forced plain', {{ force: true }}), '[contenteditable] element');
          await expectError(() => page.locator('#checkbox-email').fill('forced checkbox', {{ force: true }}), 'Input of type "checkbox"');
        }}

        async function type_input(page) {{
          await page.setContent('<input id="message">');
          await page.type('#message', 'hello');
          assert(await page.evaluate(() => document.querySelector('#message').value) === 'hello');
        }}

        async function locator_count(page) {{
          await page.setContent('<ul>' + Array.from({{ length: 25 }}, (_, i) => `<li>Item ${{i}}</li>`).join('') + '</ul>');
          assert(await page.locator('li').count() === 25);
        }}

        async function locator_nth_text(page) {{
          await page.setContent('<ul><li>first</li><li>second</li><li>third</li></ul>');
          assert(await page.locator('li').nth(2).innerText() === 'third');
        }}

        async function role_locator(page) {{
          await page.setContent('<button aria-label="Save record">Save</button>');
          assert(await page.getByRole('button', {{ name: 'Save' }}).isVisible());
        }}

        async function text_locator(page) {{
          await page.setContent('<article><p>Quarterly revenue report</p></article>');
          assert(await page.getByText('revenue').isVisible());
        }}

        async function wait_for_selector(page) {{
          await page.setContent('<main id="root"><div id="hidden" style="display:none">Hidden</div><div id="gone">Gone</div><iframe srcdoc="<span id=&quot;frame-hidden&quot; style=&quot;display:none&quot;>Hidden</span>"></iframe></main>');
          await page.evaluate(() => setTimeout(() => {{
            const node = document.createElement('div');
            node.id = 'done';
            node.textContent = 'Done';
            document.querySelector('#root').appendChild(node);
            document.querySelector('#gone').remove();
          }}, 20));
          assert(await (await page.waitForSelector('#done', {{ timeout: 2000 }})).textContent() === 'Done');
          assert(await page.waitForSelector('#hidden', {{ state: 'hidden', timeout: 500 }}) === null);
          assert(await page.waitForSelector('#missing', {{ state: 'hidden', timeout: 500 }}) === null);
          assert(await page.waitForSelector('#missing', {{ state: 'detached', timeout: 500 }}) === null);
          assert(await page.locator('#hidden').waitFor({{ state: 'hidden', timeout: 500 }}) === undefined);
          assert(await page.locator('#missing').waitFor({{ state: 'detached', timeout: 500 }}) === undefined);
          const root = await page.$('#root');
          assert(await root.waitForSelector('#hidden', {{ state: 'hidden', timeout: 500 }}) === null);
          assert(await root.waitForSelector('#gone', {{ state: 'detached', timeout: 500 }}) === null);
          assert(await root.waitForSelector('#missing', {{ state: 'hidden', timeout: 500 }}) === null);
          const frame = page.frames()[1];
          assert(await frame.waitForSelector('#frame-hidden', {{ state: 'hidden', timeout: 500 }}) === null);
          await expectError(() => page.waitForSelector('#done', {{ state: 'enabled', timeout: 500 }}), 'state: expected one of (attached|detached|visible|hidden)');
          await expectError(() => page.locator('#done').waitFor({{ state: 'enabled', timeout: 500 }}), 'state: expected one of (attached|detached|visible|hidden)');
          await expectError(() => root.waitForSelector('#hidden', {{ state: 'enabled', timeout: 500 }}), 'state: expected one of (attached|detached|visible|hidden)');
          await expectError(() => frame.waitForSelector('#frame-hidden', {{ state: 'enabled', timeout: 500 }}), 'state: expected one of (attached|detached|visible|hidden)');
        }}

        async function screenshot(page) {{
          await page.setContent('<h1>Screenshot</h1>');
          const png = await page.screenshot();
          assert(png[0] === 0x89 && png[1] === 0x50 && png[2] === 0x4e && png[3] === 0x47);
        }}

        async function webvoyager_checkout_workflow(page) {{
          await page.setContent(`
            <main>
              <label>Search catalog <input id="query" placeholder="Search catalog"></label>
              <label>Category
                <select id="category">
                  <option value="all">All</option>
                  <option value="travel">Travel</option>
                  <option value="office">Office</option>
                </select>
              </label>
              <label>Maximum price <input id="max-price" type="number" value="999"></label>
              <button id="apply">Apply filters</button>
              <section id="results" aria-label="Results"></section>
              <aside>
                <output id="cart-count" aria-label="Cart count">0</output>
                <output id="cart-total" aria-label="Cart total">$0</output>
              </aside>
              <label>Email <input id="email" type="email"></label>
              <button id="place-order">Place order</button>
              <strong id="confirmation"></strong>
            </main>
            <script>
            const products = [
              {{ name: 'Noise cancelling headphones', category: 'travel', price: 129 }},
              {{ name: 'Travel adapter', category: 'travel', price: 29 }},
              {{ name: 'Desk lamp', category: 'office', price: 64 }},
              {{ name: 'Notebook set', category: 'office', price: 18 }}
            ];
            const cart = [];
            function render() {{
              const query = document.querySelector('#query').value.toLowerCase();
              const category = document.querySelector('#category').value;
              const max = Number(document.querySelector('#max-price').value || 999);
              const results = products.filter(product =>
                product.name.toLowerCase().includes(query) &&
                (category === 'all' || product.category === category) &&
                product.price <= max
              );
              document.querySelector('#results').innerHTML = results.map(product => \\`
                <article data-testid="result">
                  <h2>\\${{product.name}}</h2>
                  <p>$\\${{product.price}}</p>
                  <button aria-label="Add \\${{product.name}}" data-name="\\${{product.name}}">Add</button>
                </article>
              \\`).join('');
            }}
            document.querySelector('#apply').addEventListener('click', render);
            document.querySelector('#results').addEventListener('click', event => {{
              const button = event.target.closest('button[data-name]');
              if (!button) return;
              const product = products.find(item => item.name === button.dataset.name);
              cart.push(product);
              document.querySelector('#cart-count').textContent = String(cart.length);
              document.querySelector('#cart-total').textContent = '$' + cart.reduce((total, item) => total + item.price, 0);
            }});
            document.querySelector('#place-order').addEventListener('click', () => {{
              document.querySelector('#confirmation').textContent =
                \\`Confirmed \\${{cart.length}} items for \\${{document.querySelector('#email').value}}\\`;
            }});
            render();
            </script>
          `);
          await page.mouse.move(500, 500);
          await page.evaluate(() => {{ window.events = []; }});
          await page.getByPlaceholder('Search catalog').fill('travel');
          await page.getByRole('button', {{ name: 'Apply filters' }}).click();
          assert(await page.locator('[data-testid="result"]').count() === 1);
          await page.getByRole('button', {{ name: 'Add Travel adapter' }}).click();
          await page.getByPlaceholder('Search catalog').fill('noise');
          await page.getByLabel('Category').selectOption('travel');
          await page.getByLabel('Maximum price').fill('150');
          await page.getByRole('button', {{ name: 'Apply filters' }}).click();
          assert(await page.locator('[data-testid="result"]').count() === 1);
          await page.getByRole('button', {{ name: 'Add Noise cancelling headphones' }}).click();
          await page.getByLabel('Email').fill('ada@example.com');
          await page.getByRole('button', {{ name: 'Place order' }}).click();
          assert(await page.locator('#cart-count').textContent() === '2');
          assert(await page.locator('#cart-total').textContent() === '$158');
          assert(await page.locator('#confirmation').textContent() === 'Confirmed 2 items for ada@example.com');
        }}

        async function mind2web_table_triage_workflow(page) {{
          await page.setContent(`
            <main>
              <label>Status
                <select id="status">
                  <option value="all">All</option>
                  <option value="open">Open</option>
                  <option value="closed">Closed</option>
                </select>
              </label>
              <label>Owner <input id="owner"></label>
              <label><input id="urgent" type="checkbox"> Urgent only</label>
              <button id="run">Run triage</button>
              <table>
                <thead><tr><th>Ticket</th><th>Owner</th><th>Status</th><th>Priority</th><th></th></tr></thead>
                <tbody></tbody>
              </table>
              <div id="toast" role="status"></div>
            </main>
            <script>
            const tickets = [
              {{ title: 'Invoice export', owner: 'Sam', status: 'open', priority: 'P0' }},
              {{ title: 'Login copy', owner: 'Rae', status: 'open', priority: 'P2' }},
              {{ title: 'Billing retry', owner: 'Sam', status: 'closed', priority: 'P1' }},
              {{ title: 'Webhook audit', owner: 'Sam', status: 'open', priority: 'P1' }}
            ];
            function render() {{
              const status = document.querySelector('#status').value;
              const owner = document.querySelector('#owner').value.toLowerCase();
              const urgent = document.querySelector('#urgent').checked;
              const rows = tickets.filter(ticket =>
                (status === 'all' || ticket.status === status) &&
                (!owner || ticket.owner.toLowerCase().includes(owner)) &&
                (!urgent || ticket.priority === 'P0')
              );
              document.querySelector('tbody').innerHTML = rows.map(ticket => \\`
                <tr>
                  <td>\\${{ticket.title}}</td><td>\\${{ticket.owner}}</td><td>\\${{ticket.status}}</td><td>\\${{ticket.priority}}</td>
                  <td><button aria-label="Assign \\${{ticket.title}}" data-title="\\${{ticket.title}}">Assign</button></td>
                </tr>
              \\`).join('');
            }}
            document.querySelector('#run').addEventListener('click', render);
            document.querySelector('tbody').addEventListener('click', event => {{
              const button = event.target.closest('button[data-title]');
              if (button) document.querySelector('#toast').textContent = \\`Assigned \\${{button.dataset.title}}\\`;
            }});
            render();
            </script>
          `);
          await page.getByLabel('Status').selectOption('open');
          await page.getByLabel('Owner').fill('Sam');
          await page.getByLabel('Urgent only').check();
          await page.getByRole('button', {{ name: 'Run triage' }}).click();
          assert(await page.locator('tbody tr').count() === 1);
          assert((await page.locator('tbody tr').first().innerText()).includes('Invoice export'));
          await page.getByRole('button', {{ name: 'Assign Invoice export' }}).click();
          assert(await page.getByRole('status').textContent() === 'Assigned Invoice export');
        }}

        async function research_navigation_workflow(page) {{
          const detailUrl = dataUrl(`
            <title>Alpine Detail</title>
            <article>
              <h1>Alpine expansion report</h1>
              <dl><dt>Revenue</dt><dd>$4.2M</dd><dt>Risk</dt><dd>Low</dd></dl>
            </article>
          `);
          await page.goto(dataUrl(`
            <title>Research Home</title>
            <main>
              <input aria-label="Research query" value="alpine">
              <a href="${{detailUrl}}">Open Alpine report</a>
              <button id="save" onclick="document.body.dataset.saved='alpine'">Save result</button>
            </main>
          `));
          assert(await page.getByLabel('Research query').inputValue() === 'alpine');
          const detailHref = await page.getByRole('link', {{ name: 'Open Alpine report' }}).getAttribute('href');
          await page.goto(detailHref);
          await page.waitForLoadState();
          assert(await page.title() === 'Alpine Detail');
          assert((await page.locator('article').innerText()).includes('Revenue'));
          await page.goBack();
          await page.waitForLoadState();
          await page.getByRole('button', {{ name: 'Save result' }}).click();
          assert(await page.evaluate(() => document.body.dataset.saved) === 'alpine');
        }}

        const allCases = [
          ['goto_and_title', goto_and_title],
          ['set_content_and_read_text', set_content_and_read_text],
          ['evaluate_json', evaluate_json],
          ['click_button', click_button],
          ['fill_input', fill_input],
          ['type_input', type_input],
          ['locator_count', locator_count],
          ['locator_nth_text', locator_nth_text],
          ['role_locator', role_locator],
          ['text_locator', text_locator],
          ['wait_for_selector', wait_for_selector],
          ['screenshot', screenshot],
          ['webvoyager_checkout_workflow', webvoyager_checkout_workflow],
          ['mind2web_table_triage_workflow', mind2web_table_triage_workflow],
          ['research_navigation_workflow', research_navigation_workflow],
        ];
        const requestedCases = {requested_cases};
        const requestedSet = new Set(requestedCases);
        const missingCases = requestedCases.filter(name => !allCases.some(([caseName]) => caseName === name));
        if (missingCases.length) throw new Error(`unknown benchmark case(s): ${{missingCases.join(', ')}}`);
        const cases = requestedCases.length ? allCases.filter(([name]) => requestedSet.has(name)) : allCases;

        (async () => {{
          let browserVersion = null;
          const samples = [];
          const lifecycle = {json.dumps(lifecycle)};
          if (lifecycle === 'cold-browser') {{
            for (let index = 0; index < {iterations}; index++) {{
              const timings = {{}};
              for (const [name, fn] of cases) {{
                const started = performance.now();
                const browser = await launchChromium();
                browserVersion = browserVersion || await browser.version();
                try {{
                  const page = await browser.newPage();
                  try {{
                    await fn(page);
                    timings[name] = (performance.now() - started) / 1000;
                  }} finally {{
                    await page.close().catch(() => {{}});
                  }}
                }} finally {{
                  await browser.close().catch(() => {{}});
                }}
              }}
              samples.push(timings);
            }}
          }} else {{
            const browser = await launchChromium();
            browserVersion = await browser.version();
            try {{
              for (let index = 0; index < {iterations}; index++) {{
                const timings = {{}};
                if (lifecycle === 'warm-page') {{
                  const page = await browser.newPage();
                  try {{
                    for (const [name, fn] of cases) {{
                      const started = performance.now();
                      await fn(page);
                      timings[name] = (performance.now() - started) / 1000;
                    }}
                  }} finally {{
                    await page.close().catch(() => {{}});
                  }}
                }} else {{
                  for (const [name, fn] of cases) {{
                    const page = await browser.newPage();
                    const started = performance.now();
                    try {{
                      await fn(page);
                      timings[name] = (performance.now() - started) / 1000;
                    }} finally {{
                      await page.close().catch(() => {{}});
                    }}
                  }}
                }}
                samples.push(timings);
              }}
            }} finally {{
              await browser.close().catch(() => {{}});
            }}
          }}
          const result = {{
            implementation: 'typescript-playwright',
            iterations: {iterations},
            metadata: {{
              suite: 'equivalent',
              lifecycle: {json.dumps(lifecycle)},
              case_count: cases.length,
              comparison_mode: 'playwright_api_equivalent_cases',
              node: process.version,
              platform: process.platform,
              arch: process.arch,
              browser_version: browserVersion,
              playwright_ts_package: process.env.PLAYWRIGHT_TS_PACKAGE || null,
              browser_executable: process.env.BENCHMARK_CHROMIUM_EXECUTABLE || process.env.RUSTWRIGHT_CHROMIUM || process.env.CHROME || process.env.CHROMIUM || null,
            }},
            cases: Object.fromEntries(cases.map(([name]) => {{
              const values = samples.map(sample => sample[name] * 1000).sort((a, b) => a - b);
              const mean = values.reduce((total, value) => total + value, 0) / values.length;
              const midpoint = Math.floor(values.length / 2);
              const median = values.length % 2 ? values[midpoint] : (values[midpoint - 1] + values[midpoint]) / 2;
              const variance = values.length > 1 ? values.reduce((total, value) => total + Math.pow(value - mean, 2), 0) / (values.length - 1) : 0;
              return [name, {{
                mean_ms: mean,
                median_ms: median,
                p25_ms: values[Math.floor((values.length - 1) * 0.25)],
                p75_ms: values[Math.floor((values.length - 1) * 0.75)],
                min_ms: values[0],
                max_ms: values[values.length - 1],
                stdev_ms: Math.sqrt(variance),
              }}];
            }})),
          }};
          result.total_mean_ms = Object.values(result.cases).reduce((total, value) => total + value.mean_ms, 0);
          console.log('BENCHMARK_JSON ' + JSON.stringify(result));
        }})().catch(error => {{
          console.error(error && error.stack ? error.stack : error);
          process.exit(1);
        }});
        """
    )


def typescript_puppeteer_code(
    iterations: int,
    *,
    lifecycle: str = "warm-browser",
    case_filters: list[str] | None = None,
) -> str:
    requested_cases = json.dumps(case_filters or [])
    footer = typescript_stats_footer("typescript-puppeteer", iterations, "equivalent", lifecycle)
    return textwrap.dedent(
        f"""
        const puppeteer = require(process.env.PUPPETEER_PACKAGE_PATH);
        const {{ performance }} = require('node:perf_hooks');

        function dataUrl(html) {{
          return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
        }}

        function assert(condition, message = 'assertion failed') {{
          if (!condition) throw new Error(message);
        }}

        async function text(page, selector) {{
          return await page.$eval(selector, el => el.textContent);
        }}

        async function innerText(page, selector) {{
          return await page.$eval(selector, el => el.innerText);
        }}

        async function value(page, selector) {{
          return await page.$eval(selector, el => el.value);
        }}

        async function setValue(page, selector, nextValue) {{
          await page.$eval(selector, (el, value) => {{
            el.value = value;
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
          }}, nextValue);
        }}

        async function clickAria(page, label) {{
          await page.click(`[aria-label="${{label}}"]`);
        }}

        async function textLocatorVisible(page, needle) {{
          return await page.evaluate((needle) => {{
            const normalize = value => String(value ?? '').replace(/\\s+/g, ' ').trim();
            const elementText = node => {{
              if (!node) return '';
              if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
              if (node.nodeType !== Node.ELEMENT_NODE) return '';
              const tag = node.tagName || '';
              const type = String(node.getAttribute('type') || '').toLowerCase();
              if (tag === 'INPUT') {{
                if (['button', 'submit', 'reset'].includes(type)) return node.value || (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : '');
                if (type === 'image') return node.getAttribute('alt') || node.getAttribute('title') || node.value || '';
                return '';
              }}
              if (tag === 'TEXTAREA') return node.value || node.textContent || '';
              let text = '';
              for (const child of node.childNodes) text += elementText(child);
              return text;
            }};
            const visible = el => {{
              if (!el || !el.isConnected) return false;
              if ((el.tagName || '') === 'OPTION') return el.parentElement ? visible(el.parentElement) : false;
              const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
              const style = view.getComputedStyle(el);
              if (style.visibility === 'hidden' || style.display === 'none') return false;
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            }};
            const expected = normalize(needle).toLowerCase();
            const matchesText = el => normalize(elementText(el)).toLowerCase().includes(expected);
            const candidates = Array.from(document.querySelectorAll('*')).filter(matchesText);
            const matches = candidates.filter(el => !Array.from(el.children || []).some(matchesText));
            return visible(matches[0] || null);
          }}, needle);
        }}

        async function launchChromium() {{
          return await puppeteer.launch({{
            headless: true,
            executablePath: process.env.PUPPETEER_EXECUTABLE_PATH,
            args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check'],
          }});
        }}

        async function goto_and_title(page) {{
          await page.goto(dataUrl('<title>Bench</title><main>ready</main>'));
          assert(await page.title() === 'Bench');
        }}

        async function set_content_and_read_text(page) {{
          await page.setContent('<section><h1>Dashboard</h1><p id="status">Ready</p></section>');
          assert(await text(page, '#status') === 'Ready');
        }}

        async function evaluate_json(page) {{
          await page.setContent('<div></div>');
          const value = await page.evaluate(() => ({{ sum: 1 + 2, ok: true }}));
          assert(value.sum === 3 && value.ok === true);
          assert(Number.isNaN(await page.evaluate(() => Number.NaN)));
          assert(await page.evaluate(() => Infinity) === Infinity);
          assert(await page.evaluate(() => -Infinity) === -Infinity);
          assert(Object.is(await page.evaluate(() => -0), -0));
          const values = await page.evaluate(() => ({{
            name: 'json payload',
            ok: true,
            count: 3,
            nested: [1, 'two', {{ ok: true, count: 3 }}]
          }}));
          assert(values.name === 'json payload');
          assert(values.ok === true && values.count === 3);
          assert(values.nested[0] === 1 && values.nested[1] === 'two');
          assert(values.nested[2].ok === true && values.nested[2].count === 3);
        }}

        async function click_button(page) {{
          await page.setContent('<button id="go" onclick="document.body.dataset.clicked=\\'yes\\'">Go</button>');
          await page.click('#go');
          assert(await page.evaluate(() => document.body.dataset.clicked) === 'yes');
        }}

        async function fill_input(page) {{
          await page.setContent(`
            <input id='email'>
            <input id='hidden-email' style='display:none' value='hidden'>
            <input id='disabled-email' disabled value='disabled'>
            <input id='readonly-email' readonly value='readonly'>
            <input id='checkbox-email' type='checkbox' value='old'>
            <input id='number-code' type='number'>
            <input id='date-code' type='date'>
            <select id='plan'><option value='basic'>Basic</option><option value='pro' selected>Pro</option></select>
            <button id='button-email' value='button-value'>Button</button>
            <div id='editable-email' contenteditable>editable</div>
            <div id='plain-email'>plain</div>
          `);
          await page.type('#email', 'user@example.com');
          assert(await value(page, '#email') === 'user@example.com');
          assert(await page.$eval('#plain-email', el => el.tagName) === 'DIV');
          assert(await page.$eval('#plan', el => el.tagName) === 'SELECT');
          assert(await page.$eval('#checkbox-email', el => el.type) === 'checkbox');
          assert(await page.$eval('#number-code', el => el.type) === 'number');
          assert(await page.$eval('#date-code', el => el.type) === 'date');
          assert(await value(page, '#plan') === 'pro');
          assert(await page.$eval('#button-email', el => el.tagName) === 'BUTTON');
          await setValue(page, '#email', '');
          assert(await value(page, '#email') === '');
          await setValue(page, '#email', 'forced@example.com');
          assert(await value(page, '#email') === 'forced@example.com');
          assert(await value(page, '#hidden-email') === 'hidden');
          assert(await value(page, '#disabled-email') === 'disabled');
          assert(await value(page, '#readonly-email') === 'readonly');
          await page.$eval('#editable-email', el => {{ el.textContent = 'forced editable'; }});
          assert(await text(page, '#editable-email') === 'forced editable');
        }}

        async function type_input(page) {{
          await page.setContent('<input id="message">');
          await page.type('#message', 'hello');
          assert(await value(page, '#message') === 'hello');
        }}

        async function locator_count(page) {{
          await page.setContent('<ul>' + Array.from({{ length: 25 }}, (_, i) => `<li>Item ${{i}}</li>`).join('') + '</ul>');
          assert(await page.$$eval('li', items => items.length) === 25);
        }}

        async function locator_nth_text(page) {{
          await page.setContent('<ul><li>first</li><li>second</li><li>third</li></ul>');
          assert(await page.$$eval('li', items => items[2].innerText) === 'third');
        }}

        async function role_locator(page) {{
          await page.setContent('<button aria-label="Save record">Save</button>');
          assert(await page.$eval('button[aria-label*=Save]', el => !!el));
        }}

        async function text_locator(page) {{
          await page.setContent('<article><p>Quarterly revenue report</p></article>');
          assert(await textLocatorVisible(page, 'revenue'));
        }}

        async function wait_for_selector(page) {{
          await page.setContent('<main id="root"><div id="hidden" style="display:none">Hidden</div><div id="gone">Gone</div><iframe srcdoc="<span id=&quot;frame-hidden&quot; style=&quot;display:none&quot;>Hidden</span>"></iframe></main>');
          await page.evaluate(() => setTimeout(() => {{
            const node = document.createElement('div');
            node.id = 'done';
            node.textContent = 'Done';
            document.querySelector('#root').appendChild(node);
            document.querySelector('#gone').remove();
          }}, 20));
          const done = await page.waitForSelector('#done', {{ timeout: 2000 }});
          assert(await done.evaluate(el => el.textContent) === 'Done');
          await page.waitForSelector('#hidden', {{ hidden: true, timeout: 500 }});
          await page.waitForSelector('#missing', {{ hidden: true, timeout: 500 }});
          await page.waitForSelector('#gone', {{ hidden: true, timeout: 500 }});
          const root = await page.$('#root');
          if (typeof root.waitForSelector === 'function') {{
            await root.waitForSelector('#hidden', {{ hidden: true, timeout: 500 }});
            await root.waitForSelector('#gone', {{ hidden: true, timeout: 500 }});
            await root.waitForSelector('#missing', {{ hidden: true, timeout: 500 }});
          }} else {{
            assert(await root.$eval('#hidden', el => getComputedStyle(el).display) === 'none');
            assert(await root.$('#gone') === null);
            assert(await root.$('#missing') === null);
          }}
          const frame = page.frames()[1];
          assert(await frame.$eval('#frame-hidden', el => getComputedStyle(el).display) === 'none');
        }}

        async function screenshot(page) {{
          await page.setContent('<h1>Screenshot</h1>');
          const png = await page.screenshot();
          assert(png[0] === 0x89 && png[1] === 0x50 && png[2] === 0x4e && png[3] === 0x47);
        }}

        async function webvoyager_checkout_workflow(page) {{
          await page.setContent(`
            <main>
              <label>Search catalog <input id="query" placeholder="Search catalog"></label>
              <label>Category <select id="category"><option value="all">All</option><option value="travel">Travel</option><option value="office">Office</option></select></label>
              <label>Maximum price <input id="max-price" type="number" value="999"></label>
              <button id="apply">Apply filters</button>
              <section id="results" aria-label="Results"></section>
              <output id="cart-count" aria-label="Cart count">0</output>
              <output id="cart-total" aria-label="Cart total">$0</output>
              <label>Email <input id="email" type="email"></label>
              <button id="place-order">Place order</button>
              <strong id="confirmation"></strong>
            </main>
            <script>
            const products = [
              {{ name: 'Noise cancelling headphones', category: 'travel', price: 129 }},
              {{ name: 'Travel adapter', category: 'travel', price: 29 }},
              {{ name: 'Desk lamp', category: 'office', price: 64 }},
              {{ name: 'Notebook set', category: 'office', price: 18 }}
            ];
            const cart = [];
            function render() {{
              const query = document.querySelector('#query').value.toLowerCase();
              const category = document.querySelector('#category').value;
              const max = Number(document.querySelector('#max-price').value || 999);
              const results = products.filter(product => product.name.toLowerCase().includes(query) && (category === 'all' || product.category === category) && product.price <= max);
              document.querySelector('#results').innerHTML = results.map(product => '<article data-testid="result"><h2>' + product.name + '</h2><p>$' + product.price + '</p><button aria-label="Add ' + product.name + '" data-name="' + product.name + '">Add</button></article>').join('');
            }}
            document.querySelector('#apply').addEventListener('click', render);
            document.querySelector('#results').addEventListener('click', event => {{
              const button = event.target.closest('button[data-name]');
              if (!button) return;
              const product = products.find(item => item.name === button.dataset.name);
              cart.push(product);
              document.querySelector('#cart-count').textContent = String(cart.length);
              document.querySelector('#cart-total').textContent = '$' + cart.reduce((total, item) => total + item.price, 0);
            }});
            document.querySelector('#place-order').addEventListener('click', () => {{
              document.querySelector('#confirmation').textContent = 'Confirmed ' + cart.length + ' items for ' + document.querySelector('#email').value;
            }});
            render();
            </script>
          `);
          await setValue(page, '#query', 'travel');
          await page.click('#apply');
          assert(await page.$$eval('[data-testid=result]', items => items.length) === 1);
          await clickAria(page, 'Add Travel adapter');
          await setValue(page, '#query', 'noise');
          await page.select('#category', 'travel');
          await setValue(page, '#max-price', '150');
          await page.click('#apply');
          assert(await page.$$eval('[data-testid=result]', items => items.length) === 1);
          await clickAria(page, 'Add Noise cancelling headphones');
          await setValue(page, '#email', 'ada@example.com');
          await page.click('#place-order');
          assert(await text(page, '#cart-count') === '2');
          assert(await text(page, '#cart-total') === '$158');
          assert(await text(page, '#confirmation') === 'Confirmed 2 items for ada@example.com');
        }}

        async function mind2web_table_triage_workflow(page) {{
          await page.setContent(`
            <main>
              <label>Status <select id="status"><option value="all">All</option><option value="open">Open</option><option value="closed">Closed</option></select></label>
              <label>Owner <input id="owner"></label>
              <label><input id="urgent" type="checkbox"> Urgent only</label>
              <button id="run">Run triage</button>
              <table><tbody></tbody></table>
              <div id="toast" role="status"></div>
            </main>
            <script>
            const tickets = [
              {{ title: 'Invoice export', owner: 'Sam', status: 'open', priority: 'P0' }},
              {{ title: 'Login copy', owner: 'Rae', status: 'open', priority: 'P2' }},
              {{ title: 'Billing retry', owner: 'Sam', status: 'closed', priority: 'P1' }},
              {{ title: 'Webhook audit', owner: 'Sam', status: 'open', priority: 'P1' }}
            ];
            function render() {{
              const status = document.querySelector('#status').value;
              const owner = document.querySelector('#owner').value.toLowerCase();
              const urgent = document.querySelector('#urgent').checked;
              const rows = tickets.filter(ticket => (status === 'all' || ticket.status === status) && (!owner || ticket.owner.toLowerCase().includes(owner)) && (!urgent || ticket.priority === 'P0'));
              document.querySelector('tbody').innerHTML = rows.map(ticket => '<tr><td>' + ticket.title + '</td><td>' + ticket.owner + '</td><td>' + ticket.status + '</td><td>' + ticket.priority + '</td><td><button aria-label="Assign ' + ticket.title + '" data-title="' + ticket.title + '">Assign</button></td></tr>').join('');
            }}
            document.querySelector('#run').addEventListener('click', render);
            document.querySelector('tbody').addEventListener('click', event => {{
              const button = event.target.closest('button[data-title]');
              if (button) document.querySelector('#toast').textContent = 'Assigned ' + button.dataset.title;
            }});
            render();
            </script>
          `);
          await page.select('#status', 'open');
          await setValue(page, '#owner', 'Sam');
          await page.click('#urgent');
          await page.click('#run');
          assert(await page.$$eval('tbody tr', rows => rows.length) === 1);
          assert((await innerText(page, 'tbody tr')).includes('Invoice export'));
          await clickAria(page, 'Assign Invoice export');
          assert(await text(page, '#toast') === 'Assigned Invoice export');
        }}

        async function research_navigation_workflow(page) {{
          const detailUrl = dataUrl('<title>Alpine Detail</title><article><h1>Alpine expansion report</h1><dl><dt>Revenue</dt><dd>$4.2M</dd><dt>Risk</dt><dd>Low</dd></dl></article>');
          await page.goto(dataUrl('<title>Research Home</title><main><input aria-label="Research query" value="alpine"><a href="' + detailUrl.replace(/"/g, '&quot;') + '">Open Alpine report</a><button id="save" onclick="document.body.dataset.saved=\\'alpine\\'">Save result</button></main>'));
          assert(await value(page, '[aria-label="Research query"]') === 'alpine');
          const detailHref = await page.$eval('a', el => el.href);
          await page.goto(detailHref);
          assert(await page.title() === 'Alpine Detail');
          assert((await innerText(page, 'article')).includes('Revenue'));
          await page.goBack();
          await page.click('#save');
          assert(await page.evaluate(() => document.body.dataset.saved) === 'alpine');
        }}

        const allCases = [
          ['goto_and_title', goto_and_title],
          ['set_content_and_read_text', set_content_and_read_text],
          ['evaluate_json', evaluate_json],
          ['click_button', click_button],
          ['fill_input', fill_input],
          ['type_input', type_input],
          ['locator_count', locator_count],
          ['locator_nth_text', locator_nth_text],
          ['role_locator', role_locator],
          ['text_locator', text_locator],
          ['wait_for_selector', wait_for_selector],
          ['screenshot', screenshot],
          ['webvoyager_checkout_workflow', webvoyager_checkout_workflow],
          ['mind2web_table_triage_workflow', mind2web_table_triage_workflow],
          ['research_navigation_workflow', research_navigation_workflow],
        ];
        const requestedCases = {requested_cases};
        const requestedSet = new Set(requestedCases);
        const missingCases = requestedCases.filter(name => !allCases.some(([caseName]) => caseName === name));
        if (missingCases.length) throw new Error(`unknown benchmark case(s): ${{missingCases.join(', ')}}`);
        const cases = requestedCases.length ? allCases.filter(([name]) => requestedSet.has(name)) : allCases;

        (async () => {{
          let browserVersion = null;
          const samples = [];
          const lifecycle = {json.dumps(lifecycle)};
          if (lifecycle === 'cold-browser') {{
            for (let index = 0; index < {iterations}; index++) {{
              const timings = {{}};
              for (const [name, fn] of cases) {{
                const started = performance.now();
                const browser = await launchChromium();
                browserVersion = browserVersion || await browser.version();
                try {{
                  const page = await browser.newPage();
                  try {{
                    await fn(page);
                    timings[name] = (performance.now() - started) / 1000;
                  }} finally {{
                    await page.close().catch(() => {{}});
                  }}
                }} finally {{
                  await browser.close().catch(() => {{}});
                }}
              }}
              samples.push(timings);
            }}
          }} else {{
            const browser = await launchChromium();
            browserVersion = await browser.version();
            try {{
              for (let index = 0; index < {iterations}; index++) {{
                const timings = {{}};
                if (lifecycle === 'warm-page') {{
                  const page = await browser.newPage();
                  try {{
                    for (const [name, fn] of cases) {{
                      const started = performance.now();
                      await fn(page);
                      timings[name] = (performance.now() - started) / 1000;
                    }}
                  }} finally {{
                    await page.close().catch(() => {{}});
                  }}
                }} else {{
                  for (const [name, fn] of cases) {{
                    const page = await browser.newPage();
                    const started = performance.now();
                    try {{
                      await fn(page);
                      timings[name] = (performance.now() - started) / 1000;
                    }} finally {{
                      await page.close().catch(() => {{}});
                    }}
                  }}
                }}
                samples.push(timings);
              }}
            }} finally {{
              await browser.close().catch(() => {{}});
            }}
          }}
        {textwrap.indent(footer, "  ")}
        }})().catch(error => {{
          console.error(error && error.stack ? error.stack : error);
          process.exit(1);
        }});
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--impl",
        choices=[
            "rustwright",
            "playwright",
            "typescript-playwright",
            "typescript-puppeteer",
            "all",
        ],
        default="rustwright",
    )
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument(
        "--reference-path",
        default=str(ROOT / ".audit-playwright"),
        help="Path containing a real Playwright installation for --impl playwright or --impl all.",
    )
    parser.add_argument("--suite", choices=["equivalent", "strict"], default="equivalent")
    parser.add_argument(
        "--lifecycle",
        choices=["warm-browser", "warm-page", "cold-browser", "cold-container"],
        default="warm-browser",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_filters",
        help="Run only the named Python benchmark case. Repeat for multiple cases.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.suite == "strict" and args.impl not in STRICT_IMPLS | {"all"}:
        raise SystemExit("--suite strict currently supports only --impl rustwright, --impl playwright, or --impl all")
    if args.impl == "all":
        results = [
            run_playwright_like(
                "rustwright",
                args.iterations,
                suite=args.suite,
                lifecycle=args.lifecycle,
                case_filters=args.case_filters,
            )
        ]
        try:
            results.append(
                run_playwright_like(
                    "playwright",
                    args.iterations,
                    reference_path=args.reference_path,
                    suite=args.suite,
                    lifecycle=args.lifecycle,
                    case_filters=args.case_filters,
                )
            )
        except PlaywrightReferenceUnavailable as error:
            results.append({"implementation": "playwright", "status": "skipped", "reason": str(error)})
        if args.suite == "equivalent":
            try:
                results.append(
                    run_typescript_playwright(
                        args.iterations,
                        reference_path=args.reference_path,
                        suite=args.suite,
                        lifecycle=args.lifecycle,
                        case_filters=args.case_filters,
                    )
                )
            except TypeScriptPlaywrightUnavailable as error:
                results.append({"implementation": "typescript-playwright", "status": "skipped", "reason": str(error)})
            if include_puppeteer_in_all():
                try:
                    results.append(
                        run_typescript_puppeteer(
                            args.iterations,
                            reference_path=args.reference_path,
                            suite=args.suite,
                            lifecycle=args.lifecycle,
                            case_filters=args.case_filters,
                        )
                    )
                except TypeScriptPuppeteerUnavailable as error:
                    results.append({"implementation": "typescript-puppeteer", "status": "skipped", "reason": str(error)})
        result = {
            "implementation": "all",
            "iterations": args.iterations,
            "suite": args.suite,
            "lifecycle": args.lifecycle,
            "results": results,
            "speedups": speedup_report(results),
        }
    elif args.impl == "typescript-playwright":
        result = run_typescript_playwright(
            args.iterations,
            reference_path=args.reference_path,
            suite=args.suite,
            lifecycle=args.lifecycle,
            case_filters=args.case_filters,
        )
    elif args.impl == "typescript-puppeteer":
        result = run_typescript_puppeteer(
            args.iterations,
            reference_path=args.reference_path,
            suite=args.suite,
            lifecycle=args.lifecycle,
            case_filters=args.case_filters,
        )
    else:
        result = run_playwright_like(
            args.impl,
            args.iterations,
            reference_path=args.reference_path,
            suite=args.suite,
            lifecycle=args.lifecycle,
            case_filters=args.case_filters,
        )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.impl == "all":
        cases = filter_cases(select_cases(args.suite), args.case_filters)
        print(f"benchmark comparison across {len(cases)} cases and {args.iterations} iteration(s)")
        for item in result["results"]:
            if item.get("status") == "skipped":
                print(f"{item['implementation']:16s} skipped: {item['reason']}")
            else:
                print(f"{item['implementation']:16s} {item['total_mean_ms']:8.2f} ms total mean")
        for name, value in result["speedups"].items():
            print(f"{name}: {value:.1f}%")
    else:
        case_names = list(result["cases"])
        print(f"{args.impl}: {result['total_mean_ms']:.2f} ms mean across {len(case_names)} cases")
        for name, values in result["cases"].items():
            print(f"{name:24s} {values['mean_ms']:8.2f} ms mean {values['median_ms']:8.2f} ms median")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
