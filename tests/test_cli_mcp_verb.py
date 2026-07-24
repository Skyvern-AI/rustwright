import json
import stat
import sys

import pytest

from rustwright import cli


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="stub server binaries use POSIX shell scripts"
)


def _install_stub_binary(directory, monkeypatch, *, exit_code=0, name="rustwright-mcp"):
    record = directory / f"{name}-invocation.txt"
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        f': > "{record}"\n'
        f'[ "$#" -eq 0 ] || printf \'%s\\n\' "$@" >> "{record}"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(directory))
    return record


def _recorded_argv(record):
    text = record.read_text()
    return text.splitlines()


def test_mcp_spawns_binary_with_verbatim_argv_and_exit_code(tmp_path, monkeypatch):
    record = _install_stub_binary(tmp_path, monkeypatch, exit_code=37)

    assert cli.main(["mcp", "--caps=files,network", "extra"], program="rustwright") == 37
    assert _recorded_argv(record) == ["--caps=files,network", "extra"]


def test_mcp_prefers_rustwright_mcp_over_mcp_rs_on_path(tmp_path, monkeypatch):
    preferred = _install_stub_binary(tmp_path, monkeypatch, exit_code=7)
    fallback = _install_stub_binary(tmp_path, monkeypatch, exit_code=9, name="mcp-rs")

    assert cli.main(["mcp"], program="rustwright") == 7
    assert preferred.exists()
    assert not fallback.exists()


def test_mcp_falls_back_to_cargo_installed_binary_name(tmp_path, monkeypatch):
    record = _install_stub_binary(tmp_path, monkeypatch, exit_code=5, name="mcp-rs")

    assert cli.main(["mcp", "arg"], program="rustwright") == 5
    assert _recorded_argv(record) == ["arg"]


def test_mcp_missing_binary_prints_install_help_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PATH", str(tmp_path))

    assert cli.main(["mcp"], program="rustwright") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "cargo install --git https://github.com/Skyvern-AI/rustwright mcp-rs"
        in captured.err
    )
    assert len(captured.err.splitlines()) == 2
    assert "Traceback" not in captured.err


def test_leading_json_does_not_route_to_mcp(tmp_path, monkeypatch, capsys):
    record = _install_stub_binary(tmp_path, monkeypatch)

    assert cli.main(["--json", "mcp"], program="rustwright") == 2
    captured = capsys.readouterr()
    assert not record.exists()
    error = json.loads(captured.out)
    assert error["success"] is False
    assert error["command"] == "unknown"
    assert error["error"]["code"] == "invalid_argument"
    assert "mcp" in error["error"]["message"]
    assert captured.err == ""


def test_top_level_help_includes_mcp(capsys):
    assert cli.main(["--help"], program="rustwright") == 0
    assert (
        "mcp                run the native MCP server (requires the rustwright-mcp binary)"
        in capsys.readouterr().out
    )


def test_help_mcp_prints_usage_and_install_hint(capsys):
    assert cli.main(["help", "mcp"], program="rustwright") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage: rustwright mcp [args...]" in captured.out
    assert "cargo install --git https://github.com/Skyvern-AI/rustwright mcp-rs" in captured.out
