import builtins
import json
import sys
from types import ModuleType

from rustwright import cli


def _install_stub_mcp(monkeypatch, main):
    package = ModuleType("rustwright_mcp")
    package.__path__ = []
    server = ModuleType("rustwright_mcp.server")
    server.main = main
    package.server = server
    monkeypatch.setitem(sys.modules, "rustwright_mcp", package)
    monkeypatch.setitem(sys.modules, "rustwright_mcp.server", server)


def test_mcp_routes_in_process_with_verbatim_argv_and_exit_code(monkeypatch):
    calls = []
    original_argv = sys.argv

    def main():
        calls.append(list(sys.argv))
        return 37

    _install_stub_mcp(monkeypatch, main)

    assert cli.main(["mcp", "--caps=files,network", "extra"], program="rustwright") == 37
    assert calls == [["rustwright mcp", "--caps=files,network", "extra"]]
    assert sys.argv is original_argv


def test_mcp_missing_package_prints_install_help_without_traceback(monkeypatch, capsys):
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
    assert "pip install rustwright-mcp" in captured.err
    assert "or uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp" in captured.err
    assert len(captured.err.splitlines()) == 2
    assert "Traceback" not in captured.err


def test_leading_json_does_not_route_to_mcp(monkeypatch, capsys):
    calls = []
    _install_stub_mcp(monkeypatch, lambda: calls.append(list(sys.argv)))

    assert cli.main(["--json", "mcp"], program="rustwright") == 2
    captured = capsys.readouterr()
    assert calls == []
    error = json.loads(captured.out)
    assert error["success"] is False
    assert error["command"] == "unknown"
    assert error["error"]["code"] == "invalid_argument"
    assert "mcp" in error["error"]["message"]
    assert captured.err == ""


def test_top_level_help_includes_mcp(capsys):
    assert cli.main(["--help"], program="rustwright") == 0
    assert "mcp                run the MCP server (requires rustwright-mcp)" in capsys.readouterr().out


def test_help_mcp_prints_usage_and_install_hint(capsys):
    assert cli.main(["help", "mcp"], program="rustwright") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage: rustwright mcp [args...]" in captured.out
    assert "pip install rustwright-mcp" in captured.out
