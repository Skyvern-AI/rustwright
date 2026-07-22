"""Durable, per-user state for persistent agent browser sessions."""

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .errors import AgentError

try:
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised by an import-hook regression test
    _fcntl = None


_SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STATE_FIELDS = {
    "schema",
    "session",
    "owner_pid",
    "endpoint",
    "control_token",
    "session_nonce",
    "active_target_id",
    "tabs",
    "next_tab_id",
    "next_ref_id",
    "dirty",
    "launch_config_hash",
}


def persistent_sessions_supported() -> bool:
    return _fcntl is not None and (sys.platform == "darwin" or sys.platform.startswith("linux"))


def validate_session_name(name: str) -> str:
    if not isinstance(name, str) or _SESSION_NAME.fullmatch(name) is None:
        raise AgentError(
            "invalid_argument",
            "Session name must contain 1 to 64 letters, digits, underscores, or hyphens",
        )
    return name


def launch_config_hash(
    headed: bool,
    executable_path: Optional[str],
    browser_args: Any,
) -> str:
    value = {
        "headed": bool(headed),
        "executable_path": executable_path,
        "browser_args": list(browser_args or []),
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unsafe_path(message: str) -> AgentError:
    return AgentError("session_lost", message)


def _validate_directory(path: Path, require_private: bool = True) -> None:
    try:
        info = os.lstat(str(path))
    except OSError:
        raise _unsafe_path("The agent runtime directory is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _unsafe_path("The agent runtime path must be a real directory")
    if info.st_uid != os.getuid():
        raise _unsafe_path("The agent runtime directory is not owned by the current user")
    if require_private and info.st_mode & 0o022:
        raise _unsafe_path("The agent runtime directory must not be group or world writable")


def _make_private_directory(path: Path) -> Path:
    try:
        os.makedirs(str(path), mode=0o700, exist_ok=True)
    except OSError:
        raise _unsafe_path("The agent runtime directory could not be created") from None
    _validate_directory(path)
    return path


def runtime_dir() -> Path:
    """Return a private runtime directory without accepting a symlink target."""

    configured = os.environ.get("RUSTWRIGHT_AGENT_RUNTIME_DIR")
    if configured:
        return _make_private_directory(Path(configured).expanduser())

    xdg_value = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_value:
        xdg = Path(xdg_value).expanduser()
        try:
            _validate_directory(xdg)
        except AgentError:
            pass
        else:
            return _make_private_directory(xdg / "rustwright" / "agent")

    temporary = os.environ.get("TMPDIR") or tempfile.gettempdir() or "/tmp"
    return _make_private_directory(Path(temporary) / ("rustwright-agent-%d" % os.getuid()))


def session_dir(name: str) -> Path:
    validate_session_name(name)
    path = runtime_dir() / name
    try:
        os.mkdir(str(path), 0o700)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise _unsafe_path("The session directory could not be created") from None
    _validate_directory(path)
    return path


def state_path(name: str) -> Path:
    return session_dir(name) / "state.json"


def lock_path(name: str) -> Path:
    return session_dir(name) / "session.lock"


def stop_path(name: str) -> Path:
    return session_dir(name) / "stop.json"


def error_path(name: str) -> Path:
    return session_dir(name) / "error.json"


def bootstrap_ack_path(name: str) -> Path:
    return session_dir(name) / "bootstrap-ack.json"


def owner_lock_path(name: str) -> Path:
    return session_dir(name) / "owner.lock"


def _validate_regular_file(path: Path) -> os.stat_result:
    try:
        info = os.lstat(str(path))
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise
        raise _unsafe_path("A session state file is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _unsafe_path("A session state path must be a regular file")
    if info.st_uid != os.getuid():
        raise _unsafe_path("A session state file is not owned by the current user")
    return info


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Any, value: Dict[str, Any]) -> None:
    """Write compact JSON atomically using a private exclusive temp file."""

    target = Path(path)
    parent = target.parent
    _validate_directory(parent)
    try:
        _validate_regular_file(target)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise

    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow
    temporary = None  # type: Optional[Path]
    fd = None  # type: Optional[int]
    try:
        for _attempt in range(100):
            candidate = parent / (".%s.%s.tmp" % (target.name, secrets.token_hex(8)))
            try:
                fd = os.open(str(candidate), flags, 0o600)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    continue
                raise
            temporary = candidate
            break
        if fd is None or temporary is None:
            raise OSError(errno.EEXIST, "could not allocate an atomic state file")
        offset = 0
        while offset < len(encoded):
            offset += os.write(fd, encoded[offset:])
        os.fsync(fd)
        os.close(fd)
        fd = None

        # Re-check an existing destination so a pre-existing symlink is refused
        # rather than silently replaced. os.replace itself never follows it.
        try:
            _validate_regular_file(target)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        os.replace(str(temporary), str(target))
        temporary = None
        _fsync_directory(parent)
    except AgentError:
        raise
    except OSError:
        raise _unsafe_path("The session state could not be written") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(str(temporary))
            except OSError:
                pass


def read_json(path: Any, missing_ok: bool = False) -> Optional[Dict[str, Any]]:
    target = Path(path)
    try:
        before = _validate_regular_file(target)
    except OSError as exc:
        if exc.errno == errno.ENOENT and missing_ok:
            return None
        if exc.errno == errno.ENOENT:
            raise _unsafe_path("The session state file does not exist") from None
        raise

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), os.O_RDONLY | nofollow)
        try:
            after = os.fstat(fd)
            if not stat.S_ISREG(after.st_mode) or after.st_uid != os.getuid():
                raise _unsafe_path("A session state path must be an owned regular file")
            if before.st_dev != after.st_dev or before.st_ino != after.st_ino:
                raise _unsafe_path("The session state changed while it was opened")
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > 4 * 1024 * 1024:
                    raise _unsafe_path("The session state file is too large")
        finally:
            os.close(fd)
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except AgentError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise _unsafe_path("The session state file is invalid") from None
    if not isinstance(value, dict):
        raise _unsafe_path("The session state file is invalid")
    return value


