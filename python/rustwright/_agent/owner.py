"""Detached browser owner for persistent agent CLI sessions."""

import argparse
import os
import secrets
import signal
import sys
import time
from typing import Any, List, Optional

from rustwright.sync_api import sync_playwright

from .attach import browser_ws_endpoint, page_target_id
from .errors import AgentError
from .state import (
    atomic_write_json,
    bootstrap_ack_path,
    error_path,
    launch_config_hash,
    owner_lifetime_lock,
    read_json,
    remove_session_files,
    state_path,
    stop_path,
    validate_session_name,
    write_state,
)


_STOP_REQUESTED = False


def _request_stop(_signum: int, _frame: Any) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _reject_browser_args(values: List[str]) -> None:
    forbidden = (
        "--remote-debugging-port",
        "--remote-debugging-pipe",
        "--user-data-dir",
    )
    for value in values:
        lowered = value.lower()
        if any(lowered == item or lowered.startswith(item + "=") for item in forbidden):
            raise AgentError(
                "invalid_argument",
                "A browser argument conflicts with persistent session management",
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rustwright-owner")
    parser.add_argument("--session", required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--executable-path")
    parser.add_argument("--browser-arg", action="append", default=[])
    parser.add_argument("--bootstrap-token", required=True)
    return parser


def _write_startup_error(session: str) -> None:
    try:
        atomic_write_json(
            error_path(session),
            {
                "schema": 1,
                "code": "browser_launch_failed",
                "message": "The persistent browser owner could not be started",
            },
        )
    except Exception:
        pass


def _remove_file(path: Any) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _await_bootstrap_ack(session: str, token: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not _STOP_REQUESTED and time.monotonic() < deadline:
        acknowledgement = read_json(bootstrap_ack_path(session), missing_ok=True)
        state = read_json(state_path(session), missing_ok=True)
        if (
            acknowledgement is not None
            and acknowledgement.get("token") == token
            and state is not None
            and state.get("owner_pid") == os.getpid()
        ):
            _remove_file(bootstrap_ack_path(session))
            return
        time.sleep(0.05)
    raise AgentError("session_lost", "The browser owner startup was not acknowledged")


def main(argv: Optional[List[str]] = None) -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    session = "default"
    playwright = None
    browser = None
    published_state = False
    lock_context = None
    lock_entered = False
    return_code = 1
    try:
        args = _parser().parse_args(argv)
        session = validate_session_name(args.session)
        _reject_browser_args(args.browser_arg)
        try:
            os.unlink(str(error_path(session)))
        except OSError:
            pass

        _remove_file(bootstrap_ack_path(session))

        lock_context = owner_lifetime_lock(session)
        lock_context.__enter__()
        lock_entered = True

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _request_stop)

        playwright = sync_playwright().start()
        launch_options = {
            "headless": not args.headed,
            "args": list(args.browser_arg) + ["--remote-debugging-port=0"],
        }
        if args.executable_path is not None:
            launch_options["executable_path"] = args.executable_path
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page()
        endpoint = browser_ws_endpoint(browser)
        if not endpoint:
            raise RuntimeError("endpoint unavailable")
        target = page_target_id(page)
        if not target:
            raise RuntimeError("target unavailable")

        control_token = secrets.token_hex(32)
        state = {
            "schema": 1,
            "session": session,
            "owner_pid": os.getpid(),
            "endpoint": endpoint,
            "control_token": control_token,
            "session_nonce": secrets.token_hex(16),
            "active_target_id": target,
            "tabs": {target: "t1"},
            "next_tab_id": 2,
            "next_ref_id": 1,
            "dirty": None,
            "launch_config_hash": launch_config_hash(
                args.headed,
                args.executable_path,
                args.browser_arg,
            ),
        }
        write_state(session, state)
        published_state = True
        _await_bootstrap_ack(session, args.bootstrap_token)

        while not _STOP_REQUESTED:
            request = read_json(stop_path(session), missing_ok=True)
            if request is not None and request.get("control_token") == control_token:
                break
            time.sleep(0.1)

        return_code = 0
    except Exception:
        _write_startup_error(session)
        return_code = 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        if return_code == 0:
            try:
                remove_session_files(session)
            except Exception:
                pass
        elif published_state:
            _remove_file(state_path(session))
            _remove_file(stop_path(session))
            _remove_file(bootstrap_ack_path(session))
        if lock_entered and lock_context is not None:
            lock_context.__exit__(None, None, None)
    return return_code


if __name__ == "__main__":
    sys.exit(main())
