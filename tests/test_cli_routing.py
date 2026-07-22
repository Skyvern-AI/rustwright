import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from rustwright import cli
from rustwright._agent import cli as agent_cli


@pytest.fixture
def agent_calls(monkeypatch):
    calls = []

    def capture(argv):
        calls.append(list(argv))
        return 73

    monkeypatch.setattr(agent_cli, "main", capture)
    return calls


@pytest.mark.parametrize("verb", sorted(cli.AGENT_VERBS))
def test_every_agent_verb_routes_verbatim(verb, agent_calls):
    assert cli.main([verb], program="rustwright") == 73
    assert agent_calls == [[verb]]


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "snapshot"],
        ["--session", "work", "click", "e3"],
        ["--session=work", "status"],
    ],
)
def test_leading_agent_global_flags_route_verbatim(argv, agent_calls):
    assert cli.main(argv, program="rustwright") == 73
    assert agent_calls == [argv]


@pytest.mark.parametrize(
    "argv",
    [
        ["screenshot"],
        ["screenshot", "shot.jpg"],
        ["screenshot", "--type", "jpeg", "shot.jpg"],
        ["screenshot", "--ref=e1", "shot.png"],
        ["--session", "work", "screenshot", "shot.png"],
    ],
)
def test_session_screenshot_arity_routes_to_agent(argv, agent_calls):
    assert cli.main(argv, program="rustwright") == 73
    assert agent_calls == [argv]


@pytest.mark.parametrize(
    "argv, expected_rest",
    [
        (["screenshot", "https://example.test", "shot.png"], ["https://example.test", "shot.png"]),
        (
            ["screenshot", "--wait-for-selector", "main", "https://example.test", "shot.png"],
            ["--wait-for-selector", "main", "https://example.test", "shot.png"],
        ),
        (
            ["screenshot", "--browser=chromium", "https://example.test", "shot.png"],
            ["--browser=chromium", "https://example.test", "shot.png"],
        ),
    ],
)
def test_two_positional_screenshot_routes_to_one_shot(monkeypatch, agent_calls, argv, expected_rest):
    calls = []

    def capture(rest, *, program="playwright"):
        calls.append((list(rest), program))
        return 29

    monkeypatch.setattr(cli, "screenshot", capture)

    assert cli.main(argv, program="rustwright") == 29
    assert calls == [(expected_rest, "rustwright")]
    assert agent_calls == []


def test_screenshot_help_combines_both_forms(capsys, agent_calls):
    assert cli.main(["screenshot", "--help"], program="rustwright") == 0
    output = capsys.readouterr().out
    assert "rustwright screenshot [file] [--full] [--ref REF]" in output
    assert "rustwright screenshot <url> <file> [options]" in output
    assert agent_calls == []


def test_chromium_alias_routes_to_agent_open(agent_calls):
    assert cli.main(["cr", "example.test"], program="rustwright") == 73
    assert agent_calls == [["open", "--browser", "chromium", "example.test"]]


@pytest.mark.parametrize("alias", ["cr", "chromium"])
def test_leading_globals_chromium_alias_routes_to_agent_open(alias, agent_calls):
    assert cli.main(["--session", "work", alias, "example.com"], program="rustwright") == 73
    assert agent_calls == [
        ["--session", "work", "open", "--browser", "chromium", "example.com"]
    ]


@pytest.mark.parametrize("alias", ["ff", "wk"])
def test_unsupported_browser_alias_does_not_import_agent(alias, capsys, agent_calls):
    assert cli.main([alias], program="rustwright") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"{alias} is not implemented; Rustwright currently supports Chromium over direct CDP.\n"
    )
    assert agent_calls == []


def test_top_level_help_has_session_and_compatibility_sections(capsys):
    assert cli.main(["--help"], program="rustwright") == 0
    output = capsys.readouterr().out
    assert "Browser session (persistent):" in output
    assert "Playwright-compatible tools:" in output


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_leading_json_top_level_help_works(help_flag, capsys):
    assert cli.main(["--json", help_flag], program="rustwright") == 0
    captured = capsys.readouterr()
    assert "Browser session (persistent):" in captured.out
    assert "Unknown Rustwright CLI command" not in captured.err


