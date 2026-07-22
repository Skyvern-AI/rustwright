import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from rustwright._agent import cli
from rustwright._agent.state import (
    launch_config_hash,
    mark_dirty,
    read_state,
    state_path,
    write_state,
)


def _url():
    html = """<!doctype html><title>persistent</title>
    <h1>Same page</h1>
    <button onclick="document.title='clicked';this.textContent='done'">Run</button>"""
    return "data:text/html," + quote(html)


def _json_call(capsys, *arguments):
    code = cli.main(["--json"] + list(arguments))
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, captured
    return code, json.loads(lines[0])


def _ref(snapshot):
    matches = re.findall(r"\[ref=(e[1-9][0-9]*)\]", snapshot)
    assert matches, snapshot
    return matches[-1]


def _subprocess_env(runtime):
    env = os.environ.copy()
    env["RUSTWRIGHT_AGENT_RUNTIME_DIR"] = str(runtime)
    source = str(Path(__file__).resolve().parents[1] / "python")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_subprocess(runtime, session, *command):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rustwright._agent.cli",
            "--json",
            "--session",
            session,
        ]
        + list(command),
        env=_subprocess_env(runtime),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        check=False,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result
    return result.returncode, json.loads(lines[0]), result.stderr


def _run_top_level_subprocess(runtime, session, *command):
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
        env=_subprocess_env(runtime),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        check=False,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result
    return result.returncode, json.loads(lines[0]), result.stderr


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("RUSTWRIGHT_AGENT_RUNTIME_DIR", str(runtime))
    sessions = []

    def register(name):
        sessions.append(name)
        return name

    yield runtime, register
    for name in sessions:
        cli.main(["--json", "--session", name, "close", "--force"])


def test_in_process_persistence_detach_and_status(isolated_runtime, capsys):
    _runtime, register = isolated_runtime
    name = register("in-process")
    code, opened = _json_call(capsys, "--session", name, "open", _url())
    assert code == 0
    assert opened["data"]["title"] == "persistent"
    owner_pid = read_state(name)["owner_pid"]
    os.kill(owner_pid, 0)

    code, snapped = _json_call(capsys, "--session", name, "snapshot")
    assert code == 0
    assert snapped["data"]["url"] == opened["data"]["url"]
    ref = _ref(snapped["data"]["snapshot"])

    code, clicked = _json_call(capsys, "--session", name, "click", ref)
    assert code == 0
    assert clicked["data"]["title"] == "clicked"
    os.kill(owner_pid, 0)

    code, status = _json_call(capsys, "--session", name, "status")
    assert code == 0
    assert status["data"]["running"] is True
    assert "endpoint" not in status["data"]
    assert "control_token" not in status["data"]

    code, _closed = _json_call(capsys, "--session", name, "close", "--force")
    assert code == 0
    code, status = _json_call(capsys, "--session", name, "status")
    assert code == 0
    assert status["data"]["running"] is False


def test_true_subprocess_open_snapshot_click_close(isolated_runtime):
    runtime, register = isolated_runtime
    name = register("subprocess")
    code, opened, stderr = _run_subprocess(runtime, name, "open", _url())
    assert (code, stderr) == (0, "")
    assert opened["data"]["title"] == "persistent"

    code, snapped, stderr = _run_subprocess(runtime, name, "snapshot")
    assert (code, stderr) == (0, "")
    assert snapped["data"]["url"] == opened["data"]["url"]
    ref = _ref(snapped["data"]["snapshot"])

    code, clicked, stderr = _run_subprocess(runtime, name, "click", ref)
    assert (code, stderr) == (0, "")
    assert clicked["data"]["title"] == "clicked"

    code, closed, stderr = _run_subprocess(runtime, name, "close")
    assert (code, stderr) == (0, "")
    assert closed["success"] is True


def test_top_level_cli_open_snapshot_click_status_close(isolated_runtime):
    runtime, register = isolated_runtime
    name = register("top-level")

    code, opened, stderr = _run_top_level_subprocess(runtime, name, "open", _url())
    assert (code, stderr) == (0, "")
    assert opened["data"]["title"] == "persistent"

    code, snapped, stderr = _run_top_level_subprocess(runtime, name, "snapshot")
    assert (code, stderr) == (0, "")
    ref = _ref(snapped["data"]["snapshot"])

    code, clicked, stderr = _run_top_level_subprocess(runtime, name, "click", ref)
    assert (code, stderr) == (0, "")
    assert clicked["data"]["title"] == "clicked"

    code, status, stderr = _run_top_level_subprocess(runtime, name, "status")
    assert (code, stderr) == (0, "")
    assert status["data"]["running"] is True

    code, closed, stderr = _run_top_level_subprocess(runtime, name, "close")
    assert (code, stderr) == (0, "")
    assert closed["data"]["running"] is False


