"""Filesystem policy for MCP-produced artifacts and upload inputs."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading
from typing import BinaryIO, Iterator, Protocol, Sequence
import uuid


DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 200 * 1024 * 1024


class FilePolicyError(ValueError):
    """A file path or size violated the configured policy."""


class RootsProvider(Protocol):
    """Supplies roots from which input files may be read."""

    def roots(self) -> Sequence[Path]:
        ...


class EnvRootsProvider:
    """Read the current input root from ``RUSTWRIGHT_MCP_WORKSPACE``."""

    def roots(self) -> Sequence[Path]:
        raw_root = os.environ.get("RUSTWRIGHT_MCP_WORKSPACE")
        if not raw_root:
            return ()
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise FilePolicyError("RUSTWRIGHT_MCP_WORKSPACE must be an absolute path")
        return (root.resolve(strict=True),)


def _configured_limit(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise FilePolicyError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise FilePolicyError(f"{name} must be a positive integer")
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class FilePolicy:
    """Confine outputs and validate input paths against explicit roots."""

    def __init__(
        self,
        *,
        output_root: Path | str | None = None,
        roots_provider: RootsProvider | None = None,
        max_file_bytes: int | None = None,
        max_total_bytes: int | None = None,
    ) -> None:
        if output_root is None:
            configured_root = os.environ.get("RUSTWRIGHT_MCP_OUTPUT_DIR")
            if configured_root:
                root = Path(configured_root).expanduser()
            else:
                configured_cache = os.environ.get("XDG_CACHE_HOME")
                cache_home = (
                    Path(configured_cache).expanduser()
                    if configured_cache
                    else Path.home() / ".cache"
                )
                root = cache_home / "rustwright-mcp" / "output" / str(uuid.uuid4())
        else:
            root = Path(output_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root_existed = root.exists()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.output_root = root.resolve(strict=True)
        if not root_existed:
            os.chmod(self.output_root, 0o700)
        self.roots_provider = roots_provider or EnvRootsProvider()
        self.max_file_bytes = (
            max_file_bytes
            if max_file_bytes is not None
            else _configured_limit(
                "RUSTWRIGHT_MCP_OUTPUT_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES
            )
        )
        self.max_total_bytes = (
            max_total_bytes
            if max_total_bytes is not None
            else _configured_limit(
                "RUSTWRIGHT_MCP_OUTPUT_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES
            )
        )
        if self.max_file_bytes <= 0 or self.max_total_bytes <= 0:
            raise FilePolicyError("output byte caps must be positive")
        self._lock = threading.RLock()
        self._created_outputs: set[Path] = set()

    def _confined_output_path(
        self,
        requested: str | None,
        purpose: str,
        suffix: str,
    ) -> Path:
        if requested is None:
            candidate = self.output_root / f"{purpose}-{uuid.uuid4().hex}{suffix}"
        else:
            raw_path = Path(requested).expanduser()
            candidate = raw_path if raw_path.is_absolute() else self.output_root / raw_path
        resolved = candidate.resolve(strict=False)
        if not _is_within(resolved, self.output_root):
            shown = requested if requested is not None else str(candidate)
            raise FilePolicyError(
                f"{purpose} paths are confined to RUSTWRIGHT_MCP_OUTPUT_DIR "
                f"({self.output_root}); got {shown}"
            )
        return resolved

    def _ensure_secure_parent(self, path: Path) -> None:
        relative_parent = path.parent.relative_to(self.output_root)
        current = self.output_root
        for component in relative_parent.parts:
            current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise FilePolicyError(f"output path has an unsafe parent: {path}")

    def reserve_output(
        self,
        requested: str | None = None,
        *,
        purpose: str = "output",
        suffix: str = ".png",
    ) -> Path:
        """Exclusively create a mode-0600 file beneath the output root."""
        if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
            raise FilePolicyError("output suffix must be a simple file extension")
        with self._lock:
            path = self._confined_output_path(requested, purpose, suffix)
            self._ensure_secure_parent(path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                raise FilePolicyError(f"output file already exists: {path}") from None
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._created_outputs.add(path)
            return path

    def discard_output(self, path: Path) -> None:
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._created_outputs.discard(path)

    @staticmethod
    def _remove_entry(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        try:
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                path.rmdir()
            else:
                path.unlink()
        except FileNotFoundError:
            pass

    def _discard_invalid_output(
        self,
        path: Path,
        *,
        resolved: Path | None,
        is_link: bool,
    ) -> None:
        lexical_path = Path(os.path.abspath(path))
        if not _is_within(lexical_path, self.output_root):
            return
        if is_link:
            # Unlink the confined directory entry, never its resolved target.
            try:
                os.unlink(lexical_path)
            except FileNotFoundError:
                pass
            self._created_outputs.discard(lexical_path)
            return
        if resolved is not None and _is_within(resolved, self.output_root):
            self._remove_entry(lexical_path)
            self._created_outputs.discard(lexical_path)

    def _artifact_link(self, path: Path) -> str:
        for root in self.roots_provider.roots():
            try:
                workspace_root = Path(root).expanduser().resolve(strict=True)
            except OSError:
                continue
            if _is_within(self.output_root, workspace_root):
                return path.relative_to(workspace_root).as_posix()
        return path.relative_to(self.output_root).as_posix()

    def finalize_output(self, path: Path) -> str:
        """Enforce caps, evict old artifacts, and return a relative link."""
        with self._lock:
            try:
                info = os.lstat(path)
            except OSError:
                self._discard_invalid_output(path, resolved=None, is_link=False)
                raise FilePolicyError(f"output file escaped the configured root: {path}")
            is_link = os.path.islink(path)
            resolved = Path(os.path.realpath(path))
            if (
                is_link
                or not stat.S_ISREG(info.st_mode)
                or not _is_within(resolved, self.output_root)
            ):
                self._discard_invalid_output(
                    path,
                    resolved=resolved,
                    is_link=is_link,
                )
                raise FilePolicyError(f"output file escaped the configured root: {path}")
            os.chmod(resolved, 0o600)
            if info.st_size > self.max_file_bytes:
                resolved.unlink()
                self._created_outputs.discard(resolved)
                raise FilePolicyError(
                    f"output file exceeds the {self.max_file_bytes}-byte per-file cap"
                )
            self._evict_to_total_cap(protected=resolved)
            return self._artifact_link(resolved)

    def _artifact_files(self) -> list[tuple[int, Path, int]]:
        artifacts: list[tuple[int, Path, int]] = []
        for path in tuple(self._created_outputs):
            try:
                info = path.lstat()
            except FileNotFoundError:
                self._created_outputs.discard(path)
                continue
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                artifacts.append((info.st_mtime_ns, path, info.st_size))
        return artifacts

    def _evict_to_total_cap(self, *, protected: Path) -> None:
        artifacts = self._artifact_files()
        total = sum(size for _, _, size in artifacts)
        if total <= self.max_total_bytes:
            return
        candidates = sorted(
            (item for item in artifacts if item[1] != protected), key=lambda item: item[0]
        )
        for _, path, size in candidates:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            self._created_outputs.discard(path)
            total -= size
            if total <= self.max_total_bytes:
                return
        if total > self.max_total_bytes:
            try:
                protected.unlink()
            except FileNotFoundError:
                pass
            self._created_outputs.discard(protected)
            raise FilePolicyError(
                f"output file cannot fit within the {self.max_total_bytes}-byte total cap"
            )

    def artifact_link(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, self.output_root):
            raise FilePolicyError(f"artifact is outside the configured output root: {path}")
        return self._artifact_link(resolved)

    def validate_input(self, requested: str | Path) -> Path:
        """Validate an absolute, regular, non-symlink input under an allowed root."""
        raw_path = Path(requested).expanduser()
        if not raw_path.is_absolute():
            raise FilePolicyError("input files must use absolute paths")
        try:
            original_info = raw_path.lstat()
        except FileNotFoundError:
            raise FilePolicyError(f"input file does not exist: {raw_path}") from None
        if stat.S_ISLNK(original_info.st_mode):
            raise FilePolicyError(f"input file must not be a symlink: {raw_path}")
        resolved = raw_path.resolve(strict=True)
        roots = tuple(self.roots_provider.roots())
        if not roots:
            raise FilePolicyError("no input workspace root is configured")
        if not any(_is_within(resolved, root.resolve(strict=True)) for root in roots):
            raise FilePolicyError(f"input file is outside the allowed workspace: {raw_path}")
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise FilePolicyError(f"input path is not a regular file: {raw_path}")
        return resolved

    def read_inputs(
        self, requested: Sequence[str | Path]
    ) -> list[tuple[Path, bytes]]:
        """Validate and read inputs while enforcing the shared byte caps.

        The descriptor-backed reads repeat the regular-file check performed by
        :meth:`validate_input` and never follow a final-component symlink.  The
        limits are shared with outputs so one policy configuration bounds every
        file payload crossing the MCP boundary.
        """
        loaded: list[tuple[Path, bytes]] = []
        total = 0
        for entry in requested:
            path = self.validate_input(entry)
            with self.open_input(path) as handle:
                initial_size = os.fstat(handle.fileno()).st_size
                if initial_size > self.max_file_bytes:
                    raise FilePolicyError(
                        f"input file exceeds the {self.max_file_bytes}-byte "
                        f"per-file cap: {path}"
                    )
                if total + initial_size > self.max_total_bytes:
                    raise FilePolicyError(
                        f"input files exceed the {self.max_total_bytes}-byte total cap"
                    )

                content = bytearray()
                while chunk := handle.read(1024 * 1024):
                    content.extend(chunk)
                    if len(content) > self.max_file_bytes:
                        raise FilePolicyError(
                            f"input file exceeds the {self.max_file_bytes}-byte "
                            f"per-file cap: {path}"
                        )
                    if total + len(content) > self.max_total_bytes:
                        raise FilePolicyError(
                            f"input files exceed the {self.max_total_bytes}-byte total cap"
                        )
                loaded.append((path, bytes(content)))
                total += len(content)
        return loaded

    @contextmanager
    def open_input(self, requested: str | Path) -> Iterator[BinaryIO]:
        """Open a validated input without following a final-component symlink."""
        path = self.validate_input(requested)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise FilePolicyError(f"input path is not a regular file: {path}")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                yield handle
        finally:
            os.close(descriptor)


_policy: FilePolicy | None = None
_policy_lock = threading.Lock()


def get_file_policy() -> FilePolicy:
    global _policy
    with _policy_lock:
        if _policy is None:
            _policy = FilePolicy()
        return _policy


def _reset_file_policy() -> None:
    """Reset the process singleton for isolated tests."""
    global _policy
    with _policy_lock:
        _policy = None
