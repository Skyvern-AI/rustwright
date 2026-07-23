import json
import os
import threading

import pytest

from rustwright._agent import cli
from rustwright._agent.errors import AgentError
from rustwright._agent.refs import RefAllocator
from rustwright._agent.state import (
    atomic_write_json,
    clear_dirty,
    launch_config_hash,
    mark_dirty,
    owner_lifetime_lock,
    owner_lock_is_held,
    read_json,
    read_state,
    runtime_dir,
    session_dir,
    session_lock,
    state_path,
    validate_session_name,
    write_state,
)


def _state(name):
    return {
        "schema": 1,
        "session": name,
        "mode": "owned",
        "remote": None,
        "owner_pid": os.getpid(),
        "endpoint": "ws://127.0.0.1:1/browser/example",
        "control_token": "control-value",
        "session_nonce": "nonce-value",
        "active_target_id": "target-value",
        "tabs": {"target-value": "t1"},
        "next_tab_id": 2,
        "next_ref_id": 1,
        "dirty": None,
        "launch_config_hash": launch_config_hash(False, None, []),
    }


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    path.mkdir(mode=0o700)
    monkeypatch.setenv("RUSTWRIGHT_AGENT_RUNTIME_DIR", str(path))
    return path


@pytest.mark.parametrize("name", ["default", "A", "a_b-c", "x" * 64])
def test_validate_session_name_accepts_documented_names(name):
    assert validate_session_name(name) == name


@pytest.mark.parametrize("name", ["", "space name", "../escape", "x" * 65, "dot.name"])
def test_validate_session_name_rejects_other_names(name):
    with pytest.raises(AgentError) as caught:
        validate_session_name(name)
    assert caught.value.code == "invalid_argument"


def test_runtime_dir_honors_environment_and_rejects_symlink(tmp_path, monkeypatch):
    configured = tmp_path / "configured"
    monkeypatch.setenv("RUSTWRIGHT_AGENT_RUNTIME_DIR", str(configured))
    assert runtime_dir() == configured
    assert configured.stat().st_mode & 0o777 == 0o700

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("RUSTWRIGHT_AGENT_RUNTIME_DIR", str(linked))
    with pytest.raises(AgentError):
        runtime_dir()


def test_atomic_json_round_trip_and_symlink_refusal(isolated_runtime, tmp_path):
    path = session_dir("atomic") / "value.json"
    atomic_write_json(path, {"value": 1})
    assert read_json(path) == {"value": 1}
    assert path.stat().st_mode & 0o777 == 0o600

    target = tmp_path / "outside.json"
    target.write_text("outside", encoding="utf-8")
    linked = session_dir("atomic") / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(AgentError):
        atomic_write_json(linked, {"value": 2})
    assert target.read_text(encoding="utf-8") == "outside"


def test_session_lock_times_out_for_second_thread(isolated_runtime):
    result = []

    def contend():
        try:
            with session_lock("locked", timeout=0.1):
                result.append("acquired")
        except AgentError as exc:
            result.append(exc.code)

    with session_lock("locked"):
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == ["session_busy"]


def test_dirty_journal_round_trip(isolated_runtime):
    value = _state("dirty")
    write_state("dirty", value)
    marker = mark_dirty(value)
    assert marker
    assert read_state("dirty")["dirty"] == marker
    clear_dirty(value)
    assert read_state("dirty")["dirty"] is None


def test_owner_lock_reports_lifetime_ownership(isolated_runtime):
    assert owner_lock_is_held("owner-proof") is False
    with owner_lifetime_lock("owner-proof"):
        assert owner_lock_is_held("owner-proof") is True
    assert owner_lock_is_held("owner-proof") is False


def test_cli_ref_reservation_survives_crash_window(isolated_runtime):
    value = _state("ref-crash")
    write_state("ref-crash", value)

    class ReservationSession:
        def __init__(self, next_ref):
            self.allocator = RefAllocator(next_ref=next_ref, session_nonce="nonce-value")

        def prepare_ref_reservation(self, count):
            return self.allocator.prepare(count)

        @property
        def next_ref_id(self):
            return self.allocator.next_ref

    first_process = ReservationSession(value["next_ref_id"])
    cli._reserve_refs_before_dispatch(value, first_process)

    # Simulate process death before dispatch/_persist. A replacement process
    # must begin after the entire durably reserved range.
    durable = read_state("ref-crash")
    assert durable["next_ref_id"] == 1001
    assert durable["dirty"] is not None
    replacement = RefAllocator(
        next_ref=durable["next_ref_id"],
        session_nonce=durable["session_nonce"],
    )
    assert replacement.take(1000) == 1001


def test_bad_flag_uses_one_json_error_envelope(capsys):
    assert cli.main(["--json", "--not-a-real-flag"]) == 2
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["version"] == 1
    assert envelope["success"] is False
    assert envelope["error"]["code"] == "invalid_argument"
    assert captured.err == ""


def test_status_data_redacts_connection_material(isolated_runtime):
    value = _state("redacted")
    value["endpoint"] = "ws://127.0.0.1:1/browser/do-not-display"
    value["control_token"] = "do-not-display-token"
    rendered = json.dumps(cli._status_data("redacted", value), sort_keys=True)
    assert value["endpoint"] not in rendered
    assert value["control_token"] not in rendered
    assert value["active_target_id"] not in rendered
    assert "running" in rendered
