import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
import time

import pytest


def _rustwright_mcp_is_installed():
    try:
        metadata.distribution("rustwright-mcp")
    except metadata.PackageNotFoundError:
        return False
    return importlib.util.find_spec("rustwright_mcp") is not None


requires_rustwright_mcp = pytest.mark.skipif(
    not _rustwright_mcp_is_installed(),
    reason="requires the separately installed rustwright-mcp package",
)


def _installed_rustwright_mcp_console_scripts():
    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        candidates = entry_points.select(
            group="console_scripts", name="rustwright-mcp"
        )
    else:
        candidates = [
            entry_point
            for entry_point in entry_points.get("console_scripts", [])
            if entry_point.name == "rustwright-mcp"
        ]
    return [
        entry_point
        for entry_point in candidates
        if getattr(getattr(entry_point, "dist", None), "name", "")
        .lower()
        .replace("_", "-")
        == "rustwright-mcp"
    ]


@requires_rustwright_mcp
def test_mcp_console_script_and_cli_verb_use_same_callable():
    from rustwright_mcp import server

    entry_points = _installed_rustwright_mcp_console_scripts()

    assert len(entry_points) == 1
    # rustwright.cli._mcp_main imports this module and invokes server.main.
    verb_dispatch_target = server.main
    assert entry_points[0].load() is verb_dispatch_target


def _send_message(process, message):
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_response(process, messages, request_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                f"timed out waiting for JSON-RPC response {request_id}; "
                f"child return code: {process.poll()}"
            )
        try:
            kind, payload = messages.get(timeout=remaining)
        except Empty:
            pytest.fail(
                f"timed out waiting for JSON-RPC response {request_id}; "
                f"child return code: {process.poll()}"
            )
        assert kind == "message", payload
        if payload.get("id") == request_id:
            return payload


@requires_rustwright_mcp
def test_mcp_cli_real_stdio_initialize_and_tools_list():
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    python_path = str(repository / "python")
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path
    environment["RUSTWRIGHT_MCP_HEADLESS"] = "1"
    environment["RUSTWRIGHT_MCP_TOOLSET"] = "mirror"

    process = subprocess.Popen(
        [sys.executable, "-m", "rustwright.cli", "mcp"],
        cwd=repository,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    messages = Queue()
    stdout_lines = []

    def read_stdout():
        for line in process.stdout:
            stdout_lines.append(line)
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                    raise ValueError("stdout line is not a JSON-RPC 2.0 message")
            except (json.JSONDecodeError, ValueError) as exc:
                messages.put(("error", f"{exc}: {line!r}"))
            else:
                messages.put(("message", payload))

    stdout_reader = Thread(target=read_stdout, daemon=True)
    stdout_reader.start()

    try:
        _send_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "rustwright-cli-integration-test",
                        "version": "1.0",
                    },
                },
            },
        )
        initialize_response = _read_response(process, messages, 1, timeout=10)
        assert "error" not in initialize_response
        initialize_result = initialize_response["result"]
        assert initialize_result["protocolVersion"] == "2024-11-05"
        assert initialize_result["serverInfo"]["name"] == "rustwright-mcp"

        _send_message(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        _send_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        tools_response = _read_response(process, messages, 2, timeout=10)
        assert "error" not in tools_response
        tools = tools_response["result"]["tools"]
        assert tools
        assert "browser_navigate" in {tool["name"] for tool in tools}

        assert process.stdin is not None
        process.stdin.close()
        return_code = process.wait(timeout=10)
    finally:
        if process.poll() is None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    stdout_reader.join(timeout=3)
    stderr = process.stderr.read()

    assert not stdout_reader.is_alive(), "stdout reader did not observe EOF"
    assert return_code == 0, stderr
    assert stdout_lines, "MCP server produced no protocol messages"
    assert all(json.loads(line).get("jsonrpc") == "2.0" for line in stdout_lines)