def _validate_state(value: Dict[str, Any], expected_session: str) -> Dict[str, Any]:
    if set(value) != _STATE_FIELDS or value.get("schema") != 1:
        raise _unsafe_path("The session state schema is unsupported")
    if value.get("session") != expected_session:
        raise _unsafe_path("The session state name does not match")
    if isinstance(value.get("owner_pid"), bool) or not isinstance(value.get("owner_pid"), int):
        raise _unsafe_path("The session owner pid is invalid")
    for field in ("endpoint", "control_token", "session_nonce", "launch_config_hash"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise _unsafe_path("The session state is incomplete")
    active_target = value.get("active_target_id")
    if active_target is not None and not isinstance(active_target, str):
        raise _unsafe_path("The active tab metadata is invalid")
    tabs = value.get("tabs")
    if not isinstance(tabs, dict) or any(
        not isinstance(target, str) or not isinstance(tab, str) for target, tab in tabs.items()
    ):
        raise _unsafe_path("The tab metadata is invalid")
    for field in ("next_tab_id", "next_ref_id"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise _unsafe_path("The session counter metadata is invalid")
    dirty = value.get("dirty")
    if dirty is not None and (not isinstance(dirty, str) or not dirty):
        raise _unsafe_path("The session journal marker is invalid")
    return value


def read_state(name: str) -> Optional[Dict[str, Any]]:
    validate_session_name(name)
    value = read_json(state_path(name), missing_ok=True)
    if value is None:
        return None
    return _validate_state(value, name)


def write_state(name: str, value: Dict[str, Any]) -> None:
    validate_session_name(name)
    _validate_state(value, name)
    atomic_write_json(state_path(name), value)


def mark_dirty(state: Dict[str, Any]) -> str:
    """Durably mark a state-changing command as in progress."""

    marker = "%d-%s" % (time.monotonic_ns(), secrets.token_hex(8))
    state["dirty"] = marker
    write_state(state["session"], state)
    return marker


def clear_dirty(state: Dict[str, Any]) -> None:
    state["dirty"] = None
    write_state(state["session"], state)


def _open_owned_lock(path: Path, message: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = None  # type: Optional[int]
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | nofollow, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _unsafe_path(message)
        return fd
    except AgentError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise _unsafe_path(message) from None


@contextmanager
def owner_lifetime_lock(name: str) -> Iterator[None]:
    """Hold the dedicated owner identity lock for the process lifetime."""

    validate_session_name(name)
    fd = _open_owned_lock(owner_lock_path(name), "The owner lock could not be opened")
    acquired = False
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AgentError("session_busy", "A browser owner is already running") from None
            raise _unsafe_path("The owner lock failed") from None
        yield
    finally:
        if acquired:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def owner_lock_is_held(name: str) -> bool:
    """Return whether a live owner currently holds the identity lock."""

    validate_session_name(name)
    fd = _open_owned_lock(owner_lock_path(name), "The owner lock could not be opened")
    acquired = False
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            acquired = True
            return False
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise _unsafe_path("The owner lock failed") from None
    finally:
        if acquired:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


@contextmanager
def session_lock(name: str, timeout: float = 30.0) -> Iterator[None]:
    validate_session_name(name)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        raise AgentError("invalid_argument", "Lock timeout must be a non-negative number")
    path = lock_path(name)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | nofollow, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _unsafe_path("The session lock must be an owned regular file")
    except AgentError:
        raise
    except OSError:
        raise _unsafe_path("The session lock could not be opened") from None

    deadline = time.monotonic() + float(timeout)
    acquired = False
    try:
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise _unsafe_path("The session lock failed") from None
                if time.monotonic() >= deadline:
                    raise AgentError("session_busy", "The browser session is busy")
                time.sleep(0.025)
        yield
    finally:
        if acquired:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def remove_session_files(name: str, include_lock: bool = False) -> None:
    """Remove runtime control files without following attacker-supplied paths."""

    paths = [state_path(name), stop_path(name), error_path(name), bootstrap_ack_path(name)]
    if include_lock:
        paths.append(lock_path(name))
    for path in paths:
        try:
            os.unlink(str(path))
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise _unsafe_path("The session runtime files could not be removed") from None
    _fsync_directory(session_dir(name))
