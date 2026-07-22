import builtins
import sys
from types import ModuleType

import pytest

from rustwright import cli
from rustwright._agent import cli as agent_cli
from rustwright._agent.errors import AgentError


def _install_stub_mcp(monkeypatch, main):
    package = ModuleType("rustwright_mcp")
    package.__path__ = []
    server = ModuleType("rustwright_mcp.server")
    server.main = main
    package.server = server
    monkeypatch.setitem(sys.modules, "rustwright_mcp", package)
    monkeypatch.setitem(sys.modules, "rustwright_mcp.server", server)


def test_validation_mcp_passthrough_is_exact_and_argv_restores_after_success(monkeypatch):
    observed = []
    original_argv = sys.argv

    def server_main():
        observed.append((sys.argv[0], list(sys.argv[1:])))
        return 41

    _install_stub_mcp(monkeypatch, server_main)

    assert cli.main(
        ["mcp", "--caps=network", "extra", "args"], program="rustwright"
    ) == 41
    assert observed == [("rustwright mcp", ["--caps=network", "extra", "args"])]
    assert sys.argv is original_argv


def test_validation_mcp_exception_propagates_and_argv_restores(monkeypatch):
    original_argv = sys.argv
    expected = RuntimeError("stub server failure")

    def server_main():
        assert sys.argv[1:] == ["--caps=network", "extra", "args"]
        raise expected

    _install_stub_mcp(monkeypatch, server_main)

    with pytest.raises(RuntimeError) as raised:
        cli.main(
            ["mcp", "--caps=network", "extra", "args"], program="rustwright"
        )

    assert raised.value is expected
    assert sys.argv is original_argv


def test_validation_missing_mcp_is_clean_two_line_error(monkeypatch, capsys):
    real_import = builtins.__import__

    def import_without_mcp(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "rustwright_mcp":
            raise ModuleNotFoundError(
                "No module named 'rustwright_mcp'", name="rustwright_mcp"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_mcp)

    assert cli.main(["mcp"], program="rustwright") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "rustwright mcp requires the separately installed rustwright-mcp package; "
        "install it with: pip install rustwright-mcp",
        "or uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' "
        "rustwright-mcp",
    ]
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "argv, unknown_command",
    [
        (["--session", "x", "mcp"], "--session"),
        (["--json", "mcp"], "--json"),
    ],
)
def test_validation_leading_agent_globals_do_not_route_to_mcp(
    monkeypatch, capsys, argv, unknown_command
):
    mcp_calls = []
    _install_stub_mcp(monkeypatch, lambda: mcp_calls.append(list(sys.argv)))

    assert cli.main(argv, program="rustwright") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Unknown Rustwright CLI command: {unknown_command}\n"
    assert mcp_calls == []


def test_validation_help_mcp_succeeds(capsys):
    assert cli.main(["help", "mcp"], program="rustwright") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage: rustwright mcp [args...]" in captured.out
    assert "pip install rustwright-mcp" in captured.out


def test_validation_mcp_as_click_ref_uses_normal_agent_path(monkeypatch, capsys):
    mcp_calls = []
    _install_stub_mcp(monkeypatch, lambda: mcp_calls.append(list(sys.argv)))

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
    assert mcp_calls == []
