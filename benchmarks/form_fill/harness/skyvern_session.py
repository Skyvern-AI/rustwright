#!/usr/bin/env python3
"""Provision short-lived Skyvern browser sessions without third-party packages."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from threading import Lock
from typing import Any, Callable


DEFAULT_BASE_URL = "https://api.skyvern.com"
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_TERMINAL_STATUSES = {"cancelled", "closed", "completed", "failed", "timeout"}
LOGGER = logging.getLogger(__name__)


class SkyvernSessionError(RuntimeError):
    """A sanitized browser-session lifecycle failure."""


class _RequestFailure(SkyvernSessionError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        safe_create_retry: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.safe_create_retry = safe_create_retry


def redact_browser_address(browser_address: str) -> str:
    """Return only an address hostname, never its token-bearing path."""
    try:
        hostname = urllib.parse.urlsplit(browser_address.strip()).hostname
    except (AttributeError, ValueError):
        hostname = None
    return hostname or "<redacted>"


def build_api_url(base_url: str, path: str) -> str:
    """Build an API URL while rejecting credentials, queries, and fragments."""
    try:
        parsed = urllib.parse.urlsplit(base_url.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("SKYVERN_BASE_URL must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "SKYVERN_BASE_URL must be an HTTP(S) origin without credentials, "
            "a query, or a fragment"
        )
    base_path = parsed.path.rstrip("/")
    request_path = path.strip("/")
    combined_path = f"{base_path}/{request_path}" if request_path else base_path or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, combined_path, "", "")
    )


def is_session_ready(payload: object) -> bool:
    """Return whether a session response is ready for a CDP connection."""
    if not isinstance(payload, Mapping):
        return False
    address = payload.get("browser_address")
    return payload.get("status") == "running" and isinstance(address, str) and bool(
        address.strip()
    )


class SkyvernSession:
    """Context manager for one short-lived Skyvern browser session."""

    def __init__(
        self,
        timeout_minutes: int = 5,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        request_timeout_seconds: float = 60.0,
        startup_timeout_seconds: float = 90.0,
        poll_interval_seconds: float = 1.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        if isinstance(timeout_minutes, bool) or not 5 <= timeout_minutes <= 1440:
            raise ValueError("timeout_minutes must be between 5 and 1440")
        if request_timeout_seconds <= 0 or startup_timeout_seconds <= 0:
            raise ValueError("request and startup timeouts must be positive")
        if poll_interval_seconds < 0 or backoff_seconds < 0:
            raise ValueError("poll interval and backoff must be non-negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        configured_base = base_url
        if configured_base is None:
            configured_base = os.environ.get("SKYVERN_BASE_URL", DEFAULT_BASE_URL)
        self._base_url = configured_base.strip()
        # Validate before any request and without logging the configured value.
        build_api_url(self._base_url, "v1/browser_sessions")
        self._api_key_override = api_key
        self._api_key = ""
        self._request_timeout_seconds = request_timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self.timeout_minutes = timeout_minutes
        self.session_id: str | None = None
        self.browser_address: str | None = None
        self.download_path: str | None = None
        self._closed = False
        self._close_lock = Lock()

    @property
    def cdp_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise SkyvernSessionError("Skyvern browser session is not active")
        return {"x-api-key": self._api_key}

    def _request_json(
        self,
        method: str,
        path: str,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json", "x-api-key": self._api_key}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            build_api_url(self._base_url, path),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._request_timeout_seconds
            ) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _RequestFailure(
                f"{operation} failed with HTTP {status}",
                retryable=status in _RETRYABLE_HTTP_STATUSES,
                # A rate-limit response definitively rejected the request. Other
                # create failures may be ambiguous, so do not risk duplicate sessions.
                safe_create_retry=status in {429, 500, 502, 503},
            ) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise _RequestFailure(
                f"{operation} failed because of a network error", retryable=True
            ) from None

        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _RequestFailure(f"{operation} returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise _RequestFailure(f"{operation} returned an invalid response shape")
        return decoded

    def _with_backoff(
        self,
        operation: str,
        request: Callable[[], dict[str, Any]],
        *,
        create: bool = False,
    ) -> dict[str, Any]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return request()
            except _RequestFailure as exc:
                should_retry = exc.retryable and (not create or exc.safe_create_retry)
                if not should_retry or attempt == self._max_attempts:
                    raise
                LOGGER.warning(
                    "%s request failed; retrying with backoff (attempt %d/%d)",
                    operation,
                    attempt,
                    self._max_attempts,
                )
                time.sleep(self._backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError("retry loop ended unexpectedly")

    def _apply_response(self, payload: Mapping[str, Any]) -> None:
        download_path = payload.get("download_path")
        if download_path is None or isinstance(download_path, str):
            self.download_path = download_path
        if is_session_ready(payload):
            address = payload["browser_address"]
            assert isinstance(address, str)
            self.browser_address = address.strip()

    def _poll_until_ready(
        self, initial_payload: dict[str, Any], deadline: float
    ) -> None:
        payload = initial_payload
        while True:
            self._apply_response(payload)
            if self.browser_address:
                return
            status = payload.get("status")
            if isinstance(status, str) and status.lower() in _TERMINAL_STATUSES:
                raise SkyvernSessionError(
                    "Skyvern browser session reached a terminal state before running"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SkyvernSessionError(
                    "Skyvern browser session did not become ready before the deadline"
                )
            time.sleep(min(self._poll_interval_seconds, remaining))
            assert self.session_id is not None
            session_id = urllib.parse.quote(self.session_id, safe="")
            payload = self._with_backoff(
                "poll",
                lambda: self._request_json(
                    "GET", f"v1/browser_sessions/{session_id}", "session poll"
                ),
            )

    def __enter__(self) -> SkyvernSession:
        if self.session_id is not None and not self._closed:
            raise SkyvernSessionError("Skyvern browser session is already active")
        api_key = self._api_key_override
        if api_key is None:
            api_key = os.environ.get("SKYVERN_CLOUD_API_KEY", "")
        self._api_key = api_key.strip()
        if not self._api_key:
            raise SkyvernSessionError("SKYVERN_CLOUD_API_KEY is required")

        self.session_id = None
        self.browser_address = None
        self.download_path = None
        self._closed = False
        deadline = time.monotonic() + self._startup_timeout_seconds
        try:
            payload = self._with_backoff(
                "create",
                lambda: self._request_json(
                    "POST",
                    "v1/browser_sessions",
                    "session create",
                    {"timeout": self.timeout_minutes},
                ),
                create=True,
            )
            session_id = payload.get("browser_session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise SkyvernSessionError(
                    "Skyvern session create response omitted browser_session_id"
                )
            self.session_id = session_id.strip()
            self._poll_until_ready(payload, deadline)
            assert self.browser_address is not None
            LOGGER.info(
                "Skyvern browser session ready on host %s",
                redact_browser_address(self.browser_address),
            )
            return self
        except Exception:
            if self.session_id is not None:
                self.close()
            raise

    def close(self) -> None:
        """Close this session, swallowing and safely logging close failures."""
        with self._close_lock:
            if self._closed or self.session_id is None or not self._api_key:
                return
            session_id = urllib.parse.quote(self.session_id, safe="")
            try:
                self._with_backoff(
                    "close",
                    lambda: self._request_json(
                        "POST",
                        f"v1/browser_sessions/{session_id}/close",
                        "session close",
                    ),
                )
            except Exception:
                LOGGER.warning(
                    "Skyvern browser session close failed after bounded retries"
                )
                return
            self._closed = True
            self._api_key = ""
            LOGGER.info("Skyvern browser session closed")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _close_sessions(sessions: list[SkyvernSession]) -> None:
    if not sessions:
        return
    with ThreadPoolExecutor(max_workers=len(sessions)) as executor:
        futures = [executor.submit(session.close) for session in sessions]
        for future in as_completed(futures):
            future.result()


@contextmanager
def session_pool(
    n: int, **session_options: Any
) -> Iterator[list[SkyvernSession]]:
    """Provision and clean up ``n`` independent sessions concurrently."""
    if isinstance(n, bool) or n < 1:
        raise ValueError("session pool size must be at least 1")
    sessions = [SkyvernSession(**session_options) for _ in range(n)]
    entered: list[SkyvernSession] = []
    try:
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=n) as executor:
            futures = [executor.submit(session.__enter__) for session in sessions]
            for future in as_completed(futures):
                try:
                    entered.append(future.result())
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            _close_sessions(entered)
            entered.clear()
            raise errors[0]
        yield sessions
    finally:
        _close_sessions(entered)
