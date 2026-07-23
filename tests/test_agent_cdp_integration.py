import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from rustwright._agent import cli
from rustwright.sync_api import sync_playwright


def _page_url():
    html = """<!doctype html><title>remote</title>
    <h1>Remote page</h1>
    <button onclick="document.title='remote-clicked';this.textContent='done'">Run</button>"""
    return "data:text/html," + quote(html)


def _subprocess_env(runtime, extra=None):
    env = os.environ.copy()
    env["RUSTWRIGHT_AGENT_RUNTIME_DIR"] = str(runtime)
    source = str(Path(__file__).resolve().parents[1] / "python")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra or {})
    return env


def _run_cli(runtime, session, *command, extra_env=None):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rustwright.cli",
            "--json",
            "--session",
            session,
        ]
        + list(command),
        env=_subprocess_env(runtime, extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=75,
        check=False,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result
    return result.returncode, json.loads(lines[0]), result.stderr


def _snapshot_ref(snapshot):
    matches = re.findall(r"\[ref=(e[1-9][0-9]*)\]", snapshot)
    assert matches, snapshot
    return matches[-1]


def test_remote_cdp_persists_across_commands_and_close_does_not_stop_browser(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    session = "remote-integration"
    header_value = "remote-secret-marker"
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--remote-debugging-port=0"],
    )
    page = browser.new_page()
    page.goto(_page_url())
    endpoint = browser._ws_endpoint

    try:
        code, opened, stderr = _run_cli(
            runtime,
            session,
            "open",
            "--cdp-endpoint",
            endpoint,
            "--cdp-header",
            "x-agent-test=" + header_value,
            "--cdp-timeout-ms",
            "30000",
        )
        assert (code, stderr) == (0, "")
        assert opened["data"]["title"] == "remote"

        code, snapped, stderr = _run_cli(runtime, session, "snapshot")
        assert (code, stderr) == (0, "")
        ref = _snapshot_ref(snapped["data"]["snapshot"])

        code, clicked, stderr = _run_cli(runtime, session, "click", ref)
        assert (code, stderr) == (0, "")
        assert clicked["data"]["title"] == "remote-clicked"

        code, status, stderr = _run_cli(runtime, session, "status")
        assert (code, stderr) == (0, "")
        assert status["data"]["running"] is True
        assert status["data"]["session"] == session
        assert status["data"]["mode"] == "remote"
        assert status["data"]["tabs"] >= 1
        assert set(status["data"]) == {"running", "session", "mode", "tabs"}
        rendered_status = json.dumps(status)
        assert endpoint not in rendered_status
        assert header_value not in rendered_status

        state_file = runtime / session / "state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["mode"] == "remote"
        assert state["remote"] == {
            "endpoint": endpoint,
            "headers": {"x-agent-test": header_value},
            "timeout_ms": 30000,
        }
        assert "owner_pid" not in state
        assert "control_token" not in state
        assert (state_file.stat().st_mode & 0o777) == 0o600
        assert not (runtime / session / "owner.lock").exists()

        code, closed, stderr = _run_cli(runtime, session, "close")
        assert (code, stderr) == (0, "")
        assert closed["success"] is True
        assert not state_file.exists()
        assert browser.is_connected()
        assert page.title() == "remote-clicked"
    finally:
        _run_cli(runtime, session, "close", "--force")
        browser.close()
        playwright.stop()


def test_dead_remote_fails_loudly_without_spawning_an_owner(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    session = "dead-remote"
    endpoint = "ws://127.0.0.1:1/devtools/browser/nope"
    header_value = "dead-remote-secret-marker"
    try:
        code, result, stderr = _run_cli(
            runtime,
            session,
            "open",
            "--cdp-endpoint",
            endpoint,
            "--cdp-header",
            "x-agent-test=" + header_value,
        )
        assert (code, stderr) == (3, "")
        assert result["error"] == {
            "code": "session_lost",
            "message": "remote CDP session unreachable",
        }
        rendered = json.dumps(result)
        assert endpoint not in rendered
        assert header_value not in rendered
        assert not (runtime / session / "state.json").exists()
        assert not (runtime / session / "owner.lock").exists()
    finally:
        _run_cli(runtime, session, "close", "--force")


def test_remote_disconnect_during_later_command_is_session_lost(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    session = "remote-disconnect"
    header_value = "disconnect-secret-marker"
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--remote-debugging-port=0"],
    )
    browser.new_page().goto(_page_url())
    endpoint = browser._ws_endpoint

    try:
        code, _opened, stderr = _run_cli(
            runtime,
            session,
            "open",
            "--cdp-endpoint",
            endpoint,
            "--cdp-header",
            "x-agent-test=" + header_value,
        )
        assert (code, stderr) == (0, "")

        browser.close()
        code, result, stderr = _run_cli(runtime, session, "snapshot")
        assert (code, stderr) == (3, "")
        assert result["error"] == {
            "code": "session_lost",
            "message": "remote CDP session unreachable",
        }
        rendered = json.dumps(result)
        assert endpoint not in rendered
        assert header_value not in rendered
        assert not (runtime / session / "owner.lock").exists()
    finally:
        _run_cli(runtime, session, "close", "--force")
        if browser.is_connected():
            browser.close()
        playwright.stop()


def test_remote_environment_shape_and_header_validation(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("RUSTWRIGHT_AGENT_RUNTIME_DIR", str(runtime))
    endpoint = "wss://browser.example.test/devtools/browser/example"
    monkeypatch.setenv("RUSTWRIGHT_AGENT_CDP_ENDPOINT", endpoint)
    monkeypatch.setenv(
        "RUSTWRIGHT_AGENT_CDP_HEADERS",
        json.dumps({"x-api-key": "example-value"}),
    )
    args = cli.build_parser().parse_args(["open"])
    cli._validate_command(args, ["open"])
    assert args.remote_config == {
        "endpoint": endpoint,
        "headers": {"x-api-key": "example-value"},
        "timeout_ms": 60000,
    }

    bad_value = "not-json-sensitive-value"
    monkeypatch.setenv("RUSTWRIGHT_AGENT_CDP_HEADERS", bad_value)
    try:
        assert cli.main(["--json", "--session", "environment-shape", "open"]) == 2
        captured = capsys.readouterr()
        assert bad_value not in captured.out
        assert bad_value not in captured.err
        error = json.loads(captured.out)["error"]
        assert error["code"] == "invalid_argument"
    finally:
        cli.main(["--json", "--session", "environment-shape", "close", "--force"])


@pytest.mark.parametrize("header", ["missing-separator", "bad name=value", "name=line\nbreak"])
def test_bad_cli_header_is_rejected_without_echoing_value(tmp_path, header):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    endpoint = "ws://127.0.0.1:1/devtools/browser/nope"
    try:
        code, result, stderr = _run_cli(
            runtime,
            "bad-header",
            "open",
            "--cdp-endpoint",
            endpoint,
            "--cdp-header",
            header,
        )
        assert (code, stderr) == (2, "")
        rendered = json.dumps(result)
        assert header not in rendered
        assert result["error"]["code"] == "invalid_argument"
    finally:
        _run_cli(runtime, "bad-header", "close", "--force")


@pytest.mark.parametrize(
    "local_options",
    [
        ["--headed"],
        ["--executable-path", "/not/used"],
        ["--browser-arg", "--no-sandbox"],
    ],
)
def test_remote_endpoint_rejects_local_global_options_without_echoing_endpoint(
    tmp_path,
    local_options,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    endpoint = "ws://127.0.0.1:1/devtools/browser/nope"
    try:
        code, result, stderr = _run_cli(
            runtime,
            "conflicting-flags",
            *local_options,
            "open",
            "--cdp-endpoint",
            endpoint,
        )
        assert (code, stderr) == (2, "")
        rendered = json.dumps(result)
        assert endpoint not in rendered
        assert result["error"]["code"] == "invalid_argument"
        assert not (runtime / "conflicting-flags" / "owner.lock").exists()
    finally:
        _run_cli(runtime, "conflicting-flags", "close", "--force")


def test_remote_endpoint_rejects_non_chromium_browser_without_echoing_endpoint(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    endpoint = "ws://127.0.0.1:1/devtools/browser/nope"
    try:
        code, result, stderr = _run_cli(
            runtime,
            "conflicting-browser",
            "open",
            "--cdp-endpoint",
            endpoint,
            "-b",
            "firefox",
        )
        assert (code, stderr) == (2, "")
        rendered = json.dumps(result)
        assert endpoint not in rendered
        assert result["error"]["code"] == "invalid_argument"
        assert not (runtime / "conflicting-browser" / "owner.lock").exists()
    finally:
        _run_cli(runtime, "conflicting-browser", "close", "--force")
