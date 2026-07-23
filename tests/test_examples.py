from __future__ import annotations

import ast
import struct
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EXAMPLE_TIMEOUT_SECONDS = 60
SCREENSHOT_OUTPUT = ROOT / "screenshot_element.png"


def _required_example(filename: str) -> Path:
    script = EXAMPLES / filename
    assert script.is_file(), f"Missing required example script: examples/{filename}"
    return script


@lru_cache(maxsize=1)
def _chromium_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from rustwright.sync_api import sync_playwright; "
                "playwright = sync_playwright().start(); "
                "print('available' if playwright.chromium.executable_path else 'missing'); "
                "playwright.stop()"
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )


def _require_chromium() -> None:
    probe = _chromium_probe()
    assert probe.returncode == 0, (
        "Could not inspect Rustwright's Chromium installation.\n"
        f"stdout:\n{probe.stdout}\n"
        f"stderr:\n{probe.stderr}"
    )
    if probe.stdout.strip() == "missing":
        pytest.skip("Chromium/Chrome executable not found")
    assert probe.stdout.strip() == "available", probe.stdout


def _run_example(filename: str) -> subprocess.CompletedProcess[str]:
    script = _required_example(filename)
    _require_chromium()
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=EXAMPLE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"examples/{filename} exited with status {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _marker_value(stdout: str, marker: str) -> str:
    matching_lines = [line for line in stdout.splitlines() if line.startswith(marker)]
    assert matching_lines, f"Expected stdout to contain a line starting with {marker!r}.\nstdout:\n{stdout}"
    assert len(matching_lines) == 1, f"Expected exactly one {marker!r} line, got {matching_lines!r}"
    return matching_lines[0][len(marker) :].strip()


def test_fill_form_example() -> None:
    """Contract: submit two fields and print `submitted: Ada Lovelace (ada@example.test)`."""
    result = _run_example("fill_form.py")

    submitted = _marker_value(result.stdout, "submitted: ")
    assert submitted == "Ada Lovelace (ada@example.test)"


def test_scrape_table_example() -> None:
    """Contract: print `rows: ` followed by this fixture table as a list of dictionaries."""
    result = _run_example("scrape_table.py")

    rendered_rows = _marker_value(result.stdout, "rows: ")
    try:
        rows = ast.literal_eval(rendered_rows)
    except (SyntaxError, ValueError) as exc:
        raise AssertionError(f"The rows marker must contain a Python literal, got {rendered_rows!r}") from exc
    assert rows == [
        {"Product": "Notebook", "Price": "$4.50", "Stock": "12"},
        {"Product": "Pen", "Price": "$1.25", "Stock": "40"},
    ]


def test_screenshot_element_example() -> None:
    """Contract: save one element to `screenshot_element.png` and print `saved: <PNG path>`."""
    _required_example("screenshot_element.py")
    assert not SCREENSHOT_OUTPUT.exists(), (
        f"Refusing to overwrite or remove an existing artifact: {SCREENSHOT_OUTPUT.name}"
    )

    try:
        result = _run_example("screenshot_element.py")
        rendered_path = _marker_value(result.stdout, "saved: ")
        saved_path = Path(rendered_path)
        if not saved_path.is_absolute():
            saved_path = ROOT / saved_path

        assert saved_path.resolve() == SCREENSHOT_OUTPUT.resolve(), (
            f"The example must save {SCREENSHOT_OUTPUT.name}, got {rendered_path!r}"
        )
        assert SCREENSHOT_OUTPUT.is_file(), f"Screenshot was not created: {SCREENSHOT_OUTPUT.name}"
        assert SCREENSHOT_OUTPUT.stat().st_size > 0, "Screenshot PNG is empty"
        png = SCREENSHOT_OUTPUT.read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "Screenshot does not have a valid PNG signature"
        width, height = struct.unpack(">II", png[16:24])
        # The 260px content width plus 24px padding on both sides produces a 308px border box.
        assert width == 308
        # Font rendering varies across Linux CI environments, so require a sane height instead of an exact value.
        assert 40 < height < 600
    finally:
        SCREENSHOT_OUTPUT.unlink(missing_ok=True)
