#!/usr/bin/env python3
"""Run the same guarded form-fill choreography with Playwright or Rustwright."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from harness.library_probe import run_library_probes


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "field_map.example.json"
HIGHLIGHT_JS = """element => {
    element.__formFillBenchmarkStyles = [
        element.style.outline,
        element.style.outlineOffset,
        element.style.boxShadow,
    ];
    element.style.outline = '3px solid #ff3b30';
    element.style.outlineOffset = '2px';
    element.style.boxShadow = '0 0 0 4px rgba(255,59,48,.25)';
}"""
RESTORE_HIGHLIGHT_JS = """element => {
    const saved = element.__formFillBenchmarkStyles || ['', '', ''];
    element.style.outline = saved[0];
    element.style.outlineOffset = saved[1];
    element.style.boxShadow = saved[2];
    delete element.__formFillBenchmarkStyles;
}"""
INIT_SUBMIT_GUARD_JS = """(() => {
    const state = {
        attempted: false,
        selector: null,
        blockedPersistentTransports: 0,
    };
    Object.defineProperty(window, '__formFillBenchmarkSubmitGuard', {
        configurable: false,
        writable: false,
        value: state,
    });
    const block = event => {
        state.attempted = true;
        event.preventDefault();
        event.stopImmediatePropagation();
    };
    document.addEventListener('submit', block, true);
    const prototype = HTMLFormElement.prototype;
    for (const method of ['submit', 'requestSubmit']) {
        Object.defineProperty(prototype, method, {
            configurable: false,
            writable: false,
            value: function guardedSubmission() {
                state.attempted = true;
                throw new Error('Form submission is disabled by the benchmark guard');
            },
        });
    }
    if ('ServiceWorkerContainer' in window) {
        Object.defineProperty(ServiceWorkerContainer.prototype, 'register', {
            configurable: false,
            writable: false,
            value: function blockedServiceWorkerRegistration() {
                return Promise.reject(
                    new Error('Service workers are disabled by the benchmark guard')
                );
            },
        });
    }
    const blockedTransport = name => class BlockedPersistentTransport {
        constructor() {
            state.blockedPersistentTransports += 1;
            throw new DOMException(
                `${name} is disabled by the benchmark guard`,
                'SecurityError'
            );
        }
    };
    for (const name of [
        'WebSocket',
        'WebTransport',
        'EventSource',
        'RTCPeerConnection',
        'webkitRTCPeerConnection',
        'Worker',
        'SharedWorker',
    ]) {
        if (name in window) {
            Object.defineProperty(window, name, {
                configurable: false,
                writable: false,
                value: blockedTransport(name),
            });
        }
    }
})();"""
DISABLE_SUBMIT_CONTROLS_JS = """selector => {
    const state = window.__formFillBenchmarkSubmitGuard;
    if (!state) {
        throw new Error('Pre-document form submission guard is not installed');
    }
    state.selector = selector;
    const controls = Array.from(document.querySelectorAll(selector));
    for (const control of controls) {
        control.disabled = true;
        control.setAttribute('data-form-fill-benchmark-guarded', 'true');
    }
    return controls.length;
}"""
READ_SUBMIT_GUARD_JS = """selector => {
    const state = window.__formFillBenchmarkSubmitGuard;
    const controls = Array.from(document.querySelectorAll(selector));
    return {
        installed: Boolean(state),
        attempted: Boolean(state && state.attempted),
        blockedPersistentTransports: state ? state.blockedPersistentTransports : 0,
        controlCount: controls.length,
        guardedCount: controls.filter(control =>
            control.disabled &&
            control.getAttribute('data-form-fill-benchmark-guarded') === 'true'
        ).length,
    };
}"""


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def parse_cdp_connect_headers(raw_headers: str) -> dict[str, str] | None:
    """Parse optional CDP headers without ever including their values in errors."""
    if not raw_headers.strip():
        return None
    try:
        payload = json.loads(raw_headers)
    except json.JSONDecodeError:
        raise ValueError("CDP_CONNECT_HEADERS must be valid JSON") from None
    if not isinstance(payload, dict) or not all(
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(value, str)
        for name, value in payload.items()
    ):
        raise ValueError("CDP_CONNECT_HEADERS must be a JSON object of string headers")
    return payload


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError(f"Unsupported field-map version in {path}")
    if not payload.get("ready_selector") or not payload.get("submit_selector"):
        raise ValueError("The field map requires ready_selector and submit_selector")
    fields = payload.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("The field map must contain at least one field")
    slugs: set[str] = set()
    for field in fields:
        required = {"label", "slug", "kind", "selector"}
        missing = required.difference(field)
        if missing:
            raise ValueError(f"Field is missing keys {sorted(missing)}: {field!r}")
        if field["kind"] not in {"text", "combobox", "file"}:
            raise ValueError(f"Unsupported field kind: {field['kind']!r}")
        if field["slug"] in slugs:
            raise ValueError(f"Duplicate field slug: {field['slug']!r}")
        slugs.add(field["slug"])
        if field["kind"] == "file":
            if "asset" not in field:
                raise ValueError(f"File field is incomplete: {field['label']!r}")
        elif "value" not in field:
            raise ValueError(f"Field has no value: {field['label']!r}")
    return payload


def visual_locator(page: Any, field: dict[str, Any]) -> Any:
    locator = page.locator(field.get("visual", field["selector"]))
    if "visual_index" in field:
        locator = locator.nth(int(field["visual_index"]))
    return locator


def perform_action(
    page: Any,
    field: dict[str, Any],
    config_dir: Path,
) -> None:
    control = page.locator(field["selector"])
    if field["kind"] == "text":
        control.fill(field["value"])
        actual = control.evaluate("element => element.value")
        if actual != field["value"]:
            raise AssertionError(
                f"{field['label']} did not retain its value: {actual!r}"
            )
        return

    if field["kind"] == "file":
        asset_root = (config_dir / "assets").resolve()
        if asset_root.parent != config_dir.resolve():
            raise ValueError("assets directory must not escape the field-map directory")
        asset = (config_dir / field["asset"]).resolve()
        if asset.parent != asset_root:
            raise ValueError(
                f"{field['label']} asset must be a direct child of {asset_root}"
            )
        if asset.suffix.lower() != ".pdf":
            raise ValueError(f"{field['label']} asset must be a PDF fixture")
        if not asset.is_file():
            raise FileNotFoundError(f"Missing upload asset for {field['label']}: {asset}")
        if asset.stat().st_size > 1024 * 1024:
            raise ValueError(f"{field['label']} asset exceeds the 1 MiB fixture limit")
        control.set_input_files(str(asset))
        # Some forms immediately replace the input after attempting an upload.
        # The context route blocks that request, so successful API completion is
        # the strongest safe assertion available without allowing data to leave.
        return

    control.click()
    control.type(field["value"])
    option = page.locator("[role=option]").first
    option_text = option.evaluate(
        "element => (element.innerText || element.textContent || '').trim()"
    )
    if field["value"].lower() not in option_text.lower():
        raise AssertionError(
            f"{field['label']} first option was {option_text!r}, "
            f"expected {field['value']!r}"
        )
    option.click()
    selected_text = visual_locator(page, field).evaluate(
        "element => (element.innerText || element.textContent || '').trim()"
    )
    expected = field.get("selected_text", field["value"])
    if expected.lower() not in selected_text.lower():
        raise AssertionError(
            f"{field['label']} did not retain selection {expected!r}: "
            f"{selected_text!r}"
        )


def make_remote_context(browser: Any) -> Any:
    try:
        return browser.new_context(
            viewport={"width": 400, "height": 600}, service_workers="block"
        )
    except Exception as exc:
        raise RuntimeError(
            "The remote CDP endpoint must allow a dedicated browser context; "
            "the benchmark will not reuse or mutate a provider-owned context"
        ) from exc


def install_network_guard(context: Any) -> tuple[list[str], list[str]]:
    """Block state changes and persistent transports below page JavaScript."""
    blocked_methods: list[str] = []
    blocked_websockets: list[str] = []

    def guard(route: Any) -> None:
        method = route.request.method.upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            route.continue_()
            return
        blocked_methods.append(method)
        route.abort("blockedbyclient")

    context.route("**/*", guard)

    def guard_websocket(route: Any) -> None:
        # A routed WebSocket has no network peer unless the handler calls
        # connect_to_server(). Leaving it disconnected avoids the synchronous
        # close-in-callback deadlock seen in some Playwright/Chromium pairs.
        blocked_websockets.append("websocket")

    context.route_web_socket("**/*", guard_websocket)
    return blocked_methods, blocked_websockets


def main() -> None:
    backend = os.environ.get("BACKEND", "playwright")
    if backend not in {"playwright", "rustwright"}:
        raise ValueError("BACKEND must be playwright or rustwright")
    if backend == "rustwright":
        from rustwright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    job_url = required_environment("BENCH_JOB_URL")
    config_path = Path(os.environ.get("BENCH_FIELD_CONFIG", DEFAULT_CONFIG)).resolve()
    config = load_config(config_path)
    output_dir = Path(os.environ.get("OUT_DIR", SCRIPT_DIR / "out" / backend)).resolve()
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    pause_scale = float(os.environ.get("BENCH_PAUSE_SCALE", "1"))
    if pause_scale < 0:
        raise ValueError("BENCH_PAUSE_SCALE must be non-negative")
    remote = bool(os.environ.get("CDP_URL", "").strip())
    skip_uploads_value = os.environ.get("BENCH_SKIP_UPLOADS", "1" if remote else "0")
    if skip_uploads_value not in {"0", "1"}:
        raise ValueError("BENCH_SKIP_UPLOADS must be 0 or 1")
    skip_uploads = skip_uploads_value == "1"
    skipped_fields = [
        field["label"]
        for field in config["fields"]
        if field.get("network_dependent")
        or (skip_uploads and field["kind"] == "file")
    ]
    fields = [
        field
        for field in config["fields"]
        if not field.get("network_dependent")
        and not (skip_uploads and field["kind"] == "file")
    ]

    script_start_epoch = time.time()
    total_start = time.monotonic()
    timeline: list[dict[str, Any]] = []
    timeline_cursor_s = 0.0
    scripted_pause_ms = 0.0
    per_field: list[dict[str, Any]] = []
    screenshot_count = 0
    probe = None

    def finish_interval(label: str, kind: str, end_s: float | None = None) -> None:
        nonlocal timeline_cursor_s
        if end_s is None:
            end_s = time.monotonic() - total_start
        timeline.append(
            {"label": label, "kind": kind, "t0_s": timeline_cursor_s, "t1_s": end_s}
        )
        timeline_cursor_s = end_s

    def pause(milliseconds: float) -> None:
        nonlocal scripted_pause_ms
        actual_ms = milliseconds * pause_scale
        time.sleep(actual_ms / 1000.0)
        scripted_pause_ms += actual_ms
        finish_interval(f"scripted pause ({actual_ms:g} ms)", "pause")

    with sync_playwright() as playwright:
        cdp_url = os.environ.get("CDP_URL", "").strip()
        if cdp_url:
            connect_options: dict[str, Any] = {"timeout": 120_000}
            cdp_headers = parse_cdp_connect_headers(
                os.environ.get("CDP_CONNECT_HEADERS", "")
            )
            if cdp_headers is not None:
                connect_options["headers"] = cdp_headers
            try:
                browser = playwright.chromium.connect_over_cdp(
                    cdp_url, **connect_options
                )
            except Exception as exc:
                raise RuntimeError(
                    "Remote CDP connection failed; endpoint details were omitted "
                    f"({type(exc).__name__})"
                ) from None
            context = make_remote_context(browser)
            context_mode = "remote_isolated_context"
            launch_label = "library startup and remote CDP connect"
        else:
            launch_options: dict[str, Any] = {
                "headless": os.environ.get("HEADED") != "1"
            }
            executable = os.environ.get("BENCH_CHROMIUM_EXECUTABLE", "").strip()
            if executable:
                launch_options["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                viewport={"width": 400, "height": 600}, service_workers="block"
            )
            context_mode = "local_context"
            launch_label = "library and browser launch"
        if context.service_workers:
            raise AssertionError("Refusing to use a context with active service workers")
        blocked_request_methods, blocked_websockets = install_network_guard(context)
        context.add_init_script(INIT_SUBMIT_GUARD_JS)
        page = context.new_page()
        launch_ms = (time.monotonic() - total_start) * 1000.0
        finish_interval(launch_label, "launch", launch_ms / 1000.0)

        try:
            navigation_start = time.monotonic()
            page.goto(job_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_selector(config["ready_selector"], timeout=120_000)
            # The page and field metadata are now present. Keep the entire
            # input phase offline so no event handler can transmit dummy data,
            # even through an idempotent-looking request or existing transport.
            context.set_offline(True)
            loaded_url = page.url
            navigation_ms = (time.monotonic() - navigation_start) * 1000.0
            finish_interval("page load", "nav")
            pause(5_000)

            submit_count = page.evaluate(
                DISABLE_SUBMIT_CONTROLS_JS, config["submit_selector"]
            )
            if submit_count < 1:
                raise AssertionError("Expected at least one submit control to guard")
            finish_interval("install no-submit guard", "action")

            fill_start = time.monotonic()
            for index, field in enumerate(fields, start=1):
                field_start = time.monotonic()
                visual = visual_locator(page, field)
                visual.scroll_into_view_if_needed()
                visual.evaluate(HIGHLIGHT_JS)
                finish_interval(f"{field['label']}: focus and highlight", "action")
                pause(200)
                try:
                    perform_action(page, field, config_path.parent)
                    finish_interval(f"{field['label']}: interact", "action")
                    pause(100)
                    screenshot_path = (
                        screenshots_dir / f"{index:03d}_{field['slug']}.png"
                    )
                    page.screenshot(path=str(screenshot_path))
                    screenshot_count += 1
                finally:
                    if field["kind"] != "file" or visual.count() > 0:
                        visual.evaluate(RESTORE_HIGHLIGHT_JS)
                finish_interval(f"{field['label']}: capture and restore", "action")
                per_field.append(
                    {
                        "label": field["label"],
                        "ms": round((time.monotonic() - field_start) * 1000.0, 3),
                    }
                )
            fill_ms = (time.monotonic() - fill_start) * 1000.0

            if page.url != loaded_url:
                raise AssertionError(f"Page unexpectedly navigated away: {page.url}")
            guard = page.evaluate(READ_SUBMIT_GUARD_JS, config["submit_selector"])
            if not guard["installed"] or guard["attempted"]:
                raise AssertionError(f"No-submit guard state is unsafe: {guard!r}")
            if guard["controlCount"] != submit_count or guard["guardedCount"] != submit_count:
                raise AssertionError(f"Submit controls lost their guard: {guard!r}")
            body_text = page.locator("body").evaluate(
                "element => (element.innerText || element.textContent || '').toLowerCase()"
            )
            for forbidden in (
                "application submitted",
                "thanks for applying",
                "thank you for applying",
            ):
                if forbidden in body_text:
                    raise AssertionError(
                        f"Submission-like confirmation text appeared: {forbidden!r}"
                    )
            if screenshot_count != len(fields) or len(per_field) != len(fields):
                raise AssertionError("A field action or screenshot did not complete")

            try:
                probe = run_library_probes(
                    page, reps=int(os.environ.get("BENCH_PROBE_REPS", "20"))
                )
            except Exception:
                probe = None
        finally:
            context.close()
            browser.close()

    total_end_s = time.monotonic() - total_start
    finish_interval("validation and teardown", "action", total_end_s)
    total_ms = total_end_s * 1000.0
    action_ms = round(total_ms - scripted_pause_ms, 3)
    result = {
        "launch_ms": round(launch_ms, 3),
        "navigation_ms": round(navigation_ms, 3),
        "fill_ms": round(fill_ms, 3),
        "per_field": per_field,
        "total_ms": round(total_ms, 3),
        "scripted_pause_ms": round(scripted_pause_ms, 3),
        "action_ms": action_ms,
        "library_latency_ms": action_ms,
        "page_wait_ms": 0,
        "fields_filled": len(per_field),
        "skipped_fields": skipped_fields,
        "screenshots": screenshot_count,
        "backend": backend,
        "connection": "remote_cdp" if remote else "local",
        "context_mode": context_mode,
        "submit_guard": "installed_and_not_triggered",
        "offline_during_fill": True,
        "blocked_state_changing_requests": len(blocked_request_methods),
        "blocked_websockets": len(blocked_websockets),
        "blocked_persistent_transport_attempts": guard[
            "blockedPersistentTransports"
        ],
        "library_probe": probe,
    }
    timeline_payload = {
        "script_start_epoch": script_start_epoch,
        "intervals": timeline,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "timeline.json").write_text(
        json.dumps(timeline_payload, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "timings.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print("BENCH_RESULT " + json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
