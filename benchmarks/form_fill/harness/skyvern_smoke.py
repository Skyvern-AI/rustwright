#!/usr/bin/env python3
"""Run a minimal authenticated CDP smoke against a short-lived session."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from typing import Any

from skyvern_session import SkyvernSession, redact_browser_address


def load_backends() -> list[tuple[str, Callable[[], Any]]]:
    from rustwright.sync_api import sync_playwright as rustwright_sync_playwright

    backends: list[tuple[str, Callable[[], Any]]] = [
        ("rustwright", rustwright_sync_playwright)
    ]
    try:
        from playwright.sync_api import sync_playwright as playwright_sync_playwright
    except ModuleNotFoundError:
        print(
            "Reference Playwright is not installed; running rustwright only.",
            file=sys.stderr,
        )
    else:
        backends.append(("playwright", playwright_sync_playwright))
    return backends


def run_backend(
    backend: str,
    sync_playwright: Callable[[], Any],
    browser_address: str,
    headers: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    summary: dict[str, Any] = {
        "backend": backend,
        "connect_ms": None,
        "goto_ms": None,
        "title": None,
        "ok": False,
    }
    browser = None
    context = None
    phase = "backend startup"
    try:
        with sync_playwright() as playwright:
            phase = "connect_over_cdp"
            connect_start = time.monotonic()
            browser = playwright.chromium.connect_over_cdp(
                browser_address, headers=headers, timeout=60_000
            )
            summary["connect_ms"] = round(
                (time.monotonic() - connect_start) * 1000.0, 3
            )
            phase = "dedicated context creation"
            context = browser.new_context()
            page = context.new_page()
            phase = "goto"
            goto_start = time.monotonic()
            page.goto(
                "https://example.com", wait_until="domcontentloaded", timeout=60_000
            )
            summary["goto_ms"] = round(
                (time.monotonic() - goto_start) * 1000.0, 3
            )
            phase = "title read"
            summary["title"] = page.title()
            summary["ok"] = summary["title"] == "Example Domain"
            if not summary["ok"]:
                return summary, "title validation"
            return summary, None
    except Exception:
        return summary, phase
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def main() -> int:
    backends = load_backends()
    failures: list[str] = []
    try:
        with SkyvernSession() as session:
            assert session.browser_address is not None
            print(
                "Skyvern CDP host: "
                + redact_browser_address(session.browser_address),
                file=sys.stderr,
            )
            for backend, sync_playwright in backends:
                summary, failed_phase = run_backend(
                    backend,
                    sync_playwright,
                    session.browser_address,
                    session.cdp_headers,
                )
                print(json.dumps(summary, separators=(",", ":")))
                if failed_phase is not None:
                    failures.append(f"{backend}: {failed_phase}")
    except Exception as exc:
        print(
            f"Skyvern session smoke failed during session lifecycle: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    for failure in failures:
        print(f"Smoke failure at {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
