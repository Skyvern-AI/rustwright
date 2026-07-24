import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
from threading import Thread
import time

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]


def _native_server_binary():
    found = shutil.which("rustwright-mcp") or shutil.which("mcp-rs")
    if found:
        return Path(found)
    for profile in ("debug", "release"):
        for name in ("rustwright-mcp", "mcp-rs"):
            candidate = REPOSITORY / "mcp-rs" / "target" / profile / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


requires_native_server = pytest.mark.skipif(
    _native_server_binary() is None,
    reason="requires the native rustwright-mcp server binary "
    "(cargo build --manifest-path mcp-rs/Cargo.toml)",
)


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


@requires_native_server
def test_mcp_cli_real_stdio_initialize_and_tools_list():
    binary = _native_server_binary()
    environment = os.environ.copy()
    python_path = str(REPOSITORY / "python")
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path
    # The CLI verb resolves the server from PATH; put the binary's directory first.
    environment["PATH"] = str(binary.parent) + os.pathsep + environment.get("PATH", "")

    process = subprocess.Popen(
        [sys.executable, "-m", "rustwright.cli", "mcp"],
        cwd=REPOSITORY,
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
        tools = {tool["name"] for tool in tools_response["result"]["tools"]}
        assert {
            "browser_navigate",
            "browser_navigate_back",
            "browser_navigate_forward",
            "browser_snapshot",
            "browser_click",
            "browser_scroll",
            "browser_take_screenshot",
        } <= tools

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
