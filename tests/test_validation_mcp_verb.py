import stat
import sys

import pytest

from rustwright import cli
from rustwright._agent import cli as agent_cli
from rustwright._agent.errors import AgentError


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


def test_validation_mcp_passthrough_is_exact_including_spaces(tmp_path, monkeypatch):
    record = _install_stub_binary(tmp_path, monkeypatch, exit_code=41)

    assert (
        cli.main(["mcp", "--caps=network", "two words", "x"], program="rustwright")
        == 41
    )
    assert record.read_text().splitlines() == ["--caps=network", "two words", "x"]


def test_validation_mcp_zero_exit_passes_through(tmp_path, monkeypatch):
    record = _install_stub_binary(tmp_path, monkeypatch, exit_code=0)

    assert cli.main(["mcp"], program="rustwright") == 0
    assert record.read_text() == ""


def test_validation_missing_binary_is_clean_two_line_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PATH", str(tmp_path))

    assert cli.main(["mcp"], program="rustwright") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "rustwright mcp requires the native rustwright-mcp server binary; "
        "install it with: cargo install --git https://github.com/Skyvern-AI/rustwright rustwright-mcp",
        "or install the rustwright-mcp npm package once it is published",
    ]
    assert "Traceback" not in captured.err


def test_validation_leading_session_flag_does_not_route_to_mcp(
    tmp_path, monkeypatch, capsys
):
    record = _install_stub_binary(tmp_path, monkeypatch)

    assert cli.main(["--session", "x", "mcp"], program="rustwright") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Unknown Rustwright CLI command: mcp\n"
    assert not record.exists()


def test_validation_leading_json_flag_does_not_route_to_mcp(
    tmp_path, monkeypatch, capsys
):
    import json as json_module

    record = _install_stub_binary(tmp_path, monkeypatch)

    assert cli.main(["--json", "mcp"], program="rustwright") == 2
    captured = capsys.readouterr()
    error = json_module.loads(captured.out)
    assert error["success"] is False
    assert error["command"] == "unknown"
    assert error["error"]["code"] == "invalid_argument"
    assert "mcp" in error["error"]["message"]
    assert captured.err == ""
    assert not record.exists()


def test_validation_help_mcp_succeeds(capsys):
    assert cli.main(["help", "mcp"], program="rustwright") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage: rustwright mcp [args...]" in captured.out
    assert (
        "cargo install --git https://github.com/Skyvern-AI/rustwright rustwright-mcp"
        in captured.out
    )


def test_validation_mcp_as_click_ref_uses_normal_agent_path(
    tmp_path, monkeypatch, capsys
):
    record = _install_stub_binary(tmp_path, monkeypatch)

    def normal_agent_failure(args, argv):
        assert args.command == "click"
        assert args.ref == "mcp"
        assert argv == ["click", "mcp"]
        raise AgentError("invalid_ref", "Ref must have the form e1 or @e1")

    monkeypatch.setattr(agent_cli, "_run", normal_agent_failure)

    assert cli.main(["click", "mcp"], program="rustwright") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error[invalid_ref]: Ref must have the form e1 or @e1\n"
    assert not record.exists()
