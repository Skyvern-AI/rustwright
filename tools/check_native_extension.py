#!/usr/bin/env python3
from __future__ import annotations

import inspect
import sys


REQUIRED_PAGE_METHODS = {
    "dispatch_mouse_events",
    "dispatch_mouse_click_sequence",
}

REQUIRED_WAIT_FOR_SELECTOR_PARAMETERS = [
    "self",
    "locator_json",
    "index",
    "state",
    "timeout_ms",
    "strict",
]


def main() -> int:
    try:
        from rustwright import _rustwright
    except Exception as exc:
        print(f"Could not import rustwright._rustwright: {exc}", file=sys.stderr)
        return 1

    missing = sorted(name for name in REQUIRED_PAGE_METHODS if not hasattr(_rustwright.Page, name))
    if missing:
        print(
            "rustwright native extension is stale; missing Page methods: "
            + ", ".join(missing)
            + ". Rebuild the Docker image or reinstall the Rust extension.",
            file=sys.stderr,
        )
        return 1

    try:
        parameters = list(inspect.signature(_rustwright.Page.wait_for_selector).parameters)
    except Exception as exc:
        print(f"Could not inspect Page.wait_for_selector signature: {exc}", file=sys.stderr)
        return 1

    if parameters != REQUIRED_WAIT_FOR_SELECTOR_PARAMETERS:
        print(
            "rustwright native extension is stale; Page.wait_for_selector signature is "
            f"{parameters}, expected {REQUIRED_WAIT_FOR_SELECTOR_PARAMETERS}. "
            "Rebuild the Docker image or reinstall the Rust extension.",
            file=sys.stderr,
        )
        return 1

    print("rustwright native extension ABI check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
