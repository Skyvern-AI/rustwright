#!/usr/bin/env python3
"""Serve controlled fixtures locally or encode them for a remote browser."""

from __future__ import annotations

import argparse
import base64
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType


HOST = "127.0.0.1"
DEFAULT_PORT = 8099
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _fixture_path(name: str) -> Path:
    """Return a fixture path while rejecting traversal and non-HTML files."""
    if not name or Path(name).name != name or not name.endswith(".html"):
        raise ValueError("fixture name must be a plain .html filename")
    path = FIXTURES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"fixture does not exist: {name}")
    return path


def fixture_data_url(name: str) -> str:
    """Return the exact fixture bytes as a network-free HTML data URL."""
    encoded = base64.b64encode(_fixture_path(name).read_bytes()).decode("ascii")
    return f"data:text/html;base64,{encoded}"


def _environment_port() -> int:
    value = os.environ.get("FIXTURE_PORT", str(DEFAULT_PORT))
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("FIXTURE_PORT must be an integer") from error
    if not 0 <= port <= 65535:
        raise ValueError("FIXTURE_PORT must be between 0 and 65535")
    return port


class _FixtureRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class FixtureServer:
    """Context-managed localhost server for browsers on the runner host."""

    def __init__(self, port: int | None = None) -> None:
        self.port = _environment_port() if port is None else port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("fixture server is not running")
        return f"http://{HOST}:{self.port}"

    def fixture_url(self, name: str) -> str:
        _fixture_path(name)
        return f"{self.base_url}/{name}"

    def start(self) -> FixtureServer:
        if self._server is not None:
            raise RuntimeError("fixture server is already running")
        handler = partial(_FixtureRequestHandler, directory=str(FIXTURES_DIR))
        self._server = ThreadingHTTPServer((HOST, self.port), handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="controlled-fixture-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def wait(self) -> None:
        if self._thread is None:
            raise RuntimeError("fixture server is not running")
        while self._thread.is_alive():
            self._thread.join(timeout=1)

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"localhost port (default: FIXTURE_PORT or {DEFAULT_PORT})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with FixtureServer(args.port) as server:
            print(f"Serving controlled fixtures at {server.base_url}/", flush=True)
            server.wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
