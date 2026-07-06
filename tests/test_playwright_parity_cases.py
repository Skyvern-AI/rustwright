from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.automation_cases import CASES  # noqa: E402


def _looks_like_reference_chromium_instability(output: str) -> bool:
    if "Page.captureScreenshot" in output and "Unable to capture screenshot" in output:
        return True
    if "page_event_waiters_reject_on_page_crash" in output and (
        "greenlet.error: cannot switch to a different thread" in output
        or "Target page, context or browser has been closed" in output
        or "Target closed" in output
    ):
        return True
    return (
        "TargetClosedError" in output
        and "chrome-headless-shell" in output
        and (
            "--single-process" in output
            or "MachPortRendezvousServer" in output
            or "bootstrap_check_in" in output
            or "LSNotificationCode" in output
        )
    )


def _run_parity(implementation: str) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_parity_cases.py"),
            "--impl",
            implementation,
            "--reference-path",
            str(ROOT / ".audit-playwright"),
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=240,
    )
    if proc.returncode != 0:
        output = proc.stderr or proc.stdout
        if implementation == "playwright" and _looks_like_reference_chromium_instability(output):
            pytest.xfail("real Playwright Chromium is unstable on this macOS host")
        raise AssertionError(output)
    return json.loads(proc.stdout)


@pytest.mark.parametrize("implementation", ["rustwright", "playwright"])
def test_core_automation_cases_match_playwright_and_rustwright(implementation):
    if implementation == "playwright" and not (ROOT / ".audit-playwright" / "playwright").is_dir():
        pytest.skip("real Playwright reference package is not installed in .audit-playwright")

    result = _run_parity(implementation)

    assert result["total"] == len(CASES)
    assert result["passed"] == result["total"]
