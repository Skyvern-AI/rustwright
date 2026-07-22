"""Persistent named-session command line interface."""

import argparse
import errno
from importlib import metadata
import ipaddress
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import actions
from .attach import AttachedSession
from .errors import AgentError
from .state import (
    atomic_write_json,
    bootstrap_ack_path,
    error_path,
    launch_config_hash,
    mark_dirty,
    owner_lock_is_held,
    persistent_sessions_supported,
    read_json,
    read_state,
    remove_session_files,
    session_lock,
    state_path,
    stop_path,
    validate_session_name,
    write_state,
)


_COMMANDS = {
    "open",
    "navigate",
    "back",
    "reload",
    "snapshot",
    "click",
    "fill",
    "type",
    "select",
    "hover",
    "press",
    "wait",
    "tabs",
    "screenshot",
    "eval",
    "status",
    "close",
}

_SNAPSHOT_REF_RESERVATION = 1000


def _version() -> str:
    try:
        return metadata.version("rustwright")
    except metadata.PackageNotFoundError:
        return "0.1.1"


class ParserExit(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class NonExitingArgumentParser(argparse.ArgumentParser):
    """An argparse parser that reports errors through the normal envelope."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._captured_message = ""

    def _print_message(self, message: Optional[str], file: Any = None) -> None:
        if message:
            self._captured_message += message

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        raise ParserExit(status, (message or "") + self._captured_message)

    def error(self, message: str) -> None:
        raise AgentError("invalid_argument", message)


def _bounded_integer(name: str, minimum: int, maximum: int) -> Any:
    def parse(value: str) -> int:
        try:
            result = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("%s must be an integer" % name) from None
        if result < minimum or result > maximum:
            raise argparse.ArgumentTypeError(
                "%s must be between %d and %d" % (name, minimum, maximum)
            )
        return result

    return parse


def _add_wait_until(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait-until",
        choices=("domcontentloaded", "load", "networkidle"),
        default="domcontentloaded",
    )


def build_parser() -> NonExitingArgumentParser:
    parser = NonExitingArgumentParser(prog="rustwright")
    parser.add_argument("--version", action="version", version=_version())
    parser.add_argument("--session", default="default")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--timeout-ms",
        type=_bounded_integer("timeout-ms", 1, 120000),
        default=5000,
    )
    parser.add_argument(
        "--navigation-timeout-ms",
        type=_bounded_integer("navigation-timeout-ms", 1, 120000),
        default=60000,
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--executable-path")
    parser.add_argument("--browser-arg", action="append", default=[])
    parser.add_argument("--allow-eval", action="store_true")

    commands = parser.add_subparsers(dest="command", required=True)
    open_parser = commands.add_parser("open")
    open_parser.add_argument("url", nargs="?")
    open_parser.add_argument(
        "-b",
        "--browser",
        default="chromium",
        help="browser to use (cr or chromium; other Chromium executables require --executable-path)",
    )

    navigate_parser = commands.add_parser("navigate")
    navigate_parser.add_argument("url")
    _add_wait_until(navigate_parser)

    back_parser = commands.add_parser("back")
    _add_wait_until(back_parser)
    reload_parser = commands.add_parser("reload")
    _add_wait_until(reload_parser)

    snapshot_parser = commands.add_parser("snapshot")
    snapshot_parser.add_argument("--depth", type=_bounded_integer("depth", 0, 12))
    snapshot_parser.add_argument(
        "--max-chars",
        type=_bounded_integer("max-chars", 1000, 200000),
    )

    click_parser = commands.add_parser("click")
    click_parser.add_argument("ref")
    click_parser.add_argument("--button", choices=("left", "right", "middle"), default="left")
    click_parser.add_argument(
        "--click-count",
        type=_bounded_integer("click-count", 1, 3),
        default=1,
    )

    fill_parser = commands.add_parser("fill")
    fill_parser.add_argument("ref")
    fill_parser.add_argument("text")
    type_parser = commands.add_parser("type")
    type_parser.add_argument("ref")
    type_parser.add_argument("text")
    type_parser.add_argument(
        "--delay-ms",
        type=_bounded_integer("delay-ms", 0, 1000),
        default=0,
    )

    select_parser = commands.add_parser("select")
    select_parser.add_argument("ref")
    select_parser.add_argument("values", nargs="+")
    hover_parser = commands.add_parser("hover")
    hover_parser.add_argument("ref")
    press_parser = commands.add_parser("press")
    press_parser.add_argument("key")

    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("milliseconds", nargs="?", type=_bounded_integer("milliseconds", 0, 60000))
    wait_parser.add_argument("--text")
    wait_parser.add_argument("--text-gone")
    wait_parser.add_argument(
        "--load",
        choices=("domcontentloaded", "load", "networkidle"),
    )

    tabs_parser = commands.add_parser("tabs")
    tab_commands = tabs_parser.add_subparsers(dest="tabs_action", required=True)
    tab_commands.add_parser("list")
    tab_new = tab_commands.add_parser("new")
    tab_new.add_argument("url", nargs="?")
    tab_use = tab_commands.add_parser("use")
    tab_use.add_argument("tab_id")
    tab_close = tab_commands.add_parser("close")
    tab_close.add_argument("tab_id", nargs="?")

    screenshot_parser = commands.add_parser("screenshot")
    screenshot_parser.add_argument("path", nargs="?")
    screenshot_parser.add_argument("--full", action="store_true")
    screenshot_parser.add_argument("--ref")
    screenshot_parser.add_argument("--type", choices=("png", "jpeg"), default="png")
    screenshot_parser.add_argument(
        "--quality",
        type=_bounded_integer("quality", 0, 100),
    )
    screenshot_parser.add_argument("--force", action="store_true")

    eval_parser = commands.add_parser("eval")
    eval_parser.add_argument("expression", nargs="?")
    eval_parser.add_argument("--stdin", action="store_true")

    commands.add_parser("status")
    close_parser = commands.add_parser("close")
    close_parser.add_argument("--force", action="store_true")
    return parser


def _guess_invocation(argv: List[str]) -> Tuple[bool, str, str]:
    json_output = "--json" in argv
    command = next((item for item in argv if item in _COMMANDS), "unknown")
    session = "default"
    for index, item in enumerate(argv):
        if item == "--session" and index + 1 < len(argv):
            session = argv[index + 1]
        elif item.startswith("--session="):
            session = item.split("=", 1)[1]
    return json_output, command, session


def _clean_error(error: AgentError) -> Dict[str, Any]:
    value = {"code": error.code, "message": error.message}
    if error.hint:
        value["hint"] = error.hint
    return value


def _write_stdout(value: str) -> None:
    sys.stdout.write(value)
    sys.stdout.flush()


def _write_stderr(value: str) -> None:
    sys.stderr.write(value)
    sys.stderr.flush()


def _emit_json(
    success: bool,
    command: str,
    session: str,
    data: Optional[Dict[str, Any]],
    error: Optional[AgentError],
    warnings: Optional[List[str]] = None,
) -> None:
    envelope = {
        "version": 1,
        "success": success,
        "command": command,
        "session": session,
        "data": data,
        "error": _clean_error(error) if error is not None else None,
        "warnings": list(warnings or []),
    }
    _write_stdout(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n")


def _emit_error(json_output: bool, command: str, session: str, error: AgentError) -> None:
    if json_output:
        _emit_json(False, command, session, None, error)
        return
    _write_stderr("error[%s]: %s\n" % (error.code, error.message))
    if error.hint:
        _write_stderr("hint: %s\n" % error.hint)


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def _validate_endpoint(endpoint: str) -> None:
    try:
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError
        if parsed.hostname == "localhost":
            return
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError
    except ValueError:
        raise AgentError("session_lost", "The browser owner endpoint is invalid") from None


def _launch_flags_explicit(argv: List[str]) -> bool:
    prefixes = ("--headed", "--executable-path", "--browser-arg")
    return any(item == prefix or item.startswith(prefix + "=") for item in argv for prefix in prefixes)


def _remove_startup_error(session: str) -> None:
    for path in (error_path(session), bootstrap_ack_path(session)):
        try:
            os.unlink(str(path))
        except OSError:
            pass


def _terminate_spawned_owner(process: Any) -> None:
    """Terminate and reap a child whose bootstrap did not complete."""

    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=2.0)
    except Exception:
        pass


def _spawn_owner(args: argparse.Namespace) -> Dict[str, Any]:
    _remove_startup_error(args.session)
    bootstrap_token = secrets.token_hex(32)
    command = [
        sys.executable,
        "-m",
        "rustwright._agent.owner",
        "--session",
        args.session,
        "--bootstrap-token",
        bootstrap_token,
    ]
    if args.headed:
        command.append("--headed")
    if args.executable_path is not None:
        command.extend(["--executable-path", args.executable_path])
    for browser_arg in args.browser_arg:
        command.extend(["--browser-arg", browser_arg])
    try:
        process = subprocess.Popen(
            command,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        raise AgentError("session_lost", "The persistent browser owner could not be started") from None

    ready = False
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = read_state(args.session)
            if state is not None:
                _validate_endpoint(state["endpoint"])
                if not _pid_alive(state["owner_pid"]) or not owner_lock_is_held(args.session):
                    break
                atomic_write_json(
                    bootstrap_ack_path(args.session),
                    {"token": bootstrap_token},
                )
                ready = True
                return state
            failure = read_json(error_path(args.session), missing_ok=True)
            if failure is not None or process.poll() is not None:
                break
            time.sleep(0.05)
        raise AgentError(
            "session_lost",
            "The persistent browser owner did not become ready",
            "Close the session with --force before retrying.",
        )
    finally:
        if not ready:
            _terminate_spawned_owner(process)


def _state_for_browser(args: argparse.Namespace, argv: List[str]) -> Dict[str, Any]:
    state = read_state(args.session)
    if state is None:
        state = _spawn_owner(args)
    elif not _pid_alive(state["owner_pid"]):
        raise AgentError(
            "session_lost",
            "The persistent browser owner is not running",
            "Close the session with --force before retrying.",
        )
    _validate_endpoint(state["endpoint"])
    if _launch_flags_explicit(argv):
        requested = launch_config_hash(args.headed, args.executable_path, args.browser_arg)
        if requested != state["launch_config_hash"]:
            raise AgentError(
                "invalid_argument",
                "Launch-only options do not match the running session",
                "Close the session before changing browser launch options.",
            )
    return state


def _attach(args: argparse.Namespace, state: Dict[str, Any]) -> AttachedSession:
    return AttachedSession(
        state["endpoint"],
        state["tabs"],
        active_target_id=state["active_target_id"],
        next_tab_id=state["next_tab_id"],
        next_ref_id=state["next_ref_id"],
        session_nonce=state["session_nonce"],
        restore_refs=state["dirty"] is None,
        action_timeout_ms=args.timeout_ms,
        navigation_timeout_ms=args.navigation_timeout_ms,
        allow_eval=args.allow_eval,
    )


def _is_mutating(args: argparse.Namespace) -> bool:
    if args.command == "open":
        return True
    if args.command == "tabs":
        return args.tabs_action != "list"
    return args.command in {
        "snapshot",
        "navigate",
        "back",
        "reload",
        "click",
        "fill",
        "type",
        "select",
        "hover",
        "press",
        "wait",
        "eval",
    }


def _validate_command(args: argparse.Namespace) -> None:
    validate_session_name(args.session)
    if args.command == "open":
        if args.browser in {"ff", "firefox", "wk", "webkit"}:
            raise AgentError(
                "invalid_argument",
                f"{args.browser} is not implemented; Rustwright currently supports Chromium over direct CDP.",
            )
        if args.browser in {"chrome", "msedge"} and args.executable_path is None:
            raise AgentError(
                "invalid_argument",
                f"{args.browser} channel selection is not supported; use --executable-path to launch that browser.",
            )
        if args.browser not in {"cr", "chromium", "chrome", "msedge"}:
            raise AgentError("invalid_argument", f"Unknown browser: {args.browser}")
    forbidden_browser_args = (
        "--remote-debugging-port",
        "--remote-debugging-pipe",
        "--user-data-dir",
    )
    for browser_arg in args.browser_arg:
        lowered = browser_arg.lower()
        if any(
            lowered == forbidden or lowered.startswith(forbidden + "=")
            for forbidden in forbidden_browser_args
        ):
            raise AgentError(
                "invalid_argument",
                "A browser argument conflicts with persistent session management",
            )
    if args.command == "wait":
        supplied = [
            args.milliseconds is not None,
            args.text is not None,
            args.text_gone is not None,
            args.load is not None,
        ]
        if sum(1 for value in supplied if value) != 1:
            raise AgentError("invalid_argument", "Exactly one wait condition is required")
    if args.command == "eval":
        if not args.allow_eval:
            raise AgentError(
                "eval_disabled",
                "Browser evaluation is disabled",
                "Repeat the command with --allow-eval.",
            )
        if (args.expression is None) == (not args.stdin):
            raise AgentError("invalid_argument", "Provide an expression or --stdin, but not both")
    if args.command == "screenshot":
        if args.full and args.ref is not None:
            raise AgentError("invalid_argument", "--full and --ref cannot be used together")
        if args.quality is not None and args.type != "jpeg":
            raise AgentError("invalid_argument", "--quality is only valid for jpeg screenshots")


def _dispatch(args: argparse.Namespace, session: AttachedSession) -> Dict[str, Any]:
    command = args.command
    if command == "open":
        if args.url is None:
            result = actions.snapshot(session)
            result["message"] = "opened"
            return result
        return actions.navigate(session, args.url)
    if command == "navigate":
        return actions.navigate(session, args.url, args.wait_until)
    if command == "back":
        return actions.navigate_back(session, args.wait_until)
    if command == "reload":
        return actions.reload(session, args.wait_until)
    if command == "snapshot":
        return actions.snapshot(session, args.depth, args.max_chars)
    if command == "click":
        return actions.click(session, args.ref, args.button, args.click_count)
    if command == "fill":
        return actions.fill(session, args.ref, args.text)
    if command == "type":
        return actions.type_text(session, args.ref, args.text, args.delay_ms)
    if command == "select":
        return actions.select_option(session, args.ref, args.values)
    if command == "hover":
        return actions.hover(session, args.ref)
    if command == "press":
        return actions.press_key(session, args.key)
    if command == "wait":
        return actions.wait_for(
            session,
            time_ms=args.milliseconds,
            text=args.text,
            text_gone=args.text_gone,
            load_state=args.load,
        )
    if command == "tabs":
        action = "select" if args.tabs_action == "use" else args.tabs_action
        return actions.tabs(
            session,
            action,
            tab_id=getattr(args, "tab_id", None),
            url=getattr(args, "url", None),
        )
    if command == "screenshot":
        return actions.take_screenshot(
            session,
            ref=args.ref,
            type=args.type,
            full_page=args.full,
            quality=args.quality,
        )
    if command == "eval":
        expression = sys.stdin.read(200001) if args.stdin else args.expression
        if args.stdin and len(expression) > 200000:
            raise AgentError(
                "invalid_argument",
                "expression must be a string with length between 1 and 200000",
            )
        return actions.evaluate(session, expression)
    raise AgentError("invalid_request", "Unknown browser command")


def _persist(state: Dict[str, Any], session: AttachedSession) -> None:
    state["active_target_id"] = session.active_target_id
    state["tabs"] = session.tab_metadata()
    state["next_tab_id"] = session.next_tab_id
    state["next_ref_id"] = session.next_ref_id
    state["dirty"] = None
    write_state(state["session"], state)


def _reserve_refs_before_dispatch(
    state: Dict[str, Any],
    session: AttachedSession,
) -> None:
    """Durably advance the ref high-water mark before snapshot evaluation."""

    session.prepare_ref_reservation(_SNAPSHOT_REF_RESERVATION)
    state["next_ref_id"] = session.next_ref_id
    mark_dirty(state)


def _write_screenshot(args: argparse.Namespace, result: Dict[str, Any]) -> Dict[str, Any]:
    image = result.get("image")
    if not isinstance(image, bytes):
        raise AgentError("screenshot_failed", "The screenshot did not return image bytes")
    destination = Path(args.path or ("rustwright-screenshot.%s" % ("jpg" if args.type == "jpeg" else "png")))
    destination = Path(os.path.abspath(str(destination)))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | nofollow
    if args.force:
        flags |= os.O_TRUNC
    else:
        flags |= os.O_EXCL
    try:
        if destination.exists() and destination.is_symlink():
            raise OSError(errno.ELOOP, "symlink")
        fd = os.open(str(destination), flags, 0o600)
        try:
            offset = 0
            while offset < len(image):
                offset += os.write(fd, image[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise AgentError(
                "invalid_argument",
                "The screenshot path already exists",
                "Use --force to replace an existing regular file.",
            ) from None
        raise AgentError("screenshot_failed", "The screenshot file could not be written") from None
    return {
        "message": result.get("message", "captured screenshot"),
        "path": str(destination),
        "mime_type": result.get("mime_type"),
        "bytes": len(image),
    }


def _status_data(session_name: str, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if state is None or not _pid_alive(state["owner_pid"]):
        return {"running": False, "session": session_name}
    return {
        "running": True,
        "session": session_name,
        "owner_pid": state["owner_pid"],
        "tabs": len(state["tabs"]),
        "dirty": state["dirty"] is not None,
    }


def _force_stop(session: str, pid: Any) -> None:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or pid == os.getpid()
        or not owner_lock_is_held(session)
    ):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and owner_lock_is_held(session):
        time.sleep(0.05)


def _close_session(args: argparse.Namespace) -> Dict[str, Any]:
    try:
        state = read_state(args.session)
    except AgentError:
        if not args.force:
            raise
        remove_session_files(args.session)
        return {"message": "closed session", "running": False}
    if state is None:
        remove_session_files(args.session)
        return {"message": "closed session", "running": False}

    if args.force:
        if not owner_lock_is_held(args.session):
            remove_session_files(args.session)
            return {"message": "closed session", "running": False}
        atomic_write_json(stop_path(args.session), {"control_token": state["control_token"]})
        graceful_deadline = time.monotonic() + 1.0
        while time.monotonic() < graceful_deadline and owner_lock_is_held(args.session):
            time.sleep(0.05)
        if owner_lock_is_held(args.session):
            _force_stop(args.session, state["owner_pid"])
        if owner_lock_is_held(args.session):
            raise AgentError("timeout", "The browser owner did not stop in time")
        remove_session_files(args.session)
        return {"message": "closed session", "running": False}

    if not _pid_alive(state["owner_pid"]):
        raise AgentError(
            "session_lost",
            "The persistent browser owner is not running",
            "Repeat close with --force to clear the stale session.",
        )
    atomic_write_json(stop_path(args.session), {"control_token": state["control_token"]})
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if read_state(args.session) is None:
            remove_session_files(args.session)
            return {"message": "closed session", "running": False}
        time.sleep(0.05)
    raise AgentError(
        "timeout",
        "The browser owner did not stop in time",
        "Repeat close with --force to clean up the session.",
    )


def _human_success(args: argparse.Namespace, data: Dict[str, Any]) -> None:
    if args.command == "status":
        if data["running"]:
            _write_stdout(
                "running\t%s\t%d\t%d\n"
                % (data["session"], data["owner_pid"], data["tabs"])
            )
        else:
            _write_stdout("stopped\n")
        return
    if args.command == "tabs" and args.tabs_action == "list":
        message = str(data.get("message", ""))
        lines = message.splitlines()
        if lines and lines[0] == "tabs":
            lines = lines[1:]
        _write_stdout(("\n".join(lines) + "\n") if lines else "")
        return
    if args.command == "screenshot":
        _write_stdout(str(data["path"]) + "\n")
        return
    if args.command == "snapshot":
        prefix = ""
    elif args.command == "eval":
        _write_stdout(json.dumps(data.get("value"), separators=(",", ":")) + "\n")
        prefix = ""
    else:
        suffix = ""
        if args.command in ("click", "fill", "type", "select", "hover"):
            suffix = " " + args.ref.lstrip("@")
        prefix = "ok %s%s\n" % (args.command, suffix)
    body = prefix
    if "url" in data:
        body += "url: %s\n" % data.get("url", "")
    if "title" in data:
        body += "title: %s\n" % data.get("title", "")
    if "snapshot" in data:
        body += "snapshot:\n%s\n" % data.get("snapshot", "")
    if body:
        _write_stdout(body)


def _success_exit(args: argparse.Namespace, data: Dict[str, Any]) -> int:
    if args.json_output:
        _emit_json(True, args.command, args.session, data, None)
    else:
        _human_success(args, data)
    return 0


def _run(args: argparse.Namespace, argv: List[str]) -> Dict[str, Any]:
    if not persistent_sessions_supported():
        raise AgentError(
            "unsupported_platform",
            "persistent sessions require macOS or Linux",
        )
    _validate_command(args)
    with session_lock(args.session):
        if args.command == "status":
            return _status_data(args.session, read_state(args.session))
        if args.command == "close":
            return _close_session(args)

        state = _state_for_browser(args, argv)
        session = None  # type: Optional[AttachedSession]
        try:
            session = _attach(args, state)
            if state["dirty"] is not None:
                session.clear_active_refs()
            if _is_mutating(args):
                _reserve_refs_before_dispatch(state, session)
            result = _dispatch(args, session)
            if args.command == "screenshot":
                result = _write_screenshot(args, result)
            _persist(state, session)
            return result
        finally:
            if session is not None:
                session.close()


def main(argv: Optional[List[str]] = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    json_output, command, session = _guess_invocation(actual_argv)
    try:
        args = build_parser().parse_args(actual_argv)
        json_output = args.json_output
        command = args.command
        session = args.session
        result = _run(args, actual_argv)
        return _success_exit(args, result)
    except ParserExit as exc:
        if exc.status == 0:
            if json_output:
                _emit_json(True, command, session, {"text": exc.message}, None)
            else:
                _write_stdout(exc.message)
            return 0
        error = AgentError("invalid_argument", exc.message.strip() or "Invalid arguments")
    except AgentError as exc:
        error = exc
    except KeyboardInterrupt:
        error = AgentError("interrupted", "Interrupted")
        try:
            _emit_error(json_output, command, session, error)
        except BrokenPipeError:
            pass
        return 130
    except BrokenPipeError:
        return 1
    except Exception:
        error = AgentError("action_failed", "The command failed")

    try:
        _emit_error(json_output, command, session, error)
    except BrokenPipeError:
        return 1
    if error.code == "session_busy":
        return 4
    return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