def test_agent_cli_uses_top_level_program_and_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == cli._version()

    assert cli.main(["open", "--help"]) == 0
    output = capsys.readouterr().out
    assert "usage: rustwright open" in output
    assert "rustwright-agent" not in output


@pytest.mark.parametrize("browser", ["ff", "firefox", "wk", "webkit"])
def test_agent_cli_rejects_unsupported_browser_names(browser, capsys):
    assert cli.main(["open", "--browser", browser]) == 2
    assert capsys.readouterr().err == (
        f"error[invalid_argument]: {browser} is not implemented; "
        "Rustwright currently supports Chromium over direct CDP.\n"
    )


@pytest.mark.parametrize("browser", ["chrome", "msedge"])
def test_agent_cli_requires_executable_path_for_branded_browser(browser, capsys):
    assert cli.main(["open", "--browser", browser]) == 2
    output = capsys.readouterr().err
    assert "error[invalid_argument]" in output
    assert "--executable-path" in output


def test_dirty_state_rejects_old_ref_until_resnapshot(isolated_runtime, capsys):
    _runtime, register = isolated_runtime
    name = register("dirty-recovery")
    code, opened = _json_call(capsys, "--session", name, "open", _url())
    assert code == 0
    old_ref = _ref(opened["data"]["snapshot"])
    state = read_state(name)
    mark_dirty(state)

    code, failed = _json_call(capsys, "--session", name, "click", old_ref)
    assert code == 5
    assert failed["error"]["code"] == "stale_ref"
    assert read_state(name)["dirty"] is not None

    code, snapped = _json_call(capsys, "--session", name, "snapshot")
    assert code == 0
    assert read_state(name)["dirty"] is None
    assert _ref(snapped["data"]["snapshot"]) != old_ref


def test_concurrent_invocations_serialize(isolated_runtime):
    runtime, register = isolated_runtime
    name = register("concurrent")
    code, _opened, _stderr = _run_subprocess(runtime, name, "open", _url())
    assert code == 0
    command = [
        sys.executable,
        "-m",
        "rustwright._agent.cli",
        "--json",
        "--session",
        name,
        "wait",
        "150",
    ]
    first = subprocess.Popen(
        command,
        env=_subprocess_env(runtime),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        env=_subprocess_env(runtime),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_out, first_err = first.communicate(timeout=45)
    second_out, second_err = second.communicate(timeout=45)
    assert first.returncode == second.returncode == 0
    assert first_err == second_err == ""
    assert json.loads(first_out)["success"] is True
    assert json.loads(second_out)["success"] is True
    assert read_state(name)["dirty"] is None


def test_force_close_cleans_dead_owner_state(isolated_runtime, capsys, monkeypatch):
    _runtime, register = isolated_runtime
    name = register("wedged")
    state = {
        "schema": 1,
        "session": name,
        # Model a dead owner's stale state after its PID has been reused by this
        # unrelated live process. The free owner lock must prevent signaling.
        "owner_pid": os.getpid(),
        "endpoint": "ws://127.0.0.1:1/browser/dead",
        "control_token": "dead-control",
        "session_nonce": "dead-nonce",
        "active_target_id": "dead-target",
        "tabs": {"dead-target": "t1"},
        "next_tab_id": 2,
        "next_ref_id": 1,
        "dirty": "unfinished",
        "launch_config_hash": launch_config_hash(False, None, []),
    }
    write_state(name, state)
    signals = []

    def record_signal(pid, signum):
        signals.append((pid, signum))

    monkeypatch.setattr(cli.os, "kill", record_signal)
    code, closed = _json_call(capsys, "--session", name, "close", "--force")
    assert code == 0
    assert closed["success"] is True
    assert not state_path(name).exists()
    assert signals == []


def test_spawn_timeout_terminates_and_reaps_child(isolated_runtime, monkeypatch):
    _runtime, _register = isolated_runtime

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.reaped = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.reaped = True
            return 0

    child = FakeProcess()
    clock = iter([0.0, 6.0])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    args = SimpleNamespace(
        session="startup-timeout",
        headed=False,
        executable_path=None,
        browser_arg=[],
    )

    with pytest.raises(Exception) as caught:
        cli._spawn_owner(args)
    assert getattr(caught.value, "code", None) == "session_lost"
    assert child.terminated is True
    assert child.reaped is True


def test_eval_stdin_is_read_with_a_hard_bound(monkeypatch):
    class OversizedInput:
        def __init__(self):
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return "x" * size

    source = OversizedInput()
    monkeypatch.setattr(cli.sys, "stdin", source)
    args = cli.build_parser().parse_args(["--allow-eval", "eval", "--stdin"])

    with pytest.raises(Exception) as caught:
        cli._dispatch(args, object())
    assert getattr(caught.value, "code", None) == "invalid_argument"
    assert source.read_sizes == [200001]
