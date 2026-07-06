from __future__ import annotations

import argparse
import fnmatch
import inspect
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.automation_cases import CASES  # noqa: E402


def _is_connected(browser: Any) -> bool:
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


def _safe_close(target: Any) -> None:
    try:
        target.close()
    except Exception:
        pass


def _launch_chromium(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:
        message = str(exc)
        if "mach_port_rendezvous" not in message and "bootstrap_check_in" not in message:
            raise
        return playwright.chromium.launch(headless=True, args=["--single-process"])


def _load_sync_playwright(implementation: str, reference_path: str | None) -> Callable[..., Any]:
    if implementation == "rustwright":
        from rustwright.sync_api import sync_playwright

        return sync_playwright

    if implementation != "playwright":
        raise ValueError(f"unknown implementation: {implementation}")

    if reference_path:
        path = str(Path(reference_path).resolve())
        if path not in sys.path:
            sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "playwright" or name.startswith("playwright."):
            del sys.modules[name]
    module = importlib.import_module("playwright.sync_api")
    module_path = Path(getattr(module, "__file__", "")).resolve()
    if ROOT / "python" in module_path.parents or module_path.name == "sync_api.py" and "rustwright" in str(module_path):
        raise RuntimeError(
            "The local drop-in playwright alias is shadowing real Playwright. "
            "Pass --reference-path .audit-playwright or run outside the editable repo environment."
        )
    return module.sync_playwright


def _select_cases(patterns: list[str]) -> list[Callable[..., Any]]:
    if not patterns:
        return list(CASES)
    selected = [
        case
        for case in CASES
        if any(fnmatch.fnmatchcase(case.__name__, pattern) for pattern in patterns)
    ]
    missing = [pattern for pattern in patterns if not any(fnmatch.fnmatchcase(case.__name__, pattern) for case in CASES)]
    if missing:
        raise ValueError(f"no parity cases matched: {', '.join(missing)}")
    return selected


def run_cases(sync_playwright: Callable[..., Any], cases: list[Callable[..., Any]] | None = None) -> list[dict[str, str]]:
    cases = cases or list(CASES)
    results: list[dict[str, str]] = []
    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            for case in cases:
                if not _is_connected(browser):
                    _safe_close(browser)
                    browser = _launch_chromium(p)
                try:
                    page = browser.new_page()
                except Exception:
                    if _is_connected(browser):
                        raise
                    _safe_close(browser)
                    browser = _launch_chromium(p)
                    page = browser.new_page()
                try:
                    parameters = inspect.signature(case).parameters
                    if "playwright" in parameters:
                        case(page, playwright=p)
                    else:
                        case(page)
                    results.append({"case": case.__name__, "status": "passed"})
                finally:
                    _safe_close(page)
        finally:
            _safe_close(browser)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run shared core automation parity cases.")
    parser.add_argument("--impl", choices=["rustwright", "playwright"], required=True)
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only parity cases whose names match this exact name or shell-style glob. May be repeated.",
    )
    parser.add_argument(
        "--reference-path",
        default=str(ROOT / ".audit-playwright"),
        help="Path containing a real Playwright installation for --impl playwright.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    sync_playwright = _load_sync_playwright(args.impl, args.reference_path)
    cases = _select_cases(args.case)
    results = run_cases(sync_playwright, cases)
    payload = {
        "implementation": args.impl,
        "cases": results,
        "passed": sum(1 for result in results if result["status"] == "passed"),
        "total": len(results),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{args.impl}: {payload['passed']}/{payload['total']} parity cases passed")
        for result in results:
            print(f"{result['case']}: {result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