def test_leading_json_unsupported_alias_uses_error_envelope(capsys):
    assert cli.main(["--json", "ff"], program="rustwright") == 2
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["success"] is False
    assert envelope["command"] == "open"
    assert envelope["error"]["code"] == "invalid_argument"
    assert "ff is not implemented" in envelope["error"]["message"]
    assert captured.err == ""


@pytest.mark.parametrize("verb", ["not-a-command", "mcp"])
def test_leading_json_unknown_verb_uses_error_envelope(verb, capsys):
    assert cli.main(["--json", verb], program="rustwright") == 2
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["success"] is False
    assert envelope["command"] == "unknown"
    assert envelope["error"]["code"] == "invalid_argument"
    assert verb in envelope["error"]["message"]
    assert captured.err == ""


def test_help_for_agent_verb_routes_to_agent(agent_calls):
    assert cli.main(["help", "snapshot"], program="rustwright") == 73
    assert agent_calls == [["snapshot", "--help"]]


def test_leading_globals_help_for_agent_verb_preserves_globals(agent_calls):
    assert cli.main(["--session", "work", "help", "snapshot"], program="rustwright") == 73
    assert agent_calls == [["--session", "work", "snapshot", "--help"]]


def test_help_screenshot_prints_combined_forms(capsys, agent_calls):
    assert cli.main(["help", "screenshot"], program="rustwright") == 0
    output = capsys.readouterr().out
    assert "rustwright screenshot [file] [--full] [--ref REF]" in output
    assert "rustwright screenshot <url> <file> [options]" in output
    assert agent_calls == []


def test_pyproject_has_only_the_top_level_console_script():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    contents = pyproject.read_text(encoding="utf-8")
    assert 'rustwright = "rustwright.cli:main"' in contents
    assert "rustwright-agent" not in contents


def test_missing_fcntl_keeps_non_session_cli_available_and_rejects_sessions():
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    python_path = str(repository / "python")
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path
    script = textwrap.dedent(
        """
        import contextlib
        import importlib.abc
        import io
        import json
        import sys

        class MissingFcntl(importlib.abc.MetaPathFinder):
            attempts = 0

            def find_spec(self, fullname, path=None, target=None):
                if fullname == "fcntl":
                    self.attempts += 1
                    raise ModuleNotFoundError("simulated missing fcntl", name=fullname)
                return None

        blocker = MissingFcntl()
        sys.meta_path.insert(0, blocker)
        from rustwright import cli

        non_session_stdout = io.StringIO()
        non_session_stderr = io.StringIO()
        with contextlib.redirect_stdout(non_session_stdout), contextlib.redirect_stderr(non_session_stderr):
            non_session_code = cli.main(["trace"], program="rustwright")

        session_stdout = io.StringIO()
        session_stderr = io.StringIO()
        with contextlib.redirect_stdout(session_stdout), contextlib.redirect_stderr(session_stderr):
            session_code = cli.main(["open"], program="rustwright")

        print(json.dumps({
            "fcntl_import_attempts": blocker.attempts,
            "non_session_code": non_session_code,
            "non_session_stdout": non_session_stdout.getvalue(),
            "non_session_stderr": non_session_stderr.getvalue(),
            "session_code": session_code,
            "session_stdout": session_stdout.getvalue(),
            "session_stderr": session_stderr.getvalue(),
        }))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["fcntl_import_attempts"] >= 1
    assert payload["non_session_code"] == 0
    assert "usage: rustwright trace" in payload["non_session_stdout"]
    assert payload["non_session_stderr"] == ""
    assert payload["session_code"] == 2
    assert payload["session_stdout"] == ""
    assert payload["session_stderr"] == (
        "error[unsupported_platform]: persistent sessions require macOS or Linux\n"
    )
    assert "Traceback" not in result.stderr
