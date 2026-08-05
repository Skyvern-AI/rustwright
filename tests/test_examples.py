from __future__ import annotations

import ast
import os
import signal
import struct
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EXAMPLE_TIMEOUT_SECONDS = 60
SUBPROCESS_DIAGNOSTIC_STREAM_CHARS = 16_000
SCREENSHOT_OUTPUT = ROOT / "screenshot_element.png"
PYTHON_EXAMPLE_BOOTSTRAP = """
import faulthandler
import runpy
import signal
import sys
from pathlib import Path

faulthandler.enable(file=sys.stderr, all_threads=True)
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
script = sys.argv[1]
sys.argv = [script]
sys.path[0] = str(Path(script).resolve().parent)
runpy.run_path(script, run_name="__main__")
"""


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


def _bounded_subprocess_stream(text: str) -> str:
    if len(text) <= SUBPROCESS_DIAGNOSTIC_STREAM_CHARS:
        return text
    half = SUBPROCESS_DIAGNOSTIC_STREAM_CHARS // 2
    omitted = len(text) - (half * 2)
    return f"{text[:half]}\n... {omitted} characters omitted ...\n{text[-half:]}"


def _run_python_script(script: Path, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-c", PYTHON_EXAMPLE_BOOTSTRAP, str(script)]
    started_at = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        alive_at_timeout = process.poll() is None
        stack_signal = getattr(signal, "SIGUSR1", None)
        stack_signal_sent = False
        if alive_at_timeout and stack_signal is not None:
            try:
                process.send_signal(stack_signal)
                stack_signal_sent = True
            except ProcessLookupError:
                pass
            if stack_signal_sent:
                # Give faulthandler a short failure-only window to write all
                # Python thread stacks before terminating the hung child.
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
        if os.name == "posix":
            # The Python child may have launched Chromium. Terminate the isolated
            # process group so a timed-out example cannot leak descendants into
            # later tests, even if the Python process exited after the stack signal.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate()
        elapsed_seconds = time.monotonic() - started_at
        signal_name = "SIGUSR1" if stack_signal is not None else "unavailable"
        raise AssertionError(
            "========== RUSTWRIGHT SUBPROCESS TIMEOUT DIAGNOSTIC BEGIN ==========\n"
            f"script={script.name!r} pid={process.pid} timeout_seconds={timeout_seconds} "
            f"elapsed_seconds={elapsed_seconds:.3f}\n"
            f"child_type=python alive_at_timeout={alive_at_timeout} "
            f"stack_signal={signal_name} stack_signal_sent={stack_signal_sent}\n"
            f"stdout_so_far:\n{_bounded_subprocess_stream(stdout)}\n"
            f"stderr_so_far_and_python_stacks:\n{_bounded_subprocess_stream(stderr)}\n"
            "========== RUSTWRIGHT SUBPROCESS TIMEOUT DIAGNOSTIC END =========="
        ) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_example(filename: str) -> subprocess.CompletedProcess[str]:
    script = _required_example(filename)
    _require_chromium()
    result = _run_python_script(script, EXAMPLE_TIMEOUT_SECONDS)
    assert result.returncode == 0, (
        f"examples/{filename} exited with status {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def test_python_example_timeout_captures_output_and_thread_stacks(tmp_path: Path) -> None:
    script = tmp_path / "hung_example.py"
    script.write_text(
        "import threading\n"
        "import time\n"
        "\n"
        "def background_worker():\n"
        "    time.sleep(10)\n"
        "\n"
        "threading.Thread(target=background_worker, name='fixture-worker').start()\n"
        "print('child reached hang', flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError) as exc_info:
        _run_python_script(script, timeout_seconds=0.5)

    diagnostic = str(exc_info.value)
    assert "RUSTWRIGHT SUBPROCESS TIMEOUT DIAGNOSTIC BEGIN" in diagnostic
    assert "child reached hang" in diagnostic
    if hasattr(signal, "SIGUSR1"):
        assert "stack_signal_sent=True" in diagnostic
        assert "background_worker" in diagnostic
        assert "hung_example.py" in diagnostic


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
